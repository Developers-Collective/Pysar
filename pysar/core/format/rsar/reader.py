import io
import struct
from typing import BinaryIO

from pysar.core.base import ReaderBase
from pysar.core.types import FileTag
from pysar.core.model.brsar import (
    BrsarData,
    SoundType,
    Sound3DParam,
    SeqSoundInfo,
    StreamSoundInfo,
    WaveSoundInfo,
    SoundDataEntry,
    SoundBankEntry,
    PlayerInfoEntry,
    FilePositionEntry,
    FileEntry,
    GroupTableEntry,
    GroupDataEntry,
    ArcCommonInfo,
    EmbeddedFile,
)
from pysar.core.format.rsar.string_trie import StringTrie
from pysar.core.format.rsar.provenance import read_trailer
from pysar.io.binary import (
    read_file_header,
    read_section_header,
    read_reference,
    read_offset_from_ref,
    read_terminated_string,
)


class BrsarReader(ReaderBase):
    EXPECTED_MAGIC = FileTag.BRSAR
    SUPPORTED_VERSIONS = {0x0104}

    def read(self, data: BinaryIO) -> BrsarData:
        """Read a complete BRSAR file."""
        base_offset = data.tell()

        # File header (16 bytes)
        header = read_file_header(data)
        self._validate(header)

        # Section table: 3 pairs of (offset, size)
        symb_off, symb_size, info_off, info_size, file_off, file_size = (
            struct.unpack('>IIIIII', data.read(24))
        )

        # Read FILE block first to build offset->id maps
        embedded_files, offset_to_id, id_to_offset = self._read_file_block(
            data, base_offset + file_off,
        )

        # Read SYMB block
        (
            names,
            snd_trie, ply_trie, grp_trie, bnk_trie,
            snd_trie_raw, ply_trie_raw, grp_trie_raw, bnk_trie_raw,
        ) = self._read_symb_block(
            data, base_offset + symb_off,
        )

        # Read INFO block
        (sound_entries, bank_entries, player_entries,
         file_entries, group_entries, arc_info) = self._read_info_block(
            data, base_offset + info_off, offset_to_id,
        )

        self._recover_info_referenced_files(
            data,
            base_offset=base_offset,
            file_block_offset=base_offset + file_off,
            file_block_size=file_size,
            group_entries=group_entries,
            embedded_files=embedded_files,
            offset_to_id=offset_to_id,
            id_to_offset=id_to_offset,
        )
        self._reconcile_group_file_ids(
            group_entries,
            offset_to_id,
            base_offset=base_offset,
        )

        provenance = read_trailer(
            data,
            base_offset=base_offset,
            declared_size=header.file_size,
        )

        return BrsarData(
            version=header.version,
            names=names,
            snd_trie=snd_trie,
            player_trie=ply_trie,
            group_trie=grp_trie,
            bank_trie=bnk_trie,
            snd_trie_raw=snd_trie_raw,
            player_trie_raw=ply_trie_raw,
            group_trie_raw=grp_trie_raw,
            bank_trie_raw=bnk_trie_raw,
            sound_entries=sound_entries,
            bank_entries=bank_entries,
            player_entries=player_entries,
            file_entries=file_entries,
            group_entries=group_entries,
            arc_common_info=arc_info,
            embedded_files=embedded_files,
            offset_to_id=offset_to_id,
            id_to_offset=id_to_offset,
            provenance=provenance,
        )

    # ------------------------------------------------------------------
    # FILE block
    # ------------------------------------------------------------------

    def _read_file_block(
        self,
        data: BinaryIO,
        block_offset: int,
    ) -> tuple[dict[int, EmbeddedFile], dict[int, int], dict[int, int]]:
        """Read the FILE block and capture each embedded file as raw bytes."""
        data.seek(block_offset)
        section = read_section_header(data)

        # Skip 24 bytes of padding after the section header
        data.read(24)

        embedded_files: dict[int, EmbeddedFile] = {}
        offset_to_id: dict[int, int] = {}
        id_to_offset: dict[int, int] = {}
        file_id = 0

        end_offset = block_offset + section.size

        while data.tell() < end_offset:
            origin = data.tell()

            # Peek at magic to identify file type
            peek = data.read(4)
            if len(peek) < 4:
                break

            # Remaining bytes are padding.
            if peek == b'\x00\x00\x00\x00':
                break

            magic = peek.decode('ascii', errors='replace')
            data.seek(origin)

            # Read the NW4R file header to get file_size
            if origin + 16 > end_offset:
                break
            file_header = read_file_header(data)
            data.seek(origin)

            # Guard against malformed headers in padding/noise.
            if file_header.file_size <= 0:
                break
            if origin + file_header.file_size > end_offset:
                break

            # Capture the entire file as raw bytes
            raw = data.read(file_header.file_size)

            ef = EmbeddedFile(
                file_id=file_id,
                offset=origin,
                raw_data=raw,
                magic=magic,
            )
            embedded_files[file_id] = ef
            offset_to_id[origin] = file_id
            id_to_offset[file_id] = origin
            file_id += 1

            # Skip past any null padding between files
            while data.tell() < end_offset:
                b = data.read(1)
                if not b or b != b'\x00':
                    if b:
                        data.seek(-1, io.SEEK_CUR)
                    break

        return embedded_files, offset_to_id, id_to_offset

    @staticmethod
    def _info_referenced_allocations(
        group_entries: list[GroupDataEntry],
        *,
        base_offset: int,
    ) -> dict[int, int]:
        allocations: dict[int, int] = {}

        def add(relative_offset: int, size: int) -> None:
            relative_offset = int(relative_offset)
            size = int(size)
            if relative_offset <= 0 or size <= 0:
                return
            absolute_offset = int(base_offset) + relative_offset
            previous = allocations.get(absolute_offset)
            if previous is None or size < previous:
                allocations[absolute_offset] = size

        for group in group_entries:
            # Preserve groups without a usable subtable as well as the common
            # case where these duplicate the first data/audio sub-entry.
            add(group.group_file_offset, group.group_file_size)
            add(group.group_audio_offset, group.group_audio_size)
            for entry in group.group_table:
                if entry.file_data_size > 0:
                    add(
                        group.group_file_offset + entry.file_data_offset,
                        entry.file_data_size,
                    )
                if entry.audio_data_size > 0:
                    add(
                        group.group_audio_offset + entry.audio_data_offset,
                        entry.audio_data_size,
                    )
        return allocations

    def _recover_info_referenced_files(
        self,
        data: BinaryIO,
        *,
        base_offset: int,
        file_block_offset: int,
        file_block_size: int,
        group_entries: list[GroupDataEntry],
        embedded_files: dict[int, EmbeddedFile],
        offset_to_id: dict[int, int],
        id_to_offset: dict[int, int],
    ) -> None:
        section_start = int(file_block_offset)
        section_end = section_start + max(0, int(file_block_size))
        allocations = self._info_referenced_allocations(
            group_entries,
            base_offset=base_offset,
        )
        next_file_id = max(embedded_files, default=-1) + 1

        for origin, allocation_size in sorted(allocations.items()):
            if origin in offset_to_id:
                continue
            if origin < section_start or origin + 16 > section_end:
                continue
            try:
                data.seek(origin)
                header = read_file_header(data)
            except (OSError, UnicodeDecodeError, struct.error):
                continue
            declared_size = int(header.file_size)
            if declared_size < 16 or declared_size > allocation_size:
                continue
            if origin + declared_size > section_end:
                continue
            if int(header.byte_order) not in (0xFEFF, 0xFFFE):
                continue

            data.seek(origin)
            raw = data.read(declared_size)
            if len(raw) != declared_size:
                continue
            embedded_files[next_file_id] = EmbeddedFile(
                file_id=next_file_id,
                offset=origin,
                raw_data=raw,
                magic=header.magic,
            )
            offset_to_id[origin] = next_file_id
            id_to_offset[next_file_id] = origin
            next_file_id += 1

    @staticmethod
    def _reconcile_group_file_ids(
        group_entries: list[GroupDataEntry],
        offset_to_id: dict[int, int],
        *,
        base_offset: int,
    ) -> None:
        base_offset = int(base_offset)
        for group in group_entries:
            group.file_id = (
                offset_to_id.get(base_offset + int(group.group_file_offset))
                if group.group_file_offset
                else None
            )
            group.audio_file_id = (
                offset_to_id.get(base_offset + int(group.group_audio_offset))
                if group.group_audio_offset
                else None
            )
            for entry in group.group_table:
                entry.file_id = (
                    offset_to_id.get(
                        base_offset
                        + int(group.group_file_offset)
                        + int(entry.file_data_offset)
                    )
                    if group.group_file_offset and entry.file_data_size > 0
                    else None
                )
                entry.audio_file_id = (
                    offset_to_id.get(
                        base_offset
                        + int(group.group_audio_offset)
                        + int(entry.audio_data_offset)
                    )
                    if group.group_audio_offset and entry.audio_data_size > 0
                    else None
                )

    # ------------------------------------------------------------------
    # SYMB block
    # ------------------------------------------------------------------

    def _read_symb_block(
        self,
        data: BinaryIO,
        block_offset: int,
    ) -> tuple[
        list[str], StringTrie, StringTrie, StringTrie, StringTrie,
        bytes, bytes, bytes, bytes,
    ]:
        """Read the SYMB block: name table and four PATRICIA tries."""
        data.seek(block_offset)
        section = read_section_header(data)

        # 5 offsets: name_table, snd_trie, ply_trie, grp_trie, bnk_trie
        tbl_off, snd_off, ply_off, grp_off, bnk_off = struct.unpack('>IIIII', data.read(20))

        # All offsets are relative to block_offset + 8 (after section header)
        base = block_offset + 8

        # Read name table
        data.seek(base + tbl_off)
        n_names = struct.unpack('>I', data.read(4))[0]

        # Skip the offset table (n_names * u32)
        data.seek(n_names * 4, io.SEEK_CUR)

        names = [read_terminated_string(data) for _ in range(n_names)]

        # Capture raw trie byte ranges exactly as found in file.
        symb_end = block_offset + section.size
        trie_starts = [snd_off, ply_off, grp_off, bnk_off]
        raw_tries: list[bytes] = []
        for i, start in enumerate(trie_starts):
            start_abs = base + start
            if i + 1 < len(trie_starts):
                end_abs = base + trie_starts[i + 1]
            else:
                end_abs = symb_end
            data.seek(start_abs)
            raw_tries.append(data.read(max(0, end_abs - start_abs)))

        snd_trie_raw, ply_trie_raw, grp_trie_raw, bnk_trie_raw = raw_tries

        # Parse tries from the captured raw bytes.
        snd_trie = StringTrie(io.BytesIO(snd_trie_raw), names)
        ply_trie = StringTrie(io.BytesIO(ply_trie_raw), names)
        grp_trie = StringTrie(io.BytesIO(grp_trie_raw), names)
        bnk_trie = StringTrie(io.BytesIO(bnk_trie_raw), names)

        return (
            names,
            snd_trie, ply_trie, grp_trie, bnk_trie,
            snd_trie_raw, ply_trie_raw, grp_trie_raw, bnk_trie_raw,
        )

    # ------------------------------------------------------------------
    # INFO block
    # ------------------------------------------------------------------

    def _read_info_block(
        self,
        data: BinaryIO,
        block_offset: int,
        offset_to_id: dict[int, int],
    ) -> tuple[
        list[SoundDataEntry],
        list[SoundBankEntry],
        list[PlayerInfoEntry],
        list[FileEntry],
        list[GroupDataEntry],
        ArcCommonInfo,
    ]:
        """Read the INFO block with all sub-tables."""
        data.seek(block_offset)
        section = read_section_header(data)

        # 6 references: snd, bnk, ply, file, grp, arc_common
        snd_tbl_off = read_offset_from_ref(data, block_offset)
        bnk_tbl_off = read_offset_from_ref(data, block_offset)
        ply_tbl_off = read_offset_from_ref(data, block_offset)
        file_tbl_off = read_offset_from_ref(data, block_offset)
        grp_tbl_off = read_offset_from_ref(data, block_offset)
        arc_info_off = read_offset_from_ref(data, block_offset)

        # Read arc common info (7 x u16)
        data.seek(arc_info_off)
        values = struct.unpack('>HHHHHHH', data.read(14))
        arc_info = ArcCommonInfo(*values)

        # Read sound table
        sound_entries = self._read_sound_table(data, snd_tbl_off, block_offset)

        # Read bank table
        bank_entries = self._read_bank_table(data, bnk_tbl_off, block_offset)

        # Read player table
        player_entries = self._read_player_table(data, ply_tbl_off, block_offset)

        # Read file table
        file_entries = self._read_file_table(data, file_tbl_off, block_offset)

        # Read group table
        group_entries = self._read_group_table(data, grp_tbl_off, block_offset, offset_to_id)

        return sound_entries, bank_entries, player_entries, file_entries, group_entries, arc_info

    def _read_sound_table(
        self,
        data: BinaryIO,
        table_offset: int,
        info_base: int,
    ) -> list[SoundDataEntry]:
        data.seek(table_offset)
        n_entries = struct.unpack('>I', data.read(4))[0]

        refs = [read_offset_from_ref(data, info_base) for _ in range(n_entries)]

        entries = []
        for ref_off in refs:
            data.seek(ref_off)
            entry = self._read_sound_entry(data, info_base)
            entries.append(entry)

        return entries

    def _read_sound_entry(self, data: BinaryIO, info_base: int) -> SoundDataEntry:
        file_name_index, file_index, player_index = struct.unpack('>III', data.read(12))

        snd3d_ref = read_reference(data)

        volume, player_priority, snd_type_raw, remote_filter = struct.unpack('>BBBB', data.read(4))
        sound_type = SoundType(snd_type_raw)

        snd_info_ref = read_reference(data)

        user_param1, user_param2, pan_mode, pan_curve, actor_player_id, _ = (
            struct.unpack('>IIBBBB', data.read(12))
        )

        # Read Sound3DParam
        if snd3d_ref.flag != 0:
            snd3d_off = info_base + 8 + snd3d_ref.offset
            data.seek(snd3d_off)
            flags, decay_curve, decay_ratio, doppler_factor, _, _ = (
                struct.unpack('>IBBBBI', data.read(12))
            )
            sound_3d = Sound3DParam(
                flags=flags,
                decay_curve=decay_curve,
                decay_ratio=decay_ratio,
                doppler_factor=doppler_factor,
            )
        else:
            sound_3d = Sound3DParam()

        # Read type-specific sound info
        sound_info = None
        if snd_info_ref.flag != 0:
            snd_info_off = info_base + 8 + snd_info_ref.offset
            data.seek(snd_info_off)

            match sound_type:
                case SoundType.SEQ:
                    vals = struct.unpack('>IIIBBHI', data.read(20))
                    sound_info = SeqSoundInfo(
                        seq_label_offset=vals[0],
                        bank_index=vals[1],
                        alloc_track=vals[2],
                        channel_priority=vals[3],
                        release_priority_fix=vals[4],
                    )
                case SoundType.STRM:
                    vals = struct.unpack('>IHHI', data.read(12))
                    sound_info = StreamSoundInfo(
                        start_position=vals[0],
                        n_alloc_channels=vals[1],
                        alloc_track_flag=vals[2],
                    )
                case SoundType.WAVE:
                    vals = struct.unpack('>iIBBHI', data.read(16))
                    sound_info = WaveSoundInfo(
                        wave_index=vals[0],
                        alloc_track=vals[1],
                        channel_priority=vals[2],
                        release_priority_fix=vals[3],
                    )

        return SoundDataEntry(
            file_name_index=file_name_index,
            file_index=file_index,
            player_index=player_index,
            volume=volume,
            player_priority=player_priority,
            sound_type=sound_type,
            remote_filter=remote_filter,
            user_param1=user_param1,
            user_param2=user_param2,
            pan_mode=pan_mode,
            pan_curve=pan_curve,
            actor_player_id=actor_player_id,
            sound_3d=sound_3d,
            sound_info=sound_info,
        )

    def _read_bank_table(
        self,
        data: BinaryIO,
        table_offset: int,
        info_base: int,
    ) -> list[SoundBankEntry]:
        data.seek(table_offset)
        n_entries = struct.unpack('>I', data.read(4))[0]

        refs = [read_offset_from_ref(data, info_base) for _ in range(n_entries)]

        entries = []
        for ref_off in refs:
            data.seek(ref_off)
            file_name_index, file_index, bank_index = struct.unpack('>III', data.read(12))
            entries.append(SoundBankEntry(
                file_name_index=file_name_index,
                file_index=file_index,
                bank_index=bank_index,
            ))

        return entries

    def _read_player_table(
        self,
        data: BinaryIO,
        table_offset: int,
        info_base: int,
    ) -> list[PlayerInfoEntry]:
        data.seek(table_offset)
        n_entries = struct.unpack('>I', data.read(4))[0]

        refs = [read_offset_from_ref(data, info_base) for _ in range(n_entries)]

        entries = []
        for ref_off in refs:
            data.seek(ref_off)
            file_name_index, n_playable_sounds, _, _, heap_size, _ = (
                struct.unpack('>IBBHII', data.read(16))
            )
            entries.append(PlayerInfoEntry(
                file_name_index=file_name_index,
                n_playable_sounds=n_playable_sounds,
                heap_size=heap_size,
            ))

        return entries

    def _read_file_table(
        self,
        data: BinaryIO,
        table_offset: int,
        info_base: int,
    ) -> list[FileEntry]:
        data.seek(table_offset)
        n_entries = struct.unpack('>I', data.read(4))[0]

        refs = [read_offset_from_ref(data, info_base) for _ in range(n_entries)]

        entries = []
        for ref_off in refs:
            data.seek(ref_off)
            entry = self._read_file_entry(data, info_base)
            entries.append(entry)

        return entries

    def _read_file_entry(self, data: BinaryIO, info_base: int) -> FileEntry:
        file_size, wave_file_size, entry_num = struct.unpack('>IIi', data.read(12))

        ext_file_ref = read_reference(data)
        file_tbl_ref = read_reference(data)

        external_file_path = None
        if ext_file_ref.flag != 0:
            ext_off = info_base + 8 + ext_file_ref.offset
            data.seek(ext_off)

            # Read until null or until the file table ref offset
            if file_tbl_ref.flag != 0:
                tbl_off = info_base + 8 + file_tbl_ref.offset
                chunk = data.read(tbl_off - ext_off)
            else:
                chunk = data.read(256)

            null = chunk.find(b'\x00')
            if null != -1:
                chunk = chunk[:null]
            external_file_path = chunk.decode('ascii', errors='replace')

        file_positions = []
        if file_tbl_ref.flag != 0:
            tbl_off = info_base + 8 + file_tbl_ref.offset
            data.seek(tbl_off)
            n_pos = struct.unpack('>I', data.read(4))[0]

            pos_refs = [read_offset_from_ref(data, info_base) for _ in range(n_pos)]
            for pos_off in pos_refs:
                data.seek(pos_off)
                grp_idx, idx = struct.unpack('>II', data.read(8))
                file_positions.append(FilePositionEntry(group_index=grp_idx, index=idx))

        return FileEntry(
            file_size=file_size,
            wave_file_size=wave_file_size,
            entry_num=entry_num,
            external_file_path=external_file_path,
            file_positions=file_positions,
        )

    def _read_group_table(
        self,
        data: BinaryIO,
        table_offset: int,
        info_base: int,
        offset_to_id: dict[int, int],
    ) -> list[GroupDataEntry]:
        data.seek(table_offset)
        n_entries = struct.unpack('>I', data.read(4))[0]

        refs = [read_offset_from_ref(data, info_base) for _ in range(n_entries)]

        entries = []
        for ref_off in refs:
            data.seek(ref_off)
            entry = self._read_group_entry(data, info_base, offset_to_id)
            entries.append(entry)

        return entries

    def _read_group_entry(
        self,
        data: BinaryIO,
        info_base: int,
        offset_to_id: dict[int, int],
    ) -> GroupDataEntry:
        file_name_index, entry_num = struct.unpack('>ii', data.read(8))
        ext_file_ref = read_reference(data)

        grp_file_off, grp_file_size, grp_audio_off, grp_audio_size = (
            struct.unpack('>IIII', data.read(16))
        )

        grp_tbl_ref = read_reference(data)

        # Resolve primary file IDs
        file_id = offset_to_id.get(grp_file_off)
        audio_file_id = offset_to_id.get(grp_audio_off)

        # External file path
        external_file_path = None
        if ext_file_ref.flag != 0:
            data.seek(info_base + 8 + ext_file_ref.offset)
            external_file_path = read_terminated_string(data)

        # Group table entries
        group_table = []
        if grp_tbl_ref.flag != 0:
            data.seek(info_base + 8 + grp_tbl_ref.offset)
            n_entries = struct.unpack('>I', data.read(4))[0]

            entry_refs = [read_offset_from_ref(data, info_base) for _ in range(n_entries)]
            for entry_off in entry_refs:
                data.seek(entry_off)
                (grp_idx, file_data_off, file_data_size,
                 audio_data_off, audio_data_size, _) = struct.unpack('>IIIIII', data.read(24))

                sub_file_id = None
                sub_audio_id = None

                # A zero-size side is intentionally absent. Audio-only file
                # entries use file_data_off=0, which must not resolve to the
                # group's first data file merely because they share a base.
                if grp_file_off != 0 and file_data_size > 0:
                    sub_file_id = offset_to_id.get(grp_file_off + file_data_off)
                if grp_audio_off != 0 and audio_data_size > 0:
                    sub_audio_id = offset_to_id.get(grp_audio_off + audio_data_off)

                group_table.append(GroupTableEntry(
                    group_index=grp_idx,
                    file_data_offset=file_data_off,
                    file_data_size=file_data_size,
                    audio_data_offset=audio_data_off,
                    audio_data_size=audio_data_size,
                    file_id=sub_file_id,
                    audio_file_id=sub_audio_id,
                ))

        return GroupDataEntry(
            file_name_index=file_name_index,
            entry_num=entry_num,
            external_file_path=external_file_path,
            group_file_offset=grp_file_off,
            group_file_size=grp_file_size,
            group_audio_offset=grp_audio_off,
            group_audio_size=grp_audio_size,
            group_table=group_table,
            file_id=file_id,
            audio_file_id=audio_file_id,
        )
