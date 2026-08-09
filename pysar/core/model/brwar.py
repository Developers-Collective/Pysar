from dataclasses import dataclass, field

from pysar.core.model.brwav import BrwavData


@dataclass
class BrwarEntry:
    brwav: BrwavData

    # Original offset in the DATA section (for reference lookups)
    original_offset: int = 0

    # Original size (may differ from serialized size after edits)
    original_size: int = 0


@dataclass
class BrwarData:
    version: int = 0x0100

    # List of BRWAV entries
    entries: list[BrwarEntry] = field(default_factory=list)

    # Raw bytes for pass-through (if unmodified)
    raw_bytes: bytes | None = None

    def __len__(self) -> int:
        return len(self.entries)

    def __getitem__(self, index: int) -> BrwarEntry:
        return self.entries[index]

    def __iter__(self):
        return iter(self.entries)

    def __str__(self) -> str:
        return f"BrwarData({len(self.entries)} entries)"

    def __repr__(self) -> str:
        return self.__str__()