import struct
from typing import BinaryIO

from pysar.core import NW4RInvalidFileError, NW4RUnsupportedVersionError
from pysar.core.base import ReaderBase
from pysar.core.format.rwav.reader import BrwavReader
from pysar.core.model.brwar import BrwarData, BrwarEntry
from pysar.core.types import FileTag


class BrwarReader(ReaderBase):
    EXPECTED_MAGIC = FileTag.BRWAR
    SUPPORTED_VERSIONS = {0x0100}

    def read(self, data: BinaryIO) -> BrwarData:
        """Read a BRWAR file from a binary stream."""
        base_offset = data.tell()

        # Read file header (0x20 bytes)
        header = data.read(0x20)
        if len(header) < 0x20 or header[:4] != b"RWAR":
            raise NW4RInvalidFileError("Not a valid BRWAR file")

        (
            bom, version, file_size, header_size, n_sections,
            tabl_offset, tabl_size,
            data_offset, data_size,
        ) = struct.unpack(">HHIHH IIII", header[4:0x20])

        if version not in self.SUPPORTED_VERSIONS:
            raise NW4RUnsupportedVersionError(
                f"Unsupported BRWAR version: 0x{version:04X}"
            )

        # Read TABL section
        data.seek(base_offset + tabl_offset)
        tabl_header = data.read(8)
        if tabl_header[:4] != b"TABL":
            raise NW4RInvalidFileError("TABL section missing")

        n_entries = struct.unpack(">I", data.read(4))[0]

        # Read table entries (reference + size for each)
        table_entries = []
        for _ in range(n_entries):
            ref_type, _, _, ref_offset, wav_size = struct.unpack(">BBHII", data.read(12))
            table_entries.append((ref_offset, wav_size))

        # Read DATA section and parse BRWAVs
        data.seek(base_offset + data_offset)
        data_header = data.read(8)
        if data_header[:4] != b"DATA":
            raise NW4RInvalidFileError("DATA section missing")

        data_section_start = base_offset + data_offset

        # Skip DATA header padding (0x18 bytes after the 8-byte header)
        data.seek(data_section_start + 0x20)

        # Parse each BRWAV
        brwav_reader = BrwavReader()
        entries = []

        for ref_offset, wav_size in table_entries:
            # Offset is relative to DATA section start
            wav_offset = data_section_start + ref_offset
            data.seek(wav_offset)

            # Read the BRWAV
            brwav_data = brwav_reader.read(data)

            entries.append(BrwarEntry(
                brwav=brwav_data,
                original_offset=ref_offset,
                original_size=wav_size,
            ))

        # Capture raw bytes
        data.seek(base_offset)
        raw_bytes = data.read(file_size)

        return BrwarData(
            version=version,
            entries=entries,
            raw_bytes=raw_bytes,
        )