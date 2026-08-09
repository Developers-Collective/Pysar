import io
import struct
from typing import BinaryIO

from pysar.core.base import WriterBase
from pysar.core.types import ByteOrder
from pysar.core.model.brbnk import (
    BrbnkData,
    InstrumentRegion,
    InstParam,
    RangeTable,
    IndexTable,
    RegionType,
)


def _round_up(x: int, alignment: int) -> int:
    return (x + alignment - 1) & ~(alignment - 1)


def _align_buffer(buffer: io.BytesIO, alignment: int = 4) -> None:
    pos = buffer.tell()
    aligned = _round_up(pos, alignment)
    if aligned > pos:
        buffer.write(b"\x00" * (aligned - pos))


class BrbnkWriter(WriterBase):
    VERSION = 0x0102
    HEADER_SIZE = 0x20

    def write(self, model: BrbnkData, output: BinaryIO) -> None:
        """Write a BRBNK file to a binary stream."""
        # We'll build the DATA block first, then construct the file
        data_buffer = io.BytesIO()

        # DATA block header
        data_buffer.write(b"DATA")
        data_buffer.write(struct.pack(">I", 0))  # Size placeholder

        # Instrument count
        n_instruments = len(model.instruments)
        data_buffer.write(struct.pack(">I", n_instruments))

        # Reserve space for instrument references
        inst_ref_start = data_buffer.tell()
        for _ in range(n_instruments):
            data_buffer.write(b"\x00" * 8)

        _align_buffer(data_buffer, 4)

        # Write instrument data and collect references
        inst_refs = []
        for inst in model.instruments:
            ref_type, ref_offset = self._write_region(
                data_buffer, inst.root_region
            )
            inst_refs.append((ref_type, ref_offset))

        # Go back and write instrument references
        data_buffer.seek(inst_ref_start)
        for ref_type, ref_offset in inst_refs:
            data_buffer.write(struct.pack(">BBHI", 1, ref_type, 0, ref_offset))

        data_bytes = data_buffer.getvalue()
        data_size = len(data_bytes)

        # Align data size
        aligned_data_size = _round_up(data_size, 0x20)
        data_bytes = bytearray(data_bytes)
        data_bytes.extend(b"\x00" * (aligned_data_size - data_size))
        # BinaryBlockHeader.size covers the complete padded section, matching
        # the file header's section-table size and Nintendo-authored RBNKs.
        struct.pack_into(">I", data_bytes, 4, aligned_data_size)
        data_bytes = bytes(data_bytes)

        # Build file
        file_buffer = io.BytesIO()

        # File header
        data_offset = self.HEADER_SIZE
        wave_offset = 0  # No embedded waves for v1.2
        wave_size = 0

        file_buffer.write(struct.pack(
            ">4sHHIHH IIII",
            b"RBNK",
            ByteOrder.BIG_ENDIAN,
            model.version or self.VERSION,
            0,  # File size placeholder
            self.HEADER_SIZE,
            1,  # 1 section (DATA only for v1.2)
            data_offset,
            aligned_data_size,
            wave_offset,
            wave_size,
        ))

        # Write DATA block
        file_buffer.write(data_bytes)

        # Update file size
        file_size = file_buffer.tell()
        file_buffer.seek(8)
        file_buffer.write(struct.pack(">I", file_size))

        output.write(file_buffer.getvalue())

    def _write_region(
            self,
            buffer: io.BytesIO,
            region: InstrumentRegion,
    ) -> tuple[int, int]:
        if region.is_null():
            return RegionType.NULL, 0

        if region.region_type == RegionType.DIRECT:
            offset = buffer.tell() - 8  # Relative to content start (after section header)
            self._write_inst_param(buffer, region.inst_param)
            return RegionType.DIRECT, offset

        if region.region_type == RegionType.RANGE:
            offset = buffer.tell() - 8
            self._write_range_table(buffer, region.range_table)
            return RegionType.RANGE, offset

        if region.region_type == RegionType.INDEX:
            offset = buffer.tell() - 8
            self._write_index_table(buffer, region.index_table)
            return RegionType.INDEX, offset

        return RegionType.NULL, 0

    def _write_inst_param(self, buffer: io.BytesIO, param: InstParam) -> None:
        buffer.write(struct.pack(
            ">IbbbbbBBBBBBBf",
            param.wave_index,
            param.attack & 0xFF,
            param.decay & 0xFF,
            param.sustain & 0xFF,
            param.release & 0xFF,
            param.hold & 0xFF,
            param.wave_data_location_type,
            param.note_off_type,
            param.alternate_assign,
            param.original_key,
            param.volume,
            param.pan,
            param.surround_pan,
            param.pitch,
        ))

        # Null references (LFO, graph env, randomizer)
        for _ in range(3):
            buffer.write(b"\x00" * 8)

        # Reserved
        buffer.write(b"\x00" * 4)

    def _write_range_table(self, buffer: io.BytesIO, table: RangeTable) -> None:

        # Write bounds
        n_entries = len(table.bounds)
        buffer.write(struct.pack(">B", n_entries))
        for bound in table.bounds:
            buffer.write(struct.pack(">B", bound))

        # Align to 4 bytes
        _align_buffer(buffer, 4)

        # Reserve space for references
        ref_start = buffer.tell()
        for _ in range(n_entries):
            buffer.write(b"\x00" * 8)

        # Write sub-regions and collect references
        refs = []
        for region in table.regions:
            ref_type, ref_offset = self._write_region(buffer, region)
            refs.append((ref_type, ref_offset))

        # Go back and write references
        end_pos = buffer.tell()
        buffer.seek(ref_start)
        for ref_type, ref_offset in refs:
            buffer.write(struct.pack(">BBHI", 1, ref_type, 0, ref_offset))
        buffer.seek(end_pos)

    def _write_index_table(self, buffer: io.BytesIO, table: IndexTable) -> None:
        buffer.write(struct.pack(
            ">BBH",
            table.min_index,
            table.max_index,
            0,  # Reserved
        ))

        # Reserve space for references
        n_entries = table.max_index - table.min_index + 1
        ref_start = buffer.tell()
        for _ in range(n_entries):
            buffer.write(b"\x00" * 8)

        # Write sub-regions and collect references
        refs = []
        for region in table.regions:
            ref_type, ref_offset = self._write_region(buffer, region)
            refs.append((ref_type, ref_offset))

        # Go back and write references
        end_pos = buffer.tell()
        buffer.seek(ref_start)
        for ref_type, ref_offset in refs:
            buffer.write(struct.pack(">BBHI", 1, ref_type, 0, ref_offset))
        buffer.seek(end_pos)
