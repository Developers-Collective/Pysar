import io
import struct
from typing import BinaryIO

from pysar.core.base import WriterBase, NW4RFileHeader, NW4RSectionHeader
from pysar.core.types import ByteOrder, FileTag, NW4R_EMPTY_REFERENCE, Reference
from pysar.core.model.brsar import (
    BrsarData,
    SoundType,
    SoundDataEntry,
    SoundBankEntry,
    PlayerInfoEntry,
    FileEntry,
    GroupDataEntry,
    ArcCommonInfo,
    EmbeddedFile,
)
from pysar.io.binary import (
    pad_to_alignment,
    write_file_header,
    write_reference,
    write_section_header,
)
from pysar.core.format.rsar.provenance import append_trailer

HEADER_SIZE = 0x40


def ordered_embedded_file_ids(model: BrsarData) -> list[int]:
    """Return a stable physical FILE order grouped by each load group."""
    available = set(model.embedded_files)
    ordered: list[int] = []
    seen: set[int] = set()

    def append(file_id: int | None) -> None:
        if file_id is None:
            return
        file_id = int(file_id)
        if file_id in available and file_id not in seen:
            seen.add(file_id)
            ordered.append(file_id)

    for group in model.group_entries:
        for sub in group.group_table:
            append(sub.file_id)
        for sub in group.group_table:
            append(sub.audio_file_id)

    ordered.extend(sorted(available - seen))
    return ordered


class BrsarWriter(WriterBase):

    def write(self, model: BrsarData, out: BinaryIO) -> None:
        out.write(self.to_bytes(model))

    def to_bytes(self, model: BrsarData) -> bytes:
        """Serialize a complete BRSAR file."""

        # Step 1: Build FILE block and collect FILE-relative offsets
        file_block, rel_file_lookup = self._build_file_block(model)

        # Step 2: Build SYMB block
        symb_block = self._build_symb_block(model)

        # Step 3: Build INFO block once to determine final layout size
        # (group/file offsets are fixed-width ints, so block size is stable).
        info_block = self._build_info_block(model, rel_file_lookup)

        # Step 4: Assemble final file
        symb_offset = HEADER_SIZE
        symb_size = len(symb_block)

        info_offset = symb_offset + symb_size
        info_size = len(info_block)

        file_offset = info_offset + info_size
        file_size = len(file_block)

        # Convert FILE-relative offsets to absolute BRSAR offsets.
        abs_file_lookup = {
            file_id: file_offset + rel_off
            for file_id, rel_off in rel_file_lookup.items()
        }

        # Rebuild INFO with correct absolute offsets.
        info_block = self._build_info_block(model, abs_file_lookup)
        info_size = len(info_block)
        file_offset = info_offset + info_size
        total_size = file_offset + file_size

        # If INFO size ever changes, recompute once more for consistency.
        abs_file_lookup = {
            file_id: file_offset + rel_off
            for file_id, rel_off in rel_file_lookup.items()
        }
        info_block = self._build_info_block(model, abs_file_lookup)
        info_size = len(info_block)
        file_offset = info_offset + info_size

        total_size = file_offset + file_size

        buf = io.BytesIO()

        # Write file header
        write_file_header(NW4RFileHeader(
            magic=FileTag.BRSAR,
            byte_order=ByteOrder.BIG_ENDIAN,
            version=model.version,
            file_size=total_size,
            header_size=HEADER_SIZE,
            n_sections=3,
        ), buf)

        # Write section table (6 x u32)
        buf.write(struct.pack(
            '>IIIIII',
            symb_offset, symb_size,
            info_offset, info_size,
            file_offset, file_size,
        ))

        # Pad header to 0x40
        pad_to_alignment(buf, HEADER_SIZE)

        # Write the three blocks
        buf.write(symb_block)
        buf.write(info_block)
        buf.write(file_block)

        # Keep the NW4R header's file_size Nintendo-compatible. Provenance is a
        # PySAR-only trailer after those declared bytes and is ignored by the
        # game runtime and by tools that read only the standard file size.
        return append_trailer(buf.getvalue(), model.provenance)

    # ------------------------------------------------------------------
    # FILE block
    # ------------------------------------------------------------------

    def _build_file_block(
        self, model: BrsarData,
    ) -> tuple[bytes, dict[int, int]]:
        """Build FILE block and return (bytes, file_lookup).

        file_lookup maps file_id -> offset relative to FILE block start.
        Caller converts these to absolute BRSAR offsets once final layout
        is known.
        """
        buf = io.BytesIO()

        # Section header (8 bytes) + 24 bytes padding
        write_section_header(NW4RSectionHeader(magic='FILE', size=0), buf)
        buf.write(b'\x00' * 24)

        # Write embedded files in order, tracking relative offsets
        relative_lookup: dict[int, int] = {}

        file_ids = ordered_embedded_file_ids(model)
        for file_id in file_ids:
            ef = model.embedded_files[file_id]
            relative_lookup[file_id] = buf.tell()
            # Legacy serializer writes embedded files contiguously (no inter-file padding).
            buf.write(ef.raw_data)

        # Finalize size
        file_block_size = buf.tell()
        buf.seek(4)
        buf.write(struct.pack('>I', file_block_size))

        return buf.getvalue(), relative_lookup

    # ------------------------------------------------------------------
    # SYMB block
    # ------------------------------------------------------------------

    def _build_symb_block(self, model: BrsarData) -> bytes:
        """Build the SYMB block."""
        buf = io.BytesIO()

        # Section header + 5 offset placeholders
        buf.write(b'SYMB' + b'\x00' * 4)  # header with size placeholder
        buf.write(b'\x00' * (5 * 4))      # 5 offsets: tbl, snd, ply, grp, bnk

        # Write name offset table and strings
        # All offsets are relative to SYMB block start + 8 (after section header)
        offset_tbl_offset = buf.tell() - 8
        n_names = len(model.names)
        name_tbl_start = buf.tell() + 4 + (4 * n_names)

        buf.write(struct.pack('>I', n_names))

        offset_to_name = name_tbl_start
        for name in model.names:
            buf.write(struct.pack('>I', offset_to_name - 8))
            offset_to_name += len(name) + 1

        # Write null-terminated strings
        for name in model.names:
            buf.write(name.encode('ascii') + b'\x00')

        # Always rebuild tries from the parsed objects. Reusing raw captures can
        # shift later offsets because the stored padding depends on the original
        # serialized size.
        snd_trie_offset = buf.tell() - 8
        if model.snd_trie is not None:
            buf.write(bytes(model.snd_trie))
        else:
            buf.write(struct.pack('>ii', -1, 0))  # empty trie

        ply_trie_offset = buf.tell() - 8
        if model.player_trie is not None:
            buf.write(bytes(model.player_trie))
        else:
            buf.write(struct.pack('>ii', -1, 0))

        grp_trie_offset = buf.tell() - 8
        if model.group_trie is not None:
            buf.write(bytes(model.group_trie))
        else:
            buf.write(struct.pack('>ii', -1, 0))

        bnk_trie_offset = buf.tell() - 8
        if model.bank_trie is not None:
            buf.write(bytes(model.bank_trie))
        else:
            buf.write(struct.pack('>ii', -1, 0))

        # Pad to 0x20
        pad_to_alignment(buf, 0x20)
        block_size = buf.tell()

        # Fill in header
        buf.seek(4)
        buf.write(struct.pack(
            '>IIIIII',
            block_size,
            offset_tbl_offset,
            snd_trie_offset,
            ply_trie_offset,
            grp_trie_offset,
            bnk_trie_offset,
        ))

        return buf.getvalue()

    # ------------------------------------------------------------------
    # INFO block
    # ------------------------------------------------------------------

    def _build_info_block(
        self,
        model: BrsarData,
        file_lookup: dict[int, int],
    ) -> bytes:
        """Build the INFO block.

        file_lookup maps file_id -> absolute offset in the final BRSAR.
        """
        buf = io.BytesIO()

        # Section header
        write_section_header(NW4RSectionHeader(magic='INFO', size=0), buf)

        # 6 reference placeholders (snd, bnk, ply, file, grp, arc_common)
        refs_pos = buf.tell()
        for _ in range(6):
            buf.write(NW4R_EMPTY_REFERENCE)

        # Write all sub-tables, capturing their offsets (relative to block+8)
        snd_tbl_off = self._write_sound_table(buf, model.sound_entries)
        bnk_tbl_off = self._write_bank_table(buf, model.bank_entries)
        ply_tbl_off = self._write_player_table(buf, model.player_entries)
        file_tbl_off = self._write_file_table(buf, model.file_entries)
        grp_tbl_off = self._write_group_table(buf, model.group_entries, file_lookup, model.embedded_files)
        arc_info_off = buf.tell() - 8
        self._write_arc_common_info(buf, model.arc_common_info)

        # Pad to 0x20
        pad_to_alignment(buf, 0x20)
        block_size = buf.tell()

        # Fill in section header size
        buf.seek(4)
        buf.write(struct.pack('>I', block_size))

        # Fill in references
        buf.seek(refs_pos)
        for off in [snd_tbl_off, bnk_tbl_off, ply_tbl_off, file_tbl_off, grp_tbl_off, arc_info_off]:
            write_reference(Reference(flag=1, data_type=0, offset=off), buf)

        return buf.getvalue()

    # ------------------------------------------------------------------
    # INFO sub-tables
    # ------------------------------------------------------------------

    def _write_sound_table(
        self, buf: io.BytesIO, entries: list[SoundDataEntry],
    ) -> int:
        """Write the sound data table. Returns offset relative to INFO+8."""
        tbl_off = buf.tell() - 8
        n = len(entries)
        buf.write(struct.pack('>I', n))

        # Reserve reference slots
        refs_pos = buf.tell()
        for _ in range(n):
            buf.write(NW4R_EMPTY_REFERENCE)

        # Write entries
        offsets = []
        for entry in entries:
            offsets.append(buf.tell() - 8)
            self._write_sound_entry(buf, entry)

        # Fill in references
        end = buf.tell()
        buf.seek(refs_pos)
        for off in offsets:
            write_reference(Reference(flag=1, data_type=0, offset=off), buf)
        buf.seek(end)

        pad_to_alignment(buf, 4)
        return tbl_off

    def _write_sound_entry(self, buf: io.BytesIO, entry: SoundDataEntry) -> None:
        entry_start = buf.tell()

        buf.write(struct.pack('>III', entry.file_name_index, entry.file_index, entry.player_index))

        # Sound3D ref placeholder
        snd3d_ref_pos = buf.tell()
        buf.write(NW4R_EMPTY_REFERENCE)

        buf.write(struct.pack('>BBBB',
            entry.volume, entry.player_priority,
            entry.sound_type, entry.remote_filter,
        ))

        # Sound info ref placeholder
        snd_info_ref_pos = buf.tell()
        buf.write(NW4R_EMPTY_REFERENCE)

        buf.write(struct.pack('>IIBBBB',
            entry.user_param1, entry.user_param2,
            entry.pan_mode, entry.pan_curve,
            entry.actor_player_id, 0,
        ))

        # Write sound_info inline
        snd_info_off = buf.tell() - 8
        match entry.sound_type:
            case SoundType.SEQ:
                info = entry.sound_info
                buf.write(struct.pack('>IIIBBHI',
                    info.seq_label_offset, info.bank_index, info.alloc_track,
                    info.channel_priority, info.release_priority_fix, 0, 0,
                ))
            case SoundType.STRM:
                info = entry.sound_info
                buf.write(struct.pack('>IHHI',
                    info.start_position, info.n_alloc_channels,
                    info.alloc_track_flag, 0,
                ))
            case SoundType.WAVE:
                info = entry.sound_info
                buf.write(struct.pack('>iIBBHI',
                    info.wave_index, info.alloc_track,
                    info.channel_priority, info.release_priority_fix, 0, 0,
                ))

        # Write Sound3DParam inline
        snd3d_off = buf.tell() - 8
        s3d = entry.sound_3d
        buf.write(struct.pack('>IBBBBI',
            s3d.flags, s3d.decay_curve, s3d.decay_ratio,
            s3d.doppler_factor, 0, 0,
        ))

        # Fill in references
        end = buf.tell()
        buf.seek(snd3d_ref_pos)
        write_reference(Reference(flag=1, data_type=0, offset=snd3d_off), buf)
        buf.seek(snd_info_ref_pos)
        write_reference(Reference(flag=1, data_type=entry.sound_type, offset=snd_info_off), buf)
        buf.seek(end)

    def _write_bank_table(
        self, buf: io.BytesIO, entries: list[SoundBankEntry],
    ) -> int:
        tbl_off = buf.tell() - 8
        n = len(entries)
        buf.write(struct.pack('>I', n))

        refs_pos = buf.tell()
        for _ in range(n):
            buf.write(NW4R_EMPTY_REFERENCE)

        offsets = []
        for entry in entries:
            offsets.append(buf.tell() - 8)
            buf.write(struct.pack('>III',
                entry.file_name_index, entry.file_index, entry.bank_index,
            ))

        end = buf.tell()
        buf.seek(refs_pos)
        for off in offsets:
            write_reference(Reference(flag=1, data_type=0, offset=off), buf)
        buf.seek(end)

        pad_to_alignment(buf, 4)
        return tbl_off

    def _write_player_table(
        self, buf: io.BytesIO, entries: list[PlayerInfoEntry],
    ) -> int:
        tbl_off = buf.tell() - 8
        n = len(entries)
        buf.write(struct.pack('>I', n))

        refs_pos = buf.tell()
        for _ in range(n):
            buf.write(NW4R_EMPTY_REFERENCE)

        offsets = []
        for entry in entries:
            offsets.append(buf.tell() - 8)
            buf.write(struct.pack('>IBBHII',
                entry.file_name_index, entry.n_playable_sounds,
                0, 0, entry.heap_size, 0,
            ))

        end = buf.tell()
        buf.seek(refs_pos)
        for off in offsets:
            write_reference(Reference(flag=1, data_type=0, offset=off), buf)
        buf.seek(end)

        pad_to_alignment(buf, 4)
        return tbl_off

    def _write_file_table(
        self, buf: io.BytesIO, entries: list[FileEntry],
    ) -> int:
        tbl_off = buf.tell() - 8
        n = len(entries)
        buf.write(struct.pack('>I', n))

        # Reserve refs
        refs_pos = buf.tell()
        for _ in range(n):
            buf.write(NW4R_EMPTY_REFERENCE)

        offsets = []
        for entry in entries:
            offsets.append(buf.tell() - 8)
            self._write_file_entry(buf, entry)

        end = buf.tell()
        buf.seek(refs_pos)
        for off in offsets:
            write_reference(Reference(flag=1, data_type=0, offset=off), buf)
        buf.seek(end)

        pad_to_alignment(buf, 4)
        return tbl_off

    def _write_file_entry(self, buf: io.BytesIO, entry: FileEntry) -> None:
        entry_start = buf.tell()
        entry_off = entry_start - 8  # offset relative to INFO+8

        buf.write(struct.pack('>IIi', entry.file_size, entry.wave_file_size, entry.entry_num))

        # External file path ref
        ext_ref_pos = buf.tell()
        buf.write(NW4R_EMPTY_REFERENCE)

        # File position table ref
        tbl_ref_pos = buf.tell()
        buf.write(NW4R_EMPTY_REFERENCE)

        # Write external file path if present
        ext_path_off = -1
        if entry.external_file_path is not None:
            ext_path_off = buf.tell() - 8
            buf.write(entry.external_file_path.encode('ascii'))
            pad_to_alignment(buf, 4)

        # Write file position table (always present, even with 0 entries).
        pos_tbl_off = buf.tell() - 8
        n_pos = len(entry.file_positions)
        buf.write(struct.pack('>I', n_pos))

        pos_refs_start = buf.tell()
        for _ in range(n_pos):
            buf.write(NW4R_EMPTY_REFERENCE)

        pos_offsets = []
        for pos in entry.file_positions:
            pos_offsets.append(buf.tell() - 8)
            buf.write(struct.pack('>II', pos.group_index, pos.index))

        end = buf.tell()
        buf.seek(pos_refs_start)
        for off in pos_offsets:
            write_reference(Reference(flag=1, data_type=0, offset=off), buf)
        buf.seek(end)

        # Fill in refs
        end = buf.tell()
        if ext_path_off >= 0:
            buf.seek(ext_ref_pos)
            write_reference(Reference(flag=1, data_type=0, offset=ext_path_off), buf)
        buf.seek(tbl_ref_pos)
        write_reference(Reference(flag=1, data_type=0, offset=pos_tbl_off), buf)
        buf.seek(end)

    def _write_group_table(
        self,
        buf: io.BytesIO,
        entries: list[GroupDataEntry],
        file_lookup: dict[int, int],
        embedded_files: dict[int, EmbeddedFile],
    ) -> int:
        tbl_off = buf.tell() - 8
        n = len(entries)
        buf.write(struct.pack('>I', n))

        refs_pos = buf.tell()
        for _ in range(n):
            buf.write(NW4R_EMPTY_REFERENCE)

        offsets = []
        for entry in entries:
            offsets.append(buf.tell() - 8)
            self._write_group_entry(buf, entry, file_lookup, embedded_files)

        end = buf.tell()
        buf.seek(refs_pos)
        for off in offsets:
            write_reference(Reference(flag=1, data_type=0, offset=off), buf)
        buf.seek(end)

        pad_to_alignment(buf, 4)
        return tbl_off

    def _write_group_entry(
        self,
        buf: io.BytesIO,
        entry: GroupDataEntry,
        file_lookup: dict[int, int],
        embedded_files: dict[int, EmbeddedFile],
    ) -> None:
        # Derive bases from the group's live physical references.  Cached base
        # IDs can become stale after structural edits; using the lowest actual
        # offset guarantees every serialized sub-entry delta is unsigned.
        group_file_ids = {
            int(sub.file_id)
            for sub in entry.group_table
            if sub.file_id is not None and int(sub.file_id) in file_lookup
        }
        group_audio_ids = {
            int(sub.audio_file_id)
            for sub in entry.group_table
            if sub.audio_file_id is not None and int(sub.audio_file_id) in file_lookup
        }
        grp_file_off = min(
            (file_lookup[file_id] for file_id in group_file_ids),
            default=0,
        )
        grp_audio_off = min(
            (file_lookup[file_id] for file_id in group_audio_ids),
            default=0,
        )

        # Dynamically calculate the group file size based on actual embedded files
        grp_file_size = 0
        if group_file_ids:
            max_file_end = 0
            for sub in entry.group_table:
                if sub.file_id is not None and sub.file_id in file_lookup:
                    file_end = file_lookup[sub.file_id] + len(embedded_files[sub.file_id].raw_data)
                    if file_end > max_file_end:
                        max_file_end = file_end
            grp_file_size = max_file_end - grp_file_off if max_file_end > grp_file_off else 0

        # Dynamically calculate the group audio size based on actual embedded files
        grp_audio_size = 0
        if group_audio_ids:
            max_audio_end = 0
            for sub in entry.group_table:
                if sub.audio_file_id is not None and sub.audio_file_id in file_lookup:
                    audio_end = file_lookup[sub.audio_file_id] + len(embedded_files[sub.audio_file_id].raw_data)
                    if audio_end > max_audio_end:
                        max_audio_end = audio_end
            grp_audio_size = max_audio_end - grp_audio_off if max_audio_end > grp_audio_off else 0

        buf.write(struct.pack('>ii', entry.file_name_index, entry.entry_num))

        # External file path ref placeholder
        ext_ref_pos = buf.tell()
        buf.write(NW4R_EMPTY_REFERENCE)

        buf.write(struct.pack('>IIII',
            grp_file_off, grp_file_size,
            grp_audio_off, grp_audio_size,
        ))

        # Group table ref placeholder
        grp_tbl_ref_pos = buf.tell()
        buf.write(NW4R_EMPTY_REFERENCE)

        # Write external file path if present
        ext_path_off = -1
        if entry.external_file_path is not None:
            ext_path_off = buf.tell() - 8
            buf.write(entry.external_file_path.encode('ascii'))
            pad_to_alignment(buf, 4)

        # Write group sub-table
        grp_tbl_off = buf.tell() - 8
        n_sub = len(entry.group_table)
        buf.write(struct.pack('>I', n_sub))

        sub_refs_start = buf.tell()
        for _ in range(n_sub):
            buf.write(NW4R_EMPTY_REFERENCE)

        sub_offsets = []
        for sub in entry.group_table:
            sub_offsets.append(buf.tell() - 8)

            # Sub-entry offsets: simple subtraction matching old code:
            #   file_data_off  = file_lookup[sub.file]  - grp_file_off
            #   audio_data_off = file_lookup[sub.audio] - grp_audio_off
            if sub.file_id is not None and sub.file_id in file_lookup:
                file_data_off = file_lookup[sub.file_id] - grp_file_off
            else:
                file_data_off = 0

            if sub.audio_file_id is not None and sub.audio_file_id in file_lookup:
                audio_data_off = file_lookup[sub.audio_file_id] - grp_audio_off
            else:
                audio_data_off = 0

            file_data_size = sub.file_data_size
            if sub.file_id is not None and sub.file_id in embedded_files:
                file_data_size = len(embedded_files[sub.file_id].raw_data)

            audio_data_size = sub.audio_data_size
            if sub.audio_file_id is not None and sub.audio_file_id in embedded_files:
                audio_data_size = len(embedded_files[sub.audio_file_id].raw_data)

            buf.write(struct.pack('>IIIIII',
                sub.group_index, file_data_off, file_data_size,
                audio_data_off, audio_data_size, 0,
            ))

        # Fill in sub-table refs
        end = buf.tell()
        buf.seek(sub_refs_start)
        for off in sub_offsets:
            write_reference(Reference(flag=1, data_type=0, offset=off), buf)

        # Fill in ext path ref
        if ext_path_off >= 0:
            buf.seek(ext_ref_pos)
            write_reference(Reference(flag=1, data_type=0, offset=ext_path_off), buf)

        # Fill in group table ref
        buf.seek(grp_tbl_ref_pos)
        write_reference(Reference(flag=1, data_type=0, offset=grp_tbl_off), buf)

        buf.seek(end)

    def _write_arc_common_info(self, buf: io.BytesIO, info: ArcCommonInfo) -> None:
        buf.write(struct.pack('>HHHHHHH',
            info.n_seq_sounds, info.n_seq_tracks,
            info.n_stream_sounds, info.n_stream_tracks,
            info.n_stream_channels,
            info.n_wave_sounds, info.n_wave_tracks,
        ))
