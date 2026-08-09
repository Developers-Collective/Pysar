import io
import struct
from typing import BinaryIO

from pysar.core.base import WriterBase
from pysar.core.types import ByteOrder
from pysar.core.model.brwar import BrwarData
from pysar.core.format.rwav.writer import BrwavWriter


def _align_up(x: int, alignment: int = 0x20) -> int:
    return (x + alignment - 1) & ~(alignment - 1)

def _extract_brwav_blob(model_entry, brwav_writer: BrwavWriter) -> tuple[bytes, int]:
    """Return BRWAV bytes + declared file size, preferring raw bytes like legacy writer."""
    raw = getattr(model_entry.brwav, "raw_bytes", None)
    wav_bytes = raw if isinstance(raw, (bytes, bytearray)) and len(raw) >= 16 else brwav_writer.to_bytes(model_entry.brwav)

    if len(wav_bytes) >= 12 and wav_bytes[:4] == b"RWAV":
        declared_size = struct.unpack(">I", wav_bytes[8:12])[0]
        if 0 < declared_size <= len(wav_bytes):
            return bytes(wav_bytes[:declared_size]), declared_size

    return bytes(wav_bytes), len(wav_bytes)


class BrwarWriter(WriterBase):
    VERSION = 0x0100
    HEADER_SIZE = 0x20
    ENTRY_SIZE = 12  # Reference (8) + size (4)

    def write(self, model: BrwarData, output: BinaryIO) -> None:
        """Write a BRWAR file to a binary stream."""
        buffer = io.BytesIO()
        n_entries = len(model.entries)

        # Write file header placeholder
        buffer.write(struct.pack(
            ">4sHHIHH IIII",
            b"RWAR",
            ByteOrder.BIG_ENDIAN,
            model.version or self.VERSION,
            0,  # file_size placeholder
            self.HEADER_SIZE,
            2,  # n_sections (TABL + DATA)
            self.HEADER_SIZE,  # tabl_offset (always 0x20)
            0,  # tabl_size placeholder
            0,  # data_offset placeholder
            0,  # data_size placeholder
        ))

        # Write TABL section
        tabl_start = buffer.tell()
        buffer.write(b"TABL")
        buffer.write(struct.pack(">I", 0))  # size placeholder
        buffer.write(struct.pack(">I", n_entries))

        # Write entry placeholders (will fill in later)
        entry_table_pos = buffer.tell()
        for _ in range(n_entries):
            buffer.write(b"\x00" * self.ENTRY_SIZE)

        # Align TABL section
        padding = _align_up(buffer.tell(), 0x20) - buffer.tell()
        buffer.write(b"\x00" * padding)

        tabl_size = buffer.tell() - tabl_start
        data_offset = buffer.tell()

        # Write DATA section
        data_start = buffer.tell()
        buffer.write(b"DATA")
        buffer.write(struct.pack(">I", 0))  # size placeholder
        buffer.write(b"\x00" * 0x18)  # padding

        # Write each BRWAV and track offsets
        brwav_writer = BrwavWriter()
        wav_entries = []  # (offset_relative_to_data, size)

        for entry in model.entries:
            # Current position relative to DATA section
            rel_offset = buffer.tell() - data_start

            # Legacy-compatible behavior: keep original RWAV blob when available.
            wav_bytes, wav_size = _extract_brwav_blob(entry, brwav_writer)
            buffer.write(wav_bytes)

            wav_entries.append((rel_offset, wav_size))

            # Align to 0x20
            padding = _align_up(buffer.tell(), 0x20) - buffer.tell()
            buffer.write(b"\x00" * padding)

        # Final alignment
        padding = _align_up(buffer.tell(), 0x20) - buffer.tell()
        buffer.write(b"\x00" * padding)

        file_size = buffer.tell()
        data_size = file_size - data_start

        # Fill in entry table
        buffer.seek(entry_table_pos)
        for rel_offset, wav_size in wav_entries:
            # Reference: type(1), padding(1), padding(2), offset(4), size(4)
            buffer.write(struct.pack(">BBHII", 1, 0, 0, rel_offset, wav_size))

        # Fill in file header
        buffer.seek(0x08)
        buffer.write(struct.pack(">I", file_size))

        buffer.seek(0x14)
        buffer.write(struct.pack(">III", tabl_size, data_offset, data_size))

        # Fill in TABL size
        buffer.seek(tabl_start + 4)
        buffer.write(struct.pack(">I", tabl_size))

        # Fill in DATA size
        buffer.seek(data_start + 4)
        buffer.write(struct.pack(">I", data_size))

        # Write final output
        output.write(buffer.getvalue())