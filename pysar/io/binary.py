import struct
from typing import BinaryIO

from pysar.core.base import NW4RFileHeader, NW4RSectionHeader
from pysar.core.types import ByteOrder, Reference
from pysar.core.exceptions import (
    NW4RInvalidFileError,
    NW4RUnsupportedVersionError,
    NW4RByteOrderError,
)


def read_file_header(data: BinaryIO) -> NW4RFileHeader:
    """Read a standard NW4R file header (16 bytes)."""
    raw = data.read(16)
    magic, bom, version, file_size, header_size, n_sections = struct.unpack(
        ">4sHHIHH", raw
    )
    return NW4RFileHeader(
        magic=magic.decode("ascii"),
        byte_order=bom,
        version=version,
        file_size=file_size,
        header_size=header_size,
        n_sections=n_sections,
    )


def write_file_header(header: NW4RFileHeader, output: BinaryIO) -> None:
    """Write a standard NW4R file header (16 bytes)."""
    output.write(
        struct.pack(
            ">4sHHIHH",
            header.magic.encode("ascii"),
            header.byte_order,
            header.version,
            header.file_size,
            header.header_size,
            header.n_sections,
        )
    )


def read_section_header(data: BinaryIO) -> NW4RSectionHeader:
    """Read a standard NW4R section header (8 bytes)."""
    raw = data.read(8)
    magic, size = struct.unpack(">4sI", raw)
    return NW4RSectionHeader(magic=magic.decode("ascii"), size=size)


def write_section_header(header: NW4RSectionHeader, output: BinaryIO) -> None:
    """Write a standard NW4R section header (8 bytes)."""
    output.write(struct.pack(">4sI", header.magic.encode("ascii"), header.size))


def read_reference(data: BinaryIO) -> Reference:
    """Read a NW4R reference/pointer structure (8 bytes)."""
    raw = data.read(8)
    flag, data_type, _, offset = struct.unpack(">BBHI", raw)
    return Reference(flag=flag, data_type=data_type, offset=offset)


def write_reference(ref: Reference, output: BinaryIO) -> None:
    """Write a NW4R reference/pointer structure (8 bytes)."""
    output.write(struct.pack(">BBHI", ref.flag, ref.data_type, 0, ref.offset))


def check_file(
    magic: str,
    byte_order: int,
    version: int,
    expected_magic: str,
    supported_versions: set[int],
) -> None:
    """
    Validate a NW4R file header.

    Raises appropriate exceptions for invalid files.
    """
    if magic != expected_magic:
        raise NW4RInvalidFileError(
            f"Invalid magic: expected '{expected_magic}', got '{magic}'"
        )

    if byte_order not in (ByteOrder.BIG_ENDIAN, ByteOrder.LITTLE_ENDIAN):
        raise NW4RByteOrderError(f"Invalid byte order marker: 0x{byte_order:04X}")

    if version not in supported_versions:
        versions_str = ", ".join(f"0x{v:04X}" for v in sorted(supported_versions))
        raise NW4RUnsupportedVersionError(
            f"Unsupported version 0x{version:04X}. Supported: {versions_str}"
        )


def align_up(value: int, alignment: int) -> int:
    """Align a value up to the next multiple of alignment."""
    if alignment == 0:
        return value
    remainder = value % alignment
    if remainder == 0:
        return value
    return value + (alignment - remainder)


def pad_to_alignment(output: BinaryIO, alignment: int) -> int:
    """Write padding bytes to align the stream position."""
    pos = output.tell()
    aligned = align_up(pos, alignment)
    padding = aligned - pos
    if padding > 0:
        output.write(b"\x00" * padding)
    return padding


def int_align(value: int, align: int) -> int:
    """
    Aligns an integer value by the given alignment.
    :param value: The value to align.
    :param align: The alignment to use for the value.
    """
    return value if align <= 0 else (value + align - 1) // align * align


def read_terminated_string(data: BinaryIO, encoding: str = 'ascii') -> str:
    """Read a null-terminated string from the given stream."""
    result = bytearray()
    while True:
        char = data.read(1)
        if not char or char == b'\x00':
            break
        result += char
    return result.decode(encoding)


def read_offset_from_ref(data: BinaryIO, base_offset: int) -> int:
    """Read a reference and resolve it to an absolute offset.

    Returns -1 if the reference is null.
    """
    ref = read_reference(data)
    if ref.flag == 0 and ref.data_type == 0 and ref.offset == 0:
        return -1
    if ref.flag == 1:  # relative
        return base_offset + 8 + ref.offset
    return ref.offset