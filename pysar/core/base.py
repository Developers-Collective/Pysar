"""Shared base types for NW4R readers, writers, and editors."""

from dataclasses import dataclass
from enum import Flag, auto
from typing import BinaryIO, Protocol, Self, TypeVar, runtime_checkable

T = TypeVar("T")


# Protocols

@runtime_checkable
class Readable(Protocol[T]):
    def read(self, data: BinaryIO) -> T: ...

@runtime_checkable
class Writable(Protocol[T]):
    def write(self, model: T, output: BinaryIO) -> None: ...

@runtime_checkable
class Editable(Protocol):
    @property
    def is_dirty(self) -> bool: ...
    def to_bytes(self) -> bytes: ...
    def save(self, path: str) -> Self: ...
# Common NW4R headers

@dataclass(frozen=True)
class NW4RFileHeader:        # 16 bytes
    magic: str
    byte_order: int
    version: int
    file_size: int
    header_size: int
    n_sections: int


@dataclass(frozen=True)
class NW4RSectionHeader:    # 8 bytes
    magic: str
    size: int

# Generic change tracking used by editable NW4R files.

class DirtyFlags(Flag):
    NONE = 0
    HEADER = auto()
    METADATA = auto()
    DATA = auto()
    ALL = HEADER | METADATA | DATA
# Base helpers for readers, writers, and editors.

class ReaderBase:
    EXPECTED_MAGIC: str = ""
    SUPPORTED_VERSIONS: set[int] = set()

    def _validate(self, header: NW4RFileHeader) -> None:
        from pysar.io.binary import check_file
        check_file(
            header.magic, header.byte_order, header.version,
            self.EXPECTED_MAGIC, self.SUPPORTED_VERSIONS
        )

    @classmethod
    def from_file(cls, path: str) -> T:
        with open(path, 'rb') as f:
            return cls().read(f)


class WriterBase:
    def to_bytes(self, model: T) -> bytes:
        import io
        buf = io.BytesIO()
        self.write(model, buf)
        return buf.getvalue()

    def to_file(self, model: T, path: str) -> None:
        with open(path, 'wb') as f:
            self.write(model, f)


class EditorBase:
    def __init__(self, raw: bytes | None = None):
        self._raw = raw
        self._dirty = DirtyFlags.NONE

    @property
    def is_dirty(self) -> bool:
        return self._dirty != DirtyFlags.NONE

    def mark_dirty(self, flags: DirtyFlags) -> None:
        self._dirty |= flags

    def clear_dirty(self) -> None:
        self._dirty = DirtyFlags.NONE

    def save(self, path: str) -> Self:
        from pathlib import Path
        Path(path).write_bytes(self.to_bytes())
        self.clear_dirty()
        return self