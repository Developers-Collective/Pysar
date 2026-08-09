from dataclasses import dataclass, field
from enum import IntEnum
from typing import Self

class WaveDataLocationType(IntEnum):
    INDEX = 0       # Index into BRWAR
    ADDRESS = 1     # Direct memory address
    CALLBACK = 2    # Runtime callback function


class NoteOffType(IntEnum):
    RELEASE = 0     # Normal release envelope
    IGNORE = 1      # Ignore note-off (play full sample)
    CUT = 2         # Immediate cut (no release phase)


class RegionType(IntEnum):
    INVALID = 0
    DIRECT = 1      # Direct reference to InstParam
    RANGE = 2       # Range table (key/velocity splits by upper bound)
    INDEX = 3       # Index table (key/velocity splits by direct index)
    NULL = 4        # No instrument


@dataclass
class InstParam:
    """
    Defines how a single sample is played.
    """
    # Wave reference
    wave_index: int = 0
    wave_data_location_type: WaveDataLocationType = WaveDataLocationType.INDEX

    # ADSR envelope (0-127 range, converted to time internally)
    attack: int = 0
    decay: int = 0
    sustain: int = 127
    release: int = 0
    hold: int = 0

    # Playback parameters
    note_off_type: NoteOffType = NoteOffType.RELEASE
    alternate_assign: int = 0
    original_key: int = 60  # Middle C

    # Mixing
    volume: int = 127
    pan: int = 64  # Center
    surround_pan: int = 0

    # Pitch adjustment (1.0 = no change)
    pitch: float = 1.0

    def __str__(self) -> str:
        return (
            f"InstParam(wave={self.wave_index}, key={self.original_key}, "
            f"vol={self.volume}, ADSR={self.attack}/{self.decay}/{self.sustain}/{self.release})"
        )


@dataclass
class RangeTable:
    """
    Range-based region table.

    Maps key/velocity ranges to instrument parameters.
    Each entry defines an upper bound, with ranges being:
    [0, bounds[0]], [bounds[0]+1, bounds[1]], etc.
    """
    # Upper bounds for each range
    bounds: list[int] = field(default_factory=list)

    # Instruments/sub-regions for each range
    regions: list["InstrumentRegion"] = field(default_factory=list)

    def get_region_for_value(self, value: int) -> "InstrumentRegion | None":
        """Find the region containing the given value."""
        for i, bound in enumerate(self.bounds):
            if value <= bound:
                if i < len(self.regions):
                    return self.regions[i]
                return None
        return None

    def get_ranges(self) -> list[tuple[int, int]]:
        """Get (min, max) tuples for each range."""
        ranges = []
        start = 0
        for bound in self.bounds:
            ranges.append((start, bound))
            start = bound + 1
        return ranges


@dataclass
class IndexTable:
    """
    Index-based region table.

    Maps a continuous range of indices to instrument parameters.
    """
    min_index: int = 0
    max_index: int = 127

    # Instruments/sub-regions for each index
    regions: list["InstrumentRegion"] = field(default_factory=list)

    def get_region_for_value(self, value: int) -> "InstrumentRegion | None":
        """Find the region for the given index."""
        if value < self.min_index or value > self.max_index:
            return None
        idx = value - self.min_index
        if idx < len(self.regions):
            return self.regions[idx]
        return None


@dataclass
class InstrumentRegion:
    region_type: RegionType = RegionType.NULL

    # Only one of these is set based on region_type
    inst_param: InstParam | None = None
    range_table: RangeTable | None = None
    index_table: IndexTable | None = None

    @classmethod
    def direct(cls, param: InstParam) -> Self:
        """Create a direct region containing an InstParam."""
        return cls(region_type=RegionType.DIRECT, inst_param=param)

    @classmethod
    def range(cls, table: RangeTable) -> Self:
        """Create a range region."""
        return cls(region_type=RegionType.RANGE, range_table=table)

    @classmethod
    def index(cls, table: IndexTable) -> Self:
        """Create an index region."""
        return cls(region_type=RegionType.INDEX, index_table=table)

    @classmethod
    def null(cls) -> Self:
        """Create an empty region."""
        return cls(region_type=RegionType.NULL)

    def is_direct(self) -> bool:
        return self.region_type == RegionType.DIRECT

    def is_null(self) -> bool:
        return self.region_type == RegionType.NULL or self.region_type == RegionType.INVALID

    @staticmethod
    def _merge_ranges(
            a: tuple[int, int] | None,
            b: tuple[int, int] | None,
    ) -> tuple[int, int] | None:
        if a is None:
            return b
        if b is None:
            return a
        lo = max(a[0], b[0])
        hi = min(a[1], b[1])
        if lo > hi:
            return None
        return lo, hi

    def get_inst_param(
            self,
            key: int,
            velocity: int,
            split_axis: str = "key",
    ) -> InstParam | None:
        """
        Recursively resolve to an InstParam for the given key/velocity.
        This mimics the sound engine's lookup process.
        """
        if self.region_type == RegionType.DIRECT:
            return self.inst_param

        elif self.region_type == RegionType.RANGE:
            if self.range_table is None:
                return None
            lookup_value = key if split_axis == "key" else velocity
            sub_region = self.range_table.get_region_for_value(lookup_value)
            if sub_region is None:
                return None
            next_axis = "velocity" if split_axis == "key" else "key"
            return sub_region.get_inst_param(key, velocity, split_axis=next_axis)

        elif self.region_type == RegionType.INDEX:
            if self.index_table is None:
                return None
            lookup_value = key if split_axis == "key" else velocity
            sub_region = self.index_table.get_region_for_value(lookup_value)
            if sub_region is None:
                return None
            next_axis = "velocity" if split_axis == "key" else "key"
            return sub_region.get_inst_param(key, velocity, split_axis=next_axis)

        return None

    def get_all_inst_params(
            self,
            split_axis: str = "key",
    ) -> list[tuple[InstParam, tuple[int, int] | None, tuple[int, int] | None]]:
        """
        Get all InstParams in this region with their key/velocity ranges.
        Returns list of (InstParam, key_range, vel_range) tuples,
        where each range is (lo, hi) inclusive or None if unconstrained.
        """
        results = []

        if self.region_type == RegionType.DIRECT:
            if self.inst_param:
                results.append((self.inst_param, None, None))

        elif self.region_type == RegionType.RANGE:
            if self.range_table:
                ranges = self.range_table.get_ranges()
                for i, sub_region in enumerate(self.range_table.regions):
                    span = ranges[i] if i < len(ranges) else (0, 127)
                    next_axis = "velocity" if split_axis == "key" else "key"
                    for param, key_range, vel_range in sub_region.get_all_inst_params(split_axis=next_axis):
                        if split_axis == "key":
                            merged_key = self._merge_ranges(key_range, span)
                            if merged_key is not None:
                                results.append((param, merged_key, vel_range))
                        else:
                            merged_vel = self._merge_ranges(vel_range, span)
                            if merged_vel is not None:
                                results.append((param, key_range, merged_vel))

        elif self.region_type == RegionType.INDEX:
            if self.index_table:
                for i, sub_region in enumerate(self.index_table.regions):
                    idx = self.index_table.min_index + i
                    span = (idx, idx)
                    next_axis = "velocity" if split_axis == "key" else "key"
                    for param, key_range, vel_range in sub_region.get_all_inst_params(split_axis=next_axis):
                        if split_axis == "key":
                            merged_key = self._merge_ranges(key_range, span)
                            if merged_key is not None:
                                results.append((param, merged_key, vel_range))
                        else:
                            merged_vel = self._merge_ranges(vel_range, span)
                            if merged_vel is not None:
                                results.append((param, key_range, merged_vel))

        return results


@dataclass
class Instrument:
    """
    A single instrument/program in the bank.

    Contains the root region which may have sub-regions for
    key and velocity splits.
    """
    # Program number
    program: int = 0

    # Root region (key splits, then velocity splits, then InstParam)
    root_region: InstrumentRegion = field(default_factory=InstrumentRegion.null)

    # Optional name (for SF2 compatibility)
    name: str = ""

    def get_inst_param(self, key: int, velocity: int) -> InstParam | None:
        """Get the InstParam for a specific key/velocity."""
        return self.root_region.get_inst_param(key, velocity)

    def get_all_inst_params(
            self,
    ) -> list[tuple[InstParam, tuple[int, int] | None, tuple[int, int] | None]]:
        """Get all InstParams in this instrument."""
        return self.root_region.get_all_inst_params()

    def is_empty(self) -> bool:
        """Check if this instrument has no regions."""
        return self.root_region.is_null()


@dataclass
class BrbnkData:
    version: int = 0x0102

    # List of instruments (indexed by program number)
    instruments: list[Instrument] = field(default_factory=list)

    # Embedded wave data (for versions < 1.2)
    # In v1.2+, waves are in external BRWAR
    #
    # We have no current impl for them specifically,
    # so we store them as raw bytes
    embedded_waves: list[bytes] = field(default_factory=list)
    has_embedded_waves: bool = False

    # Raw bytes for pass-through
    raw_bytes: bytes | None = None

    def __len__(self) -> int:
        return len(self.instruments)

    def __getitem__(self, program: int) -> Instrument:
        if 0 <= program < len(self.instruments):
            return self.instruments[program]
        raise KeyError(f"Program {program} not found")

    def get_inst_param(self, program: int, key: int, velocity: int) -> InstParam | None:
        """Get InstParam for a specific program/key/velocity combination."""
        if program < 0 or program >= len(self.instruments):
            return None
        return self.instruments[program].get_inst_param(key, velocity)

    def get_all_wave_indices(self) -> set[int]:
        """Get all unique wave indices referenced by this bank."""
        indices = set()
        for inst in self.instruments:
            for param, _, _ in inst.get_all_inst_params():
                if param.wave_data_location_type == WaveDataLocationType.INDEX:
                    indices.add(param.wave_index)
        return indices

    def __str__(self) -> str:
        non_empty = sum(1 for i in self.instruments if not i.is_empty())
        return f"BrbnkData(v{self.version:04X}, {non_empty}/{len(self.instruments)} instruments)"