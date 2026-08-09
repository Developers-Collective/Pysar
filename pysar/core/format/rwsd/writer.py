import io
import struct
from typing import BinaryIO

from pysar.core.base import WriterBase, NW4RFileHeader, NW4RSectionHeader
from pysar.core.types import FileTag, ByteOrder, Reference, NW4R_EMPTY_REFERENCE
from pysar.core.model.brwsd import (
    BrwsdData, WaveSound, WsdInfo, TrackInfo, NoteEvent, NoteInfo,
)
from pysar.io.binary import (
    write_file_header,
    write_section_header,
    write_reference,
    pad_to_alignment,
    align_up,
)


VERSION_1_3 = 0x0103
FILE_HEADER_SIZE = 0x20


class BrwsdWriter(WriterBase):
    def write(self, model: BrwsdData, out: BinaryIO) -> None:
        out.write(self.to_bytes(model))

    def to_bytes(self, model: BrwsdData) -> bytes:
        """Serialize the full BRWSD file."""
        buf = io.BytesIO()

        # Build DATA block content (refs relative to content start)
        data_content = self._build_data_content(model)

        # Write DATA section header
        data_section_start = FILE_HEADER_SIZE
        data_section_size = 8 + len(data_content)  # section header + content

        # Pad data section to 0x20 alignment
        padded_data_size = align_up(data_section_size, 0x20)

        # Calculate file size
        file_size = FILE_HEADER_SIZE + padded_data_size
        version = model.version if model.version else VERSION_1_3

        # Write file header
        file_header = NW4RFileHeader(
            magic=FileTag.BRWSD,
            byte_order=ByteOrder.BIG_ENDIAN,
            version=version,
            file_size=file_size,
            header_size=FILE_HEADER_SIZE,
            n_sections=1,  # DATA only for v1.3
        )
        write_file_header(file_header, buf)

        # Write section table (offsets and sizes)
        buf.write(struct.pack(
            '>IIII',
            data_section_start,  # data_offset
            padded_data_size,    # data_size
            0,                   # wave_offset (unused in v1.3)
            0,                   # wave_size
        ))

        # Write DATA section
        data_header = NW4RSectionHeader(magic='DATA', size=data_section_size)
        write_section_header(data_header, buf)
        buf.write(data_content)

        # Pad to 0x20 alignment
        pad_to_alignment(buf, 0x20)

        return buf.getvalue()

    def _build_data_content(self, model: BrwsdData) -> bytes:
        buf = io.BytesIO()
        n = len(model.wave_sounds)

        # WSD count
        buf.write(struct.pack('>I', n))

        # Placeholder refs for each WSD
        wsd_ref_pos = buf.tell()
        for _ in range(n):
            buf.write(NW4R_EMPTY_REFERENCE)

        # Write each WSD entry, track positions
        wsd_offsets: list[int] = []
        for wsd in model.wave_sounds:
            pad_to_alignment(buf, 4)
            wsd_offsets.append(buf.tell())
            self._write_wsd(buf, wsd)

        # Pad content to 0x20 alignment (for the whole block)
        pad_to_alignment(buf, 0x20)

        # Fill in WSD refs
        end_pos = buf.tell()
        buf.seek(wsd_ref_pos)
        for off in wsd_offsets:
            ref = Reference(flag=1, data_type=0, offset=off)
            write_reference(ref, buf)
        buf.seek(end_pos)

        return buf.getvalue()

    def _write_wsd(self, buf: io.BytesIO, wsd: WaveSound) -> None:
        # Placeholder refs: info, tracks, notes
        info_ref_pos = buf.tell()
        for _ in range(3):
            buf.write(NW4R_EMPTY_REFERENCE)

        # WsdInfo
        info_off = buf.tell()
        self._write_wsd_info(buf, wsd.info)

        # Track table
        trk_off = buf.tell()
        self._write_track_table(buf, wsd.tracks)

        # Note table
        note_off = buf.tell()
        self._write_note_table(buf, wsd.notes)

        # Fill in refs
        end = buf.tell()
        buf.seek(info_ref_pos)
        write_reference(Reference(flag=1, data_type=0, offset=info_off), buf)
        write_reference(Reference(flag=1, data_type=0, offset=trk_off), buf)
        write_reference(Reference(flag=1, data_type=0, offset=note_off), buf)
        buf.seek(end)

    def _write_wsd_info(self, buf: io.BytesIO, info: WsdInfo) -> None:
        """Write WsdInfo (32 bytes)."""
        buf.write(struct.pack(
            '>fBBBBBBH',
            info.pitch, info.pan, info.surround_pan,
            info.fx_send_a, info.fx_send_b, info.fx_send_c,
            info.main_send, 0,
        ))
        # Two null refs + padding
        for _ in range(2):
            buf.write(NW4R_EMPTY_REFERENCE)
        buf.write(b'\x00' * 4)

    def _write_track_table(
            self, buf: io.BytesIO, tracks: list[TrackInfo],
    ) -> None:
        n = len(tracks)
        buf.write(struct.pack('>I', n))

        ref_pos = buf.tell()
        for _ in range(n):
            buf.write(NW4R_EMPTY_REFERENCE)

        offsets: list[int] = []
        for trk in tracks:
            pad_to_alignment(buf, 4)
            offsets.append(buf.tell())
            self._write_track_info(buf, trk)

        end = buf.tell()
        buf.seek(ref_pos)
        for off in offsets:
            write_reference(Reference(flag=1, data_type=0, offset=off), buf)
        buf.seek(end)

    def _write_track_info(
            self, buf: io.BytesIO, trk: TrackInfo,
    ) -> None:
        # Placeholder ref to note event table
        evt_ref_pos = buf.tell()
        buf.write(NW4R_EMPTY_REFERENCE)

        # Note event table
        evt_tbl_off = buf.tell()
        n = len(trk.note_events)
        buf.write(struct.pack('>I', n))

        evt_ref_start = buf.tell()
        for _ in range(n):
            buf.write(NW4R_EMPTY_REFERENCE)

        evt_offsets: list[int] = []
        for evt in trk.note_events:
            pad_to_alignment(buf, 4)
            evt_offsets.append(buf.tell())
            self._write_note_event(buf, evt)

        # Fill in event refs
        end = buf.tell()
        buf.seek(evt_ref_start)
        for off in evt_offsets:
            write_reference(Reference(flag=1, data_type=0, offset=off), buf)

        # Fill in table ref
        buf.seek(evt_ref_pos)
        write_reference(Reference(flag=1, data_type=0, offset=evt_tbl_off), buf)
        buf.seek(end)

    def _write_note_event(
            self, buf: io.BytesIO, evt: NoteEvent,
    ) -> None:
        """Write NoteEvent (16 bytes)."""
        buf.write(struct.pack('>ffII', evt.position, evt.length, evt.note_index, 0))

    def _write_note_table(
            self, buf: io.BytesIO, notes: list[NoteInfo],
    ) -> None:
        n = len(notes)
        buf.write(struct.pack('>I', n))

        ref_pos = buf.tell()
        for _ in range(n):
            buf.write(NW4R_EMPTY_REFERENCE)

        offsets: list[int] = []
        for note in notes:
            pad_to_alignment(buf, 4)
            offsets.append(buf.tell())
            self._write_note_info(buf, note)

        end = buf.tell()
        buf.seek(ref_pos)
        for off in offsets:
            write_reference(Reference(flag=1, data_type=0, offset=off), buf)
        buf.seek(end)

    def _write_note_info(
            self, buf: io.BytesIO, note: NoteInfo,
    ) -> None:
        buf.write(struct.pack(
            '>iBBBBB3sBBBBf',
            note.wave_index,
            note.attack, note.decay, note.sustain,
            note.release, note.hold,
            b'\x00\x00\x00',
            note.original_key, note.volume,
            note.pan, note.surround_pan,
            note.pitch,
        ))
        # Three null refs + padding
        for _ in range(3):
            buf.write(NW4R_EMPTY_REFERENCE)
        buf.write(b'\x00' * 4)