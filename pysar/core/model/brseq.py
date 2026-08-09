from dataclasses import dataclass, field
from enum import IntEnum
from typing import Self, Any

from pysar.core.format.rseq.mml import MML, MMLEX, is_note, note_to_name

_MML_VALUES = frozenset(member.value for member in MML)


@dataclass
class Prefix:
    """Prefix modifier for a command."""
    type: MML  # RANDOM, VARIABLE, TIME, TIME_RANDOM, TIME_VARIABLE, IF
    args: list[int] = field(default_factory=list)


@dataclass
class Command:
    opcode: int  # MML, MMLEX, or note (0-127)
    args: list[int] = field(default_factory=list)
    prefixes: list[Prefix] = field(default_factory=list)

    # Source location tracking
    offset: int = 0  # Byte offset in DATA block

    raw_bytes: bytes | None = field(default=None, repr=False, compare=False)
    _is_extended: bool = field(default=False, repr=False, compare=False)

    @property
    def is_note(self) -> bool:
        return is_note(self.opcode)

    @property
    def is_extended(self) -> bool:
        if getattr(self, '_is_extended', False):
            return True
        return isinstance(self.opcode, MMLEX) or (
                isinstance(self.opcode, int) and 0x80 <= self.opcode <= 0xFF
                and self.opcode not in _MML_VALUES
        )

    @property
    def has_if(self) -> bool:
        return any(p.type == MML.IF for p in self.prefixes)

    @property
    def has_random(self) -> bool:
        return any(p.type == MML.RANDOM for p in self.prefixes)

    @property
    def has_variable(self) -> bool:
        return any(p.type == MML.VARIABLE for p in self.prefixes)

    @property
    def has_time(self) -> bool:
        return any(p.type in (MML.TIME, MML.TIME_RANDOM, MML.TIME_VARIABLE) for p in self.prefixes)

    def get_mml(self) -> MML | MMLEX | int:
        """Get the command as MML/MMLEX enum or note value."""
        if self.is_note:
            return self.opcode
        if self.is_extended:
            try:
                return MMLEX(self.opcode)
            except ValueError:
                return self.opcode
        try:
            return MML(self.opcode)
        except ValueError:
            try:
                return MMLEX(self.opcode)
            except ValueError:
                return self.opcode

    def __str__(self) -> str:
        if self.is_note:
            name = note_to_name(self.opcode)
            if self.args:
                return f"{name} {', '.join(map(str, self.args))}"
            return name

        try:
            if self.is_extended:
                raise ValueError
            mml = MML(self.opcode)
            name = mml.name.lower()
        except ValueError:
            try:
                mml = MMLEX(self.opcode)
                name = mml.name.lower()
            except ValueError:
                name = f"0x{self.opcode:02X}"

        # Build prefix string
        prefix_str = ""
        for p in self.prefixes:
            if p.type == MML.IF:
                prefix_str += "_if"
            elif p.type == MML.RANDOM:
                prefix_str += "_r"
            elif p.type == MML.VARIABLE:
                prefix_str += "_v"
            elif p.type == MML.TIME:
                prefix_str += "_t"
            elif p.type == MML.TIME_RANDOM:
                prefix_str += "_tr"
            elif p.type == MML.TIME_VARIABLE:
                prefix_str += "_tv"

        if self.args:
            return f"{name}{prefix_str} {', '.join(map(str, self.args))}"
        return f"{name}{prefix_str}"


@dataclass
class Label:
    name: str
    offset: int  # Relative offset in DATA block

    def __str__(self) -> str:
        return f"{self.name}:"

    def __hash__(self) -> int:
        return hash((self.name, self.offset))


@dataclass
class Track:
    """
    A sequence track containing commands.
    A BRSEQ can have up to 16 tracks.
    """
    # Starting label (named or anonymous)
    label: Label | None = None

    # Commands in this track
    commands: list[Command] = field(default_factory=list)

    # Offset range
    start_offset: int = 0
    end_offset: int = 0

    # References to other tracks/labels
    calls: list[int] = field(default_factory=list)  # Offsets of CALL targets
    jumps: list[int] = field(default_factory=list)  # Offsets of JUMP targets
    opens: list[tuple[int, int]] = field(default_factory=list)  # (track_no, offset)

    @property
    def name(self) -> str:
        if self.label:
            return self.label.name
        return f"_anon_{self.start_offset:04X}"

    def get_command(self, opcode: MML | MMLEX) -> list[Command]:
        """Get all commands with the given opcode."""
        target = opcode.value if isinstance(opcode, (MML, MMLEX)) else opcode
        return [cmd for cmd in self.commands if cmd.opcode == target]

    def __str__(self) -> str:
        lines = [f"{self.name}:"]
        for cmd in self.commands:
            lines.append(f"    {cmd}")
        return "\n".join(lines)


@dataclass
class BrseqData:
    version: int = 0x0100

    # Named labels from LABL block
    labels: list[Label] = field(default_factory=list)

    # All tracks (named and anonymous)
    tracks: dict[str | int, Track] = field(default_factory=dict)

    # Canonical physical command stream, in byte-offset order.  ``tracks``
    # are overlapping entry-point views over these same Command instances.
    # Older/programmatically-created models may leave this empty; the writer
    # then derives an order from ``tracks`` for backwards compatibility.
    command_stream: list[Command] = field(default_factory=list)

    # Raw sequence data bytes (for pass-through)
    raw_data: bytes | None = None
    data_base_offset: int = 0

    def __len__(self) -> int:
        return len(self.tracks)

    def __getitem__(self, key: str | int) -> Track:
        """Get track by name or offset."""
        if key in self.tracks:
            return self.tracks[key]

        # Try to find by offset
        for track in self.tracks.values():
            if track.start_offset == key:
                return track
            if track.label and track.label.offset == key:
                return track

        raise KeyError(f"Track not found: {key}")

    def __contains__(self, key: str | int) -> bool:
        try:
            self[key]
            return True
        except KeyError:
            return False

    def get_label_at_offset(self, offset: int) -> Label | None:
        """Find a label at the given offset."""
        for label in self.labels:
            if label.offset == offset:
                return label
        return None

    def get_all_offsets(self) -> set[int]:
        """Get all referenced offsets (for resolving labels)."""
        offsets = set()
        for track in self.tracks.values():
            offsets.add(track.start_offset)
            offsets.update(track.calls)
            offsets.update(track.jumps)
            for _, off in track.opens:
                offsets.add(off)
        return offsets

    def __str__(self) -> str:
        return f"BrseqData({len(self.labels)} labels, {len(self.tracks)} tracks)"
