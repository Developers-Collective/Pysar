from pysar.core.format.rsar import Brsar
from pysar.core.model.brsar import SeqSoundInfo, SoundDataEntry, SoundType, StreamSoundInfo, WaveSoundInfo

from pysar.services.errors import InvalidSelectionError, NoProjectOpenError
from pysar.services.models import (
    ArchiveSummary,
    BankDetails,
    BankInstrument,
    BankListItem,
    BankZone,
    FileListItem,
    FileReferrer,
    GroupListItem,
    PlayerListItem,
    ProjectSession,
    SoundDetails,
    SoundListItem,
    WaveArchiveDetails,
    WaveArchiveListItem,
    WaveListItem,
)


class ArchiveService:
    def get_summary(self, session: ProjectSession) -> ArchiveSummary:
        archive = self._require_archive(session)
        info = archive.get_rsar_info()
        return ArchiveSummary(
            version=str(info["version"]),
            sound_count=int(info["n_snd"]),
            seq_count=int(info["n_seq"]),
            wave_count=int(info["n_wav"]),
            stream_count=int(info["n_strm"]),
            bank_count=int(info["n_banks"]),
            player_count=int(info["n_players"]),
            group_count=int(info["n_groups"]),
            embedded_file_count=int(info["n_embedded"]),
        )

    def get_summary_text(self, session: ProjectSession) -> str:
        archive = self._require_archive(session)
        return archive.summary()

    def list_sounds(self, session: ProjectSession) -> list[SoundListItem]:
        archive = self._require_archive(session)
        items: list[SoundListItem] = []
        for sound_id, entry in enumerate(archive.data.sound_entries):
            data_fid, audio_fid = self._resolve_file_index(archive, entry.file_index)
            items.append(
                SoundListItem(
                    sound_id=sound_id,
                    name=self._sound_name(archive, sound_id, entry),
                    sound_type=entry.sound_type.name,
                    file_index=entry.file_index,
                    player_index=entry.player_index,
                    volume=entry.volume,
                    group_name=self._group_name_for_file_index(archive, entry.file_index),
                    bank_index=self._bank_index_from_entry(entry),
                    data_file_id=data_fid,
                    audio_file_id=audio_fid,
                )
            )
        return items

    def get_sound_details(self, session: ProjectSession, sound_id: int) -> SoundDetails:
        archive = self._require_archive(session)
        entry = self._sound_entry(archive, sound_id)
        sound_info = entry.sound_info
        return SoundDetails(
            sound_id=sound_id,
            name=self._sound_name(archive, sound_id, entry),
            sound_type=entry.sound_type.name,
            file_index=entry.file_index,
            player_index=entry.player_index,
            volume=entry.volume,
            player_priority=entry.player_priority,
            actor_player_id=entry.actor_player_id,
            pan_mode=entry.pan_mode,
            pan_curve=entry.pan_curve,
            group_name=self._group_name_for_file_index(archive, entry.file_index),
            bank_index=sound_info.bank_index if isinstance(sound_info, SeqSoundInfo) else None,
            seq_label_offset=sound_info.seq_label_offset if isinstance(sound_info, SeqSoundInfo) else None,
            wave_index=sound_info.wave_index if isinstance(sound_info, WaveSoundInfo) else None,
            alloc_track=sound_info.alloc_track if isinstance(sound_info, (SeqSoundInfo, WaveSoundInfo)) else None,
            start_position=sound_info.start_position if isinstance(sound_info, StreamSoundInfo) else None,
            n_alloc_channels=sound_info.n_alloc_channels if isinstance(sound_info, StreamSoundInfo) else None,
            alloc_track_flag=sound_info.alloc_track_flag if isinstance(sound_info, StreamSoundInfo) else None,
        )

    def list_banks(self, session: ProjectSession) -> list[BankListItem]:
        archive = self._require_archive(session)
        items: list[BankListItem] = []
        for bank_index, entry in enumerate(archive.data.bank_entries):
            data_fid, audio_fid = self._resolve_file_index(archive, entry.file_index)
            instrument_count, wave_count = self._brbnk_counts(archive, data_fid)
            items.append(
                BankListItem(
                    bank_index=bank_index,
                    name=self._name_at(archive, entry.file_name_index, fallback=f"BANK_{bank_index:04d}"),
                    file_index=entry.file_index,
                    data_file_id=data_fid,
                    audio_file_id=audio_fid,
                    instrument_count=instrument_count,
                    wave_count=wave_count,
                )
            )
        return items

    def list_groups(self, session: ProjectSession) -> list[GroupListItem]:
        archive = self._require_archive(session)
        items: list[GroupListItem] = []
        for group_index, entry in enumerate(archive.data.group_entries):
            file_size = int(entry.group_file_size or 0)
            audio_size = int(entry.group_audio_size or 0)
            if file_size == 0:
                file_size = sum(int(sub.file_data_size or 0) for sub in entry.group_table)
            if audio_size == 0:
                audio_size = sum(int(sub.audio_data_size or 0) for sub in entry.group_table)
            items.append(
                GroupListItem(
                    group_index=group_index,
                    name=self._name_at(archive, entry.file_name_index, fallback=f"GROUP_{group_index:04d}"),
                    item_count=len(entry.group_table),
                    file_id=entry.file_id,
                    audio_file_id=entry.audio_file_id,
                    file_size=file_size,
                    audio_size=audio_size,
                )
            )
        return items

    def list_players(self, session: ProjectSession) -> list[PlayerListItem]:
        archive = self._require_archive(session)
        items: list[PlayerListItem] = []
        for player_index, entry in enumerate(archive.data.player_entries):
            items.append(
                PlayerListItem(
                    player_index=player_index,
                    name=self._name_at(archive, entry.file_name_index, fallback=f"PLAYER_{player_index:04d}"),
                    playable_sound_count=entry.n_playable_sounds,
                    heap_size=entry.heap_size,
                )
            )
        return items

    def get_bank_details(self, session: ProjectSession, bank_index: int) -> BankDetails:
        archive = self._require_archive(session)
        if bank_index < 0 or bank_index >= len(archive.data.bank_entries):
            raise InvalidSelectionError(f"bank index {bank_index} is out of range")
        bank_entry = archive.data.bank_entries[bank_index]
        bank_name = self._name_at(
            archive, bank_entry.file_name_index, fallback=f"BANK_{bank_index:04d}"
        )
        data_fid, audio_fid = self._resolve_file_index(archive, bank_entry.file_index)

        embedded = (
            archive.data.embedded_files.get(data_fid) if data_fid is not None else None
        )
        if embedded is None or embedded.magic != "RBNK":
            return BankDetails(
                bank_index=bank_index,
                name=bank_name,
                instrument_count=0,
                active_instrument_count=0,
                wave_count=0,
                audio_file_id=audio_fid,
                instruments=[],
            )

        from pysar.core.format.rbnk import Brbnk

        brbnk = Brbnk.from_bytes(embedded.raw_data)
        instruments: list[BankInstrument] = []
        for program, inst in enumerate(brbnk.instruments):
            instruments.append(_build_bank_instrument(program, inst))

        return BankDetails(
            bank_index=bank_index,
            name=bank_name,
            instrument_count=brbnk.n_instruments,
            active_instrument_count=brbnk.n_active_instruments,
            wave_count=len(brbnk.get_wave_indices()),
            audio_file_id=audio_fid,
            instruments=instruments,
        )

    def get_wave_archive_details(
        self, session: ProjectSession, file_id: int
    ) -> WaveArchiveDetails:
        archive = self._require_archive(session)
        embedded = archive.data.embedded_files.get(int(file_id))
        if embedded is None or embedded.magic != "RWAR":
            raise InvalidSelectionError(f"file_id {file_id} is not an RWAR")

        from pysar.core.format.rwar import Brwar
        brwar = Brwar.from_bytes(embedded.raw_data)

        waves: list[WaveListItem] = []
        for index, entry in enumerate(brwar.data.entries):
            wi = entry.brwav.wave_info
            sample_rate = max(1, int(wi.actual_sample_rate))
            n_samples = int(wi.get_n_samples_actual())
            loop_start = int(wi.get_loop_start_samples())
            duration_ms = int(round(n_samples * 1000 / sample_rate)) if n_samples else 0
            encoding = wi.encoding.name if hasattr(wi.encoding, "name") else str(wi.encoding)
            waves.append(
                WaveListItem(
                    index=index,
                    encoding=encoding,
                    sample_rate=sample_rate,
                    n_channels=int(wi.n_channels),
                    n_samples=n_samples,
                    loop_start=loop_start,
                    is_looped=bool(wi.is_looped),
                    size_bytes=int(entry.original_size or 0),
                    duration_ms=duration_ms,
                )
            )

        return WaveArchiveDetails(
            file_id=int(file_id),
            name=f"WAR_{int(file_id):04d}",
            size=len(embedded.raw_data),
            wave_count=len(waves),
            waves=waves,
        )

    def list_wave_archives(self, session: ProjectSession) -> list[WaveArchiveListItem]:
        archive = self._require_archive(session)
        bank_links = self._wave_archive_bank_links(archive)
        items: list[WaveArchiveListItem] = []
        for file_id in sorted(archive.data.embedded_files.keys()):
            embedded = archive.data.embedded_files[file_id]
            if embedded.magic != "RWAR":
                continue
            items.append(
                WaveArchiveListItem(
                    file_id=file_id,
                    name=f"WAR_{file_id:04d}",
                    size=len(embedded.raw_data),
                    wave_count=_count_brwar_entries(embedded.raw_data),
                    linked_banks=bank_links.get(file_id, []),
                )
            )
        return items

    def list_files(self, session: ProjectSession) -> list[FileListItem]:
        archive = self._require_archive(session)
        referrers = self._file_id_referrers(archive)
        rbnk_names = self._rbnk_names(archive)
        items: list[FileListItem] = []
        for file_id in sorted(archive.data.embedded_files.keys()):
            embedded = archive.data.embedded_files[file_id]
            kind = (embedded.magic or "").strip() or "BIN"
            if kind == "RBNK" and file_id in rbnk_names:
                label = rbnk_names[file_id]
            else:
                label = f"{kind}_{file_id:04d}"
            items.append(
                FileListItem(
                    file_id=file_id,
                    kind=kind,
                    size=len(embedded.raw_data),
                    label=label,
                    linked=referrers.get(file_id, []),
                )
            )
        for file_index, entry in enumerate(archive.data.file_entries):
            if not entry.external_file_path:
                continue
            items.append(
                FileListItem(
                    file_id=-1,
                    kind="EXT",
                    size=int(entry.file_size or 0),
                    label=entry.external_file_path,
                    linked=[],
                    is_external=True,
                    file_index=file_index,
                    external_path=entry.external_file_path,
                )
            )
        return items

    @staticmethod
    def _require_archive(session: ProjectSession) -> Brsar:
        if session.archive is None:
            raise NoProjectOpenError("no archive is open")
        archive: Brsar = session.archive
        return archive

    @staticmethod
    def _sound_entry(archive, sound_id: int) -> SoundDataEntry:
        if sound_id < 0 or sound_id >= len(archive.data.sound_entries):
            raise InvalidSelectionError(f"sound id {sound_id} is out of range")
        return archive.data.sound_entries[sound_id]

    @staticmethod
    def _name_at(archive, name_index: int, *, fallback: str) -> str:
        if 0 <= name_index < len(archive.data.names):
            return archive.data.names[name_index]
        return fallback

    def _sound_name(self, archive, sound_id: int, entry: SoundDataEntry) -> str:
        return self._name_at(archive, entry.file_name_index, fallback=f"SOUND_{sound_id:05d}")

    def _group_name_for_file_index(self, archive, file_index: int) -> str | None:
        if file_index < 0 or file_index >= len(archive.data.file_entries):
            return None
        file_entry = archive.data.file_entries[file_index]
        if not file_entry.file_positions:
            return None
        group_index = file_entry.file_positions[0].group_index
        if group_index < 0 or group_index >= len(archive.data.group_entries):
            return None
        group = archive.data.group_entries[group_index]
        return self._name_at(archive, group.file_name_index, fallback=f"GROUP_{group_index:04d}")

    @staticmethod
    def _bank_index_from_entry(entry: SoundDataEntry) -> int | None:
        if entry.sound_type == SoundType.SEQ and isinstance(entry.sound_info, SeqSoundInfo):
            return entry.sound_info.bank_index
        return None

    def _resolve_file_index(self, archive, file_index: int) -> tuple[int | None, int | None]:
        if file_index < 0 or file_index >= len(archive.data.file_entries):
            return None, None
        entry = archive.data.file_entries[file_index]
        if not entry.file_positions:
            return None, None
        position = entry.file_positions[0]
        if position.group_index < 0 or position.group_index >= len(archive.data.group_entries):
            return None, None
        group = archive.data.group_entries[position.group_index]
        if position.index < 0 or position.index >= len(group.group_table):
            return None, None
        slot = group.group_table[position.index]
        return slot.file_id, slot.audio_file_id

    def _brbnk_counts(self, archive, data_file_id: int | None) -> tuple[int, int]:
        if data_file_id is None:
            return 0, 0
        embedded = archive.data.embedded_files.get(data_file_id)
        if embedded is None or embedded.magic != "RBNK":
            return 0, 0
        from pysar.core.format.rbnk import Brbnk
        try:
            brbnk = Brbnk.from_bytes(embedded.raw_data)
        except Exception:
            return 0, 0
        return brbnk.n_active_instruments, len(brbnk.get_wave_indices())

    def _rbnk_names(self, archive) -> dict[int, str]:
        names: dict[int, str] = {}
        for bank_index, bank in enumerate(archive.data.bank_entries):
            data_fid, _ = self._resolve_file_index(archive, bank.file_index)
            if data_fid is None:
                continue
            names[data_fid] = self._name_at(
                archive,
                bank.file_name_index,
                fallback=f"BANK_{bank_index:04d}",
            )
        return names

    def _wave_archive_bank_links(self, archive) -> dict[int, list[str]]:
        links: dict[int, list[str]] = {}
        for bank_index, bank in enumerate(archive.data.bank_entries):
            _, audio_file_id = self._resolve_file_index(archive, bank.file_index)
            if audio_file_id is None:
                continue
            name = self._name_at(archive, bank.file_name_index, fallback=f"BANK_{bank_index:04d}")
            links.setdefault(audio_file_id, []).append(name)
        return links

    def _file_id_referrers(self, archive) -> dict[int, list[FileReferrer]]:
        referrers: dict[int, list[FileReferrer]] = {}
        seen: dict[int, set[tuple[str, int]]] = {}

        def add(fid: int | None, kind: str, item_id: int, name: str) -> None:
            if fid is None:
                return
            key = (kind, item_id)
            slot = seen.setdefault(fid, set())
            if key in slot:
                return
            slot.add(key)
            referrers.setdefault(fid, []).append(FileReferrer(kind=kind, id=item_id, name=name))

        for sound_id, entry in enumerate(archive.data.sound_entries):
            data_fid, audio_fid = self._resolve_file_index(archive, entry.file_index)
            sound_name = self._sound_name(archive, sound_id, entry)
            add(data_fid, "sound", sound_id, sound_name)
            add(audio_fid, "sound", sound_id, sound_name)

        for bank_index, bank in enumerate(archive.data.bank_entries):
            data_fid, audio_fid = self._resolve_file_index(archive, bank.file_index)
            bank_name = self._name_at(archive, bank.file_name_index, fallback=f"BANK_{bank_index:04d}")
            add(data_fid, "bank", bank_index, bank_name)
            add(audio_fid, "bank", bank_index, bank_name)

        return referrers


def _build_bank_instrument(program: int, inst) -> BankInstrument:
    if inst.is_empty():
        return BankInstrument(
            program=program,
            name=inst.name or "",
            zone_count=0,
            wave_indices=[],
            key_low=0,
            key_high=0,
            is_empty=True,
            zones=[],
        )

    zones: list[BankZone] = []
    waves: set[int] = set()
    key_low, key_high = 127, 0
    for param, key_range, vel_range in inst.get_all_inst_params():
        klo, khi = key_range if key_range is not None else (0, 127)
        vlo, vhi = vel_range if vel_range is not None else (0, 127)
        zones.append(
            BankZone(
                wave_index=int(param.wave_index),
                key_low=int(klo),
                key_high=int(khi),
                velocity_low=int(vlo),
                velocity_high=int(vhi),
                original_key=int(param.original_key),
                volume=int(param.volume),
                pan=int(param.pan),
                pitch=float(param.pitch),
                attack=int(param.attack),
                decay=int(param.decay),
                sustain=int(param.sustain),
                release=int(param.release),
                hold=int(param.hold),
                note_off_type=int(param.note_off_type),
                alternate_assign=int(param.alternate_assign),
            )
        )
        waves.add(int(param.wave_index))
        key_low = min(key_low, int(klo))
        key_high = max(key_high, int(khi))

    if not zones:
        return BankInstrument(
            program=program,
            name=inst.name or "",
            zone_count=0,
            wave_indices=[],
            key_low=0,
            key_high=0,
            is_empty=True,
            zones=[],
        )

    return BankInstrument(
        program=program,
        name=inst.name or "",
        zone_count=len(zones),
        wave_indices=sorted(waves),
        key_low=key_low,
        key_high=key_high,
        is_empty=False,
        zones=zones,
    )


def _count_brwar_entries(raw: bytes) -> int:
    if len(raw) < 0x20 or raw[:4] != b"RWAR":
        return 0
    try:
        tabl_offset = int.from_bytes(raw[0x10:0x14], "big")
    except (ValueError, IndexError):
        return 0
    if tabl_offset <= 0 or tabl_offset + 12 > len(raw):
        return 0
    if raw[tabl_offset:tabl_offset + 4] != b"TABL":
        return 0
    return int.from_bytes(raw[tabl_offset + 8:tabl_offset + 12], "big")
