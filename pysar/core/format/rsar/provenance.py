"""Compact, optional PySAR provenance trailer for BRSAR archives.

Wire format (version 1)
"""

from __future__ import annotations

import struct
import zlib
from typing import BinaryIO

from pysar.core.model.brsar import ArchiveProvenance, PROVENANCE_ARITIES


TRAILER_MAGIC = b"PYSAR\0\0\0"
TRAILER_VERSION = 1
TRAILER_HEADER = struct.Struct(">8sBI")
TRAILER_CHECKSUM = struct.Struct(">I")

# Read-only compatibility with files emitted before the explicit PYSAR magic.
LEGACY_TRAILER_MAGIC = b"PSMD"
LEGACY_TRAILER_VERSION = 1
LEGACY_TRAILER_FOOTER = struct.Struct(">4sBBHII")
# Kept as an import-compatible alias for older integrations. New code should
# use TRAILER_HEADER/TRAILER_CHECKSUM; append_trailer never emits this footer.
TRAILER_FOOTER = LEGACY_TRAILER_FOOTER
MAX_TRAILER_SIZE = 1024 * 1024
MAX_IDENTITIES = 100_000

KIND_CODES: dict[str, int] = {
    "sound": 1,
    "bank": 2,
    "player": 3,
    "group": 4,
    "file": 5,
    "bank_instrument": 6,
    "bank_zone": 7,
    "wave": 8,
    "wsd_entry": 9,
    "group_item": 10,
}
CODE_KINDS = {value: key for key, value in KIND_CODES.items()}


def _write_varuint(value: int, out: bytearray) -> None:
    value = int(value)
    if value < 0:
        raise ValueError("Provenance IDs cannot be negative")
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def _read_varuint(payload: bytes, cursor: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(10):
        if cursor >= len(payload):
            raise ValueError("Truncated provenance varuint")
        byte = payload[cursor]
        cursor += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, cursor
        shift += 7
    raise ValueError("Oversized provenance varuint")


def encode_payload(provenance: ArchiveProvenance) -> bytes:
    out = bytearray()
    total_identities = 0
    for kind, code in sorted(KIND_CODES.items(), key=lambda item: item[1]):
        arity = PROVENANCE_ARITIES[kind]
        entries = sorted(provenance.entities.get(kind, set()))
        if not entries:
            continue
        total_identities += len(entries)
        if total_identities > MAX_IDENTITIES:
            raise ValueError("Provenance contains too many identities")
        out.extend((code, arity))
        _write_varuint(len(entries), out)
        for identity in entries:
            if len(identity) != arity:
                raise ValueError(
                    f"Provenance {kind} identity must have {arity} values"
                )
            for value in identity:
                _write_varuint(value, out)
    return bytes(out)


def decode_payload(payload: bytes) -> ArchiveProvenance:
    provenance = ArchiveProvenance(status="valid")
    cursor = 0
    total_identities = 0
    while cursor < len(payload):
        if cursor + 2 > len(payload):
            raise ValueError("Truncated provenance record")
        code, arity = payload[cursor], payload[cursor + 1]
        cursor += 2
        if arity == 0 or arity > 8:
            raise ValueError("Invalid provenance identity arity")
        count, cursor = _read_varuint(payload, cursor)
        total_identities += count
        if total_identities > MAX_IDENTITIES:
            raise ValueError("Provenance contains too many identities")
        # Every identity consumes at least one byte per component.
        if count > (len(payload) - cursor) // arity:
            raise ValueError("Invalid provenance record count")
        kind = CODE_KINDS.get(code)
        if kind is not None and PROVENANCE_ARITIES[kind] != arity:
            raise ValueError("Invalid provenance arity for known record")
        target = provenance.entries(kind) if kind is not None else None
        for _ in range(count):
            identity: list[int] = []
            for _ in range(arity):
                value, cursor = _read_varuint(payload, cursor)
                identity.append(value)
            if target is not None:
                target.add(tuple(identity))
    return provenance


def append_trailer(standard_brsar: bytes, provenance: ArchiveProvenance) -> bytes:
    payload = encode_payload(provenance)
    if not payload:
        return standard_brsar
    if len(payload) > MAX_TRAILER_SIZE:
        raise ValueError("Provenance trailer exceeds the 1 MiB safety limit")
    header = TRAILER_HEADER.pack(
        TRAILER_MAGIC,
        TRAILER_VERSION,
        len(payload),
    )
    checksum = TRAILER_CHECKSUM.pack(zlib.crc32(payload) & 0xFFFFFFFF)
    return standard_brsar + header + payload + checksum


def _decode_checked_payload(payload: bytes, checksum: int) -> ArchiveProvenance:
    if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
        raise ValueError("Invalid provenance checksum")
    return decode_payload(payload)


def _read_explicit_trailer(
    stream: BinaryIO,
    *,
    standard_end: int,
    physical_end: int,
) -> ArchiveProvenance | None:
    """Read the explicit trailer, or return None when its magic is absent."""
    trailing_size = physical_end - standard_end
    if trailing_size < len(TRAILER_MAGIC):
        return None

    stream.seek(standard_end)
    magic = stream.read(len(TRAILER_MAGIC))
    if magic != TRAILER_MAGIC:
        return None
    if trailing_size < TRAILER_HEADER.size + TRAILER_CHECKSUM.size:
        raise ValueError("Truncated provenance trailer")

    stream.seek(standard_end)
    header = stream.read(TRAILER_HEADER.size)
    magic, version, payload_size = TRAILER_HEADER.unpack(header)
    if magic != TRAILER_MAGIC or version != TRAILER_VERSION:
        raise ValueError("Unsupported provenance trailer")
    if payload_size <= 0 or payload_size > MAX_TRAILER_SIZE:
        raise ValueError("Invalid provenance payload size")

    expected_end = (
        standard_end + TRAILER_HEADER.size + payload_size
        + TRAILER_CHECKSUM.size
    )
    if expected_end != physical_end:
        raise ValueError("Provenance trailer does not fill trailing bytes")

    payload = stream.read(payload_size)
    checksum_raw = stream.read(TRAILER_CHECKSUM.size)
    if len(payload) != payload_size or len(checksum_raw) != TRAILER_CHECKSUM.size:
        raise ValueError("Truncated provenance trailer")
    checksum, = TRAILER_CHECKSUM.unpack(checksum_raw)
    return _decode_checked_payload(payload, checksum)


def _read_legacy_trailer(
    stream: BinaryIO,
    *,
    standard_end: int,
    physical_end: int,
) -> ArchiveProvenance | None:
    """Read the old payload+PSMD-footer form, or None if it is absent."""
    trailing_size = physical_end - standard_end
    if trailing_size < LEGACY_TRAILER_FOOTER.size:
        return None

    stream.seek(physical_end - LEGACY_TRAILER_FOOTER.size)
    footer = stream.read(LEGACY_TRAILER_FOOTER.size)
    magic, version, flags, reserved, payload_size, checksum = (
        LEGACY_TRAILER_FOOTER.unpack(footer)
    )
    if magic != LEGACY_TRAILER_MAGIC:
        return None
    if (
        version != LEGACY_TRAILER_VERSION
        or flags != 0
        or reserved != 0
        or payload_size <= 0
        or payload_size > MAX_TRAILER_SIZE
    ):
        raise ValueError("Invalid legacy provenance footer")

    payload_start = physical_end - LEGACY_TRAILER_FOOTER.size - payload_size
    if payload_start != standard_end:
        raise ValueError("Legacy provenance trailer does not fill trailing bytes")
    stream.seek(payload_start)
    payload = stream.read(payload_size)
    if len(payload) != payload_size:
        raise ValueError("Truncated legacy provenance trailer")
    return _decode_checked_payload(payload, checksum)


def read_trailer(
    stream: BinaryIO,
    *,
    base_offset: int,
    declared_size: int,
) -> ArchiveProvenance:
    """Read a trailer only when it begins exactly at the NW4R file end."""
    fallback = ArchiveProvenance(status="absent")
    restore = stream.tell()
    try:
        base_offset = int(base_offset)
        declared_size = int(declared_size)
        if base_offset < 0 or declared_size < 0:
            return ArchiveProvenance(status="invalid")
        stream.seek(0, 2)
        physical_end = stream.tell()
        standard_end = base_offset + declared_size
        if physical_end < standard_end:
            return ArchiveProvenance(status="invalid")
        if physical_end == standard_end:
            return fallback

        explicit = _read_explicit_trailer(
            stream, standard_end=standard_end, physical_end=physical_end,
        )
        if explicit is not None:
            return explicit

        legacy = _read_legacy_trailer(
            stream, standard_end=standard_end, physical_end=physical_end,
        )
        return fallback if legacy is None else legacy
    except (OSError, ValueError, struct.error):
        return ArchiveProvenance(status="invalid")
    finally:
        try:
            stream.seek(restore)
        except (OSError, ValueError):
            pass
