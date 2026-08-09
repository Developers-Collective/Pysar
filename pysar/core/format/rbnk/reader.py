import struct
from typing import BinaryIO

from pysar.core import NW4RInvalidFileError, NW4RUnsupportedVersionError
from pysar.core.base import ReaderBase
from pysar.core.types import FileTag

from pysar.core.model.brbnk import (
    BrbnkData,
    Instrument,
    InstrumentRegion,
    InstParam,
    RangeTable,
    IndexTable,
    RegionType,
    WaveDataLocationType,
    NoteOffType,
)


def _round_up(x: int, alignment: int) -> int:
    return (x + alignment - 1) & ~(alignment - 1)


class BrbnkReader(ReaderBase):
    """
    Reader for BRBNK (Nintendo Wii Bank) files.
    """

    EXPECTED_MAGIC = FileTag.BRBNK
    SUPPORTED_VERSIONS = {0x0100, 0x0101, 0x0102}

    def read(self, data: BinaryIO) -> BrbnkData:
        """Read a BRBNK file from a binary stream."""
        base_offset = data.tell()

        # Read file header
        header = data.read(0x20)
        if len(header) < 0x20 or header[:4] != b"RBNK":
            raise NW4RInvalidFileError("Not a valid BRBNK file")

        (
            bom, version, file_size, header_size, n_sections,
            data_offset, data_size,
            wave_offset, wave_size,
        ) = struct.unpack(">HHIHH IIII", header[4:0x20])

        if version not in self.SUPPORTED_VERSIONS:
            raise NW4RUnsupportedVersionError(
                f"Unsupported BRBNK version: 0x{version:04X}"
            )

        # Read DATA block
        data_block_start = base_offset + data_offset
        data.seek(data_block_start)

        block_header = data.read(8)
        if block_header[:4] != b"DATA":
            raise NW4RInvalidFileError("DATA block missing")

        # Read instrument table
        n_instruments = struct.unpack(">I", data.read(4))[0]
        inst_table_offset = data.tell()

        # Read instrument references
        inst_refs = []
        for _ in range(n_instruments):
            ref_type, ref_data_type, _, ref_offset = struct.unpack(">BBHI", data.read(8))
            inst_refs.append((ref_data_type, ref_offset))

        # Content base = after the 8-byte section header ("DATA" + size)
        content_base = data_block_start + 8

        # Parse each instrument
        instruments = []
        for i, (ref_type, ref_offset) in enumerate(inst_refs):
            region = self._read_region(
                data, ref_type, ref_offset,
                content_base, version
            )
            instruments.append(Instrument(
                program=i,
                root_region=region,
            ))

        # Read WAVE block if present (versions < 1.2)
        embedded_waves = []
        has_embedded_waves = False
        if wave_offset > 0 and wave_size > 0 and version < 0x0102:
            has_embedded_waves = True
            # TODO: Parse embedded wave block

        # Capture raw bytes
        data.seek(base_offset)
        raw_bytes = data.read(file_size)

        return BrbnkData(
            version=version,
            instruments=instruments,
            embedded_waves=embedded_waves,
            has_embedded_waves=has_embedded_waves,
            raw_bytes=raw_bytes,
        )

    def _read_region(
            self,
            data: BinaryIO,
            ref_type: int,
            ref_offset: int,
            content_base: int,
            version: int,
    ) -> InstrumentRegion:
        """Read an instrument region based on its type.

        content_base: absolute position of DATA section content
                      (data_block_start + 8, after the section header).
        """
        region_type = RegionType(ref_type)

        if region_type == RegionType.NULL or region_type == RegionType.INVALID:
            return InstrumentRegion.null()

        if region_type == RegionType.DIRECT:
            data.seek(content_base + ref_offset)
            param = self._read_inst_param(data, version)
            return InstrumentRegion.direct(param)

        if region_type == RegionType.RANGE:
            data.seek(content_base + ref_offset)
            range_table = self._read_range_table(data, content_base, version)
            return InstrumentRegion.range(range_table)

        if region_type == RegionType.INDEX:
            data.seek(content_base + ref_offset)
            index_table = self._read_index_table(data, content_base, version)
            return InstrumentRegion.index(index_table)

        return InstrumentRegion.null()

    def _read_inst_param(self, data: BinaryIO, version: int) -> InstParam:
        """Read an InstParam structure."""
        (
            wave_index,
            attack, decay, sustain, release, hold,
            wav_loc_type, note_off_type, alt_assign,
            original_key, volume, pan, surround_pan,
            pitch,
        ) = struct.unpack(">IbbbbbBBBBBBBf", data.read(20))

        # Skip null references (LFO, graph env, randomizer)
        data.read(8 * 3)  # 3 references
        data.read(4)  # reserved

        # Handle version differences
        if version < 0x0101:
            volume = 127
            pitch = 1.0

        return InstParam(
            wave_index=wave_index,
            wave_data_location_type=WaveDataLocationType(wav_loc_type),
            attack=attack & 0xFF,
            decay=decay & 0xFF,
            sustain=sustain & 0xFF,
            release=release & 0xFF,
            hold=hold & 0xFF,
            note_off_type=NoteOffType(note_off_type),
            alternate_assign=alt_assign,
            original_key=original_key,
            volume=volume,
            pan=pan,
            surround_pan=surround_pan,
            pitch=pitch,
        )

    def _read_range_table(
            self,
            data: BinaryIO,
            content_base: int,
            version: int,
    ) -> RangeTable:
        table_start = data.tell()
        table_size = struct.unpack(">B", data.read(1))[0]
        bounds = list(struct.unpack(f">{table_size}B", data.read(table_size)))

        # References start after bounds, aligned to 4 bytes
        ref_start = table_start + _round_up(1 + table_size, 4)

        regions = []
        for i in range(table_size):
            data.seek(ref_start + i * 8)
            ref_type, ref_data_type, _, ref_offset = struct.unpack(">BBHI", data.read(8))
            region = self._read_region(data, ref_data_type, ref_offset, content_base, version)
            regions.append(region)

        return RangeTable(bounds=bounds, regions=regions)

    def _read_index_table(
            self,
            data: BinaryIO,
            content_base: int,
            version: int,
    ) -> IndexTable:
        min_idx, max_idx, _ = struct.unpack(">BBH", data.read(4))

        regions = []
        for _ in range(max_idx - min_idx + 1):
            ref_type, ref_data_type, _, ref_offset = struct.unpack(">BBHI", data.read(8))
            pos = data.tell()
            region = self._read_region(data, ref_data_type, ref_offset, content_base, version)
            regions.append(region)
            data.seek(pos)

        return IndexTable(min_index=min_idx, max_index=max_idx, regions=regions)