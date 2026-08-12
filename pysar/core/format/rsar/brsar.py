import io
import json
import re
import copy
from pathlib import Path
from typing import Any, BinaryIO, Callable, Self

from pysar.core.base import EditorBase, DirtyFlags
from pysar.core.exceptions import ArchiveDumpCancelled, BrsarError
from pysar.core.format.rwar import Brwar
from pysar.core.format.rwsd import Brwsd
from pysar.core.model.brsar import (
    BrsarData,
    PROVENANCE_ARITIES,
    SoundType,
    SeqSoundInfo,
    WaveSoundInfo,
    SoundDataEntry,
    SoundBankEntry,
    PlayerInfoEntry,
    FileEntry,
    FilePositionEntry,
    GroupTableEntry,
    GroupDataEntry,
    ArcCommonInfo,
    EmbeddedFile,
)
from pysar.core.format.rsar.reader import BrsarReader
from pysar.core.format.rsar.writer import BrsarWriter, ordered_embedded_file_ids
from pysar.seq import RenderOptions, SoundArchiveEngine, make_playback_context


class Brsar(EditorBase):
    def __init__(self, data: BrsarData | None = None):
        super().__init__()
        self._data = data or BrsarData()

        # Lazy-parsed subfile caches
        self._bank_cache: dict[int, object] = {}
        self._seq_cache: dict[int, object] = {}
        self._war_cache: dict[int, object] = {}
        self._wave_war_cache: dict[int, object] = {}
        self._wsd_cache: dict[int, object] = {}
        # Safe Mode is a runtime choice and intentionally never persisted.
        # Every archive therefore opens locked, even if it was saved unlocked.
        self._safe_mode = True
        self._sanitize_provenance()

    def clear_subfile_caches(self) -> None:
        self._bank_cache.clear()
        self._seq_cache.clear()
        self._war_cache.clear()
        self._wave_war_cache.clear()
        self._wsd_cache.clear()

    #
    # Safe Mode / PySAR provenance
    #

    def _sanitize_provenance(self) -> None:
        """Conservatively discard stale/out-of-range trailer identities."""
        provenance = self._data.provenance
        if provenance.status != "valid":
            for entries in provenance.entities.values():
                entries.clear()
            return

        counts = {
            "sound": len(self._data.sound_entries),
            "bank": len(self._data.bank_entries),
            "player": len(self._data.player_entries),
            "group": len(self._data.group_entries),
            "file": len(self._data.file_entries),
        }
        for kind, count in counts.items():
            provenance.entities[kind] = {
                identity for identity in provenance.entries(kind)
                if len(identity) == 1 and 0 <= identity[0] < count
            }

        valid_instruments: dict[tuple[int, int], int] = {}
        candidate_banks = {
            identity[0]
            for kind in ("bank_instrument", "bank_zone")
            for identity in provenance.entries(kind)
            if identity
        }
        for bank_index in candidate_banks:
            if not 0 <= bank_index < counts["bank"]:
                continue
            try:
                bank = self.get_bank(bank_index)
                for program, instrument in enumerate(bank.instruments):
                    if not instrument.is_empty():
                        valid_instruments[(bank_index, program)] = len(
                            instrument.get_all_inst_params()
                        )
            except Exception:
                continue
        provenance.entities["bank_instrument"] = {
            identity for identity in provenance.entries("bank_instrument")
            if len(identity) == 2 and identity in valid_instruments
        }
        provenance.entities["bank_zone"] = {
            identity for identity in provenance.entries("bank_zone")
            if (
                len(identity) == 3
                and (identity[0], identity[1]) in valid_instruments
                and 0 <= identity[2] < valid_instruments[(identity[0], identity[1])]
            )
        }

        valid_group_items = {
            (group_index, int(sub.group_index))
            for group_index, group in enumerate(self._data.group_entries)
            for sub in group.group_table
            if 0 <= int(sub.group_index) < counts["file"]
        }
        provenance.entities["group_item"] = (
            provenance.entries("group_item") & valid_group_items
        )

        valid_waves: set[tuple[int, int]] = set()
        valid_wsd_entries: set[tuple[int, int]] = set()
        candidate_files = {
            identity[0]
            for kind in ("wave", "wsd_entry")
            for identity in provenance.entries(kind)
            if identity
        }
        for file_index in candidate_files:
            if not 0 <= file_index < counts["file"]:
                continue
            try:
                if any(identity[0] == file_index for identity in provenance.entries("wave")):
                    raw = self._resolve_audio_raw(file_index)
                    if raw is not None and raw[:4] == b"RWAR":
                        archive = Brwar.from_bytes(raw)
                        valid_waves.update((file_index, index) for index in range(len(archive)))
                if any(identity[0] == file_index for identity in provenance.entries("wsd_entry")):
                    raw = self._resolve_file_raw(file_index)
                    if raw is not None and raw[:4] == b"RWSD":
                        wsd = Brwsd.from_bytes(raw)
                        valid_wsd_entries.update((file_index, index) for index in range(len(wsd)))
            except Exception:
                continue
        provenance.entities["wave"] &= valid_waves
        provenance.entities["wsd_entry"] &= valid_wsd_entries

    @property
    def safe_mode(self) -> bool:
        return self._safe_mode

    def set_safe_mode(self, enabled: bool) -> None:
        """Enable or disable structural guards for this editor instance."""
        self._safe_mode = bool(enabled)

    @staticmethod
    def _provenance_identity(kind: str, identity: tuple[object, ...]) -> tuple[int, ...]:
        if kind not in PROVENANCE_ARITIES:
            raise BrsarError(f"Unsupported Safe Mode entity kind: {kind}")
        if len(identity) == 1 and isinstance(identity[0], (tuple, list)):
            identity = tuple(identity[0])
        expected = PROVENANCE_ARITIES[kind]
        if len(identity) != expected:
            raise BrsarError(f"{kind} identity requires {expected} index value(s)")
        values = tuple(int(value) for value in identity)
        if any(value < 0 for value in values):
            raise BrsarError("Safe Mode entity indexes cannot be negative")
        return values

    def register_new(self, kind: str, *identity: object) -> None:
        """Record a newly created entity, regardless of current Safe Mode."""
        key = self._provenance_identity(kind, identity)
        entries = self._data.provenance.entries(kind)
        if key not in entries:
            entries.add(key)
            self._data.provenance.status = "valid"
            self.mark_dirty(DirtyFlags.METADATA)

    def unregister_new(self, kind: str, *identity: object, recursive: bool = False) -> None:
        key = self._provenance_identity(kind, identity)
        self._data.provenance.entries(kind).discard(key)
        if recursive:
            child_dimensions = {
                "bank": (("bank_instrument", 0), ("bank_zone", 0)),
                "bank_instrument": (("bank_zone", (0, 1)),),
                "group": (("group_item", 0),),
                "file": (("wave", 0), ("wsd_entry", 0), ("group_item", 1)),
            }
            for child_kind, dimension in child_dimensions.get(kind, ()):
                entries = self._data.provenance.entries(child_kind)
                if isinstance(dimension, tuple):
                    entries.difference_update(
                        {child for child in entries
                        if tuple(child[index] for index in dimension) == key
                        }
                    )
                else:
                    parent_index = key[0]
                    entries.difference_update(
                        {child for child in entries
                        if child[dimension] == parent_index
                        }
                    )
        self.mark_dirty(DirtyFlags.METADATA)

    def is_new(self, kind: str, *identity: object) -> bool:
        """Whether an identity (or its newly created parent) is user-created."""
        key = self._provenance_identity(kind, identity)
        if key in self._data.provenance.entries(kind):
            return True
        if kind in {"bank_instrument", "bank_zone"}:
            if (key[0],) in self._data.provenance.entries("bank"):
                return True
        if kind == "bank_zone":
            if key[:2] in self._data.provenance.entries("bank_instrument"):
                return True
        if kind in {"wave", "wsd_entry"}:
            if (key[0],) in self._data.provenance.entries("file"):
                return True
        return False

    def is_protected(self, kind: str, *identity: object) -> bool:
        return self._safe_mode and not self.is_new(kind, *identity)

    def require_safe_mutation(
            self,
            action: str,
            kind: str,
            *identity: object,
    ) -> None:
        """Reject identity-changing operations on vanilla entities."""
        key = self._provenance_identity(kind, identity)
        if not self.is_protected(kind, *key):
            return
        label = kind.replace("_", " ")
        joined = ":".join(str(value) for value in key)
        raise BrsarError(
            f"Safe Mode protects original {label} {joined}; "
            f"disable Safe Mode intentionally before {action}"
        )

    def require_safe_deletion(self, kind: str, index: int) -> None:
        """Guard deletion plus implicit reindexing/contained relationships."""
        index = int(index)
        self.require_safe_mutation("deleting it", kind, index)
        if not self._safe_mode:
            return
        tables = {
            "sound": self._data.sound_entries,
            "bank": self._data.bank_entries,
            "player": self._data.player_entries,
            "group": self._data.group_entries,
            "file": self._data.file_entries,
        }
        table = tables.get(kind)
        if table is None:
            return
        # Removing an index shifts every later table identity. Safe Mode may
        # only do that when all shifted identities are also user-created.
        if any(self.is_protected(kind, later) for later in range(index + 1, len(table))):
            raise BrsarError(
                f"Safe Mode cannot delete {kind} {index} because that would "
                "reindex a later original resource"
            )
        if kind == "group":
            group = self._data.group_entries[index]
            for sub in group.group_table:
                file_index = int(sub.group_index)
                if (
                    not self.is_new("file", file_index)
                    or not self.is_new("group_item", index, file_index)
                ):
                    raise BrsarError(
                        "Safe Mode cannot delete this new group while it "
                        "contains an original file or relationship"
                    )
                file_entry = self._data.file_entries[file_index]
                remaining_positions = [
                    position for position in file_entry.file_positions
                    if int(position.group_index) != index
                ]
                if not remaining_positions:
                    live_users = self._users_of_file(file_index)
                    if live_users:
                        user_kind, user_index = live_users[0]
                        qualifier = (
                            "original "
                            if self.is_protected(user_kind, user_index)
                            else ""
                        )
                        raise BrsarError(
                            "Safe Mode cannot delete this group because it "
                            f"would strand {qualifier}{user_kind} {user_index}"
                        )

    def _users_of_file(self, file_index: int) -> list[tuple[str, int]]:
        """Return every live INFO entry that depends on one logical FILE."""
        file_index = int(file_index)
        return [
            ("bank", index)
            for index, entry in enumerate(self._data.bank_entries)
            if int(entry.file_index) == file_index
        ] + [
            ("sound", index)
            for index, entry in enumerate(self._data.sound_entries)
            if int(entry.file_index) == file_index
        ]

    def _protected_users_of_file(self, file_index: int) -> list[tuple[str, int]]:
        return [
            (kind, index)
            for kind, index in self._users_of_file(file_index)
            if self.is_protected(kind, index)
        ]

    def remap_provenance_indices(self, kind: str, old_to_new: dict[int, int]) -> None:
        """Remap one top-level INFO-table index and all dependent identities."""
        dimensions = {
            "sound": (("sound", 0),),
            "bank": (("bank", 0), ("bank_instrument", 0), ("bank_zone", 0)),
            "player": (("player", 0),),
            "group": (("group", 0), ("group_item", 0)),
            "file": (("file", 0), ("wave", 0), ("wsd_entry", 0), ("group_item", 1)),
        }
        if kind not in dimensions:
            raise BrsarError(f"Cannot remap provenance kind: {kind}")
        changed = False
        for child_kind, dimension in dimensions[kind]:
            source = self._data.provenance.entries(child_kind)
            target: set[tuple[int, ...]] = set()
            for identity in source:
                old_value = identity[dimension]
                if old_value not in old_to_new:
                    # An omitted key represents a deleted parent.
                    changed = True
                    continue
                values = list(identity)
                values[dimension] = int(old_to_new[old_value])
                mapped = tuple(values)
                target.add(mapped)
                changed |= mapped != identity
            if target != source:
                self._data.provenance.entities[child_kind] = target
                changed = True
        if changed:
            self.mark_dirty(DirtyFlags.METADATA)

    def remap_provenance_after_delete(self, kind: str, deleted_index: int) -> None:
        """Drop one top-level identity and shift following INFO indexes."""
        deleted_index = int(deleted_index)
        counts = {
            "sound": len(self._data.sound_entries) + 1,
            "bank": len(self._data.bank_entries) + 1,
            "player": len(self._data.player_entries) + 1,
            "group": len(self._data.group_entries) + 1,
            "file": len(self._data.file_entries) + 1,
        }
        if kind not in counts:
            raise BrsarError(f"Cannot delete-remap provenance kind: {kind}")
        mapping = {
            old: old if old < deleted_index else old - 1
            for old in range(counts[kind])
            if old != deleted_index
        }
        self.remap_provenance_indices(kind, mapping)

    def remap_child_provenance_after_delete(
            self,
            kind: str,
            prefix: tuple[int, ...],
            deleted_index: int,
    ) -> None:
        """Drop/shift the last component of child identities under *prefix*."""
        expected = PROVENANCE_ARITIES.get(kind)
        if expected is None or len(prefix) != expected - 1:
            raise BrsarError(f"Invalid child provenance prefix for {kind}")
        entries = self._data.provenance.entries(kind)
        target: set[tuple[int, ...]] = set()
        for identity in entries:
            if identity[:-1] != tuple(prefix):
                target.add(identity)
            elif identity[-1] < deleted_index:
                target.add(identity)
            elif identity[-1] > deleted_index:
                target.add(identity[:-1] + (identity[-1] - 1,))
        if target != entries:
            self._data.provenance.entities[kind] = target
            self.mark_dirty(DirtyFlags.METADATA)

    def remap_child_provenance_after_insert(
            self,
            kind: str,
            prefix: tuple[int, ...],
            inserted_index: int,
    ) -> None:
        """Shift tracked child identities at/after a newly inserted slot."""
        expected = PROVENANCE_ARITIES.get(kind)
        if expected is None or len(prefix) != expected - 1:
            raise BrsarError(f"Invalid child provenance prefix for {kind}")
        entries = self._data.provenance.entries(kind)
        target: set[tuple[int, ...]] = set()
        for identity in entries:
            if identity[:-1] == tuple(prefix) and identity[-1] >= inserted_index:
                target.add(identity[:-1] + (identity[-1] + 1,))
            else:
                target.add(identity)
        if target != entries:
            self._data.provenance.entities[kind] = target
            self.mark_dirty(DirtyFlags.METADATA)

    def move_bank_instrument_provenance(self, bank_index: int, old: int, new: int) -> None:
        """Move direct instrument/zone provenance to a different program ID."""
        bank_index, old, new = int(bank_index), int(old), int(new)
        instruments = self._data.provenance.entries("bank_instrument")
        if (bank_index, old) in instruments:
            instruments.remove((bank_index, old))
            instruments.add((bank_index, new))
        zones = self._data.provenance.entries("bank_zone")
        moved = {
            (bank_index, new, identity[2])
            for identity in zones
            if identity[:2] == (bank_index, old)
        }
        zones.difference_update(
            {identity for identity in zones
            if identity[:2] == (bank_index, old)
            }
        )
        zones.update(moved)
        if moved or (bank_index, new) in instruments:
            self.mark_dirty(DirtyFlags.METADATA)

    def clear_bank_child_provenance(self, bank_index: int) -> None:
        bank_index = int(bank_index)
        for kind in ("bank_instrument", "bank_zone"):
            entries = self._data.provenance.entries(kind)
            entries.difference_update({
                identity for identity in entries if identity[0] == bank_index
            })
        self.mark_dirty(DirtyFlags.METADATA)

    def logical_file_indices_for_embedded(self, file_id: int) -> set[int]:
        """Resolve a physical embedded copy to stable logical FILE indexes."""
        file_id = int(file_id)
        return {
            int(sub.group_index)
            for group in self._data.group_entries
            for sub in group.group_table
            if sub.file_id == file_id or sub.audio_file_id == file_id
        }

    def is_wave_archive_new(self, file_id: int) -> bool:
        logical = self.logical_file_indices_for_embedded(file_id)
        return bool(logical) and all(self.is_new("file", index) for index in logical)

    def require_wave_archive_delete(self, file_id: int) -> None:
        if not self._safe_mode:
            return
        logical = self.logical_file_indices_for_embedded(file_id)
        if not logical or any(not self.is_new("file", index) for index in logical):
            raise BrsarError(
                f"Safe Mode protects original wave archive {int(file_id)}; "
                "disable Safe Mode intentionally before deleting it"
            )
        protected_users = [
            user
            for file_index in logical
            for user in self._protected_users_of_file(file_index)
        ]
        if protected_users:
            kind, index = protected_users[0]
            raise BrsarError(
                f"Safe Mode cannot delete this new wave archive while original "
                f"{kind} {index} references it"
            )

    #
    # Factory methods
    #

    @classmethod
    def new(cls) -> Self:
        """Create a new empty BRSAR."""
        editor = cls(BrsarData())
        editor.mark_dirty(DirtyFlags.ALL)
        return editor

    @classmethod
    def open(cls, path: str | Path) -> Self:
        """Open a BRSAR file from disk."""
        reader = BrsarReader()
        data = reader.from_file(str(path))
        return cls(data)

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        """Load a BRSAR from raw bytes."""
        reader = BrsarReader()
        data = reader.read(io.BytesIO(raw))
        return cls(data)

    @classmethod
    def from_stream(cls, stream: BinaryIO) -> Self:
        """Load a BRSAR from a binary stream."""
        reader = BrsarReader()
        data = reader.read(stream)
        return cls(data)

    #
    # Properties
    #

    @property
    def data(self) -> BrsarData:
        return self._data

    @property
    def version(self) -> int:
        return self._data.version

    @version.setter
    def version(self, value: int) -> None:
        self._data.version = value
        self.mark_dirty(DirtyFlags.HEADER)

    @property
    def names(self) -> list[str]:
        return self._data.names

    @property
    def n_sounds(self) -> int:
        return len(self._data.sound_entries)

    @property
    def n_banks(self) -> int:
        return len(self._data.bank_entries)

    @property
    def n_players(self) -> int:
        return len(self._data.player_entries)

    @property
    def n_groups(self) -> int:
        return len(self._data.group_entries)

    @property
    def arc_info(self) -> ArcCommonInfo:
        return self._data.arc_common_info

    #
    # Name lookups
    #

    def lookup_sound(self, name: str) -> tuple[str, int, int] | None:
        """Look up a sound by name. Returns (name, string_idx, info_idx) or None."""
        if self._data.snd_trie is not None:
            result = self._data.snd_trie.get_entry(name)
            if result[0] is not None:
                return result
        return None

    def lookup_player(self, name: str) -> tuple[str, int, int] | None:
        """Look up a player by name."""
        if self._data.player_trie is not None:
            result = self._data.player_trie.get_entry(name)
            if result[0] is not None:
                return result
        return None

    def lookup_group(self, name: str) -> tuple[str, int, int] | None:
        """Look up a group by name."""
        if self._data.group_trie is not None:
            result = self._data.group_trie.get_entry(name)
            if result[0] is not None:
                return result
        return None

    def lookup_bank(self, name: str) -> tuple[str, int, int] | None:
        """Look up a bank by name."""
        if self._data.bank_trie is not None:
            result = self._data.bank_trie.get_entry(name)
            if result[0] is not None:
                return result
        return None

    #
    # Embedded file access (lazy parsing)
    #

    def get_embedded_file(self, file_id: int) -> EmbeddedFile | None:
        """Get raw embedded file by its ID."""
        return self._data.embedded_files.get(file_id)

    def get_embedded_raw(self, file_id: int) -> bytes | None:
        """Get raw bytes of an embedded file."""
        ef = self._data.embedded_files.get(file_id)
        return ef.raw_data if ef is not None else None

    def get_bank(self, bank_index: int):
        """Get a parsed BRBNK editor by its index in the bank table."""
        if bank_index in self._bank_cache:
            return self._bank_cache[bank_index]

        from pysar.core.format.rbnk import Brbnk

        raw = self._resolve_file_raw(self._data.bank_entries[bank_index].file_index)
        if raw is None:
            raise BrsarError(f'Could not resolve bank at index {bank_index}')

        brbnk = Brbnk.from_bytes(raw)
        self._bank_cache[bank_index] = brbnk
        return brbnk

    def get_bank_war(self, bank_index: int):
        """Get the BRWAR associated with a bank (via audio data in its group)."""
        if bank_index in self._war_cache:
            return self._war_cache[bank_index]

        from pysar.core.format.rwar import Brwar

        bank_entry = self._data.bank_entries[bank_index]
        raw = self._resolve_audio_raw(bank_entry.file_index)
        if raw is None:
            raise BrsarError(f'Could not resolve BRWAR for bank index {bank_index}')

        brwar = Brwar.from_bytes(raw)
        self._war_cache[bank_index] = brwar
        return brwar

    def get_seq(self, file_index: int):
        """Get a parsed BRSEQ editor by file index."""
        if file_index in self._seq_cache:
            return self._seq_cache[file_index]

        from pysar.core.format.rseq import Brseq

        raw = self._resolve_file_raw(file_index)
        if raw is None:
            raise BrsarError(f'Could not resolve sequence at file index {file_index}')

        brseq = Brseq.from_bytes(raw)
        self._seq_cache[file_index] = brseq
        return brseq

    def get_wsd(self, file_index: int):
        """Get a parsed BRWSD editor by file index."""
        if file_index in self._wsd_cache:
            return self._wsd_cache[file_index]

        raw = self._resolve_file_raw(file_index)
        if raw is None:
            raise BrsarError(f'Could not resolve BRWSD at file index {file_index}')

        brwsd = Brwsd.from_bytes(raw)
        self._wsd_cache[file_index] = brwsd
        return brwsd

    def get_wave_war(self, file_index: int):
        """Get the BRWAR paired with a BRWSD file index."""
        if file_index in self._wave_war_cache:
            return self._wave_war_cache[file_index]

        raw = self._resolve_audio_raw(file_index)
        if raw is None:
            raise BrsarError(f'Could not resolve BRWAR at file index {file_index}')

        brwar = Brwar.from_bytes(raw)
        self._wave_war_cache[file_index] = brwar
        return brwar

    #
    # helper functions
    #

    def _resolve_file_raw(self, file_index: int) -> bytes | None:
        """Resolve a file index to raw bytes via the file/group tables."""
        if file_index < 0 or file_index >= len(self._data.file_entries):
            return None

        file_entry = self._data.file_entries[file_index]

        if not file_entry.file_positions:
            return None

        pos = file_entry.file_positions[0]
        if pos.group_index >= len(self._data.group_entries):
            return None

        group = self._data.group_entries[pos.group_index]
        if pos.index >= len(group.group_table):
            return None

        grp_entry = group.group_table[pos.index]

        # Try to find the embedded file by resolved file_id
        if grp_entry.file_id is not None and grp_entry.file_id in self._data.embedded_files:
            return self._data.embedded_files[grp_entry.file_id].raw_data

        # Fallback: try by offset
        target_offset = group.group_file_offset + grp_entry.file_data_offset
        file_id = self._data.offset_to_id.get(target_offset)
        if file_id is not None and file_id in self._data.embedded_files:
            return self._data.embedded_files[file_id].raw_data

        return None

    def _resolve_audio_raw(self, file_index: int) -> bytes | None:
        """Resolve a file index to its audio data raw bytes."""
        if file_index < 0 or file_index >= len(self._data.file_entries):
            return None

        file_entry = self._data.file_entries[file_index]
        if not file_entry.file_positions:
            return None

        pos = file_entry.file_positions[0]
        if pos.group_index >= len(self._data.group_entries):
            return None

        group = self._data.group_entries[pos.group_index]
        if pos.index >= len(group.group_table):
            return None

        grp_entry = group.group_table[pos.index]

        # Try by resolved audio_file_id
        if grp_entry.audio_file_id is not None and grp_entry.audio_file_id in self._data.embedded_files:
            return self._data.embedded_files[grp_entry.audio_file_id].raw_data

        # Fallback: try by offset
        target_offset = group.group_audio_offset + grp_entry.audio_data_offset
        file_id = self._data.offset_to_id.get(target_offset)
        if file_id is not None and file_id in self._data.embedded_files:
            return self._data.embedded_files[file_id].raw_data

        return None

    #
    # Group queries
    #

    def get_groups(self) -> dict[str, GroupDataEntry]:
        """Get all groups as a name->entry dict."""
        return {
            self._data.names[g.file_name_index] if 0 <= g.file_name_index < len(self._data.names) else '<NULL>': g
            for g in self._data.group_entries
        }

    def get_players(self) -> dict[str, PlayerInfoEntry]:
        """Get all players as a name->entry dict."""
        return {
            self._data.names[p.file_name_index] if 0 <= p.file_name_index < len(self._data.names) else '<NULL>': p
            for p in self._data.player_entries
        }

    def get_banks(self) -> dict[str, SoundBankEntry]:
        """Get all banks as a name->entry dict."""
        return {
            self._data.names[b.file_name_index] if 0 <= b.file_name_index < len(self._data.names) else '<NULL>': b
            for b in self._data.bank_entries
        }

    #
    # Sound queries
    #

    def get_sound_entry(self, name: str) -> SoundDataEntry | None:
        """Get the sound data entry for a named sound."""
        result = self.lookup_sound(name)
        if result is None:
            return None
        _, _, info_idx = result
        if 0 <= info_idx < len(self._data.sound_entries):
            return self._data.sound_entries[info_idx]
        return None

    def get_sound_names(self) -> list[str]:
        """Get all sound names."""
        return [
            self._data.names[e.file_name_index]
            for e in self._data.sound_entries
            if 0 <= e.file_name_index < len(self._data.names)
        ]

    def get_sounds_by_type(self, sound_type: SoundType) -> dict[str, SoundDataEntry]:
        """Get all sounds of a specific type."""
        result = {}
        for entry in self._data.sound_entries:
            if entry.sound_type == sound_type:
                if 0 <= entry.file_name_index < len(self._data.names):
                    name = self._data.names[entry.file_name_index]
                    result[name] = entry
        return result

    def _get_or_add_name(self, name: str) -> int:
        """Get existing name index or append a new name."""
        try:
            return self._data.names.index(name)
        except ValueError:
            self._data.names.append(name)
            return len(self._data.names) - 1

    @staticmethod
    def _validate_ascii_name(name: object, label: str) -> str:
        """Validate a symbol name before it reaches the ASCII RSAR writer."""
        value = str(name).strip()
        if not value:
            raise BrsarError(f"{label} name cannot be empty")
        try:
            value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise BrsarError(
                f"{label} names must contain ASCII characters only"
            ) from exc
        return value

    def _validate_sound_name(
            self,
            name: object,
            *,
            ignore_index: int | None = None,
    ) -> str:
        """Return a serializable sound name that is unique in the INFO table."""
        value = self._validate_ascii_name(name, "Sound")
        for index, entry in enumerate(self._data.sound_entries):
            if index == ignore_index:
                continue
            if (
                0 <= entry.file_name_index < len(self._data.names)
                and self._data.names[entry.file_name_index] == value
            ):
                raise BrsarError(f'Sound "{value}" already exists')
        return value

    def rename_sound(self, sound_index: int, name: object) -> None:
        """Rename one sound without permitting aliases or unsafe identities."""
        sound_index = int(sound_index)
        if not 0 <= sound_index < len(self._data.sound_entries):
            raise BrsarError(f"Invalid sound index {sound_index}")
        entry = self._data.sound_entries[sound_index]
        current_name = (
            self._data.names[entry.file_name_index]
            if 0 <= entry.file_name_index < len(self._data.names)
            else ""
        )
        candidate = self._validate_ascii_name(name, "Sound")
        if candidate == current_name:
            return
        self.require_safe_mutation("renaming it", "sound", sound_index)
        candidate = self._validate_sound_name(
            candidate, ignore_index=sound_index,
        )
        entry.file_name_index = self._get_or_add_name(candidate)
        self._rebuild_sound_trie()
        self.mark_dirty(DirtyFlags.DATA)

    def _rebuild_sound_trie(self) -> None:
        """Rebuild the sound trie from current sound entries."""
        from pysar.core.format.rsar.string_trie import StringTrie

        trie = StringTrie()
        for info_idx, entry in enumerate(self._data.sound_entries):
            if 0 <= entry.file_name_index < len(self._data.names):
                snd_name = self._data.names[entry.file_name_index]
                trie.insert(snd_name, entry.file_name_index, info_idx)
        self._data.snd_trie = trie
        self._data.snd_trie_raw = None

    def _rebuild_group_trie(self) -> None:
        from pysar.core.format.rsar.string_trie import StringTrie

        trie = StringTrie()
        for group_idx, entry in enumerate(self._data.group_entries):
            if 0 <= entry.file_name_index < len(self._data.names):
                group_name = self._data.names[entry.file_name_index]
                trie.insert(group_name, entry.file_name_index, group_idx)
        self._data.group_trie = trie
        self._data.group_trie_raw = None

    def _rebuild_bank_trie(self) -> None:
        """Rebuild the bank-name trie after structural bank edits."""
        from pysar.core.format.rsar.string_trie import StringTrie

        trie = StringTrie()
        for bank_idx, entry in enumerate(self._data.bank_entries):
            if 0 <= entry.file_name_index < len(self._data.names):
                bank_name = self._data.names[entry.file_name_index]
                trie.insert(bank_name, entry.file_name_index, bank_idx)
        self._data.bank_trie = trie
        self._data.bank_trie_raw = None

    def _rebuild_player_trie(self) -> None:
        from pysar.core.format.rsar.string_trie import StringTrie

        trie = StringTrie()
        for player_idx, entry in enumerate(self._data.player_entries):
            if 0 <= entry.file_name_index < len(self._data.names):
                player_name = self._data.names[entry.file_name_index]
                trie.insert(player_name, entry.file_name_index, player_idx)
        self._data.player_trie = trie
        self._data.player_trie_raw = None

    def _validate_player_values(
            self,
            name: str,
            playable_sounds: int,
            heap_size: int,
            *,
            ignore_index: int | None = None,
    ) -> tuple[str, int, int]:
        name = self._validate_ascii_name(name, "Player")
        for index, entry in enumerate(self._data.player_entries):
            if index == ignore_index:
                continue
            if 0 <= entry.file_name_index < len(self._data.names):
                if self._data.names[entry.file_name_index] == name:
                    raise BrsarError(f'Player "{name}" already exists')

        def exact_integer(value: object, label: str) -> int:
            if isinstance(value, bool):
                raise BrsarError(f"{label} must be an integer")
            if isinstance(value, int):
                return value
            if isinstance(value, str):
                text = value.strip()
                if text and re.fullmatch(r"[+-]?\d+", text):
                    return int(text)
            raise BrsarError(f"{label} must be an integer")

        playable_sounds = exact_integer(playable_sounds, "Playable sounds")
        heap_size = exact_integer(heap_size, "Heap size")
        # RSAR stores this field as one unsigned byte.
        if not 0 <= playable_sounds <= 0xFF:
            raise BrsarError("Playable sounds must be between 0 and 255")
        if not 0 <= heap_size <= 0xFFFFFFFF:
            raise BrsarError("Heap size must be between 0 and 4294967295")
        return name, playable_sounds, heap_size

    def create_player(
            self,
            name: str | None = None,
            *,
            playable_sounds: int = 0,
            heap_size: int = 0,
    ) -> int:
        base = str(name or "").strip() or f"PLAYER_{len(self._data.player_entries):04d}"
        candidate = base
        suffix = 1
        existing = {
            self._data.names[entry.file_name_index]
            for entry in self._data.player_entries
            if 0 <= entry.file_name_index < len(self._data.names)
        }
        while candidate in existing:
            candidate = f"{base}_{suffix:02d}"
            suffix += 1
        candidate, playable_sounds, heap_size = self._validate_player_values(
            candidate, playable_sounds, heap_size,
        )
        self._data.player_entries.append(PlayerInfoEntry(
            file_name_index=self._get_or_add_name(candidate),
            n_playable_sounds=playable_sounds,
            heap_size=heap_size,
        ))
        player_index = len(self._data.player_entries) - 1
        self.register_new("player", player_index)
        self._rebuild_player_trie()
        self.mark_dirty(DirtyFlags.DATA)
        return player_index

    def update_player(
            self,
            player_index: int,
            *,
            name: str,
            playable_sounds: int,
            heap_size: int,
    ) -> None:
        player_index = int(player_index)
        if not 0 <= player_index < len(self._data.player_entries):
            raise BrsarError(f"Invalid player index {player_index}")
        name, playable_sounds, heap_size = self._validate_player_values(
            name, playable_sounds, heap_size, ignore_index=player_index,
        )
        entry = self._data.player_entries[player_index]
        current_name = (
            self._data.names[entry.file_name_index]
            if 0 <= entry.file_name_index < len(self._data.names)
            else ""
        )
        if name != current_name:
            self.require_safe_mutation("renaming it", "player", player_index)
        entry.file_name_index = self._get_or_add_name(name)
        entry.n_playable_sounds = playable_sounds
        entry.heap_size = heap_size
        self._rebuild_player_trie()
        self.mark_dirty(DirtyFlags.DATA)

    def delete_player(self, player_index: int, replacement_player_index: int | None = None) -> int:
        """Delete a player and keep every sound's player index valid.

        Referenced players require an explicit replacement in the pre-delete
        index space. The return value is the replacement's new index, or -1
        when no sounds needed remapping.
        """
        player_index = int(player_index)
        count = len(self._data.player_entries)
        if not 0 <= player_index < count:
            raise BrsarError(f"Invalid player index {player_index}")
        self.require_safe_deletion("player", player_index)
        referenced = any(int(sound.player_index) == player_index for sound in self._data.sound_entries)
        replacement = None if replacement_player_index is None else int(replacement_player_index)
        if replacement is not None and (not 0 <= replacement < count or replacement == player_index):
            raise BrsarError("Replacement player is invalid")
        if referenced:
            if replacement is None:
                raise BrsarError("This player is referenced by sounds; choose a replacement player")

        replacement_new = -1
        if replacement is not None:
            replacement_new = replacement - 1 if replacement > player_index else replacement
        del self._data.player_entries[player_index]
        for sound in self._data.sound_entries:
            current = int(sound.player_index)
            if current == player_index:
                sound.player_index = replacement_new
            elif current > player_index:
                sound.player_index = current - 1
        self.remap_provenance_after_delete("player", player_index)
        self._rebuild_player_trie()
        self.mark_dirty(DirtyFlags.DATA)
        return replacement_new

    def rename_group(self, group_index: int, name: str) -> None:
        group_index = int(group_index)
        if group_index < 0 or group_index >= len(self._data.group_entries):
            raise BrsarError(f"Invalid group_index {group_index}")
        name = self._validate_ascii_name(name, "Group")
        entry = self._data.group_entries[group_index]
        current_name = (
            self._data.names[entry.file_name_index]
            if 0 <= entry.file_name_index < len(self._data.names)
            else ""
        )
        if name == current_name:
            return
        self.require_safe_mutation("renaming it", "group", group_index)
        for index, entry in enumerate(self._data.group_entries):
            if index == group_index:
                continue
            if 0 <= entry.file_name_index < len(self._data.names) and self._data.names[entry.file_name_index] == name:
                raise BrsarError(f'Group "{name}" already exists')
        self._data.group_entries[group_index].file_name_index = self._get_or_add_name(name)
        self._rebuild_group_trie()
        self.mark_dirty(DirtyFlags.DATA)

    def create_group(self, name: str | None = None) -> int:
        base_name = str(name or "").strip() or f"GROUP_{len(self._data.group_entries):04d}"
        base_name = self._validate_ascii_name(base_name, "Group")
        group_names = {
            self._data.names[g.file_name_index]
            for g in self._data.group_entries
            if 0 <= g.file_name_index < len(self._data.names)
        }
        group_name = base_name
        suffix = 1
        while group_name in group_names:
            group_name = f"{base_name}_{suffix:02d}"
            suffix += 1
        name_idx = self._get_or_add_name(group_name)
        self._data.group_entries.append(GroupDataEntry(file_name_index=name_idx, entry_num=-1))
        group_index = len(self._data.group_entries) - 1
        self.register_new("group", group_index)
        self._rebuild_group_trie()
        self.mark_dirty(DirtyFlags.DATA)
        return group_index

    def delete_group(self, group_index: int) -> None:
        group_index = int(group_index)
        if group_index < 0 or group_index >= len(self._data.group_entries):
            raise BrsarError(f"Invalid group_index {group_index}")
        self.require_safe_deletion("group", group_index)

        del self._data.group_entries[group_index]
        for file_entry in self._data.file_entries:
            file_entry.file_positions = [
                pos for pos in file_entry.file_positions
                if pos.group_index != group_index
            ]
            for pos in file_entry.file_positions:
                if pos.group_index > group_index:
                    pos.group_index -= 1
        self.remap_provenance_after_delete("group", group_index)
        self._rebuild_group_trie()
        self._refresh_group_size_fields()
        self.mark_dirty(DirtyFlags.DATA)

    def reorder_groups(self, group_indices: list[int]) -> None:
        if len(group_indices) != len(self._data.group_entries):
            raise BrsarError("Group order does not include every group")
        order = [int(idx) for idx in group_indices]
        if sorted(order) != list(range(len(self._data.group_entries))):
            raise BrsarError("Group order is invalid")
        old_to_new = {old: new for new, old in enumerate(order)}
        if self._safe_mode:
            moved_original = next(
                (
                    old for old, new in old_to_new.items()
                    if old != new and self.is_protected("group", old)
                ),
                None,
            )
            if moved_original is not None:
                raise BrsarError(
                    "Safe Mode keeps original group indexes fixed; only newly "
                    "created groups may be reordered within their existing slots"
                )
        self._data.group_entries = [self._data.group_entries[old] for old in order]
        for file_entry in self._data.file_entries:
            for position in file_entry.file_positions:
                if position.group_index in old_to_new:
                    position.group_index = old_to_new[position.group_index]
        self.remap_provenance_indices("group", old_to_new)
        self._rebuild_group_trie()
        self.mark_dirty(DirtyFlags.DATA)

    def _preflight_file_move(self, file_index: int, target_group_index: int) -> bool:
        """Validate one logical FILE move without changing archive state.

        Returns ``False`` for an exact no-op.  Keeping validation separate is
        important for batch moves: every requested move must be known-safe
        before the first group table is changed.
        """
        file_index = int(file_index)
        target_group_index = int(target_group_index)
        if file_index < 0 or file_index >= len(self._data.file_entries):
            raise BrsarError(f"Invalid file_index {file_index}")
        if target_group_index < 0 or target_group_index >= len(self._data.group_entries):
            raise BrsarError(f"Invalid group_index {target_group_index}")

        matches: list[tuple[int, int, GroupTableEntry]] = []
        for group_index, group in enumerate(self._data.group_entries):
            for sub_index, sub in enumerate(group.group_table):
                if int(sub.group_index) == file_index:
                    matches.append((group_index, sub_index, copy.copy(sub)))
        if not matches:
            raise BrsarError(f"File index {file_index} is not in any group")
        if len(matches) == 1 and matches[0][0] == target_group_index:
            return False
        if self._safe_mode:
            self.require_safe_mutation("moving it between groups", "file", file_index)
            for source_group_index, _sub_index, _sub in matches:
                self.require_safe_mutation(
                    "removing it from its group",
                    "group_item",
                    source_group_index,
                    file_index,
                )

            # Moving a logical FILE removes all inherited group positions and
            # replaces them with one target position.  Even when the FILE is
            # newly created, doing that can make a protected original sound or
            # bank unavailable in the groups from which the game loads it.
            protected_users = self._protected_users_of_file(file_index)
            if protected_users:
                user_kind, user_index = protected_users[0]
                raise BrsarError(
                    "Safe Mode cannot move this new file because original "
                    f"{user_kind} {user_index} references it"
                )
        return True

    def _commit_file_move(self, file_index: int, target_group_index: int) -> None:
        """Apply a move that has already passed ``_preflight_file_move``."""
        matches: list[tuple[int, int, GroupTableEntry]] = []
        for group_index, group in enumerate(self._data.group_entries):
            for sub_index, sub in enumerate(group.group_table):
                if int(sub.group_index) == int(file_index):
                    matches.append((group_index, sub_index, copy.copy(sub)))

        template = matches[0][2]
        for group_index, sub_index, _ in sorted(matches, key=lambda item: (item[0], item[1]), reverse=True):
            self._delete_group_table_entry(group_index, sub_index)

        target = self._data.group_entries[target_group_index]
        template.file_data_offset = 0
        template.audio_data_offset = 0
        target.group_table.append(template)
        for source_group_index, _sub_index, _sub in matches:
            self.unregister_new("group_item", source_group_index, file_index)
        self.register_new("group_item", target_group_index, file_index)
        if target.file_id is None:
            target.file_id = template.file_id
        if target.audio_file_id is None:
            target.audio_file_id = template.audio_file_id

        self._sync_file_group_metadata(file_index)
        self.mark_dirty(DirtyFlags.DATA)

    def move_file_to_group(self, file_index: int, target_group_index: int) -> None:
        file_index = int(file_index)
        target_group_index = int(target_group_index)
        if not self._preflight_file_move(file_index, target_group_index):
            return
        self._commit_file_move(file_index, target_group_index)

    def move_files_to_group(self, file_indices: list[int], target_group_index: int) -> None:
        target_group_index = int(target_group_index)
        if target_group_index < 0 or target_group_index >= len(self._data.group_entries):
            raise BrsarError(f"Invalid group_index {target_group_index}")
        unique: list[int] = []
        seen: set[int] = set()
        for value in file_indices:
            file_index = int(value)
            if file_index in seen:
                continue
            if file_index < 0 or file_index >= len(self._data.file_entries):
                raise BrsarError(f"Invalid file_index {file_index}")
            seen.add(file_index)
            unique.append(file_index)
        if not unique:
            raise BrsarError("No files selected")

        # Preflight the complete request before mutating any group table.  A
        # protected reference in a later item must not leave earlier items
        # partially moved.
        moves = [
            file_index for file_index in unique
            if self._preflight_file_move(file_index, target_group_index)
        ]
        for file_index in moves:
            self._commit_file_move(file_index, target_group_index)
        self.mark_dirty(DirtyFlags.DATA)

    def _delete_group_table_entry(self, group_index: int, sub_index: int) -> None:
        group = self._data.group_entries[group_index]
        sub = group.group_table[sub_index]
        del group.group_table[sub_index]
        for file_entry in self._data.file_entries:
            file_entry.file_positions = [
                pos for pos in file_entry.file_positions
                if not (pos.group_index == group_index and pos.index == sub_index)
            ]
            for pos in file_entry.file_positions:
                if pos.group_index == group_index and pos.index > sub_index:
                    pos.index -= 1
        if group.file_id == sub.file_id:
            group.file_id = group.group_table[0].file_id if group.group_table else None
        if group.audio_file_id == sub.audio_file_id:
            group.audio_file_id = group.group_table[0].audio_file_id if group.group_table else None
        self._refresh_group_size_fields()

    @staticmethod
    def _next_embedded_file_id(embedded_files: dict[int, EmbeddedFile]) -> int:
        if not embedded_files:
            return 0
        return max(embedded_files.keys()) + 1

    def _looks_like_brwsd_file_index(self, file_index: int) -> bool:
        raw = self._resolve_file_raw(file_index)
        return raw is not None and raw[:4] == b"RWSD"

    def _find_brwsd_file_indices(self) -> list[int]:
        indices: list[int] = []
        for file_index in range(len(self._data.file_entries)):
            if self._looks_like_brwsd_file_index(file_index):
                indices.append(file_index)
        return indices

    def _wave_archive_logical_files(self, file_id: int) -> set[int]:
        """Return FILE-table indices represented by a physical RWAR copy."""
        file_id = int(file_id)
        embedded = self._data.embedded_files.get(file_id)
        if embedded is None or embedded.magic != "RWAR":
            raise BrsarError(f"Embedded file {file_id} is not an RWAR")
        return {
            int(sub.group_index)
            for group in self._data.group_entries
            for sub in group.group_table
            if sub.audio_file_id == file_id
        }

    def _wave_archive_copy_ids(self, file_id: int) -> set[int]:
        """Find every physical RWAR copy of the same logical FILE entry.

        BRSAR stores a fresh physical copy for every group containing a logical
        file. Those copies must remain byte-identical because the FILE table has
        only one global audio size used by the Nintendo loader.
        """
        logical_files = self._wave_archive_logical_files(file_id)
        copy_ids = {int(file_id)}
        if not logical_files:
            return copy_ids
        for group in self._data.group_entries:
            for sub in group.group_table:
                if int(sub.group_index) not in logical_files or sub.audio_file_id is None:
                    continue
                embedded = self._data.embedded_files.get(int(sub.audio_file_id))
                if embedded is not None and embedded.magic == "RWAR":
                    copy_ids.add(int(sub.audio_file_id))
        return copy_ids

    def get_wave_archive_references(self, file_id: int) -> list[dict[str, Any]]:
        """Return structural references to an embedded RWAR."""
        file_id = int(file_id)
        copy_ids = self._wave_archive_copy_ids(file_id)

        references: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for group_index, group in enumerate(self._data.group_entries):
            group_name = (
                self._data.names[group.file_name_index]
                if 0 <= group.file_name_index < len(self._data.names)
                else f"GROUP_{group_index:04d}"
            )
            for sub in group.group_table:
                if sub.audio_file_id not in copy_ids:
                    continue
                file_index = int(sub.group_index)
                if sub.file_id is not None:
                    data_file_id = int(sub.file_id)
                    data_file = self._data.embedded_files.get(data_file_id)
                    kind = data_file.magic if data_file is not None else "FILE"
                    key = ("file", data_file_id)
                    if key not in seen:
                        seen.add(key)
                        references.append({
                            "kind": "file",
                            "id": data_file_id,
                            "name": f"{kind}_{data_file_id:04d}",
                        })
                for bank_index, bank in enumerate(self._data.bank_entries):
                    if int(bank.file_index) != file_index:
                        continue
                    name = (
                        self._data.names[bank.file_name_index]
                        if 0 <= bank.file_name_index < len(self._data.names)
                        else f"BANK_{bank_index:04d}"
                    )
                    key = ("bank", bank_index)
                    if key not in seen:
                        seen.add(key)
                        references.append({"kind": "bank", "id": bank_index, "name": name})
                for sound_index, sound in enumerate(self._data.sound_entries):
                    if int(sound.file_index) != file_index or sound.sound_type != SoundType.WAVE:
                        continue
                    name = self._resolve_sound_name(sound_index, sound)
                    key = ("sound", sound_index)
                    if key not in seen:
                        seen.add(key)
                        references.append({"kind": "sound", "id": sound_index, "name": name})
                key = ("group", group_index)
                if key not in seen:
                    seen.add(key)
                    references.append({"kind": "group", "id": group_index, "name": group_name})
        return references

    def _required_wave_archive_entries(self, file_id: int) -> tuple[int, list[str]]:
        """Find the highest wave index required by files paired with an RWAR."""
        from pysar.core.format.rbnk import Brbnk
        from pysar.core.format.rwsd import Brwsd

        required = 0
        users: list[str] = []
        logical_files = self._wave_archive_logical_files(file_id)
        visited_file_ids: set[int] = set()
        for group in self._data.group_entries:
            for sub in group.group_table:
                if (
                    int(sub.group_index) not in logical_files
                    or sub.audio_file_id is None
                    or sub.file_id is None
                ):
                    continue
                data_file_id = int(sub.file_id)
                if data_file_id in visited_file_ids:
                    continue
                visited_file_ids.add(data_file_id)
                embedded = self._data.embedded_files.get(data_file_id)
                if embedded is None:
                    continue
                indices: set[int] = set()
                if embedded.magic == "RBNK":
                    indices = {int(value) for value in Brbnk.from_bytes(embedded.raw_data).get_wave_indices()}
                elif embedded.magic == "RWSD":
                    indices = {int(value) for value in Brwsd.from_bytes(embedded.raw_data).get_wave_indices()}
                if indices:
                    required = max(required, max(indices) + 1)
                    users.append(f"{embedded.magic}_{data_file_id:04d}")
        return required, users

    def import_wave_archive(self, raw: bytes, *, group_index: int | None = None) -> int:
        """Add an unreferenced RWAR as an audio-only file/group entry."""
        raw = bytes(raw)
        # Validate without rewriting: raw import/export must preserve Nintendo
        # fields that this editor does not need to interpret.
        Brwar.from_bytes(raw)
        requested_group = None if group_index is None else int(group_index)
        if not self._data.group_entries and requested_group not in {None, 0}:
            raise BrsarError(f"Invalid group index {requested_group}")
        if not self._data.group_entries:
            name_index = self._get_or_add_name("GROUP_0000")
            self._data.group_entries.append(GroupDataEntry(file_name_index=name_index, entry_num=-1))
            self.register_new("group", 0)
            self._rebuild_group_trie()
        group_index = 0 if requested_group is None else requested_group
        if not 0 <= group_index < len(self._data.group_entries):
            raise BrsarError(f"Invalid group index {group_index}")

        file_id = self._next_embedded_file_id(self._data.embedded_files)
        file_index = len(self._data.file_entries)
        self._data.embedded_files[file_id] = EmbeddedFile(
            file_id=file_id, raw_data=raw, magic="RWAR",
        )
        group = self._data.group_entries[group_index]
        group.group_table.append(GroupTableEntry(
            group_index=file_index,
            file_data_offset=0,
            file_data_size=0,
            audio_data_offset=0,
            audio_data_size=len(raw),
            file_id=None,
            audio_file_id=file_id,
        ))
        sub_index = len(group.group_table) - 1
        if group.audio_file_id is None:
            group.audio_file_id = file_id
        self._data.file_entries.append(FileEntry(
            file_size=0,
            wave_file_size=len(raw),
            entry_num=-1,
            external_file_path=None,
            file_positions=[FilePositionEntry(group_index=group_index, index=sub_index)],
        ))
        self.register_new("file", file_index)
        self.register_new("group_item", group_index, file_index)
        self._sync_file_group_metadata(file_index)
        self.clear_subfile_caches()
        self.mark_dirty(DirtyFlags.DATA)
        return file_id

    def replace_wave_archive(self, file_id: int, raw: bytes) -> int:
        file_id = int(file_id)
        copy_ids = self._wave_archive_copy_ids(file_id)
        brwar = Brwar.from_bytes(bytes(raw))
        required, users = self._required_wave_archive_entries(file_id)
        if len(brwar) < required:
            detail = ", ".join(users) or "linked files"
            raise BrsarError(
                f"Replacement has {len(brwar)} waves, but {detail} requires at least {required}"
            )
        serialized = bytes(raw)
        # Commit only after every validation above succeeds. All physical group
        # copies of this logical FILE entry must change atomically.
        for copy_id in copy_ids:
            self._data.embedded_files[copy_id].raw_data = serialized
        for group in self._data.group_entries:
            for sub in group.group_table:
                if sub.audio_file_id in copy_ids:
                    sub.audio_data_size = len(serialized)
                    file_index = int(sub.group_index)
                    if 0 <= file_index < len(self._data.file_entries):
                        self._data.file_entries[file_index].wave_file_size = len(serialized)
        self._refresh_group_size_fields()
        self.clear_subfile_caches()
        self.mark_dirty(DirtyFlags.DATA)
        return len(brwar)

    def delete_wave_archive(self, file_id: int, *, detach_references: bool = False) -> None:
        """Delete an RWAR, optionally detaching data files that reference it."""
        file_id = int(file_id)
        self.require_wave_archive_delete(file_id)
        logical_files = self.logical_file_indices_for_embedded(file_id)
        copy_ids = self._wave_archive_copy_ids(file_id)
        references = self.get_wave_archive_references(file_id)
        live_references = [ref for ref in references if ref["kind"] in {"bank", "sound", "file"}]
        if live_references and not detach_references:
            names = ", ".join(ref["name"] for ref in live_references[:4])
            if len(live_references) > 4:
                names += f" (+{len(live_references) - 4})"
            raise BrsarError(f"Wave archive is referenced by {names}")

        orphan_entries: list[tuple[int, int, int]] = []
        for group_index, group in enumerate(self._data.group_entries):
            for sub_index, sub in enumerate(group.group_table):
                if sub.audio_file_id not in copy_ids:
                    continue
                file_index = int(sub.group_index)
                if sub.file_id is None:
                    orphan_entries.append((group_index, sub_index, file_index))
                    continue
                sub.audio_file_id = None
                sub.audio_data_offset = 0
                sub.audio_data_size = 0
                if 0 <= file_index < len(self._data.file_entries):
                    self._data.file_entries[file_index].wave_file_size = 0

        for copy_id in copy_ids:
            del self._data.embedded_files[copy_id]
        for group_index, sub_index, file_index in reversed(orphan_entries):
            self._delete_group_table_entry(group_index, sub_index)
            if 0 <= file_index < len(self._data.file_entries):
                entry = self._data.file_entries[file_index]
                entry.file_size = 0
                entry.wave_file_size = 0
                entry.file_positions.clear()
        for group in self._data.group_entries:
            if group.audio_file_id in copy_ids:
                remaining = [
                    int(sub.audio_file_id)
                    for sub in group.group_table
                    if sub.audio_file_id is not None and sub.audio_file_id in self._data.embedded_files
                ]
                group.audio_file_id = min(remaining) if remaining else None
        for file_index in logical_files:
            self.unregister_new("file", file_index, recursive=True)
        self._refresh_group_size_fields()
        self.clear_subfile_caches()
        self.mark_dirty(DirtyFlags.DATA)

    def _append_file_to_group(
            self,
            *,
            group_index: int,
            file_raw: bytes,
            audio_raw: bytes,
            file_size: int,
            wave_file_size: int,
            file_magic: str = "RWSD",
            audio_magic: str = "RWAR",
    ) -> int:
        """Create FILE/INFO linkage for a new file+audio pair in a group. Returns new file_index."""
        if group_index < 0 or group_index >= len(self._data.group_entries):
            raise BrsarError(f"Invalid group_index {group_index}")

        group = self._data.group_entries[group_index]
        new_file_index = len(self._data.file_entries)

        file_id = self._next_embedded_file_id(self._data.embedded_files)
        audio_file_id = file_id + 1
        self._data.embedded_files[file_id] = EmbeddedFile(
            file_id=file_id, raw_data=file_raw, magic=str(file_magic)
        )
        self._data.embedded_files[audio_file_id] = EmbeddedFile(
            file_id=audio_file_id, raw_data=audio_raw, magic=str(audio_magic)
        )

        sub = GroupTableEntry(
            # In RSAR group sub-entries this field is the file table index,
            # not the parent group index.
            group_index=new_file_index,
            file_data_offset=0,
            file_data_size=len(file_raw),
            audio_data_offset=0,
            audio_data_size=len(audio_raw),
            file_id=file_id,
            audio_file_id=audio_file_id,
        )
        group.group_table.append(sub)
        sub_index = len(group.group_table) - 1

        # If this is the first entry in the group, make it group base.
        if group.file_id is None:
            group.file_id = file_id
        if group.audio_file_id is None:
            group.audio_file_id = audio_file_id

        file_entry = FileEntry(
            file_size=file_size,
            wave_file_size=wave_file_size,
            entry_num=-1,
            external_file_path=None,
            file_positions=[FilePositionEntry(group_index=group_index, index=sub_index)],
        )
        self._data.file_entries.append(file_entry)
        self.register_new("file", new_file_index)
        self.register_new("group_item", group_index, new_file_index)
        self._sync_file_group_metadata(new_file_index)
        return new_file_index

    def _append_data_file_to_group(
            self,
            *,
            group_index: int,
            file_raw: bytes,
            file_magic: str,
    ) -> int:
        """Append an embedded file that has no companion audio payload.

        RSEQ files are data-only FILE entries.  Keeping this separate from
        ``_append_file_to_group`` prevents zero-byte pseudo-RWAR entries from
        being emitted for newly imported sequences.
        """
        group_index = int(group_index)
        if not 0 <= group_index < len(self._data.group_entries):
            raise BrsarError(f"Invalid group_index {group_index}")

        raw = bytes(file_raw)
        file_index = len(self._data.file_entries)
        file_id = self._next_embedded_file_id(self._data.embedded_files)
        self._data.embedded_files[file_id] = EmbeddedFile(
            file_id=file_id,
            raw_data=raw,
            magic=str(file_magic),
        )

        group = self._data.group_entries[group_index]
        group.group_table.append(GroupTableEntry(
            group_index=file_index,
            file_data_offset=0,
            file_data_size=len(raw),
            audio_data_offset=0,
            audio_data_size=0,
            file_id=file_id,
            audio_file_id=None,
        ))
        sub_index = len(group.group_table) - 1
        if group.file_id is None:
            group.file_id = file_id
        self._data.file_entries.append(FileEntry(
            file_size=len(raw),
            wave_file_size=0,
            entry_num=-1,
            external_file_path=None,
            file_positions=[FilePositionEntry(group_index=group_index, index=sub_index)],
        ))
        self.register_new("file", file_index)
        self.register_new("group_item", group_index, file_index)
        self._sync_file_group_metadata(file_index)
        return file_index

    def _append_data_file_to_groups(
            self,
            *,
            group_indices: list[int],
            file_raw: bytes,
            file_magic: str,
    ) -> int:
        """Append one logical data file with a physical copy in every group."""
        unique_groups: list[int] = []
        seen: set[int] = set()
        for value in group_indices:
            group_index = int(value)
            if group_index in seen:
                continue
            if not 0 <= group_index < len(self._data.group_entries):
                raise BrsarError(f"Invalid group_index {group_index}")
            seen.add(group_index)
            unique_groups.append(group_index)
        if not unique_groups:
            raise BrsarError("At least one target group is required")

        raw = bytes(file_raw)
        file_index = self._append_data_file_to_group(
            group_index=unique_groups[0],
            file_raw=raw,
            file_magic=file_magic,
        )
        for group_index in unique_groups[1:]:
            file_id = self._next_embedded_file_id(self._data.embedded_files)
            self._data.embedded_files[file_id] = EmbeddedFile(
                file_id=file_id,
                raw_data=raw,
                magic=str(file_magic),
            )
            group = self._data.group_entries[group_index]
            group.group_table.append(GroupTableEntry(
                group_index=file_index,
                file_data_offset=0,
                file_data_size=len(raw),
                audio_data_offset=0,
                audio_data_size=0,
                file_id=file_id,
                audio_file_id=None,
            ))
            if group.file_id is None:
                group.file_id = file_id
            self.register_new("group_item", group_index, file_index)

        self._sync_file_group_metadata(file_index)
        return file_index

    def _find_seq_file_indices(self) -> list[int]:
        """Return logical FILE indices whose embedded data is an RSEQ."""
        return [
            file_index
            for file_index in range(len(self._data.file_entries))
            if (self._resolve_file_raw(file_index) or b"")[:4] == b"RSEQ"
        ]

    @staticmethod
    def _seq_physical_commands(brseq) -> list:
        """Return each physical command once, in byte-offset order."""
        canonical = list(getattr(brseq.data, "command_stream", ()) or ())
        if canonical:
            return canonical
        commands = []
        seen_objects: set[int] = set()
        for track in brseq.data.tracks.values():
            for command in track.commands:
                marker = id(command)
                if marker in seen_objects:
                    continue
                seen_objects.add(marker)
                commands.append(command)
        return sorted(commands, key=lambda command: int(command.offset))

    @staticmethod
    def _seq_alloc_track_mask(brseq, label_name: str | None = None) -> int:
        """Derive the allocation mask for one entry point, never globally.

        A shared RSEQ can contain hundreds of independent sounds, each with a
        different leading ALLOC_TRACK.  OR-ing commands across the file would
        therefore allocate unrelated tracks.
        """
        from pysar.core.format.rseq.mml import MML

        labels = list(brseq.data.labels)
        if label_name is not None:
            label = next((item for item in labels if item.name == label_name), None)
            if label is None:
                raise BrsarError(f'Sequence label "{label_name}" was not found')
        else:
            label = next((item for item in labels if item.name == "main"), None)
            if label is None:
                label = next((item for item in labels if item.name.lower().endswith("_start")), None)
            if label is None:
                label = min(labels, key=lambda item: int(item.offset), default=None)
        base_offset = int(label.offset) if label is not None else 0
        commands = Brsar._seq_physical_commands(brseq)

        def allocation_at(command_index: int) -> int | None:
            command = commands[command_index]
            try:
                if command.get_mml() != MML.ALLOC_TRACK or not command.args:
                    return None
            except Exception:
                return None
            return (1 | int(command.args[0])) & 0xFFFF

        # Normal sound-label form: the label points directly to ALLOC_TRACK.
        for index, command in enumerate(commands):
            if int(command.offset) == base_offset:
                allocation = allocation_at(index)
                if allocation is not None:
                    return allocation
                break
        # smfconv also emits a *_Start label immediately after a *_Begin
        # ALLOC_TRACK.  Recover that local mask without looking at unrelated
        # entry points elsewhere in the shared file.
        for index in range(len(commands) - 1):
            if int(commands[index + 1].offset) != base_offset:
                continue
            allocation = allocation_at(index)
            if allocation is not None:
                return allocation
        # Track 0 owns the sequence entry point even for a one-track template.
        return 1

    @staticmethod
    def _seq_effective_label_offset(brseq, label_name: str | None = None) -> int:
        """Resolve a LABL name and skip a leading ALLOC_TRACK command.

        NW4R sound entries normally start immediately after ALLOC_TRACK; the
        same mask is already stored in ``SeqSoundInfo.alloc_track``.
        """
        from pysar.core.format.rseq.mml import MML

        labels = list(brseq.data.labels)
        if label_name is not None:
            label = next((item for item in labels if item.name == label_name), None)
            if label is None:
                raise BrsarError(f'Sequence label "{label_name}" was not found')
        else:
            label = next((item for item in labels if item.name == "main"), None)
            if label is None:
                label = next((item for item in labels if item.name.lower().endswith("_start")), None)
            if label is None:
                label = min(labels, key=lambda item: int(item.offset), default=None)

        base_offset = int(label.offset) if label is not None else 0
        commands = [
            command for command in Brsar._seq_physical_commands(brseq)
            if int(command.offset) >= base_offset
        ]
        if commands and int(commands[0].offset) == base_offset:
            try:
                is_alloc = commands[0].get_mml() == MML.ALLOC_TRACK
            except Exception:
                is_alloc = False
            if is_alloc and len(commands) > 1:
                return int(commands[1].offset)
        return base_offset

    @classmethod
    def _seq_label_for_effective_offset(cls, brseq, offset: int) -> str | None:
        """Find the label represented by an RSAR sequence start offset."""
        offset = int(offset)
        for label in brseq.data.labels:
            if int(label.offset) == offset:
                return label.name
            try:
                if cls._seq_effective_label_offset(brseq, label.name) == offset:
                    return label.name
            except BrsarError:
                continue
        return None

    def add_seq_sound(
            self,
            name: str,
            *,
            bank_index: int,
            player_index: int = 0,
            volume: int = 90,
            start_label: str | None = None,
            seq_file_index: int | None = None,
            brseq_raw: bytes | None = None,
            group_index: int | None = None,
            actor_player_id: int = 0,
    ) -> int:
        """Create a SEQ sound from an existing or newly embedded BRSEQ.

        Exactly one of ``seq_file_index`` and ``brseq_raw`` must be supplied.
        The return value is the stable sound-table index.
        """
        from pysar.core.format.rseq import Brseq

        sound_name = self._validate_sound_name(name)
        bank_index = int(bank_index)
        player_index = int(player_index)
        volume = int(volume)
        if not 0 <= bank_index < len(self._data.bank_entries):
            raise BrsarError(f"Invalid bank index {bank_index}")
        if not 0 <= player_index < len(self._data.player_entries):
            raise BrsarError(f"Invalid player index {player_index}")
        if not 0 <= volume <= 127:
            raise BrsarError("Volume must be between 0 and 127")
        if (seq_file_index is None) == (brseq_raw is None):
            raise BrsarError("Choose exactly one BRSEQ source")

        start_offset: int | None = None
        alloc_track: int | None = None
        if brseq_raw is not None:
            raw = bytes(brseq_raw)
            brseq = Brseq.from_bytes(raw)
            start_offset = self._seq_effective_label_offset(brseq, start_label)
            alloc_track = self._seq_alloc_track_mask(brseq, start_label)
            track_capacity = int(self._data.arc_common_info.n_seq_tracks)
            if track_capacity > 0 and alloc_track.bit_count() > track_capacity:
                raise BrsarError(
                    f"Sequence needs {alloc_track.bit_count()} tracks, but this archive reserves "
                    f"only {track_capacity}"
                )
            if group_index is None:
                if not self._data.group_entries:
                    group_index = self.create_group("GROUP_0000")
                else:
                    group_index = 0
            seq_file_index = self._append_data_file_to_group(
                group_index=int(group_index),
                file_raw=raw,
                file_magic="RSEQ",
            )
            self._seq_cache[int(seq_file_index)] = brseq
        else:
            seq_file_index = int(seq_file_index)
            if not 0 <= seq_file_index < len(self._data.file_entries):
                raise BrsarError(f"Invalid sequence file index {seq_file_index}")
            raw = self._resolve_file_raw(seq_file_index)
            if raw is None or raw[:4] != b"RSEQ":
                raise BrsarError(f"File index {seq_file_index} is not a BRSEQ")
            brseq = self.get_seq(seq_file_index)

        if start_offset is None or alloc_track is None:
            start_offset = self._seq_effective_label_offset(brseq, start_label)
            alloc_track = self._seq_alloc_track_mask(brseq, start_label)
        track_capacity = int(self._data.arc_common_info.n_seq_tracks)
        if track_capacity > 0 and alloc_track.bit_count() > track_capacity:
            raise BrsarError(
                f"Sequence needs {alloc_track.bit_count()} tracks, but this archive reserves "
                f"only {track_capacity}"
            )
        had_sequence_sounds = any(
            candidate.sound_type == SoundType.SEQ
            for candidate in self._data.sound_entries
        )
        # These INFO values are runtime pool capacities, not table counts.
        # Preserve authored capacities, but make a genuinely sequence-free
        # archive capable of playing its first newly added sequence.
        if not had_sequence_sounds:
            if int(self._data.arc_common_info.n_seq_sounds) == 0:
                self._data.arc_common_info.n_seq_sounds = 1
            if track_capacity == 0:
                self._data.arc_common_info.n_seq_tracks = max(
                    1, int(alloc_track).bit_count(),
                )
        sound_index = len(self._data.sound_entries)
        name_index = self._get_or_add_name(sound_name)
        self._data.sound_entries.append(SoundDataEntry(
            file_name_index=name_index,
            file_index=int(seq_file_index),
            player_index=player_index,
            volume=volume,
            player_priority=64,
            actor_player_id=int(actor_player_id),
            sound_type=SoundType.SEQ,
            sound_info=SeqSoundInfo(
                seq_label_offset=start_offset,
                bank_index=bank_index,
                alloc_track=alloc_track,
                channel_priority=64,
                release_priority_fix=0,
            ),
        ))
        self._rebuild_sound_trie()
        # ArcCommonInfo fields are runtime capacities, not table counts.
        self.register_new("sound", sound_index)
        self.mark_dirty(DirtyFlags.DATA)
        return sound_index

    def replace_seq_sound_data(
            self,
            sound_index: int,
            brseq_raw: bytes,
            *,
            start_label: str | None = None,
            copy_on_write: bool = True,
            group_index: int | None = None,
    ) -> int:
        """Replace one SEQ sound's bytecode, isolating shared files by default.

        Returns the logical FILE index used after replacement.  Copy-on-write
        is essential for Nintendo archives where hundreds of sound entries can
        use different labels in the same BRSEQ.  The isolated logical file is
        physically copied into every load group that held the original.  An
        explicit ``group_index`` adds supplemental coverage; it never silently
        removes inherited groups.
        """
        from pysar.core.format.rseq import Brseq

        sound_index = int(sound_index)
        if not 0 <= sound_index < len(self._data.sound_entries):
            raise BrsarError(f"Invalid sound index {sound_index}")
        entry = self._data.sound_entries[sound_index]
        if entry.sound_type != SoundType.SEQ or not isinstance(entry.sound_info, SeqSoundInfo):
            raise BrsarError(f"Sound {sound_index} is not a SEQ sound")
        self.require_safe_mutation(
            "replacing its sequence data", "sound", sound_index,
        )

        raw = bytes(brseq_raw)
        replacement = Brseq.from_bytes(raw)
        old_file_index = int(entry.file_index)
        old_brseq = self.get_seq(old_file_index)
        requested_label = start_label
        if requested_label is None:
            old_label = self._seq_label_for_effective_offset(
                old_brseq, int(entry.sound_info.seq_label_offset),
            )
            replacement_labels = {label.name for label in replacement.data.labels}
            if old_label in replacement_labels:
                requested_label = old_label

        replacement_start_offset = self._seq_effective_label_offset(
            replacement, requested_label,
        )
        replacement_alloc_track = self._seq_alloc_track_mask(replacement, requested_label)
        track_capacity = int(self._data.arc_common_info.n_seq_tracks)
        if track_capacity > 0 and replacement_alloc_track.bit_count() > track_capacity:
            raise BrsarError(
                f"Sequence needs {replacement_alloc_track.bit_count()} tracks, but this archive "
                f"reserves only {track_capacity}"
            )

        references = [
            item for item in self._data.sound_entries
            if item.sound_type == SoundType.SEQ and int(item.file_index) == old_file_index
        ]
        if copy_on_write and len(references) > 1:
            positions = self._data.file_entries[old_file_index].file_positions
            inherited_groups = list(dict.fromkeys(
                int(position.group_index) for position in positions
            ))
            for candidate_group, group in enumerate(self._data.group_entries):
                if (
                    candidate_group not in inherited_groups
                    and any(int(sub.group_index) == old_file_index for sub in group.group_table)
                ):
                    inherited_groups.append(candidate_group)
            if not inherited_groups:
                raise BrsarError("Shared BRSEQ has no group position")
            if group_index is not None and int(group_index) not in inherited_groups:
                inherited_groups.append(int(group_index))
            new_file_index = self._append_data_file_to_groups(
                group_indices=inherited_groups,
                file_raw=raw,
                file_magic="RSEQ",
            )
            entry.file_index = new_file_index
            self._seq_cache[new_file_index] = replacement
        else:
            new_file_index = old_file_index
            self._update_all_file_copies(
                old_file_index, raw, expected_magic="RSEQ",
            )
            self._seq_cache[old_file_index] = replacement

        entry.sound_info.seq_label_offset = replacement_start_offset
        entry.sound_info.alloc_track = replacement_alloc_track
        self.clear_subfile_caches()
        self._seq_cache[new_file_index] = replacement
        self.mark_dirty(DirtyFlags.DATA)
        return new_file_index

    def set_seq_sound_start_label(self, sound_index: int, label_name: str) -> int:
        """Change only a SEQ sound's entry point without rewriting its BRSEQ."""
        sound_index = int(sound_index)
        if not 0 <= sound_index < len(self._data.sound_entries):
            raise BrsarError(f"Invalid sound index {sound_index}")
        entry = self._data.sound_entries[sound_index]
        if entry.sound_type != SoundType.SEQ or not isinstance(entry.sound_info, SeqSoundInfo):
            raise BrsarError(f"Sound {sound_index} is not a SEQ sound")
        self.require_safe_mutation(
            "changing its sequence entry point", "sound", sound_index,
        )
        brseq = self.get_seq(entry.file_index)
        offset = self._seq_effective_label_offset(brseq, str(label_name))
        alloc_track = self._seq_alloc_track_mask(brseq, str(label_name))
        track_capacity = int(self._data.arc_common_info.n_seq_tracks)
        if track_capacity > 0 and alloc_track.bit_count() > track_capacity:
            raise BrsarError(
                f"Sequence needs {alloc_track.bit_count()} tracks, but this archive "
                f"reserves only {track_capacity}"
            )
        entry.sound_info.seq_label_offset = offset
        entry.sound_info.alloc_track = alloc_track
        self.mark_dirty(DirtyFlags.DATA)
        return offset

    def delete_seq_sound_entry(self, sound_index: int) -> None:
        """Delete one SEQ sound and discard its newly-created orphan payload."""
        sound_index = int(sound_index)
        if not 0 <= sound_index < len(self._data.sound_entries):
            raise BrsarError(f"Invalid sound index {sound_index}")
        if self._data.sound_entries[sound_index].sound_type != SoundType.SEQ:
            raise BrsarError(f"Sound {sound_index} is not a SEQ sound")
        self.require_safe_deletion("sound", sound_index)
        file_index = int(self._data.sound_entries[sound_index].file_index)
        del self._data.sound_entries[sound_index]
        self.remap_provenance_after_delete("sound", sound_index)
        self._rebuild_sound_trie()
        self._discard_orphan_new_seq_file(file_index)
        self.mark_dirty(DirtyFlags.DATA)

    def _discard_orphan_new_seq_file(self, file_index: int) -> bool:
        """Remove bytes/group links for an unreferenced, Pysar-created RSEQ.

        The FILE-table slot remains as a zero-size tombstone, so logical file
        indexes and Safe Mode identities never shift.
        """
        file_index = int(file_index)
        if not 0 <= file_index < len(self._data.file_entries):
            return False
        if not self.is_new("file", file_index):
            return False
        if any(int(sound.file_index) == file_index for sound in self._data.sound_entries):
            return False
        if any(int(bank.file_index) == file_index for bank in self._data.bank_entries):
            return False

        matches: list[tuple[int, int, int | None, int | None]] = []
        for group_index, group in enumerate(self._data.group_entries):
            for sub_index, sub in enumerate(group.group_table):
                if int(sub.group_index) == file_index:
                    matches.append((group_index, sub_index, sub.file_id, sub.audio_file_id))
        for group_index, sub_index, _, _ in sorted(
                matches, key=lambda item: (item[0], item[1]), reverse=True,
        ):
            self._delete_group_table_entry(group_index, sub_index)

        live_embedded_ids = {
            int(embedded_id)
            for group in self._data.group_entries
            for sub in group.group_table
            for embedded_id in (sub.file_id, sub.audio_file_id)
            if embedded_id is not None
        }
        for _, _, file_id, audio_file_id in matches:
            for embedded_id in (file_id, audio_file_id):
                if embedded_id is not None and int(embedded_id) not in live_embedded_ids:
                    self._data.embedded_files.pop(int(embedded_id), None)

        file_entry = self._data.file_entries[file_index]
        file_entry.file_size = 0
        file_entry.wave_file_size = 0
        file_entry.external_file_path = None
        file_entry.file_positions.clear()
        self._seq_cache.pop(file_index, None)
        self.unregister_new("file", file_index, recursive=True)
        self._refresh_group_size_fields()
        return True

    def _validate_bank_name(
            self,
            name: object,
            *,
            ignore_index: int | None = None,
    ) -> str:
        value = self._validate_ascii_name(name, "Bank")
        if value[0].isdigit() or not all(
            char.isalnum() or char == "_" for char in value
        ):
            raise BrsarError(
                "Bank names may contain only letters, numbers, and underscores, "
                "and cannot start with a number"
            )
        for index, entry in enumerate(self._data.bank_entries):
            if index == ignore_index:
                continue
            if (
                0 <= entry.file_name_index < len(self._data.names)
                and self._data.names[entry.file_name_index] == value
            ):
                raise BrsarError(f'Bank "{value}" already exists')
        return value

    def rename_bank(self, bank_index: int, name: object) -> None:
        bank_index = int(bank_index)
        if not 0 <= bank_index < len(self._data.bank_entries):
            raise BrsarError(f"Invalid bank index {bank_index}")
        entry = self._data.bank_entries[bank_index]
        current_name = (
            self._data.names[entry.file_name_index]
            if 0 <= entry.file_name_index < len(self._data.names)
            else ""
        )
        value = self._validate_bank_name(name, ignore_index=bank_index)
        if value == current_name:
            return
        self.require_safe_mutation("renaming it", "bank", bank_index)
        entry.file_name_index = self._get_or_add_name(value)
        self._rebuild_bank_trie()
        self.mark_dirty(DirtyFlags.DATA)

    @staticmethod
    def _remap_cache_after_delete(cache: dict[int, object], deleted_index: int) -> dict[int, object]:
        return {
            (index if index < deleted_index else index - 1): value
            for index, value in cache.items()
            if index != deleted_index
        }

    def _preflight_orphan_bank_file_delete(
            self,
            bank_index: int,
            file_index: int,
    ) -> list[tuple[int, int, int | None, int | None]] | None:
        remaining_users = [
            ("bank", index)
            for index, entry in enumerate(self._data.bank_entries)
            if index != bank_index and int(entry.file_index) == file_index
        ] + [
            ("sound", index)
            for index, entry in enumerate(self._data.sound_entries)
            if int(entry.file_index) == file_index
        ]
        if remaining_users:
            return None
        if not 0 <= file_index < len(self._data.file_entries):
            raise BrsarError(f"Bank has invalid logical file index {file_index}")

        self.require_safe_mutation("deleting it", "file", file_index)
        matches: list[tuple[int, int, int | None, int | None]] = []
        for group_index, group in enumerate(self._data.group_entries):
            for sub_index, sub in enumerate(group.group_table):
                if int(sub.group_index) != file_index:
                    continue
                if self._safe_mode:
                    self.require_safe_mutation(
                        "deleting it", "group_item", group_index, file_index,
                    )
                matches.append((
                    group_index,
                    sub_index,
                    sub.file_id,
                    sub.audio_file_id,
                ))
        return matches

    def _commit_orphan_bank_file_delete(
            self,
            file_index: int,
            matches: list[tuple[int, int, int | None, int | None]],
    ) -> None:
        candidate_ids = {
            int(embedded_id)
            for _, _, file_id, audio_file_id in matches
            for embedded_id in (file_id, audio_file_id)
            if embedded_id is not None
        }
        for group_index, sub_index, _, _ in sorted(
                matches, key=lambda item: (item[0], item[1]), reverse=True,
        ):
            self._delete_group_table_entry(group_index, sub_index)

        live_ids = {
            int(embedded_id)
            for group in self._data.group_entries
            for sub in group.group_table
            for embedded_id in (sub.file_id, sub.audio_file_id)
            if embedded_id is not None
        }
        for embedded_id in candidate_ids - live_ids:
            self._data.embedded_files.pop(embedded_id, None)

        file_entry = self._data.file_entries[file_index]
        file_entry.file_size = 0
        file_entry.wave_file_size = 0
        file_entry.external_file_path = None
        file_entry.file_positions.clear()
        self._seq_cache.pop(file_index, None)
        self._wave_war_cache.pop(file_index, None)
        self._wsd_cache.pop(file_index, None)
        self.unregister_new("file", file_index, recursive=True)
        self._refresh_group_size_fields()

    def delete_bank(
            self,
            bank_index: int,
            replacement_bank_index: int | None = None,
    ) -> int:
        """Delete a bank entry and keep SEQ/FILE references consistent."""
        bank_index = int(bank_index)
        count = len(self._data.bank_entries)
        if not 0 <= bank_index < count:
            raise BrsarError(f"Invalid bank index {bank_index}")
        self.require_safe_deletion("bank", bank_index)

        replacement = (
            None if replacement_bank_index is None else int(replacement_bank_index)
        )
        if replacement is not None and (
            not 0 <= replacement < count or replacement == bank_index
        ):
            raise BrsarError("Replacement bank is invalid")
        referenced = any(
            sound.sound_type == SoundType.SEQ
            and isinstance(sound.sound_info, SeqSoundInfo)
            and int(sound.sound_info.bank_index) == bank_index
            for sound in self._data.sound_entries
        )
        if referenced and replacement is None:
            raise BrsarError("This bank is referenced by sequence sounds; choose a replacement bank")

        file_index = int(self._data.bank_entries[bank_index].file_index)
        file_delete = self._preflight_orphan_bank_file_delete(bank_index, file_index)
        replacement_new = -1
        if replacement is not None:
            replacement_new = replacement - 1 if replacement > bank_index else replacement

        del self._data.bank_entries[bank_index]
        for sound in self._data.sound_entries:
            if sound.sound_type != SoundType.SEQ or not isinstance(sound.sound_info, SeqSoundInfo):
                continue
            current = int(sound.sound_info.bank_index)
            if current == bank_index:
                sound.sound_info.bank_index = replacement_new
            elif bank_index < current < count:
                sound.sound_info.bank_index = current - 1

        self.remap_provenance_after_delete("bank", bank_index)
        self._bank_cache = self._remap_cache_after_delete(self._bank_cache, bank_index)
        self._war_cache = self._remap_cache_after_delete(self._war_cache, bank_index)
        if file_delete is not None:
            self._commit_orphan_bank_file_delete(file_index, file_delete)
        self._rebuild_bank_trie()
        self.mark_dirty(DirtyFlags.DATA)
        return replacement_new

    def create_bank(
            self,
            name: str,
            bank_raw: bytes,
            wave_archive_raw: bytes,
            *,
            group_index: int | None = None,
    ) -> int:
        """Add a complete RBNK/RWAR bank pair and return its bank index."""
        from pysar.core.format.rbnk import Brbnk
        from pysar.core.format.rwar import Brwar

        bank_name = self._validate_bank_name(name)

        # Validate both payloads before touching the archive.
        bank_bytes = bytes(bank_raw)
        wave_bytes = bytes(wave_archive_raw)
        new_bank = Brbnk.from_bytes(bank_bytes)
        new_war = Brwar.from_bytes(wave_bytes)
        invalid = [] if new_bank.data.has_embedded_waves else sorted(
            index for index in new_bank.get_wave_indices() if not 0 <= index < len(new_war)
        )
        if invalid:
            raise BrsarError(
                f"Bank references missing RWAR wave {invalid[0]} "
                f"(archive has {len(new_war)} waves)"
            )
        if group_index is None:
            if not self._data.group_entries:
                group_index = self.create_group("GROUP_0000")
            else:
                group_index = 0

        file_index = self._append_file_to_group(
            group_index=int(group_index),
            file_raw=bank_bytes,
            audio_raw=wave_bytes,
            file_size=len(bank_bytes),
            wave_file_size=len(wave_bytes),
            file_magic="RBNK",
            audio_magic="RWAR",
        )
        name_index = self._get_or_add_name(bank_name)
        self._data.bank_entries.append(SoundBankEntry(
            file_name_index=name_index,
            file_index=file_index,
            bank_index=0,
        ))
        bank_index = len(self._data.bank_entries) - 1
        self.register_new("bank", bank_index)
        self._rebuild_bank_trie()
        self.clear_subfile_caches()
        self.mark_dirty(DirtyFlags.DATA)
        return bank_index

    def replace_bank_file(
            self,
            bank_index: int,
            bank_raw: bytes,
            wave_archive_raw: bytes | None = None,
            *,
            preserve_child_provenance: bool = False,
            bootstrap_empty_wave: bool = False,
    ) -> None:
        """Replace every physical copy of one logical bank file atomically.

        A BRSAR may embed the same logical FILE-table entry once per load
        group. Updating only the first group copy leaves an archive whose
        groups disagree, so collect and validate every copy before committing.
        """
        from pysar.core.format.rbnk import Brbnk
        from pysar.core.format.rwar import Brwar
        from pysar.core.format.rwav import Brwav

        bank_index = int(bank_index)
        if not 0 <= bank_index < len(self._data.bank_entries):
            raise BrsarError(f"Invalid bank index {bank_index}")
        file_index = int(self._data.bank_entries[bank_index].file_index)
        if not 0 <= file_index < len(self._data.file_entries):
            raise BrsarError(f"Bank has invalid logical file index {file_index}")

        bank_bytes = bytes(bank_raw)
        replacement_bank = Brbnk.from_bytes(bank_bytes)
        wave_bytes = None if wave_archive_raw is None else bytes(wave_archive_raw)
        replacement_war = None if wave_bytes is None else Brwar.from_bytes(wave_bytes)

        data_ids: set[int] = set()
        audio_ids: set[int] = set()
        matching_subs: list[GroupTableEntry] = []
        for group in self._data.group_entries:
            for sub in group.group_table:
                if int(sub.group_index) != file_index:
                    continue
                matching_subs.append(sub)
                if sub.file_id is not None:
                    data_ids.add(int(sub.file_id))
                if sub.audio_file_id is not None:
                    audio_ids.add(int(sub.audio_file_id))
        if not data_ids:
            raise BrsarError("Bank has no physical RBNK copies")
        for file_id in data_ids:
            embedded = self._data.embedded_files.get(file_id)
            if embedded is None or embedded.magic != "RBNK":
                raise BrsarError(f"Bank physical file {file_id} is not an RBNK")
        for file_id in audio_ids:
            embedded = self._data.embedded_files.get(file_id)
            if embedded is None or embedded.magic != "RWAR":
                raise BrsarError(f"Bank physical audio file {file_id} is not an RWAR")
        if wave_bytes is not None and not audio_ids:
            raise BrsarError("Bank has no companion RWAR copies for SF2 samples")
        wave_indices = replacement_bank.get_wave_indices()
        bootstrapped_wave = False
        existing_wave_counts: dict[bytes, int] = {}
        if (
            bootstrap_empty_wave
            and wave_bytes is None
            and 0 in wave_indices
            and audio_ids
        ):
            wave_counts: dict[int, int] = {}
            for file_id in audio_ids:
                current_raw = self._data.embedded_files[file_id].raw_data
                if current_raw not in existing_wave_counts:
                    existing_wave_counts[current_raw] = len(Brwar.from_bytes(current_raw))
                wave_counts[file_id] = existing_wave_counts[current_raw]
            empty_ids = [file_id for file_id, count in wave_counts.items() if count == 0]
            if empty_ids:
                if len(empty_ids) != len(audio_ids):
                    raise BrsarError(
                        "Bank companion RWAR copies disagree about whether wave 0 exists"
                    )
                current_payloads = {
                    self._data.embedded_files[file_id].raw_data
                    for file_id in audio_ids
                }
                if len(current_payloads) != 1:
                    raise BrsarError(
                        "Bank companion RWAR copies are not byte-identical; refusing to "
                        "overwrite them while creating wave 0"
                    )
                replacement_war = Brwar.from_bytes(next(iter(current_payloads)))
                replacement_war.add(Brwav.from_pcm([0] * 14, 32_000))
                wave_bytes = replacement_war.to_bytes()
                # Reparse before the archive is touched. This validates both
                # the generated RWAR and its nested RWAV serialization.
                replacement_war = Brwar.from_bytes(wave_bytes)
                if len(replacement_war) != 1:
                    raise BrsarError("Failed to create the bank's initial RWAR wave")
                Brwav.from_bytes(replacement_war[0].to_bytes())
                bootstrapped_wave = True
        if replacement_war is not None:
            invalid = sorted(index for index in wave_indices if not 0 <= index < len(replacement_war))
            if invalid:
                raise BrsarError(
                    f"Replacement bank references missing RWAR wave {invalid[0]} "
                    f"(archive has {len(replacement_war)} waves)"
                )
        elif audio_ids:
            # Existing physical copies are permitted to differ in metadata,
            # but the new mapping must be valid for every one of them.
            for file_id in audio_ids:
                current_raw = self._data.embedded_files[file_id].raw_data
                if current_raw not in existing_wave_counts:
                    existing_wave_counts[current_raw] = len(Brwar.from_bytes(current_raw))
                wave_count = existing_wave_counts[current_raw]
                invalid = sorted(index for index in wave_indices if not 0 <= index < wave_count)
                if invalid:
                    raise BrsarError(
                        f"Replacement bank references missing wave {invalid[0]} in RWAR copy {file_id}"
                    )
        elif wave_indices and not replacement_bank.data.has_embedded_waves:
            raise BrsarError("Replacement bank references waves but has no companion RWAR")

        # Commit only after all RBNK/RWAR copies and references validate.
        for file_id in data_ids:
            self._data.embedded_files[file_id].raw_data = bank_bytes
        if wave_bytes is not None:
            for file_id in audio_ids:
                self._data.embedded_files[file_id].raw_data = wave_bytes
        for sub in matching_subs:
            if sub.file_id in data_ids:
                sub.file_data_size = len(bank_bytes)
            if wave_bytes is not None and sub.audio_file_id in audio_ids:
                sub.audio_data_size = len(wave_bytes)
        file_entry = self._data.file_entries[file_index]
        file_entry.file_size = len(bank_bytes)
        if wave_bytes is not None:
            file_entry.wave_file_size = len(wave_bytes)
        # Recompute each group's physical offsets/sizes directly. Unlike the
        # generic synchronizer this does not copy the first RWAR size over
        # untouched audio copies during an RBNK-only edit.
        if not preserve_child_provenance:
            self.clear_bank_child_provenance(bank_index)
        if bootstrapped_wave:
            self.register_new("wave", file_index, 0)
        self._refresh_group_size_fields()
        self.clear_subfile_caches()
        self.mark_dirty(DirtyFlags.DATA)

    #
    # Sound modification
    #

    def create_brwsd(
            self,
            *,
            group_index: int | None = None,
            name: str | None = None,
    ) -> int:
        """
        Create a new BRWSD (+BRWAR) file mapping and return its file_index.
        """
        from pysar.core.format.rwar import Brwar
        from pysar.core.format.rwsd import Brwsd

        if group_index is None:
            if self._data.group_entries:
                group_index = 0
            else:
                group_name = "GROUP_0000"
                group_name_idx = self._get_or_add_name(group_name)
                self._data.group_entries.append(GroupDataEntry(file_name_index=group_name_idx, entry_num=-1))
                group_index = 0
                self.register_new("group", group_index)

        base_name = name or f"BRWSD_{len(self._data.file_entries):04d}"
        self._get_or_add_name(base_name)

        brwsd = Brwsd.new()
        brwar = Brwar.new()

        file_index = self._append_file_to_group(
            group_index=group_index,
            file_raw=brwsd.to_bytes(),
            audio_raw=brwar.to_bytes(),
            file_size=len(brwsd.to_bytes()),
            wave_file_size=len(brwar.to_bytes()),
        )

        self._wsd_cache.pop(file_index, None)
        self._war_cache.pop(file_index, None)
        self._wave_war_cache.pop(file_index, None)
        self.mark_dirty(DirtyFlags.DATA)
        return file_index

    def add_wav_sound(
            self,
            name: str,
            brwav,
            *,
            volume: int = 90,
            player_index: int = 0,
            actor_player_id: int = 0,
            brwsd_file_index: int | None = None,
            group_index: int | None = None,
            create_brwsd_if_missing: bool = True,
    ) -> SoundDataEntry:
        """
        Add a new WAVE sound.

        Modes:
          - auto-pick first BRWSD
          - use specific BRWSD via brwsd_file_index
          - create BRWSD when missing (optionally in group_index)
        """
        from pysar.core.format.rwar import Brwar
        from pysar.core.format.rwsd import Brwsd

        name = self._validate_sound_name(name)
        if not 0 <= volume <= 127:
            raise BrsarError(f'volume must be 0-127, got {volume}')
        if not 0 <= player_index < len(self._data.player_entries):
            raise BrsarError(
                f'player_index must be 0-{len(self._data.player_entries) - 1}, got {player_index}'
            )

        target_file_index = brwsd_file_index
        if target_file_index is None:
            wsd_indices = self._find_brwsd_file_indices()
            if wsd_indices:
                target_file_index = wsd_indices[0]
            elif create_brwsd_if_missing:
                target_file_index = self.create_brwsd(group_index=group_index, name=f"{name}_WSD")
            else:
                raise BrsarError("No BRWSD file available and create_brwsd_if_missing=False")

        if target_file_index < 0 or target_file_index >= len(self._data.file_entries):
            raise BrsarError(f"Invalid BRWSD file index {target_file_index}")
        if not self._looks_like_brwsd_file_index(target_file_index):
            raise BrsarError(f"File index {target_file_index} is not a BRWSD file")

        wsd_raw = self._resolve_file_raw(target_file_index)
        war_raw = self._resolve_audio_raw(target_file_index)
        if wsd_raw is None or war_raw is None:
            raise BrsarError(f"Could not resolve BRWSD/BRWAR for file index {target_file_index}")

        brwsd = Brwsd.from_bytes(wsd_raw)
        brwar = Brwar.from_bytes(war_raw)

        wav_idx = brwar.add(brwav)

        brwsd.add_wave_sound(wave_index=wav_idx)
        wave_sound_index = len(brwsd) - 1
        self.register_new("wave", target_file_index, wav_idx)
        self.register_new("wsd_entry", target_file_index, wave_sound_index)

        # Write BRWSD + BRWAR back to embedded files.
        self._update_file_raw_for_sound_idx(target_file_index, brwsd.to_bytes())
        self._update_audio_file_for_sound_idx(target_file_index, brwar.to_bytes())

        # Add sound entry.
        name_idx = self._get_or_add_name(name)
        sound = SoundDataEntry(
            file_name_index=name_idx,
            file_index=target_file_index,
            player_index=player_index,
            volume=volume,
            player_priority=64,
            actor_player_id=actor_player_id,
            sound_type=SoundType.WAVE,
            sound_info=WaveSoundInfo(
                wave_index=wave_sound_index,
                alloc_track=1,
                channel_priority=64,
                release_priority_fix=0,
            ),
        )
        self._data.sound_entries.append(sound)
        sound_index = len(self._data.sound_entries) - 1
        self.register_new("sound", sound_index)

        # ArcCommonInfo stores runtime voice/track pool capacities, not INFO
        # table lengths.  Preserve authored non-zero pools; a previously empty
        # archive only needs the minimum resources for this one-track sound.
        common = self._data.arc_common_info
        if common.n_wave_sounds == 0:
            common.n_wave_sounds = 1
        if common.n_wave_tracks == 0:
            common.n_wave_tracks = 1

        # Keep symbol links in sync so added sounds are resolvable in-game.
        if self._data.snd_trie is not None:
            self._data.snd_trie.insert(name, name_idx, len(self._data.sound_entries) - 1)
            self._data.snd_trie_raw = None
        else:
            self._rebuild_sound_trie()
        self.mark_dirty(DirtyFlags.DATA)
        return sound

    def delete_wav_sound(self, name: str) -> None:
        """
        Delete a WAVE sound from the BRSAR.

        Removes the sound entry, deletes its WSD slot from the BRWSD,
        shifts wave indices of other sounds sharing the same BRWSD.  Authored
        ArcCommonInfo runtime pool capacities are deliberately preserved.

        The corresponding BRWAR entry is intentionally kept to avoid
        breaking wave_index references in other WSD entries.

        Raises:
            BrsarError: If the sound is not found or is not a WAVE sound.
        """
        from pysar.core.format.rwsd import Brwsd

        result = self.lookup_sound(name)
        if result is None:
            raise BrsarError(f'Sound "{name}" not found')

        _, _, info_idx = result
        entry = self._data.sound_entries[info_idx]

        if entry.sound_type != SoundType.WAVE:
            raise BrsarError(f'Sound "{name}" is not a WAVE sound')
        self.require_safe_deletion("sound", info_idx)

        file_index = entry.file_index
        wave_info = entry.sound_info
        removed_wsd_idx = wave_info.wave_index

        # Remove the WSD entry from the BRWSD and write it back.
        wsd_raw = self._resolve_file_raw(file_index)
        if wsd_raw is not None:
            brwsd = Brwsd.from_bytes(wsd_raw)
            if 0 <= removed_wsd_idx < len(brwsd):
                del brwsd[removed_wsd_idx]

                # Shift wave indices of remaining WAVE sounds in same BRWSD.
                for s in self._data.sound_entries:
                    if s is entry:
                        continue
                    if (s.sound_type == SoundType.WAVE
                            and s.file_index == file_index
                            and isinstance(s.sound_info, WaveSoundInfo)
                            and s.sound_info.wave_index > removed_wsd_idx):
                        s.sound_info.wave_index -= 1

                # Write updated BRWSD back.
                new_raw = brwsd.to_bytes()
                self._update_file_raw_for_sound_idx(file_index, new_raw)

        # Remove the sound entry itself.
        del self._data.sound_entries[info_idx]
        self.remap_child_provenance_after_delete(
            "wsd_entry", (int(file_index),), int(removed_wsd_idx),
        )
        self.remap_provenance_after_delete("sound", info_idx)

        self._rebuild_sound_trie()
        self.mark_dirty(DirtyFlags.DATA)

    def remove_brwsd(
            self,
            file_index: int,
            *,
            remove_associated_sounds: bool = True,
    ) -> bool:
        """
        Remove a BRWSD mapping from the archive.

        This removes all WAVE sounds that point to the BRWSD (if enabled),
        unlinks the file from group/file tables, and removes the embedded BRWSD/BRWAR blobs.
        """
        if file_index < 0 or file_index >= len(self._data.file_entries):
            return False
        if not self._looks_like_brwsd_file_index(file_index):
            raise BrsarError(f"File index {file_index} is not a BRWSD file")
        self.require_safe_mutation("deleting it", "file", file_index)

        file_entry = self._data.file_entries[file_index]
        if not file_entry.file_positions:
            return False

        # Remove dependent WAVE sounds first.
        if remove_associated_sounds:
            dependent_sounds = [
                index for index, sound in enumerate(self._data.sound_entries)
                if sound.sound_type == SoundType.WAVE and sound.file_index == file_index
            ]
            for sound_index in dependent_sounds:
                self.require_safe_deletion("sound", sound_index)
            for sound_index in reversed(dependent_sounds):
                del self._data.sound_entries[sound_index]
                self.remap_provenance_after_delete("sound", sound_index)

        pos = file_entry.file_positions[0]
        group = self._data.group_entries[pos.group_index]
        if pos.index < 0 or pos.index >= len(group.group_table):
            return False
        sub = group.group_table[pos.index]

        # Remove embedded blobs if present.
        if sub.file_id is not None and sub.file_id in self._data.embedded_files:
            del self._data.embedded_files[sub.file_id]
        if sub.audio_file_id is not None and sub.audio_file_id in self._data.embedded_files:
            del self._data.embedded_files[sub.audio_file_id]

        # Remove group sub-entry.
        del group.group_table[pos.index]

        # Fix file_positions index references within that group.
        for fe in self._data.file_entries:
            for p in fe.file_positions:
                if p.group_index == pos.group_index and p.index > pos.index:
                    p.index -= 1

        # If group base pointers pointed to this removed file/audio, pick another or clear.
        if group.file_id == sub.file_id:
            group.file_id = group.group_table[0].file_id if group.group_table else None
        if group.audio_file_id == sub.audio_file_id:
            group.audio_file_id = group.group_table[0].audio_file_id if group.group_table else None

        # Keep file entry slot to avoid reindexing entire archive.
        file_entry.file_size = 0
        file_entry.wave_file_size = 0
        file_entry.external_file_path = None
        file_entry.file_positions.clear()
        self.unregister_new("file", file_index, recursive=True)

        self._rebuild_sound_trie()
        self.mark_dirty(DirtyFlags.DATA)
        return True

    def get_wav_samples(self, name: str) -> list:
        """
        Get only the BRWAV sample(s) actually used by a named SFX.

        For WAVE sounds: resolves BRWSD note 0, matching NW4R WsdPlayer.
        For SEQ sounds: resolves the sound's program in the BRBNK to find
        only the instrument regions (key splits) used, returning one BRWAV
        per region/variation.

        Returns a list of Brwav editors.
        """
        from pysar.core.format.rwar import Brwar
        from pysar.core.format.rwsd import Brwsd

        result = self.lookup_sound(name)
        if result is None:
            raise BrsarError(f'Sound "{name}" not found')

        _, _, info_idx = result
        entry = self._data.sound_entries[info_idx]

        if entry.sound_type == SoundType.WAVE:
            wave_info = entry.sound_info

            wsd_raw = self._resolve_file_raw(entry.file_index)
            if wsd_raw is None:
                raise BrsarError(f'Could not resolve BRWSD for sound "{name}"')

            war_raw = self._resolve_audio_raw_for_sound(entry)
            if war_raw is None:
                raise BrsarError(f'Could not resolve BRWAR for sound "{name}"')

            brwsd = Brwsd.from_bytes(wsd_raw)
            brwar = Brwar.from_bytes(war_raw)

            wsd_entry = brwsd[wave_info.wave_index]
            if not wsd_entry.notes:
                return []
            wave_index = int(wsd_entry.notes[0].wave_index)
            return [brwar[wave_index]] if 0 <= wave_index < len(brwar) else []

        elif entry.sound_type == SoundType.SEQ:
            seq_info = entry.sound_info

            brbnk = self.get_bank(seq_info.bank_index)
            war_raw = self._resolve_audio_raw_for_bank(seq_info.bank_index)
            if war_raw is None:
                raise BrsarError(f'Could not resolve BRWAR for sequence sound "{name}"')
            brwar = Brwar.from_bytes(war_raw)

            # Resolve which program this sound uses
            brseq = self.get_seq(entry.file_index)
            start_label, start_offset = self._resolve_seq_start(
                brseq, name, seq_info.seq_label_offset,
            )
            program = self._resolve_default_program(
                name, entry, brseq, start_label, start_offset,
            )
            if program is None:
                program = 0

            # Collect unique wave indices from the instrument's regions
            params = brbnk.get_all_inst_params(program)
            seen = set()
            samples = []
            for param in params:
                idx = param.wave_index
                if idx not in seen and 0 <= idx < len(brwar):
                    seen.add(idx)
                    samples.append(brwar[idx])
            return samples

        else:
            raise BrsarError(
                f'Sound "{name}" is type {entry.sound_type.name}, '
                f'sample extraction is only supported for WAVE and SEQ sounds'
            )

    def replace_wav_sound(self, name: str, brwav, *, note_index: int = 0) -> None:
        """Replace the WAV sample for a wave sound."""
        result = self.lookup_sound(name)
        if result is None:
            raise BrsarError(f'Sound "{name}" not found')

        _, _, info_idx = result
        entry = self._data.sound_entries[info_idx]

        if entry.sound_type != SoundType.WAVE:
            raise BrsarError(f'Sound "{name}" is not a WAVE sound')

        wave_info = entry.sound_info

        # Resolve the BRWSD and BRWAR
        wsd_raw = self._resolve_file_raw(entry.file_index)
        if wsd_raw is None:
            raise BrsarError(f'Could not resolve BRWSD for sound "{name}"')

        brwsd = Brwsd.from_bytes(wsd_raw)
        wsd_entry = brwsd[wave_info.wave_index]
        if not wsd_entry.notes:
            raise BrsarError(f'No notes found for wave sound "{name}"')
        if note_index < 0 or note_index >= len(wsd_entry.notes):
            raise BrsarError(
                f'note_index={note_index} out of range, sound "{name}" uses {len(wsd_entry.notes)} sample(s)'
            )
        wav_idx = wsd_entry.notes[note_index].wave_index

        # Get BRWAR via audio resolution
        war_raw = self._resolve_audio_raw_for_sound(entry)
        if war_raw is None:
            raise BrsarError(f'Could not resolve BRWAR for sound "{name}"')

        brwar = Brwar.from_bytes(war_raw)
        brwar[wav_idx] = brwav

        # Write back to embedded files
        self._update_audio_file_for_sound(entry, brwar.to_bytes())
        self._wave_war_cache.clear()
        self._war_cache.clear()
        self.mark_dirty(DirtyFlags.DATA)

    def replace_seq_sound(self, name: str, brwav, *, wav_no: int = 0) -> None:
        """Replace a WAV sample used by a sequence sound.

        wav_no selects which of the sound's WAVs to replace (0 = first),
        NOT a raw index into the BRWAR.
        """
        from pysar.core.format.rwar import Brwar

        result = self.lookup_sound(name)
        if result is None:
            raise BrsarError(f'Sound "{name}" not found')

        _, _, info_idx = result
        entry = self._data.sound_entries[info_idx]

        if entry.sound_type != SoundType.SEQ:
            raise BrsarError(f'Sound "{name}" is not a SEQ sound')

        seq_info = entry.sound_info
        war_raw = self._resolve_audio_raw_for_bank(seq_info.bank_index)
        if war_raw is None:
            raise BrsarError(f'Could not resolve BRWAR for sequence sound "{name}"')

        brwar = Brwar.from_bytes(war_raw)

        # Resolve the actual BRWAR indices used by this sound (same logic
        # as get_wav_samples for SEQ) so wav_no selects from the sound's
        # own wave list, not a raw BRWAR slot.
        brbnk = self.get_bank(seq_info.bank_index)
        brseq = self.get_seq(entry.file_index)
        start_label, start_offset = self._resolve_seq_start(
            brseq, name, seq_info.seq_label_offset,
        )
        program = self._resolve_default_program(
            name, entry, brseq, start_label, start_offset,
        )
        if isinstance(program, dict):
            programs = []
            for value in program.values():
                if value is None:
                    continue
                program_value = int(value)
                if program_value not in programs:
                    programs.append(program_value)
        elif program is None:
            programs = [0]
        else:
            programs = [int(program)]
        if not programs:
            programs = [0]

        from pysar.core.model.brbnk import WaveDataLocationType
        seen = set()
        wav_indices = []
        for program_value in programs:
            try:
                instrument = brbnk[program_value]
            except Exception:
                continue
            for param, _, _ in instrument.get_all_inst_params():
                if param.wave_data_location_type != WaveDataLocationType.INDEX:
                    continue
                idx = int(param.wave_index)
                if idx not in seen and 0 <= idx < len(brwar):
                    seen.add(idx)
                    wav_indices.append(idx)

        if not wav_indices:
            raise BrsarError(f'No WAV samples found for sequence sound "{name}"')
        if wav_no < 0 or wav_no >= len(wav_indices):
            raise BrsarError(
                f'wav_no={wav_no} out of range, sound "{name}" uses {len(wav_indices)} WAV(s)'
            )

        brwar[wav_indices[wav_no]] = brwav

        # Write back
        self._update_audio_file_for_bank(seq_info.bank_index, brwar.to_bytes())
        self._war_cache.clear()
        self._wave_war_cache.clear()
        self.mark_dirty(DirtyFlags.DATA)

    def patch_brstm(self, name: str, brstm, *, new_path: str | None = None) -> None:
        """Patch the BRSAR metadata required to play an external BRSTM."""
        result = self.lookup_sound(name)
        if result is None:
            raise BrsarError(f'Sound "{name}" not found')

        _, _, info_idx = result
        entry = self._data.sound_entries[info_idx]

        if entry.sound_type != SoundType.STRM:
            raise BrsarError(f'Sound "{name}" is not a STRM sound')

        file_entry = self._data.file_entries[entry.file_index]

        if new_path is not None:
            file_entry.external_file_path = new_path

        declared_size = 0
        if not brstm.is_dirty:
            declared_size = max(0, int(getattr(brstm.data, "file_size", 0) or 0))
        file_entry.file_size = declared_size or len(brstm.to_bytes())
        channel_count = max(1, int(brstm.n_channels))
        track_count = max(1, int(brstm.data.n_tracks))
        entry.sound_info.n_alloc_channels = channel_count
        entry.sound_info.alloc_track_flag = (1 << track_count) - 1
        self.mark_dirty(DirtyFlags.METADATA)

    #
    # Playback
    #

    def play_sound(
            self,
            name: str,
            loop_count: int = 1,
            sf2_path: str | Path | None = None,
            audio_driver: str | None = None,
            variation: int | None = None,
            max_ticks: int = 2_000,
    ) -> None:
        """
        Play a named sound through the native sequence runtime.

        Args:
            name: Sound name in the BRSAR.
            loop_count: How many times sequence loops should be unrolled.
            sf2_path: Kept for backward compatibility. Ignored by the new runtime.
            audio_driver: Kept for backward compatibility. Ignored by the new runtime.
            variation: Kept for backward compatibility. Ignored by the new runtime.
            max_ticks: Preview limit for SEQ playback to avoid hanging on long ambient loops.
        """
        del sf2_path, audio_driver, variation
        context = make_playback_context(self, name)
        engine = SoundArchiveEngine()
        engine.play(context, RenderOptions(loop_count=loop_count, max_ticks=max_ticks))

    def _resolve_seq_start(
            self,
            brseq,
            name: str,
            seq_label_offset: int,
    ) -> tuple[str | None, int | None]:
        """Resolve start_label/start_offset, preferring valid offsets but falling back to label."""
        start_label = None
        start_offset = None

        if seq_label_offset > 0 and self._has_command_offset(brseq, seq_label_offset):
            start_offset = seq_label_offset
            label = brseq.data.get_label_at_offset(seq_label_offset)
            if label is not None:
                start_label = label.name
        else:
            # Fallback to label-based start if the offset doesn't map to a command.
            if brseq.get_track(name) is not None or any(l.name == name for l in brseq.data.labels):
                start_label = name

        return start_label, start_offset

    @staticmethod
    def _has_command_offset(brseq, offset: int) -> bool:
        for track in brseq.data.tracks.values():
            for cmd in track.commands:
                if cmd.offset == offset:
                    return True
        return False

    def _resolve_default_program(
            self,
            name: str,
            entry: SoundDataEntry,
            brseq,
            start_label: str | None,
            start_offset: int | None,
    ) -> int | dict[int, int] | None:
        """
        Resolve default program(s) for a SEQ sound.

        Returns a dict mapping track_no -> program when multiple tracks
        have explicit PRG values, a single int when only the main track
        has one, or falls back to name-based inference.
        """
        track_programs = self._collect_programs_from_sequence(brseq, start_label, start_offset)
        if track_programs:
            if len(track_programs) == 1 and 0 in track_programs:
                return track_programs[0]
            return track_programs
        inferred = self._infer_program_for_sound(name, entry, brseq)
        if inferred is not None:
            return {0: inferred}
        return None


    @staticmethod
    def _collect_programs_from_sequence(
            brseq,
            start_label: str | None,
            start_offset: int | None,
    ) -> dict[int, int]:
        """
        Collect explicit PRG values from the start track and child tracks.

        Returns a dict mapping track_no -> first explicit program found
        in that track (or its CALL/JUMP targets).
        """
        from pysar.core.format.rseq.mml import MML

        data = brseq.data

        start_track = None
        if start_label is not None:
            start_track = data.tracks.get(start_label)
        if start_track is None and start_offset is not None:
            start_track = brseq.get_track_by_offset(start_offset)
            # Fallback: find the track that contains this offset
            # (seq_label_offset may point into the middle of a track,
            #  e.g. past alloc_track)
            if start_track is None:
                for track in data.tracks.values():
                    if track.start_offset <= start_offset < track.end_offset:
                        start_track = track
                        break
        if start_track is None:
            return {}

        # Map track_no -> first explicit program
        track_programs: dict[int, int] = {}

        def collect_prg_from_track(track, track_no: int) -> None:
            """DFS through a track and its CALL/JUMP targets to find PRG."""
            visited: set[int] = set()
            stack = [track]
            while stack:
                t = stack.pop()
                if t.start_offset in visited:
                    continue
                visited.add(t.start_offset)

                for cmd in t.commands:
                    if not cmd.is_extended and cmd.get_mml() == MML.PRG and not cmd.has_variable and cmd.args:
                        prg = cmd.args[0]
                        if isinstance(prg, int) and track_no not in track_programs:
                            track_programs[track_no] = prg
                            return  # First PRG found is enough

                # Follow CALL/JUMP targets, but skip OPEN_TRACK targets.
                for off in t.calls + t.jumps:
                    try:
                        stack.append(data[off])
                    except KeyError:
                        pass

        # Collect PRG from the main (start) track itself (track 0)
        collect_prg_from_track(start_track, 0)

        # Collect PRG from each child track opened by the start track
        visited_opens: set[int] = set()
        open_stack = [start_track]
        while open_stack:
            parent = open_stack.pop()
            if parent.start_offset in visited_opens:
                continue
            visited_opens.add(parent.start_offset)

            for track_no, off in parent.opens:
                try:
                    child_track = data[off]
                except KeyError:
                    continue
                collect_prg_from_track(child_track, track_no)

        return track_programs

    def export_sound_wav(
            self,
            name: str,
            output_path: str | Path,
            loop_count: int = 1,
            variation: int | None = None,
            sample_rate: int = 32000,
            encoding: str = "pcm16",
            max_ticks: int = 2_000,
    ) -> Path:
        """
        Render a named sound to a WAV file using the native sequence runtime.

        Args:
            name: Sound name in the BRSAR.
            output_path: Path for the output WAV file.
            loop_count: How many times loops repeat.
            variation: Kept for backward compatibility. Ignored by the new runtime.
            sample_rate: Target sample rate for the exported WAV.
            encoding: WAV PCM encoding. Supported: "pcm16", "pcm24", "pcm32".
            max_ticks: Sequence render limit for loop-heavy ambient previews and exports.

        Returns:
            Path to the created WAV file.
        """
        del variation
        context = make_playback_context(self, name)
        engine = SoundArchiveEngine()
        return engine.save_wav(
            context,
            output_path,
            RenderOptions(loop_count=loop_count, sample_rate=sample_rate, max_ticks=max_ticks),
            encoding=encoding,
        )

    def export_sound_hq_wav(
            self,
            name: str,
            output_path: str | Path,
            loop_count: int = 1,
            variation: int | None = None,
            sample_rate: int = 48000,
            encoding: str = "pcm24",
            max_ticks: int = 2_000,
    ) -> Path:
        """
        Render a named sound to a higher quality WAV file.

        Args:
            name: Sound name in the BRSAR.
            output_path: Path for the output WAV file.
            loop_count: How many times loops repeat.
            variation: Kept for backward compatibility. Ignored by the new runtime.
            sample_rate: Target sample rate for the exported WAV.
            encoding: WAV PCM encoding. Supported: "pcm24", "pcm32", "pcm16".
            max_ticks: Sequence render limit for loop-heavy ambient previews and exports.

        Returns:
            Path to the created WAV file.
        """
        return self.export_sound_wav(
            name,
            output_path,
            loop_count=loop_count,
            variation=variation,
            sample_rate=sample_rate,
            encoding=encoding,
            max_ticks=max_ticks,
        )

    def _infer_program_for_sound(
            self, name: str, entry: SoundDataEntry, brseq,
    ) -> int | None:
        """
        Infer the correct program number for a sound that uses variable-based PRG.

        Strategy: find sibling sounds that share the same BRSEQ file, bank, and
        name suffix (e.g. "_MAME"), then scan their sequence paths for explicit
        (non-variable) PRG commands.
        """
        from pysar.core.format.rseq.mml import MML

        seq_info: SeqSoundInfo = entry.sound_info

        parts = name.rsplit("_", 1)
        if len(parts) < 2:
            return None
        suffix = "_" + parts[1]

        player = brseq.get_player()

        for other_entry in self._data.sound_entries:
            if other_entry is entry:
                continue
            if other_entry.sound_type != SoundType.SEQ:
                continue
            if other_entry.file_index != entry.file_index:
                continue

            other_seq_info = other_entry.sound_info
            if not isinstance(other_seq_info, SeqSoundInfo):
                continue
            if other_seq_info.bank_index != seq_info.bank_index:
                continue

            other_name_idx = other_entry.file_name_index
            if other_name_idx < 0 or other_name_idx >= len(self._data.names):
                continue
            other_name = self._data.names[other_name_idx]
            if not other_name.endswith(suffix):
                continue

            other_offset = other_seq_info.seq_label_offset
            if other_offset <= 0:
                continue

            player.load(brseq._data, start_offset=other_offset)
            start_idx = player._ctx.tracks[0].flat_cmd_index

            for i in range(start_idx, min(start_idx + 20, len(player._flat_commands))):
                cmd = player._flat_commands[i]
                try:
                    mml = cmd.get_mml()
                except (ValueError, KeyError):
                    continue

                if not cmd.is_extended and mml == MML.PRG and not cmd.has_variable and cmd.args:
                    return cmd.args[0]

                if mml == MML.FIN or (isinstance(mml, int) and mml < 0x80):
                    break

        return None

    def export_sound(
            self,
            name: str,
            midi_path: str | Path,
            sf2_path: str | Path,
    ) -> tuple[Path, Path]:
        """
        Export a named SEQ sound as MIDI + SF2 files.

        Args:
            name: Sound name in the BRSAR.
            midi_path: Output path for the MIDI file.
            sf2_path: Output path for the SF2 soundfont.

        Returns:
            Tuple of (midi_path, sf2_path) as Path objects.
        """
        entry = self.get_sound_entry(name)
        if entry is None:
            raise BrsarError(f'Sound "{name}" not found')

        if entry.sound_type != SoundType.SEQ:
            raise BrsarError(
                f'Sound "{name}" is type {entry.sound_type.name}, '
                f'only SEQ export is supported'
            )

        seq_info: SeqSoundInfo = entry.sound_info

        brseq = self.get_seq(entry.file_index)
        brbnk = self.get_bank(seq_info.bank_index)
        brwar = self.get_bank_war(seq_info.bank_index)

        midi_path = Path(midi_path)
        sf2_path = Path(sf2_path)

        brseq.to_midi(midi_path)
        brbnk.export_sf2(sf2_path, brwar=brwar, bank_name=name)

        return midi_path, sf2_path

    #
    # audio resolution helpers
    #

    def _resolve_audio_raw_for_sound(self, entry: SoundDataEntry) -> bytes | None:
        """Resolve audio data for a sound entry."""
        return self._resolve_audio_raw(entry.file_index)

    def _resolve_audio_raw_for_bank(self, bank_index: int) -> bytes | None:
        """Resolve audio data for a bank entry."""
        if bank_index >= len(self._data.bank_entries):
            return None
        bank_entry = self._data.bank_entries[bank_index]
        return self._resolve_audio_raw(bank_entry.file_index)

    def _update_audio_file_for_sound(self, entry: SoundDataEntry, raw: bytes) -> None:
        """Write updated audio data back for a sound."""
        self._update_audio_file_for_sound_idx(entry.file_index, raw)

    def _update_audio_file_for_bank(self, bank_index: int, raw: bytes) -> None:
        """Write updated audio data back for a bank."""
        bank_entry = self._data.bank_entries[bank_index]
        self._update_audio_file_for_sound_idx(bank_entry.file_index, raw)

    def _update_audio_file_for_sound_idx(self, file_index: int, raw: bytes) -> None:
        """Write updated audio data back for a file index."""
        self._update_all_audio_copies(file_index, raw, expected_magic="RWAR")

    def _update_file_raw_for_sound_idx(self, file_index: int, raw: bytes) -> None:
        """Write updated file data (non-audio) back for a file index."""
        self._update_all_file_copies(file_index, raw, expected_magic="RWSD")

    def _update_all_file_copies(
            self,
            file_index: int,
            raw: bytes,
            *,
            expected_magic: str | None = None,
    ) -> set[int]:
        """Atomically update every physical copy of one logical FILE entry."""
        file_index = int(file_index)
        if not 0 <= file_index < len(self._data.file_entries):
            raise BrsarError(f"Invalid file index {file_index}")
        matching_subs = [
            sub
            for group in self._data.group_entries
            for sub in group.group_table
            if int(sub.group_index) == file_index
        ]
        if not matching_subs:
            raise BrsarError(f"Logical file {file_index} has no physical copies")
        if any(sub.file_id is None for sub in matching_subs):
            raise BrsarError(f"Logical file {file_index} has a missing physical data copy")
        file_ids = {
            int(sub.file_id)
            for sub in matching_subs
            if sub.file_id is not None
        }
        for file_id in file_ids:
            embedded = self._data.embedded_files.get(file_id)
            if embedded is None:
                raise BrsarError(f"Physical file {file_id} is missing")
            if expected_magic is not None and embedded.magic != expected_magic:
                raise BrsarError(
                    f"Physical file {file_id} is {embedded.magic or 'unknown'}, "
                    f"expected {expected_magic}"
                )
        payload = bytes(raw)
        for file_id in file_ids:
            self._data.embedded_files[file_id].raw_data = payload
        self._sync_file_group_metadata(file_index)
        return file_ids

    def _update_all_audio_copies(
            self,
            file_index: int,
            raw: bytes,
            *,
            expected_magic: str | None = None,
    ) -> set[int]:
        """Atomically update every companion-audio copy of a logical FILE."""
        file_index = int(file_index)
        if not 0 <= file_index < len(self._data.file_entries):
            raise BrsarError(f"Invalid file index {file_index}")
        matching_subs = [
            sub
            for group in self._data.group_entries
            for sub in group.group_table
            if int(sub.group_index) == file_index
        ]
        if not matching_subs:
            raise BrsarError(f"Logical file {file_index} has no physical copies")
        if any(sub.audio_file_id is None for sub in matching_subs):
            raise BrsarError(f"Logical file {file_index} has a missing physical audio copy")
        audio_ids = {
            int(sub.audio_file_id)
            for sub in matching_subs
            if sub.audio_file_id is not None
        }
        for audio_id in audio_ids:
            embedded = self._data.embedded_files.get(audio_id)
            if embedded is None:
                raise BrsarError(f"Physical audio file {audio_id} is missing")
            if expected_magic is not None and embedded.magic != expected_magic:
                raise BrsarError(
                    f"Physical audio file {audio_id} is {embedded.magic or 'unknown'}, "
                    f"expected {expected_magic}"
                )
        payload = bytes(raw)
        for audio_id in audio_ids:
            self._data.embedded_files[audio_id].raw_data = payload
        self._sync_file_group_metadata(file_index)
        return audio_ids

    def _sync_file_group_metadata(self, file_index: int) -> None:
        if file_index < 0 or file_index >= len(self._data.file_entries):
            return

        matches: list[tuple[int, int, GroupTableEntry]] = []
        for group_index, group in enumerate(self._data.group_entries):
            for sub_index, sub in enumerate(group.group_table):
                if int(sub.group_index) == int(file_index):
                    matches.append((group_index, sub_index, sub))

        if not matches:
            return

        file_entry = self._data.file_entries[file_index]
        file_sub = next((sub for _, _, sub in matches if sub.file_id is not None), matches[0][2])
        audio_sub = next((sub for _, _, sub in matches if sub.audio_file_id is not None), matches[0][2])
        file_size = None
        wave_file_size = None
        if file_sub.file_id is not None and file_sub.file_id in self._data.embedded_files:
            file_size = len(self._data.embedded_files[file_sub.file_id].raw_data)
            file_entry.file_size = file_size
        if audio_sub.audio_file_id is not None and audio_sub.audio_file_id in self._data.embedded_files:
            wave_file_size = len(self._data.embedded_files[audio_sub.audio_file_id].raw_data)
            file_entry.wave_file_size = wave_file_size

        file_positions: list[FilePositionEntry] = []
        seen_positions: set[tuple[int, int]] = set()
        for group_index, sub_index, sub in matches:
            if file_size is not None:
                sub.file_data_size = file_size
            if wave_file_size is not None:
                sub.audio_data_size = wave_file_size
            key = (group_index, sub_index)
            if key not in seen_positions:
                seen_positions.add(key)
                file_positions.append(FilePositionEntry(group_index=group_index, index=sub_index))

        if file_positions:
            file_entry.file_positions = file_positions
        self._refresh_group_size_fields()

    def _refresh_group_size_fields(self) -> None:
        file_lookup: dict[int, int] = {}
        cursor = 32
        for file_id in ordered_embedded_file_ids(self._data):
            file_lookup[file_id] = cursor
            cursor += len(self._data.embedded_files[file_id].raw_data)

        for group in self._data.group_entries:
            if group.group_table:
                # Group offsets are bases for unsigned relative offsets. Always
                # choose the lowest remaining physical FILE offset on each side
                # so deletion/reordering can never produce a negative delta.
                file_ids = {
                    int(sub.file_id)
                    for sub in group.group_table
                    if sub.file_id is not None and sub.file_id in file_lookup
                }
                audio_ids = {
                    int(sub.audio_file_id)
                    for sub in group.group_table
                    if sub.audio_file_id is not None and sub.audio_file_id in file_lookup
                }
                group.file_id = min(file_ids, key=file_lookup.__getitem__) if file_ids else None
                group.audio_file_id = min(audio_ids, key=file_lookup.__getitem__) if audio_ids else None
            else:
                group.file_id = None
                group.audio_file_id = None

            group_file_offset = file_lookup.get(group.file_id, 0) if group.file_id is not None else 0
            group_audio_offset = file_lookup.get(group.audio_file_id, 0) if group.audio_file_id is not None else 0
            group.group_file_offset = group_file_offset
            group.group_audio_offset = group_audio_offset
            group.group_file_size = 0
            group.group_audio_size = 0
            for sub in group.group_table:
                if sub.file_id is not None and sub.file_id in self._data.embedded_files:
                    sub.file_data_offset = max(0, file_lookup[sub.file_id] - group_file_offset)
                    sub.file_data_size = len(self._data.embedded_files[sub.file_id].raw_data)
                else:
                    sub.file_data_offset = 0
                    sub.file_data_size = 0
                if sub.audio_file_id is not None and sub.audio_file_id in self._data.embedded_files:
                    sub.audio_data_offset = max(0, file_lookup[sub.audio_file_id] - group_audio_offset)
                    sub.audio_data_size = len(self._data.embedded_files[sub.audio_file_id].raw_data)
                else:
                    sub.audio_data_offset = 0
                    sub.audio_data_size = 0
                group.group_file_size = max(
                    group.group_file_size,
                    int(sub.file_data_offset) + int(sub.file_data_size),
                )
                group.group_audio_size = max(
                    group.group_audio_size,
                    int(sub.audio_data_offset) + int(sub.audio_data_size),
                )

    @staticmethod
    def _sanitize_name(value: str, *, fallback: str = "unnamed") -> str:
        cleaned = re.sub(r'[^A-Za-z0-9._-]+', '_', value.strip())
        cleaned = cleaned.strip("._-")
        cleaned = cleaned[:120].rstrip("._-")
        safe_fallback = str(fallback or "unnamed")[:120].strip("._-") or "unnamed"
        return cleaned or safe_fallback

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _resolve_sound_name(self, sound_index: int, entry: SoundDataEntry) -> str:
        if 0 <= entry.file_name_index < len(self._data.names):
            return self._data.names[entry.file_name_index]
        return f"SOUND_{sound_index:05d}"

    def _resolve_group_index_for_file(self, file_index: int) -> int | None:
        if file_index < 0 or file_index >= len(self._data.file_entries):
            return None
        file_entry = self._data.file_entries[file_index]
        if not file_entry.file_positions:
            return None
        return file_entry.file_positions[0].group_index

    def dump_archive(
            self,
            output_dir: str | Path,
            *,
            include_raw: bool = False,
            decode_assets: bool = True,
            decode_sounds: bool = True,
            include_sound_metadata: bool = True,
            include_variations: bool = False,
            include_raw_samples: bool = False,
            decode_wave_archives: bool = False,
            include_streams: bool = False,
            loop_count: int = 1,
            seq_max_ticks: int = 250_000,
            overwrite: bool = False,
            progress: bool = True,
            external_root: str | Path | None = None,
            external_resolver: Callable[[str | None], Path | None] | None = None,
            progress_callback: Callable[[str, bool], None] | None = None,
            cancel_callback: Callable[[], bool] | None = None,
    ) -> Path:
        """
        Dump the BRSAR into a practical, low-redundancy structure.

        Layout:
            manifest.json
            raw_embedded/                  (optional)
            banks/<bank_name>/bank.sf2     (optional)
            wave_archives/RWAR_<id>/*.wav  (optional)
            sounds/<sound_name>/...wav       (when include_sound_metadata)
        """
        from pysar.core.format.rbnk import Brbnk
        from pysar.core.format.rstm import Brstm
        from pysar.seq.renderer import SequenceRenderer
        import numpy as np
        import wave

        def check_cancelled() -> None:
            if cancel_callback is not None and cancel_callback():
                raise ArchiveDumpCancelled("Archive dump cancelled")

        def write_bytes_cancellable(path: Path, payload: bytes) -> None:
            """Write large embedded payloads without making Abort unresponsive."""
            view = memoryview(payload)
            with path.open("wb") as output:
                for offset in range(0, len(view), 1024 * 1024):
                    check_cancelled()
                    output.write(view[offset:offset + 1024 * 1024])
            check_cancelled()

        # A dump must reflect in-memory editor changes even when a parsed
        # subfile has not yet been serialized by Save.
        check_cancelled()
        self._flush_caches()
        check_cancelled()

        root = Path(output_dir)
        raw_dir = root / "raw_embedded"
        banks_dir = root / "banks"
        wave_archives_dir = root / "wave_archives"
        sounds_dir = root / "sounds"

        root.mkdir(parents=True, exist_ok=True)
        if include_sound_metadata:
            sounds_dir.mkdir(parents=True, exist_ok=True)
        if include_raw:
            raw_dir.mkdir(parents=True, exist_ok=True)
        if decode_assets:
            banks_dir.mkdir(parents=True, exist_ok=True)
        if decode_wave_archives:
            wave_archives_dir.mkdir(parents=True, exist_ok=True)

        manifest: dict[str, Any] = {
            "version": f"{self._data.version >> 8}.{self._data.version & 0xFF}",
            "options": {
                "include_raw": include_raw,
                "decode_assets": decode_assets,
                "decode_sounds": decode_sounds,
                "include_sound_metadata": include_sound_metadata,
                "include_variations": include_variations,
                "include_raw_samples": include_raw_samples,
                "decode_wave_archives": decode_wave_archives,
                "include_streams": include_streams,
                "loop_count": loop_count,
                "seq_max_ticks": seq_max_ticks,
                "overwrite": overwrite,
                "external_root": str(external_root) if external_root is not None else None,
                "external_fallback": external_resolver is not None,
            },
            "counts": {
                "sounds": len(self._data.sound_entries),
                "banks": len(self._data.bank_entries),
                "groups": len(self._data.group_entries),
                "files": len(self._data.file_entries),
                "embedded_files": len(self._data.embedded_files),
            },
            "banks": [],
            "wave_archives": [],
            "raw_files": [],
            "sounds": [],
            "errors": [],
        }

        def add_error(scope: str, message: str) -> None:
            manifest["errors"].append({"scope": scope, "message": message})

        def report_progress(message: str, completed: bool = False) -> None:
            """Publish optional UI progress without affecting a dump on failure."""
            if progress_callback is None:
                return
            try:
                progress_callback(message, completed)
            except Exception:
                pass

        def save_sequence_wav(context, path: Path) -> bool:
            """Stream one sequence to PCM16 WAV and report safety truncation."""
            settings = RenderOptions(loop_count=loop_count, max_ticks=seq_max_ticks)
            renderer = SequenceRenderer()
            partial_path = path.with_name(f".{path.name}.partial")
            try:
                with wave.open(str(partial_path), "wb") as output:
                    output.setnchannels(2)
                    output.setsampwidth(2)
                    output.setframerate(settings.sample_rate)
                    for block in renderer.stream(context, settings):
                        check_cancelled()
                        pcm = np.round(np.clip(block, -1.0, 1.0) * 32767.0).astype("<i2")
                        output.writeframesraw(pcm.tobytes(order="C"))
                    check_cancelled()
                check_cancelled()
                partial_path.replace(path)
            except Exception:
                partial_path.unlink(missing_ok=True)
                raise
            return bool(renderer.last_sequence_truncated)

        def save_brstm_wav(brstm, path: Path) -> None:
            """Stream BRSTM blocks so long streams remain cooperatively cancellable."""
            if cancel_callback is None:
                brstm.decode_to_wav(path)
                return
            partial_path = path.with_name(f".{path.name}.partial")
            try:
                with wave.open(str(partial_path), "wb") as output:
                    output.setnchannels(max(1, int(brstm.data.n_channels)))
                    output.setsampwidth(2)
                    output.setframerate(max(1, int(brstm.sample_rate)))
                    for block in brstm.iter_decoded_blocks():
                        check_cancelled()
                        output.writeframesraw(np.asarray(block, dtype="<i2").tobytes(order="C"))
                    check_cancelled()
                check_cancelled()
                partial_path.replace(path)
            except Exception:
                partial_path.unlink(missing_ok=True)
                raise

        if include_raw:
            raw_extensions = {
                "RBNK": "brbnk",
                "RSEQ": "brseq",
                "RSTM": "brstm",
                "RWAR": "brwar",
                "RWAV": "brwav",
                "RWSD": "brwsd",
            }
            for file_id in sorted(self._data.embedded_files.keys()):
                check_cancelled()
                emb = self._data.embedded_files[file_id]
                magic = emb.magic or "bin"
                ext = raw_extensions.get(magic, self._sanitize_name(magic.lower(), fallback="bin"))
                filename = f"file_{file_id:06d}_{self._sanitize_name(magic, fallback='bin')}.{ext}"
                report_progress(f"Writing original file {filename}")
                raw_path = raw_dir / filename
                write_bytes_cancellable(raw_path, emb.raw_data)
                manifest["raw_files"].append(str(raw_path.relative_to(root)))
                report_progress(f"Wrote original file {filename}", completed=True)

        bank_cache: dict[int, dict[str, Any]] = {}
        variation_count_cache: dict[tuple[int, int], int] = {}
        variation_sf2_cache: dict[tuple[int, int, int], Path] = {}
        war_dumped_keys: set[str] = set()

        if decode_assets:
            for bank_idx, bank_entry in enumerate(self._data.bank_entries):
                check_cancelled()
                bank_name = (
                    self._data.names[bank_entry.file_name_index]
                    if 0 <= bank_entry.file_name_index < len(self._data.names)
                    else f"BANK_{bank_idx:04d}"
                )
                safe_bank_name = self._sanitize_name(bank_name, fallback=f"BANK_{bank_idx:04d}")
                bank_dir = banks_dir / f"{safe_bank_name}__{bank_idx:04d}"
                bank_dir.mkdir(parents=True, exist_ok=True)
                report_progress(f"Converting bank {bank_idx + 1}/{len(self._data.bank_entries)}: {bank_name}")

                bank_meta: dict[str, Any] = {
                    "bank_index": bank_idx,
                    "name": bank_name,
                    "file_index": bank_entry.file_index,
                    "outputs": [],
                    "errors": [],
                }
                try:
                    bank_raw = self._resolve_file_raw(bank_entry.file_index)
                    war_raw = self._resolve_audio_raw(bank_entry.file_index)
                    if bank_raw is None:
                        bank_meta["errors"].append("Missing BRBNK file data")
                    if war_raw is None:
                        bank_meta["errors"].append("Missing BRWAR audio data")
                    if bank_raw is None or war_raw is None:
                        self._write_json(bank_dir / "bank.json", bank_meta)
                        manifest["banks"].append(bank_meta)
                        for err in bank_meta["errors"]:
                            add_error(f"bank:{bank_idx}:{bank_name}", err)
                        report_progress(
                            f"Converted bank {bank_idx + 1}/{len(self._data.bank_entries)}: {bank_name}",
                            completed=True,
                        )
                        continue

                    brbnk = Brbnk.from_bytes(bank_raw)
                    brwar = Brwar.from_bytes(war_raw)
                    check_cancelled()

                    sf2_path = bank_dir / "bank.sf2"
                    brbnk.export_sf2(
                        sf2_path,
                        brwar=brwar,
                        bank_name=bank_name,
                        cancel_callback=cancel_callback,
                    )
                    check_cancelled()
                    bank_meta["outputs"].append(str(sf2_path.relative_to(root)))

                    if include_raw_samples:
                        key = f"file:{bank_entry.file_index}"
                        if key not in war_dumped_keys:
                            samples_dir = bank_dir / "war_samples"
                            samples_dir.mkdir(exist_ok=True)
                            for wav_idx in range(len(brwar)):
                                check_cancelled()
                                try:
                                    wav_path = samples_dir / f"sample_{wav_idx:03d}.wav"
                                    brwar[wav_idx].decode_to_wav(wav_path)
                                    check_cancelled()
                                except ArchiveDumpCancelled:
                                    raise
                                except Exception as ex:
                                    bank_meta["errors"].append(f"sample {wav_idx}: {ex}")
                            war_dumped_keys.add(key)

                    bank_cache[bank_idx] = {
                        "name": bank_name,
                        "sf2_path": sf2_path,
                        "bank_dir": bank_dir,
                        "brbnk": brbnk,
                        "brwar": brwar,
                    }
                except ArchiveDumpCancelled:
                    raise
                except Exception as ex:
                    bank_meta["errors"].append(str(ex))

                self._write_json(bank_dir / "bank.json", bank_meta)
                manifest["banks"].append(bank_meta)
                for err in bank_meta["errors"]:
                    add_error(f"bank:{bank_idx}:{bank_name}", err)
                report_progress(f"Converted bank {bank_idx + 1}/{len(self._data.bank_entries)}: {bank_name}", completed=True)

        if decode_wave_archives:
            # A logical FILE can be copied into several groups. Decode each
            # unique RWAR payload once, while retaining metadata for every
            # physical embedded id so no unlinked archive disappears.
            canonical_by_payload: dict[bytes, tuple[int, list[str], int, list[str]]] = {}
            for file_id in sorted(self._data.embedded_files):
                check_cancelled()
                embedded = self._data.embedded_files[file_id]
                if embedded.magic != "RWAR":
                    continue

                archive_dir = wave_archives_dir / f"RWAR_{file_id:06d}"
                archive_dir.mkdir(parents=True, exist_ok=True)
                report_progress(f"Converting wave archive {file_id}")
                wave_meta: dict[str, Any] = {
                    "file_id": file_id,
                    "size": len(embedded.raw_data),
                    "wave_count": 0,
                    "outputs": [],
                    "errors": [],
                }
                canonical = canonical_by_payload.get(embedded.raw_data)
                if canonical is not None:
                    canonical_id, canonical_outputs, wave_count, canonical_errors = canonical
                    wave_meta["wave_count"] = wave_count
                    wave_meta["duplicate_of"] = canonical_id
                    wave_meta["outputs"] = canonical_outputs
                    wave_meta["errors"] = canonical_errors
                else:
                    try:
                        brwar = Brwar.from_bytes(embedded.raw_data)
                        check_cancelled()
                        wave_meta["wave_count"] = len(brwar)
                        for wave_index in range(len(brwar)):
                            check_cancelled()
                            try:
                                wave_path = archive_dir / f"wave_{wave_index:04d}.wav"
                                brwar[wave_index].decode_to_wav(wave_path)
                                check_cancelled()
                                wave_meta["outputs"].append(str(wave_path.relative_to(root)))
                            except ArchiveDumpCancelled:
                                raise
                            except Exception as ex:
                                wave_meta["errors"].append(f"wave {wave_index}: {ex}")
                        canonical_by_payload[embedded.raw_data] = (
                            file_id,
                            list(wave_meta["outputs"]),
                            int(wave_meta["wave_count"]),
                            list(wave_meta["errors"]),
                        )
                    except ArchiveDumpCancelled:
                        raise
                    except Exception as ex:
                        wave_meta["errors"].append(str(ex))

                self._write_json(archive_dir / "wave_archive.json", wave_meta)
                manifest["wave_archives"].append(wave_meta)
                for err in wave_meta["errors"]:
                    add_error(f"wave_archive:{file_id}", err)
                report_progress(f"Converted wave archive {file_id}", completed=True)

        wave_file_cache: dict[int, tuple[Brwsd, Brwar]] = {}
        total_sounds = len(self._data.sound_entries)
        sound_entries = enumerate(self._data.sound_entries) if include_sound_metadata else ()
        for sound_idx, entry in sound_entries:
            check_cancelled()
            sound_name = self._resolve_sound_name(sound_idx, entry)
            safe_sound_name = self._sanitize_name(sound_name, fallback=f"SOUND_{sound_idx:05d}")
            sound_dirname = f"{safe_sound_name}__{sound_idx:05d}"
            if progress:
                print(f"[dump] {sound_idx + 1}/{total_sounds} {entry.sound_type.name} {sound_name}")

            group_index = self._resolve_group_index_for_file(entry.file_index)
            group_name = None
            if group_index is not None and 0 <= group_index < len(self._data.group_entries):
                g = self._data.group_entries[group_index]
                if 0 <= g.file_name_index < len(self._data.names):
                    group_name = self._data.names[g.file_name_index]

            sound_dir = sounds_dir / sound_dirname
            audio_dir = sound_dir / "audio"
            audio_dir.mkdir(parents=True, exist_ok=True)

            sound_meta: dict[str, Any] = {
                "sound_index": sound_idx,
                "name": sound_name,
                "safe_name": safe_sound_name,
                "sound_type": entry.sound_type.name,
                "file_index": entry.file_index,
                "player_index": entry.player_index,
                "group_index": group_index,
                "group_name": group_name,
                "errors": [],
                "outputs": [],
            }

            if decode_sounds:
                try:
                    if entry.sound_type == SoundType.SEQ:
                        seq_info: SeqSoundInfo = entry.sound_info
                        brseq = self.get_seq(entry.file_index)
                        start_label, start_offset = self._resolve_seq_start(brseq, sound_name, seq_info.seq_label_offset)
                        default_program = self._resolve_default_program(
                            sound_name,
                            entry,
                            brseq,
                            start_label,
                            start_offset,
                        )
                        prog = default_program if default_program is not None else 0

                        sound_meta["seq"] = {
                            "seq_label_offset": seq_info.seq_label_offset,
                            "bank_index": seq_info.bank_index,
                            "default_program": default_program,
                            "start_label": start_label,
                            "start_offset": start_offset,
                        }

                        seq_context = make_playback_context(self, sound_name)

                        bank_ctx = bank_cache.get(seq_info.bank_index)
                        if bank_ctx is not None:
                            sound_meta["bank_sf2"] = str(bank_ctx["sf2_path"].relative_to(root))

                        if include_variations:
                            prog_int = prog if isinstance(prog, int) else prog.get(0, 0)
                            vc_key = (seq_info.bank_index, prog_int)
                            if vc_key not in variation_count_cache:
                                variation_count_cache[vc_key] = len(seq_context.brbnk.get_variation_notes(prog_int))
                            variation_count = variation_count_cache[vc_key]

                            if variation_count > 0:
                                variation_notes = seq_context.brbnk.get_variation_notes(prog_int)
                                for variation in range(variation_count):
                                    out_path = audio_dir / f"{safe_sound_name}__var_{variation:03d}.wav"
                                    if out_path.exists() and not overwrite:
                                        sound_meta["outputs"].append(str(out_path.relative_to(root)))
                                        continue
                                    variation_context = copy.copy(seq_context)
                                    variation_context.extras = {
                                        **seq_context.extras,
                                        "note_override": variation_notes[variation],
                                    }
                                    if save_sequence_wav(variation_context, out_path):
                                        sound_meta["truncated"] = True
                                        sound_meta["errors"].append(
                                            f"Variation {variation} reached seq_max_ticks="
                                            f"{seq_max_ticks}; decoded WAV is truncated"
                                        )
                                    sound_meta["outputs"].append(str(out_path.relative_to(root)))
                            else:
                                out_path = audio_dir / f"{safe_sound_name}.wav"
                                if out_path.exists() and not overwrite:
                                    sound_meta["outputs"].append(str(out_path.relative_to(root)))
                                    self._write_json(sound_dir / "sound.json", sound_meta)
                                    manifest["sounds"].append({
                                        "sound_index": sound_idx,
                                        "name": sound_name,
                                        "group_index": group_index,
                                        "sound_type": entry.sound_type.name,
                                        "path": str(sound_dir.relative_to(root)),
                                        "n_outputs": len(sound_meta["outputs"]),
                                        "n_errors": len(sound_meta["errors"]),
                                    })
                                    continue
                                if save_sequence_wav(seq_context, out_path):
                                    sound_meta["truncated"] = True
                                    sound_meta["errors"].append(
                                        f"Sequence reached seq_max_ticks={seq_max_ticks}; "
                                        "decoded WAV is truncated"
                                    )
                                sound_meta["outputs"].append(str(out_path.relative_to(root)))
                        else:
                            out_path = audio_dir / f"{safe_sound_name}.wav"
                            if out_path.exists() and not overwrite:
                                sound_meta["outputs"].append(str(out_path.relative_to(root)))
                                self._write_json(sound_dir / "sound.json", sound_meta)
                                manifest["sounds"].append({
                                    "sound_index": sound_idx,
                                    "name": sound_name,
                                    "group_index": group_index,
                                    "sound_type": entry.sound_type.name,
                                    "path": str(sound_dir.relative_to(root)),
                                    "n_outputs": len(sound_meta["outputs"]),
                                    "n_errors": len(sound_meta["errors"]),
                                })
                                continue
                            if save_sequence_wav(seq_context, out_path):
                                sound_meta["truncated"] = True
                                sound_meta["errors"].append(
                                    f"Sequence reached seq_max_ticks={seq_max_ticks}; "
                                    "decoded WAV is truncated"
                                )
                            sound_meta["outputs"].append(str(out_path.relative_to(root)))

                    elif entry.sound_type == SoundType.STRM:
                        if include_streams:
                            strm_raw = self._resolve_file_raw(entry.file_index)
                            if strm_raw is not None:
                                out_path = audio_dir / f"{safe_sound_name}.wav"
                                if out_path.exists() and not overwrite:
                                    sound_meta["outputs"].append(str(out_path.relative_to(root)))
                                    self._write_json(sound_dir / "sound.json", sound_meta)
                                    manifest["sounds"].append({
                                        "sound_index": sound_idx,
                                        "name": sound_name,
                                        "group_index": group_index,
                                        "sound_type": entry.sound_type.name,
                                        "path": str(sound_dir.relative_to(root)),
                                        "n_outputs": len(sound_meta["outputs"]),
                                        "n_errors": len(sound_meta["errors"]),
                                    })
                                    continue
                                save_brstm_wav(Brstm.from_bytes(strm_raw), out_path)
                                sound_meta["outputs"].append(str(out_path.relative_to(root)))
                            else:
                                file_entry = self._data.file_entries[entry.file_index]
                                sound_meta["external_file_path"] = file_entry.external_file_path
                                if file_entry.external_file_path:
                                    if external_resolver is not None:
                                        ext_path = external_resolver(file_entry.external_file_path)
                                        if ext_path is not None:
                                            ext_path = Path(ext_path).expanduser()
                                            sound_meta["resolved_external_file_path"] = str(ext_path)
                                    else:
                                        ext_path = Path(
                                            str(file_entry.external_file_path).replace("\\", "/")
                                        ).expanduser()
                                        if not ext_path.is_absolute() and external_root is None:
                                            sound_meta["errors"].append(
                                                "Cannot resolve a relative external BRSTM without the BRSAR folder"
                                            )
                                        else:
                                            if not ext_path.is_absolute():
                                                ext_path = Path(external_root).expanduser() / ext_path
                                            sound_meta["resolved_external_file_path"] = str(ext_path)
                                    if "resolved_external_file_path" in sound_meta and ext_path is not None and ext_path.is_file():
                                        out_path = audio_dir / f"{safe_sound_name}.wav"
                                        save_brstm_wav(Brstm.open(ext_path), out_path)
                                        sound_meta["outputs"].append(str(out_path.relative_to(root)))
                                    elif not sound_meta["errors"]:
                                        sound_meta["errors"].append(
                                            f"External BRSTM not found: {file_entry.external_file_path}"
                                        )
                                else:
                                    sound_meta["errors"].append("No embedded or external BRSTM data available")
                        else:
                            sound_meta["notes"] = ["STRM decode skipped (include_streams=False)"]

                    elif entry.sound_type == SoundType.WAVE:
                        wave_info = entry.sound_info
                        if entry.file_index not in wave_file_cache:
                            wsd_raw = self._resolve_file_raw(entry.file_index)
                            war_raw = self._resolve_audio_raw(entry.file_index)
                            if wsd_raw is None or war_raw is None:
                                sound_meta["errors"].append("Could not resolve BRWSD/BRWAR for wave sound")
                                raise BrsarError("Missing BRWSD/BRWAR")
                            wave_file_cache[entry.file_index] = (Brwsd.from_bytes(wsd_raw), Brwar.from_bytes(war_raw))

                        brwsd, brwar = wave_file_cache[entry.file_index]
                        if wave_info.wave_index >= len(brwsd):
                            sound_meta["errors"].append(f"wave_index out of range: {wave_info.wave_index}")
                        else:
                            wsd_entry = brwsd[wave_info.wave_index]

                            if wsd_entry.notes:
                                wav_idx = wsd_entry.notes[0].wave_index
                                out_path = audio_dir / f"{safe_sound_name}.wav"
                                if out_path.exists() and not overwrite:
                                    sound_meta["outputs"].append(str(out_path.relative_to(root)))
                                    self._write_json(sound_dir / "sound.json", sound_meta)
                                    manifest["sounds"].append({
                                        "sound_index": sound_idx,
                                        "name": sound_name,
                                        "group_index": group_index,
                                        "sound_type": entry.sound_type.name,
                                        "path": str(sound_dir.relative_to(root)),
                                        "n_outputs": len(sound_meta["outputs"]),
                                        "n_errors": len(sound_meta["errors"]),
                                    })
                                    continue
                                try:
                                    brwar[wav_idx].decode_to_wav(out_path)
                                    check_cancelled()
                                    sound_meta["outputs"].append(str(out_path.relative_to(root)))
                                except ArchiveDumpCancelled:
                                    raise
                                except Exception as ex:
                                    sound_meta["errors"].append(f"note 0 (wave {wav_idx}): {ex}")
                            else:
                                sound_meta["errors"].append("Wave sound has no note 0")
                    else:
                        sound_meta["errors"].append(f"Unsupported sound type: {entry.sound_type}")
                except ArchiveDumpCancelled:
                    raise
                except Exception as ex:
                    sound_meta["errors"].append(str(ex))

            self._write_json(sound_dir / "sound.json", sound_meta)
            manifest["sounds"].append({
                "sound_index": sound_idx,
                "name": sound_name,
                "group_index": group_index,
                "sound_type": entry.sound_type.name,
                "path": str(sound_dir.relative_to(root)),
                "n_outputs": len(sound_meta["outputs"]),
                "n_errors": len(sound_meta["errors"]),
            })

            for err in sound_meta["errors"]:
                add_error(f"sound:{sound_idx}:{sound_name}", err)
            report_progress(
                f"Prepared sound {sound_idx + 1}/{total_sounds}: {sound_name}",
                completed=True,
            )

        check_cancelled()
        self._write_json(root / "manifest.json", manifest)
        check_cancelled()
        return root

    #
    # Serialization
    #

    def _flush_caches(self) -> None:
        """Re-serialize any modified cached subfiles back to embedded files."""
        for bank_idx, brbnk in self._bank_cache.items():
            if hasattr(brbnk, 'is_dirty') and brbnk.is_dirty:
                bank_entry = self._data.bank_entries[bank_idx]
                self._update_all_file_copies(
                    bank_entry.file_index,
                    brbnk.to_bytes(),
                    expected_magic="RBNK",
                )

        for file_idx, brseq in self._seq_cache.items():
            if hasattr(brseq, 'is_dirty') and brseq.is_dirty:
                self._update_all_file_copies(
                    file_idx, brseq.to_bytes(), expected_magic="RSEQ",
                )

        for bank_idx, brwar in self._war_cache.items():
            if hasattr(brwar, 'is_dirty') and brwar.is_dirty:
                self._update_audio_file_for_bank(bank_idx, brwar.to_bytes())

        for file_idx in range(len(self._data.file_entries)):
            self._sync_file_group_metadata(file_idx)

    def to_bytes(self) -> bytes:
        """Serialize to BRSAR binary."""
        self._flush_caches()
        writer = BrsarWriter()
        return writer.to_bytes(self._data)

    def save(self, path: str | Path) -> Self:
        """Save to a BRSAR file."""
        Path(path).write_bytes(self.to_bytes())
        self.clear_dirty()
        return self

    #
    # Info / Debug
    #

    def get_rsar_info(self) -> dict[str, int | str]:
        """Get general archive information."""
        n_seq = sum(1 for e in self._data.sound_entries if e.sound_type == SoundType.SEQ)
        n_wav = sum(1 for e in self._data.sound_entries if e.sound_type == SoundType.WAVE)
        n_strm = sum(1 for e in self._data.sound_entries if e.sound_type == SoundType.STRM)

        return {
            'version': f'{self._data.version >> 8}.{self._data.version & 0xFF}',
            'n_snd': len(self._data.sound_entries),
            'n_seq': n_seq,
            'n_wav': n_wav,
            'n_strm': n_strm,
            'n_banks': len(self._data.bank_entries),
            'n_players': len(self._data.player_entries),
            'n_groups': len(self._data.group_entries),
            'n_files': len(self._data.file_entries),
            'n_embedded': len(self._data.embedded_files),
            'max_seq': self._data.arc_common_info.n_seq_sounds,
            'max_seq_trk': self._data.arc_common_info.n_seq_tracks,
            'max_strm': self._data.arc_common_info.n_stream_sounds,
            'max_strm_chn': self._data.arc_common_info.n_stream_channels,
            'max_strm_trk': self._data.arc_common_info.n_stream_tracks,
            'max_wav': self._data.arc_common_info.n_wave_sounds,
        }

    def summary(self) -> str:
        """Get a human-readable summary."""
        info = self.get_rsar_info()
        lines = [
            f"BRSAR Sound Archive",
            f"  Version: {info['version']}",
            f"  Sounds: {info['n_snd']} (SEQ={info['n_seq']}, WAV={info['n_wav']}, STRM={info['n_strm']})",
            f"  Banks: {info['n_banks']}",
            f"  Players: {info['n_players']}",
            f"  Groups: {info['n_groups']}",
            f"  Embedded Files: {info['n_embedded']}",
        ]
        return "\n".join(lines)

    def __str__(self) -> str:
        return f"Brsar(v{self._data.version >> 8}.{self._data.version & 0xFF}, {self.n_sounds} sounds)"

    def __repr__(self) -> str:
        return self.__str__()

    def __len__(self) -> int:
        return self.n_sounds
