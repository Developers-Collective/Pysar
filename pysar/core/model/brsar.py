from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


PROVENANCE_ARITIES: dict[str, int] = {
    "sound": 1,
    "bank": 1,
    "player": 1,
    "group": 1,
    "file": 1,
    "bank_instrument": 2,  # bank index, program
    "bank_zone": 3,        # bank index, program, flattened zone index
    "wave": 2,             # logical FILE index, RWAR wave index
    "wsd_entry": 2,        # logical FILE index, RWSD data index
    "group_item": 2,       # group index, logical FILE index
}


@dataclass
class ArchiveProvenance:
    """IDs created by PySAR and therefore safe to structurally edit.

    An empty instance deliberately means "everything is original".  This is
    the safe fallback for vanilla archives and for files whose optional PySAR
    trailer was removed or damaged by another editor.
    """

    entities: dict[str, set[tuple[int, ...]]] = field(
        default_factory=lambda: {kind: set() for kind in PROVENANCE_ARITIES}
    )
    status: str = "absent"  # absent | valid | invalid

    def entries(self, kind: str) -> set[tuple[int, ...]]:
        if kind not in PROVENANCE_ARITIES:
            raise ValueError(f"Unsupported provenance kind: {kind}")
        return self.entities.setdefault(kind, set())

    @property
    def is_empty(self) -> bool:
        return not any(self.entities.values())


class SoundType(IntEnum):
    """Sound type discriminator for SoundDataEntry."""
    INVALID = 0
    SEQ = 1
    STRM = 2
    WAVE = 3


@dataclass
class Sound3DParam:
    flags: int = 0
    decay_curve: int = 1
    decay_ratio: int = 128
    doppler_factor: int = 0


@dataclass
class SeqSoundInfo:
    seq_label_offset: int = 0
    bank_index: int = 0
    alloc_track: int = 0
    channel_priority: int = 0
    release_priority_fix: int = 0


@dataclass
class StreamSoundInfo:
    start_position: int = 0
    n_alloc_channels: int = 0
    alloc_track_flag: int = 0


@dataclass
class WaveSoundInfo:
    wave_index: int = 0
    alloc_track: int = 1
    channel_priority: int = 64
    release_priority_fix: int = 0


@dataclass
class SoundDataEntry:
    file_name_index: int = -1
    file_index: int = -1
    player_index: int = 0
    volume: int = 90
    player_priority: int = 64
    sound_type: SoundType = SoundType.INVALID
    remote_filter: int = 0
    user_param1: int = 0
    user_param2: int = 0
    pan_mode: int = 0
    pan_curve: int = 0
    actor_player_id: int = 0
    sound_3d: Sound3DParam = field(default_factory=Sound3DParam)
    sound_info: SeqSoundInfo | StreamSoundInfo | WaveSoundInfo | None = None


@dataclass
class SoundBankEntry:
    file_name_index: int = 0
    file_index: int = 0
    bank_index: int = 0


@dataclass
class PlayerInfoEntry:
    file_name_index: int = 0
    n_playable_sounds: int = 0
    heap_size: int = 0


@dataclass
class FilePositionEntry:
    group_index: int = 0
    index: int = 0


@dataclass
class FileEntry:
    file_size: int = 0
    wave_file_size: int = 0
    entry_num: int = -1
    external_file_path: str | None = None
    file_positions: list[FilePositionEntry] = field(default_factory=list)


@dataclass
class GroupTableEntry:
    group_index: int = 0
    file_data_offset: int = 0
    file_data_size: int = 0
    audio_data_offset: int = 0
    audio_data_size: int = 0
    file_id: int | None = None
    audio_file_id: int | None = None


@dataclass
class GroupDataEntry:
    file_name_index: int = -1
    entry_num: int = -1
    external_file_path: str | None = None
    group_file_offset: int = 0
    group_file_size: int = 0
    group_audio_offset: int = 0
    group_audio_size: int = 0
    group_table: list[GroupTableEntry] = field(default_factory=list)
    file_id: int | None = None
    audio_file_id: int | None = None


@dataclass
class ArcCommonInfo:
    n_seq_sounds: int = 0
    n_seq_tracks: int = 0
    n_stream_sounds: int = 0
    n_stream_tracks: int = 0
    n_stream_channels: int = 0
    n_wave_sounds: int = 0
    n_wave_tracks: int = 0


@dataclass
class EmbeddedFile:
    """Raw bytes of an embedded subfile in the FILE block."""
    file_id: int = 0
    offset: int = 0
    raw_data: bytes = b''
    magic: str = ''


@dataclass
class BrsarData:
    version: int = 0x0104

    # SYMB block
    names: list[str] = field(default_factory=list)
    snd_trie: object = None
    player_trie: object = None
    group_trie: object = None
    bank_trie: object = None
    snd_trie_raw: bytes | None = None
    player_trie_raw: bytes | None = None
    group_trie_raw: bytes | None = None
    bank_trie_raw: bytes | None = None

    # INFO block
    sound_entries: list[SoundDataEntry] = field(default_factory=list)
    bank_entries: list[SoundBankEntry] = field(default_factory=list)
    player_entries: list[PlayerInfoEntry] = field(default_factory=list)
    file_entries: list[FileEntry] = field(default_factory=list)
    group_entries: list[GroupDataEntry] = field(default_factory=list)
    arc_common_info: ArcCommonInfo = field(default_factory=ArcCommonInfo)

    # FILE block (raw bytes per embedded file)
    embedded_files: dict[int, EmbeddedFile] = field(default_factory=dict)
    offset_to_id: dict[int, int] = field(default_factory=dict)
    id_to_offset: dict[int, int] = field(default_factory=dict)

    # Optional PySAR-only provenance. This is not an NW4R section and is
    # ignored by the Wii runtime because it follows the declared RSAR bytes.
    provenance: ArchiveProvenance = field(default_factory=ArchiveProvenance)
