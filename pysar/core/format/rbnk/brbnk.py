from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Callable, Self

from pysar.core.base import EditorBase, DirtyFlags
from pysar.core.format.rbnk.reader import BrbnkReader
from pysar.core.format.rbnk.sf2 import brbnk_to_sf2, save_sf2
from pysar.core.format.rbnk.writer import BrbnkWriter
from pysar.core.model.brbnk import (
    BrbnkData,
    Instrument,
    InstrumentRegion,
    InstParam,
    NoteOffType,
    RangeTable,
    RegionType,
)


class Brbnk(EditorBase):
    """
    High-level editor for BRBNK instrument banks.
    """

    def __init__(self, data: BrbnkData | None = None):
        super().__init__()
        self._data = data or BrbnkData()

    #
    # Factory methods
    #

    @classmethod
    def new(cls) -> Self:
        """Create a new empty bank."""
        editor = cls(BrbnkData())
        editor.mark_dirty(DirtyFlags.ALL)
        return editor

    @classmethod
    def open(cls, path: str | Path) -> Self:
        """Open a BRBNK file from disk."""
        reader = BrbnkReader()
        data = reader.from_file(str(path))
        return cls(data)

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        """Load a BRBNK from raw bytes."""
        reader = BrbnkReader()
        data = reader.read(BytesIO(raw))
        return cls(data)

    @classmethod
    def from_stream(cls, stream: BinaryIO) -> Self:
        """Load a BRBNK from a binary stream."""
        reader = BrbnkReader()
        data = reader.read(stream)
        return cls(data)

    #
    # Properties
    #

    @property
    def data(self) -> BrbnkData:
        return self._data

    @property
    def instruments(self) -> list[Instrument]:
        return self._data.instruments

    @property
    def n_instruments(self) -> int:
        return len(self._data.instruments)

    @property
    def n_active_instruments(self) -> int:
        """Number of non-empty instruments."""
        return sum(1 for i in self._data.instruments if not i.is_empty())

    #
    # Getters
    #

    def __len__(self) -> int:
        return len(self._data.instruments)

    def __getitem__(self, program: int) -> Instrument:
        return self._data[program]

    def get_inst_param(
            self,
            program: int,
            key: int = 60,
            velocity: int = 127,
    ) -> InstParam | None:
        """
        Get the instrument parameter for a specific note.

        This simulates how the sound engine selects samples.

        Args:
            program: Program/instrument number
            key: MIDI key (0-127)
            velocity: MIDI velocity (0-127)

        Returns:
            InstParam or None if not found
        """
        return self._data.get_inst_param(program, key, velocity)

    def get_all_inst_params(self, program: int) -> list[InstParam]:
        """
        Get all InstParams for a program (ignoring key/velocity splits).

        Useful for extracting all samples used by an instrument.
        """
        if program < 0 or program >= len(self._data.instruments):
            return []
        return [p for p, _, _ in self._data.instruments[program].get_all_inst_params()]

    def get_wave_indices(self) -> set[int]:
        """Get all unique wave indices used by this bank."""
        return self._data.get_all_wave_indices()

    def get_variation_notes(self, program: int) -> list[int]:
        """
        Get a MIDI note for each key-range variation of an instrument.

        Uses the upper bound of each zone's key range (capped at 126 to avoid
        FluidSynth issues at MIDI note 127). This keeps each probe note near the
        top of its zone, which gives stable results for variation playback.

        For bounds=[68, 80, 127] the zones and notes are:
          Zone 0 (0-68):   note=68
          Zone 1 (69-80):  note=80
          Zone 2 (81-127): note=126  (capped from 127)

        Args:
            program: Program/instrument number.

        Returns:
            List of MIDI notes (one per variation) that select each zone.
            Empty list if program is invalid or empty.
        """
        if program < 0 or program >= len(self._data.instruments):
            return []

        inst = self._data.instruments[program]
        if inst.is_empty():
            return []

        root = inst.root_region

        if root.region_type == RegionType.DIRECT:
            return [60]

        if root.region_type == RegionType.RANGE and root.range_table:
            return [min(b, 126) for b in root.range_table.bounds]

        if root.region_type == RegionType.INDEX and root.index_table:
            tbl = root.index_table
            return list(range(tbl.min_index, tbl.max_index + 1))

        return [60]

    #
    # Operations
    #

    def add_instrument(
            self,
            program: int,
            param: InstParam | InstrumentRegion,
    ) -> None:
        """
        Add or replace an instrument at the given program number.

        Args:
            program: Program number (0-127+)
            param: InstParam for simple instrument, or InstrumentRegion for complex
        """
        # Expand instruments list if needed
        while len(self._data.instruments) <= program:
            self._data.instruments.append(Instrument(
                program=len(self._data.instruments),
                root_region=InstrumentRegion.null()
            ))

        if isinstance(param, InstParam):
            region = InstrumentRegion.direct(param)
        else:
            region = param

        self._data.instruments[program] = Instrument(
            program=program,
            root_region=region,
        )
        self.mark_dirty(DirtyFlags.ALL)

    def remove_instrument(self, program: int) -> None:
        """Remove an instrument (set to null)."""
        if 0 <= program < len(self._data.instruments):
            self._data.instruments[program] = Instrument(
                program=program,
                root_region=InstrumentRegion.null()
            )
            self.mark_dirty(DirtyFlags.ALL)

    def set_instrument_program(self, old_program: int, new_program: int) -> None:
        """Move an instrument to a different program slot."""
        if old_program < 0 or old_program >= len(self._data.instruments):
            raise ValueError(f"Invalid program {old_program}")
        inst = self._data.instruments[old_program]
        if inst.is_empty():
            raise ValueError(f"Program {old_program} is empty")
        while len(self._data.instruments) <= new_program:
            self._data.instruments.append(Instrument(
                program=len(self._data.instruments),
                root_region=InstrumentRegion.null()
            ))
        if not self._data.instruments[new_program].is_empty():
            raise ValueError(f"Program {new_program} is already occupied")
        self._data.instruments[old_program] = Instrument(
            program=old_program, root_region=InstrumentRegion.null()
        )
        self._data.instruments[new_program] = Instrument(
            program=new_program, root_region=inst.root_region, name=inst.name
        )
        self.mark_dirty(DirtyFlags.ALL)

    def update_zone(self, program: int, zone_index: int, patch: dict) -> None:
        """Update properties of a specific zone (InstParam) by index."""
        if program < 0 or program >= len(self._data.instruments):
            raise ValueError(f"Invalid program {program}")
        inst = self._data.instruments[program]
        if inst.is_empty():
            raise ValueError(f"Program {program} is empty")
        params = inst.get_all_inst_params()
        if zone_index < 0 or zone_index >= len(params):
            raise ValueError(f"Invalid zone index {zone_index}")
        param, _, _ = params[zone_index]
        if "waveIndex" in patch:
            param.wave_index = max(0, int(patch["waveIndex"]))
        if "originalKey" in patch:
            param.original_key = max(0, min(127, int(patch["originalKey"])))
        if "volume" in patch:
            param.volume = max(0, min(127, int(patch["volume"])))
        if "pan" in patch:
            param.pan = max(0, min(127, int(patch["pan"])))
        if "pitch" in patch:
            param.pitch = max(0.0, min(16.0, float(patch["pitch"])))
        if "attack" in patch:
            param.attack = max(0, min(127, int(patch["attack"])))
        if "decay" in patch:
            param.decay = max(0, min(127, int(patch["decay"])))
        if "sustain" in patch:
            param.sustain = max(0, min(127, int(patch["sustain"])))
        if "release" in patch:
            param.release = max(0, min(127, int(patch["release"])))
        if "hold" in patch:
            param.hold = max(0, min(127, int(patch["hold"])))
        if "noteOffType" in patch:
            param.note_off_type = NoteOffType(max(0, min(2, int(patch["noteOffType"]))))
        if "alternateAssign" in patch:
            param.alternate_assign = max(0, min(255, int(patch["alternateAssign"])))
        self.mark_dirty(DirtyFlags.ALL)

    def split_key_region(self, program: int, at_key: int) -> None:
        """Split a key region at the given key boundary."""
        if program < 0 or program >= len(self._data.instruments):
            raise ValueError(f"Invalid program {program}")
        inst = self._data.instruments[program]
        if inst.is_empty():
            raise ValueError(f"Program {program} is empty")
        root = inst.root_region
        if root.region_type == RegionType.DIRECT:
            old_param = root.inst_param
            left_param = InstParam(**vars(old_param))
            right_param = InstParam(**vars(old_param))
            table = RangeTable(
                bounds=[at_key, 127],
                regions=[
                    InstrumentRegion.direct(left_param),
                    InstrumentRegion.direct(right_param),
                ],
            )
            inst.root_region = InstrumentRegion.range(table)
            self.mark_dirty(DirtyFlags.ALL)
            return
        if root.region_type == RegionType.RANGE and root.range_table:
            rt = root.range_table
            for i, bound in enumerate(rt.bounds):
                if at_key <= bound:
                    sub = rt.regions[i]
                    if sub.region_type == RegionType.DIRECT and sub.inst_param:
                        left_param = InstParam(**vars(sub.inst_param))
                        right_param = InstParam(**vars(sub.inst_param))
                        new_bounds = rt.bounds[:i] + [at_key, bound] + rt.bounds[i + 1:]
                        new_regions = rt.regions[:i] + [
                            InstrumentRegion.direct(left_param),
                            InstrumentRegion.direct(right_param),
                        ] + rt.regions[i + 1:]
                        rt.bounds = new_bounds
                        rt.regions = new_regions
                        self.mark_dirty(DirtyFlags.ALL)
                        return
                    break
            raise ValueError("Cannot split: region is not a direct leaf")
        raise ValueError("Cannot split: unsupported region structure")

    def delete_zone(self, program: int, zone_index: int) -> None:
        """Delete a zone by merging its range into a neighbor."""
        if program < 0 or program >= len(self._data.instruments):
            raise ValueError(f"Invalid program {program}")
        inst = self._data.instruments[program]
        if inst.is_empty():
            raise ValueError(f"Program {program} is empty")
        root = inst.root_region
        if root.region_type == RegionType.DIRECT:
            inst.root_region = InstrumentRegion.null()
            self.mark_dirty(DirtyFlags.ALL)
            return
        if root.region_type == RegionType.RANGE and root.range_table:
            rt = root.range_table
            if zone_index < 0 or zone_index >= len(rt.regions):
                raise ValueError(f"Invalid zone index {zone_index}")
            if len(rt.regions) <= 1:
                inst.root_region = InstrumentRegion.null()
                self.mark_dirty(DirtyFlags.ALL)
                return
            if len(rt.regions) == 2:
                remaining = rt.regions[1 - zone_index]
                inst.root_region = remaining
                self.mark_dirty(DirtyFlags.ALL)
                return
            # Expand neighbor to cover the deleted range
            if zone_index < len(rt.bounds):
                rt.bounds.pop(zone_index)
            else:
                rt.bounds.pop(zone_index - 1)
            rt.regions.pop(zone_index)
            self.mark_dirty(DirtyFlags.ALL)
            return
        raise ValueError("Cannot delete: unsupported region structure")

    def resize_key_region(self, program: int, zone_index: int, new_high: int) -> None:
        """Resize a key region's upper bound (adjusts the neighbor)."""
        if program < 0 or program >= len(self._data.instruments):
            raise ValueError(f"Invalid program {program}")
        inst = self._data.instruments[program]
        if inst.is_empty():
            raise ValueError(f"Program {program} is empty")
        root = inst.root_region
        if root.region_type != RegionType.RANGE or not root.range_table:
            raise ValueError("Cannot resize: not a range-based instrument")
        rt = root.range_table
        if zone_index < 0 or zone_index >= len(rt.bounds):
            raise ValueError(f"Invalid zone index {zone_index}")
        new_high = max(0, min(127, int(new_high)))
        rt.bounds[zone_index] = new_high
        self.mark_dirty(DirtyFlags.ALL)

    #
    # SF2 Export
    #

    def export_sf2(
            self,
            output_path: str | Path,
            brwar: "Brwar | None" = None,
            bank_name: str | None = None,
            single_zone: tuple[int, int] | None = None,
            cancel_callback: Callable[[], bool] | None = None,
    ) -> Path:
        """
        Export the bank to SF2 (SoundFont 2) format.

        Args:
            output_path: Output SF2 file path
            brwar: BRWAR containing the wave data (required for samples)
            bank_name: Optional name for the soundfont
            single_zone: If set, (program, zone_index), only export that
                         single zone mapped to full key range 0-127.

        Returns:
            Path to the created SF2 file
        """
        output_path = Path(output_path)
        name = bank_name or output_path.stem

        sf2_data = brbnk_to_sf2(
            self._data,
            brwar,
            name,
            single_zone=single_zone,
            cancel_callback=cancel_callback,
        )
        if cancel_callback is None:
            save_sf2(sf2_data, output_path)
        else:
            partial_path = output_path.with_name(f".{output_path.name}.partial")
            try:
                save_sf2(sf2_data, partial_path, cancel_callback=cancel_callback)
                partial_path.replace(output_path)
            except Exception:
                partial_path.unlink(missing_ok=True)
                raise

        return output_path

    #
    # Serialization
    #

    def to_bytes(self) -> bytes:
        """Serialize to BRBNK bytes."""
        writer = BrbnkWriter()
        return writer.to_bytes(self._data)

    def save(self, path: str | Path) -> Self:
        """Save to a BRBNK file."""
        Path(path).write_bytes(self.to_bytes())
        self.clear_dirty()
        return self

    #
    # Info/Debug
    #

    def summary(self) -> str:
        """Get a detailed summary of the bank contents."""
        lines = [
            f"BRBNK Bank: {self.n_active_instruments}/{self.n_instruments} active instruments",
            f"Version: 0x{self._data.version:04X}",
            "=" * 50,
        ]

        for inst in self._data.instruments:
            if inst.is_empty():
                continue

            params = inst.get_all_inst_params()
            waves = set(p.wave_index for p, _, _ in params)

            lines.append(
                f"  [{inst.program:3d}] {len(params)} zones, "
                f"waves: {sorted(waves)}"
            )

        return "\n".join(lines)

    def __str__(self) -> str:
        return f"Brbnk({self.n_active_instruments}/{self.n_instruments} instruments)"

    def __repr__(self) -> str:
        return self.__str__()
