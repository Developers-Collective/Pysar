import io
import re
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Self

from pysar.core.format.rseq.mml import (
    MML, MMLEX, ArgType, MML_ARG_SPEC, MMLEX_ARG_SPEC,
    is_note, DEFAULT_TEMPO, DEFAULT_TIMEBASE,
)
from pysar.core.model.brseq import BrseqData, Label, Track, Command


# =============================================================================
# MIDI Data Structures
# =============================================================================

class MidiEventType(IntEnum):
    """MIDI event types."""
    NOTE_OFF = 0x80
    NOTE_ON = 0x90
    POLY_PRESSURE = 0xA0
    CONTROL_CHANGE = 0xB0
    PROGRAM_CHANGE = 0xC0
    CHANNEL_PRESSURE = 0xD0
    PITCH_BEND = 0xE0
    SYSEX = 0xF0
    META = 0xFF


class MidiMetaType(IntEnum):
    """MIDI meta event types."""
    SEQUENCE_NUMBER = 0x00
    TEXT = 0x01
    COPYRIGHT = 0x02
    TRACK_NAME = 0x03
    INSTRUMENT_NAME = 0x04
    LYRIC = 0x05
    MARKER = 0x06
    CUE_POINT = 0x07
    CHANNEL_PREFIX = 0x20
    END_OF_TRACK = 0x2F
    SET_TEMPO = 0x51
    SMPTE_OFFSET = 0x54
    TIME_SIGNATURE = 0x58
    KEY_SIGNATURE = 0x59


class MidiCC(IntEnum):
    """Common MIDI Control Change numbers."""
    MOD_WHEEL = 1
    VOLUME = 7
    PAN = 10
    EXPRESSION = 11
    BEND_RANGE = 20
    MOD_SPEED = 21
    SUSTAIN = 64
    PORTAMENTO = 65
    TRACK_LOOP_START = 89
    TRACK_LOOP_END = 90
    ALL_SOUND_OFF = 120
    RESET_ALL = 121
    ALL_NOTES_OFF = 123


@dataclass
class MidiEvent:
    """A single MIDI event."""
    delta_time: int = 0
    status: int = 0
    data: bytes = b""
    # In-memory export provenance used to create PNMP/1 bindings. These values
    # are represented by the semantic profile when serialized, not by the
    # ordinary channel event itself.
    source_offset: int | None = field(default=None, repr=False, compare=False)
    source_id: str | None = field(default=None, repr=False, compare=False)

    @property
    def event_type(self) -> int:
        return self.status & 0xF0

    @property
    def channel(self) -> int:
        return self.status & 0x0F

    def is_note_on(self) -> bool:
        return self.event_type == MidiEventType.NOTE_ON and len(self.data) >= 2 and self.data[1] > 0

    def is_note_off(self) -> bool:
        if self.event_type == MidiEventType.NOTE_OFF:
            return True
        return self.event_type == MidiEventType.NOTE_ON and len(self.data) >= 2 and self.data[1] == 0

    @property
    def note(self) -> int:
        if self.event_type in (MidiEventType.NOTE_ON, MidiEventType.NOTE_OFF):
            return self.data[0] if self.data else 0
        return 0

    @property
    def velocity(self) -> int:
        if self.event_type in (MidiEventType.NOTE_ON, MidiEventType.NOTE_OFF):
            return self.data[1] if len(self.data) >= 2 else 0
        return 0


@dataclass
class MidiTrack:
    """A MIDI track."""
    name: str = ""
    events: list[MidiEvent] = field(default_factory=list)


@dataclass
class MidiFile:
    """A complete MIDI file."""
    format_type: int = 1  # 0 = single track, 1 = multi track, 2 = independent
    ticks_per_beat: int = 480
    tracks: list[MidiTrack] = field(default_factory=list)
    # Transient first-execution positions used to place readable Nintendo
    # annotations at meaningful MIDI ticks during export.
    source_command_ticks: dict[int, int] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        """Parse a MIDI file from bytes."""
        return MidiReader().read(BytesIO(data))

    @classmethod
    def from_file(cls, path: str | Path) -> Self:
        """Load a MIDI file from disk."""
        with open(path, "rb") as f:
            return MidiReader().read(f)

    def to_bytes(self) -> bytes:
        """Serialize to MIDI bytes."""
        buffer = BytesIO()
        MidiWriter().write(self, buffer)
        return buffer.getvalue()

    def save(self, path: str | Path) -> None:
        """Save to a MIDI file."""
        Path(path).write_bytes(self.to_bytes())


# =============================================================================
# MIDI Reader
# =============================================================================

class MidiReader:
    """Standard MIDI file reader."""

    def read(self, data: BinaryIO) -> MidiFile:
        """Read a MIDI file."""
        chunk_header = self._read_exact(data, 8, "MIDI header")
        if chunk_header[:4] != b"MThd":
            raise ValueError("Not a valid MIDI file")

        header_len = struct.unpack(">I", chunk_header[4:8])[0]
        if header_len < 6:
            raise ValueError("Invalid MIDI header length")
        header = self._read_exact(data, header_len, "MIDI header data")
        format_type, n_tracks, ticks = struct.unpack(">HHH", header[:6])
        if format_type not in (0, 1, 2):
            raise ValueError(f"Unsupported MIDI format {format_type}")
        if n_tracks == 0:
            raise ValueError("MIDI file contains no tracks")
        if format_type == 0 and n_tracks != 1:
            raise ValueError("MIDI format 0 must contain exactly one track")
        if ticks == 0:
            raise ValueError("MIDI PPQ division must be greater than zero")

        if ticks & 0x8000:
            ticks_per_beat = 480
        else:
            ticks_per_beat = ticks

        midi = MidiFile(
            format_type=format_type,
            ticks_per_beat=ticks_per_beat,
        )

        for _ in range(n_tracks):
            track = self._read_track(data)
            midi.tracks.append(track)

        return midi

    def _read_track(self, data: BinaryIO) -> MidiTrack:
        """Read a single MIDI track."""
        header = self._read_exact(data, 8, "MIDI track header")
        if header[:4] != b"MTrk":
            raise ValueError("Invalid track header")

        track_len = struct.unpack(">I", header[4:8])[0]
        track_data = BytesIO(self._read_exact(data, track_len, "MIDI track data"))

        track = MidiTrack()
        running_status = 0

        while track_data.tell() < track_len:
            delta = self._read_var_len(track_data)
            status_byte = self._read_byte(track_data)

            if status_byte & 0x80:
                # Running status is defined only for channel voice messages.
                running_status = status_byte if 0x80 <= status_byte <= 0xEF else 0
            else:
                if running_status == 0:
                    raise ValueError("MIDI running status has no preceding channel event")
                track_data.seek(-1, 1)
                status_byte = running_status

            event = MidiEvent(delta_time=delta, status=status_byte)

            if status_byte == 0xFF:
                meta_type = self._read_byte(track_data)
                length = self._read_var_len(track_data)
                event.data = bytes([meta_type]) + self._read_exact(
                    track_data, length, "MIDI meta event",
                )

                if meta_type == MidiMetaType.TRACK_NAME:
                    track.name = event.data[1:].decode("latin-1", errors="replace")

            elif status_byte == 0xF0 or status_byte == 0xF7:
                length = self._read_var_len(track_data)
                event.data = self._read_exact(track_data, length, "MIDI SysEx event")

            else:
                event_type = status_byte & 0xF0
                if event_type in (MidiEventType.PROGRAM_CHANGE, MidiEventType.CHANNEL_PRESSURE):
                    event.data = bytes([self._read_byte(track_data)])
                else:
                    event.data = bytes([self._read_byte(track_data), self._read_byte(track_data)])

            track.events.append(event)

        return track

    def _read_byte(self, data: BinaryIO) -> int:
        b = data.read(1)
        if not b:
            raise ValueError("Unexpected end of MIDI data")
        return b[0]

    def _read_var_len(self, data: BinaryIO) -> int:
        result = 0
        for _ in range(4):
            b = self._read_byte(data)
            result = (result << 7) | (b & 0x7F)
            if not (b & 0x80):
                return result
        raise ValueError("MIDI variable-length quantity exceeds four bytes")

    @staticmethod
    def _read_exact(data: BinaryIO, length: int, description: str) -> bytes:
        raw = data.read(length)
        if len(raw) != length:
            raise ValueError(f"Truncated {description}")
        return raw


# =============================================================================
# MIDI Writer
# =============================================================================

class MidiWriter:
    """Standard MIDI file writer."""

    def write(self, midi: MidiFile, output: BinaryIO) -> None:
        """Write a MIDI file."""
        output.write(b"MThd")
        output.write(struct.pack(">I", 6))
        output.write(struct.pack(">HHH", midi.format_type, len(midi.tracks), midi.ticks_per_beat))

        for track in midi.tracks:
            self._write_track(track, output)

    def _write_track(self, track: MidiTrack, output: BinaryIO) -> None:
        """Write a single track."""
        buffer = BytesIO()

        for event in track.events:
            self._write_var_len(buffer, event.delta_time)

            if event.status == 0xFF:
                buffer.write(bytes([0xFF, event.data[0]]))
                self._write_var_len(buffer, len(event.data) - 1)
                buffer.write(event.data[1:])
            elif event.status in (0xF0, 0xF7):
                buffer.write(bytes([event.status]))
                self._write_var_len(buffer, len(event.data))
                buffer.write(event.data)
            else:
                buffer.write(bytes([event.status]))
                buffer.write(event.data)

        track_data = buffer.getvalue()
        output.write(b"MTrk")
        output.write(struct.pack(">I", len(track_data)))
        output.write(track_data)

    def _write_var_len(self, output: BinaryIO, value: int) -> None:
        if value < 0:
            value = 0

        bytes_list = [value & 0x7F]
        value >>= 7
        while value:
            bytes_list.append((value & 0x7F) | 0x80)
            value >>= 7

        bytes_list.reverse()
        output.write(bytes(bytes_list))


# =============================================================================
# BRSEQ -> MIDI Conversion
# =============================================================================

def brseq_to_midi(
        brseq: BrseqData,
        ticks_per_beat: int = 480,
        max_loops: int = 2,
        start_label: str | None = None,
        start_offset: int | None = None,
) -> MidiFile:
    """
    Convert BRSEQ to MIDI.

    Uses the SequencePlayer to render the sequence tick-by-tick, then
    assembles a standard MIDI file.  Emits ``[`` and ``]`` MIDI marker
    meta-events so that a subsequent ``midi_to_brseq`` round-trip can
    reconstruct the loop structure.
    """
    from pysar.core.format.rseq.player import SequencePlayer

    # Render through the selected entry point.  The execution trace is also
    # used for loop markers and the initial timebase so shared RSEQ cues do
    # not leak control-flow or timing metadata into one another.
    player = SequencePlayer()
    player.load(
        brseq,
        start_label=start_label,
        start_offset=start_offset,
    )
    events, loop_start_tick, loop_end_tick, timebase, command_ticks = _render_brseq_timeline(
        player,
        brseq,
        max_ticks=5_000_000,
        max_loops=max_loops,
    )
    tick_scale = ticks_per_beat / max(1, timebase)

    if not events:
        midi = MidiFile(format_type=1, ticks_per_beat=ticks_per_beat)
        midi.source_command_ticks = {
            offset: int(tick * tick_scale) for offset, tick in command_ticks.items()
        }
        midi.tracks.append(_make_empty_track("empty"))
        return midi

    # Split into per-track note events and global tempo events
    track_events: dict[int, list[dict]] = {}
    tempo_events: list[dict] = []

    for ev in events:
        if ev['type'] == 'tempo':
            tempo_events.append(ev)
        elif 'track' in ev:
            track_idx = ev['track']
            if track_idx not in track_events:
                track_events[track_idx] = []
            track_events[track_idx].append(ev)

    # Deduplicate tempo events. The player emits a default 120 BPM event
    # at tick 0 before the real TEMPO command fires at tick ~1.
    tempo_events = _deduplicate_tempo_events(tempo_events)

    # Build MIDI file
    midi = MidiFile(format_type=1, ticks_per_beat=ticks_per_beat)
    midi.source_command_ticks = {
        offset: int(tick * tick_scale) for offset, tick in command_ticks.items()
    }

    # Track 0: conductor (tempo + loop markers)
    conductor = MidiTrack(name="Conductor")
    cond_abs: list[tuple[int, MidiEvent]] = []

    cond_abs.append((0, MidiEvent(
        status=0xFF,
        data=bytes([MidiMetaType.TRACK_NAME]) + b"Conductor",
    )))

    # Loop markers
    if loop_start_tick is not None:
        cond_abs.append((int(loop_start_tick * tick_scale), MidiEvent(
            status=0xFF,
            data=bytes([MidiMetaType.MARKER]) + b"[",
        )))
    if loop_end_tick is not None:
        cond_abs.append((int(loop_end_tick * tick_scale), MidiEvent(
            status=0xFF,
            data=bytes([MidiMetaType.MARKER]) + b"]",
        )))

    # Tempo events
    for tev in tempo_events:
        midi_tick = int(tev['tick'] * tick_scale)
        bpm = tev['tempo']
        if bpm <= 0:
            bpm = DEFAULT_TEMPO
        uspb = int(60_000_000 / bpm)
        cond_abs.append((midi_tick, MidiEvent(
            status=0xFF,
            data=bytes([MidiMetaType.SET_TEMPO,
                        (uspb >> 16) & 0xFF,
                        (uspb >> 8) & 0xFF,
                        uspb & 0xFF]),
        )))

    max_tick = max((ev['tick'] for ev in events), default=0)
    cond_abs.append((int(max_tick * tick_scale), MidiEvent(
        status=0xFF,
        data=bytes([MidiMetaType.END_OF_TRACK]),
    )))

    conductor.events = _abs_to_delta(cond_abs)
    midi.tracks.append(conductor)

    # One MIDI track per sequence track
    for track_idx in sorted(track_events.keys()):
        tevs = track_events[track_idx]
        channel = track_idx % 16

        track_name = f"Track_{track_idx}"
        named = [n for n in brseq.tracks.keys() if isinstance(n, str)]
        if track_idx < len(named):
            track_name = str(named[track_idx])

        midi_track = MidiTrack(name=track_name)
        abs_evts: list[tuple[int, MidiEvent]] = []

        abs_evts.append((0, MidiEvent(
            status=0xFF,
            data=bytes([MidiMetaType.TRACK_NAME]) + track_name.encode("latin-1", errors="replace"),
        )))

        last_program = -1
        last_bend_range: int | None = None
        for ev in tevs:
            midi_tick = int(ev['tick'] * tick_scale)

            if ev['type'] == 'note_on':
                prg = ev.get('program', 0) & 0x7F
                if prg != last_program:
                    last_program = prg
                    abs_evts.append((midi_tick, MidiEvent(
                        status=MidiEventType.PROGRAM_CHANGE | channel,
                        data=bytes([prg]),
                    )))
                abs_evts.append((midi_tick, MidiEvent(
                    status=MidiEventType.NOTE_ON | channel,
                    data=bytes([ev['note'] & 0x7F, ev['velocity'] & 0x7F]),
                    source_offset=ev.get('source_offset'),
                )))
            elif ev['type'] == 'note_off':
                abs_evts.append((midi_tick, MidiEvent(
                    status=MidiEventType.NOTE_OFF | channel,
                    data=bytes([ev['note'] & 0x7F, 0]),
                )))
            elif ev['type'] == 'note_change':
                prg = ev.get('program', 0) & 0x7F
                if prg != last_program:
                    last_program = prg
                    abs_evts.append((midi_tick, MidiEvent(
                        status=MidiEventType.PROGRAM_CHANGE | channel,
                        data=bytes([prg]),
                    )))
                abs_evts.append((midi_tick, MidiEvent(
                    status=MidiEventType.NOTE_OFF | channel,
                    data=bytes([ev['old_note'] & 0x7F, 0]),
                )))
                abs_evts.append((midi_tick, MidiEvent(
                    status=MidiEventType.NOTE_ON | channel,
                    data=bytes([ev['note'] & 0x7F, ev['velocity'] & 0x7F]),
                    source_offset=ev.get('source_offset'),
                )))
            elif ev['type'] == 'program_change':
                prg = ev.get('program', 0) & 0x7F
                if prg != last_program:
                    last_program = prg
                    abs_evts.append((midi_tick, MidiEvent(
                        status=MidiEventType.PROGRAM_CHANGE | channel,
                        data=bytes([prg]),
                    )))
            elif ev['type'] == 'control_change':
                abs_evts.append((midi_tick, MidiEvent(
                    status=MidiEventType.CONTROL_CHANGE | channel,
                    data=bytes([
                        max(0, min(127, int(ev.get('cc', 0)))),
                        max(0, min(127, int(ev.get('value', 0)))),
                    ]),
                )))
            elif ev['type'] == 'pitch_bend':
                bend_range = max(1, min(127, int(ev.get('range', 2))))
                if bend_range != last_bend_range:
                    last_bend_range = bend_range
                    abs_evts.append((midi_tick, MidiEvent(
                        status=MidiEventType.CONTROL_CHANGE | channel,
                        data=bytes([MidiCC.BEND_RANGE, bend_range]),
                    )))
                normalized = max(-1.0, min(
                    1.0,
                    float(ev.get('semitones', 0.0)) / bend_range,
                ))
                bend = max(0, min(16383, int(round(8192 + normalized * 8192))))
                abs_evts.append((midi_tick, MidiEvent(
                    status=MidiEventType.PITCH_BEND | channel,
                    data=bytes([bend & 0x7F, (bend >> 7) & 0x7F]),
                )))

        end_tick = max((t for t, _ in abs_evts), default=0) if abs_evts else 0
        abs_evts.append((end_tick, MidiEvent(
            status=0xFF,
            data=bytes([MidiMetaType.END_OF_TRACK]),
        )))

        midi_track.events = _abs_to_delta(abs_evts)
        midi.tracks.append(midi_track)

    if len(midi.tracks) <= 1:
        midi.tracks.append(_make_empty_track("Track_0"))

    return midi


def _deduplicate_tempo_events(events: list[dict]) -> list[dict]:
    """
    Collapse multiple tempo events at the same (or near-same) tick into
    one, keeping the *last* value.
    """
    if not events:
        return events

    by_tick: dict[int, dict] = {}
    for ev in events:
        by_tick[ev['tick']] = ev

    # If tick-0 and tick-1 both present, drop tick-0 (it's the player's
    # default 120 BPM emitted before the real TEMPO command fires).
    if 0 in by_tick and 1 in by_tick:
        del by_tick[0]

    return sorted(by_tick.values(), key=lambda e: e['tick'])


def _render_brseq_timeline(
        player,
        brseq: BrseqData,
        *,
        max_ticks: int,
        max_loops: int,
) -> tuple[list[dict], int | None, int | None, int, dict[int, int]]:
    """Render once while tracing the exact command path used by the player.

    Loop labels in a shared RSEQ are only meaningful if the selected cue
    actually reaches them.  Observing ``_execute_next_command`` keeps marker
    timing aligned with notes, waits, calls and opened tracks without a second
    approximate command-stream walk.
    """
    loop_offsets = {
        label.offset
        for label in brseq.labels
        if isinstance(label.name, str) and 'loopstart' in label.name.lower()
    }
    first_execution: dict[tuple[int, int], int] = {}
    explicit_loop_starts: dict[int, int] = {}
    loop_candidates: list[tuple[int, int, int]] = []
    command_ticks: dict[int, int] = {}
    initial_timebase = DEFAULT_TIMEBASE

    original_execute = player._execute_next_command

    def traced_execute(track_state):
        nonlocal initial_timebase

        index = track_state.flat_cmd_index
        if index < 0 or index >= len(player._flat_commands):
            return original_execute(track_state)

        cmd = player._flat_commands[index]
        tick = player._ctx.tick_counter
        track_index = track_state.track_index
        offset = cmd.offset
        command_ticks.setdefault(offset, tick)
        if offset in loop_offsets:
            first_execution.setdefault((track_index, offset), tick)

        mml = cmd.get_mml()
        if mml == MML.LOOP_START:
            explicit_loop_starts[track_index] = tick

        result = original_execute(track_state)

        # TIMEBASE is global player state.  Commands reached on the initial
        # zero-time command burst define the selected cue's initial scale.
        if mml == MML.TIMEBASE and tick == 0:
            initial_timebase = max(1, player._ctx.timebase)

        if mml == MML.JUMP and not track_state.finished:
            target_index = track_state.flat_cmd_index
            if 0 <= target_index <= index and target_index < len(player._flat_commands):
                target_offset = player._flat_commands[target_index].offset
                if target_offset in loop_offsets:
                    start = first_execution.get((track_index, target_offset))
                    if start is not None and not any(
                            candidate[0] == track_index for candidate in loop_candidates):
                        loop_candidates.append((track_index, start, tick))
        elif mml == MML.LOOP_END and not track_state.finished:
            if track_state.flat_cmd_index <= index:
                start = explicit_loop_starts.get(track_index)
                if start is not None and not any(
                        candidate[0] == track_index for candidate in loop_candidates):
                    loop_candidates.append((track_index, start, tick))

        return result

    player._execute_next_command = traced_execute
    try:
        events = player.render_events(max_ticks=max_ticks, max_loops=max_loops)
    finally:
        player._execute_next_command = original_execute

    if not loop_candidates:
        return events, None, None, initial_timebase, command_ticks

    # Track zero is the selected conductor.  Fall back to the first executed
    # child loop for sequences whose conductor only opens looping children.
    selected = next(
        (candidate for candidate in loop_candidates if candidate[0] == 0),
        loop_candidates[0],
    )
    return events, selected[1], selected[2], initial_timebase, command_ticks


def _find_loop_ticks_from_brseq(
        brseq: BrseqData,
        start_label: str | None = None,
        start_offset: int | None = None,
) -> tuple[int | None, int | None]:
    """Return executed loop bounds for one selected cue.

    Kept as a small compatibility helper; normal export uses the same trace as
    its event render and therefore does not render twice.
    """
    from pysar.core.format.rseq.player import SequencePlayer

    player = SequencePlayer()
    player.load(brseq, start_label=start_label, start_offset=start_offset)
    _, start, end, _, _ = _render_brseq_timeline(
        player,
        brseq,
        max_ticks=5_000_000,
        max_loops=1,
    )
    return start, end


# Helpers shared by both directions

def _abs_to_delta(events: list[tuple[int, MidiEvent]]) -> list[MidiEvent]:
    """Convert (absolute_tick, event) pairs to delta-time events."""
    events.sort(key=lambda x: x[0])
    result = []
    last_tick = 0
    for abs_tick, event in events:
        event.delta_time = max(0, abs_tick - last_tick)
        last_tick = abs_tick
        result.append(event)
    return result


def _make_empty_track(name: str) -> MidiTrack:
    track = MidiTrack(name=name)
    track.events = [
        MidiEvent(delta_time=0, status=0xFF,
                  data=bytes([MidiMetaType.TRACK_NAME]) + name.encode("latin-1")),
        MidiEvent(delta_time=0, status=0xFF,
                  data=bytes([MidiMetaType.END_OF_TRACK])),
    ]
    return track


# MIDI -> BRSEQ conversion

_MIN_SUBROUTINE_CMDS = 12


@dataclass
class _ChannelEvent:
    """Intermediate event for one MIDI channel."""
    brseq_tick: int
    type: str   # 'note','program','volume','pan','mod','bend',
                # 'bendrange','mod_speed','damper','tempo'
    args: list
    source_order: int = 0


@dataclass
class _EmbeddedAnnotation:
    """One Nintendo command or label placed on a MIDI timeline."""
    command: Command | None = None
    label: str | None = None
    target_arg: int | None = None
    target_label: str | None = None
    source: str = ""


@dataclass
class _PendingNote:
    """A note-on waiting for its matching note-off."""
    note: int
    velocity: int
    start_tick: int


_PYSAR_TARGETED_RE = re.compile(
    r"^pysar_(all|(?:0?[0-9]|1[0-5]))\s*:\s*(.+)$",
    re.IGNORECASE,
)
_PYSAR_RE = re.compile(r"^@pysar\s+(.+)$", re.IGNORECASE)
_PYSAR_CHANNEL_RE = re.compile(
    r"(?:^|\s+)channel\s*=\s*(\d+)\s*$",
    re.IGNORECASE,
)

_PYSAR_COMMAND_ALIASES = {
    "bend-range": "bend_range",
    "effect-send-a": "fxsend_a",
    "effect-send-b": "fxsend_b",
    "effect-send-c": "fxsend_c",
    "envelope-reset": "env_reset",
    "finish": "fin",
    "initial-pan": "init_pan",
    "low-pass": "lpf_cutoff",
    "main-send": "mainsend",
    "main-volume": "main_volume",
    "modulation-delay": "mod_delay",
    "modulation-range": "mod_range",
    "modulation-type": "mod_type",
    "portamento-key": "porta",
    "portamento-time": "porta_time",
    "priority": "prio",
    "surround-pan": "surround_pan",
    "sweep-pitch": "sweep_pitch",
}
_PYSAR_TOGGLES = {
    "tie": "tie",
    "monophonic": "monophonic",
    "portamento": "porta_sw",
}


def _midi_meta_text(event: MidiEvent) -> str | None:
    if (
            event.status != MidiEventType.META
            or not event.data
            or event.data[0] not in (MidiMetaType.TEXT, MidiMetaType.MARKER)
    ):
        return None
    return event.data[1:].decode("utf-8", errors="replace").strip()


def _infer_annotation_channel(
        track_channels: set[int], used_channels: list[int], explicit: int | None,
        source: str,
) -> int:
    if explicit is not None:
        return explicit
    if len(track_channels) == 1:
        return next(iter(track_channels))
    if not track_channels and used_channels:
        return used_channels[0]
    if len(track_channels) > 1:
        raise ValueError(
            f"Ambiguous MIDI annotation '{source}': its MIDI track uses "
            "multiple channels; add channel=1..16"
        )
    return 0


def _expand_pysar_alias(body: str) -> str:
    """Translate a friendly composer marker to the normal RSEQ spelling."""
    parts = body.strip().split(None, 1)
    if not parts:
        raise ValueError("Empty @pysar annotation")
    name = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if name in (
            "function", "function-end", "label", "call", "jump",
            "return", "ret", "open-track", "open_track",
    ):
        raise ValueError(
            "@pysar does not expose manual function/control-flow regions; "
            "write repeated music normally and let PySAR extract reusable "
            "phrases (advanced commands can use pysar_NN:)"
        )
    if name in _PYSAR_TOGGLES:
        enabled = rest.lower()
        if enabled not in ("on", "off"):
            raise ValueError(f"@pysar {name} requires 'on' or 'off'")
        return f"{_PYSAR_TOGGLES[name]}_{enabled}"

    command_name = _PYSAR_COMMAND_ALIASES.get(name, name.replace("-", "_"))
    return f"{command_name} {rest}".strip()


def _parse_embedded_annotation(payload: str, source: str) -> _EmbeddedAnnotation:
    """Parse one targeted command line or friendly alias."""
    from pysar.core.format.rseq.text import _parse_command

    line = payload.strip()
    if not line:
        raise ValueError(f"Empty PySAR MIDI annotation in '{source}'")
    if line.endswith(":"):
        label = line[:-1].strip()
        if not label or any(char.isspace() for char in label):
            raise ValueError(f"Invalid label in MIDI annotation '{source}'")
        return _EmbeddedAnnotation(label=label, source=source)

    parts = line.split(None, 1)
    command_name = parts[0].lower().replace("-", "_")
    target_arg: int | None = None
    target_label: str | None = None
    parse_line = line
    if command_name in ("call", "jump"):
        if len(parts) != 2 or not parts[1].strip():
            raise ValueError(f"{command_name} requires a label in '{source}'")
        target_arg = 0
        target_label = parts[1].strip().removeprefix("::")
    elif command_name in ("open_track", "opentrack"):
        if len(parts) != 2 or "," not in parts[1]:
            raise ValueError(f"open_track requires track, label in '{source}'")
        _, target = parts[1].split(",", 1)
        target_arg = 1
        target_label = target.strip().removeprefix("::")

    try:
        command = _parse_command(
            parse_line,
            {},
            1,
            allow_unresolved=target_label is not None,
        )
    except ValueError as exc:
        raise ValueError(f"Invalid PySAR MIDI annotation '{source}': {exc}") from exc
    _validate_embedded_command_ranges(command, source)
    return _EmbeddedAnnotation(
        command=command,
        target_arg=target_arg,
        target_label=target_label,
        source=source,
    )


def _validate_embedded_command_ranges(command: Command, source: str) -> None:
    """Fail instead of letting the binary writer wrap an annotation value."""
    command_type = command.get_mml()
    if isinstance(command_type, MML):
        spec = MML_ARG_SPEC.get(command_type, ())
    elif isinstance(command_type, MMLEX):
        spec = MMLEX_ARG_SPEC.get(command_type, ())
    else:
        return

    ranges = {
        ArgType.U8: (0, 0xFF),
        ArgType.S8: (-0x80, 0x7F),
        ArgType.U16: (0, 0xFFFF),
        ArgType.S16: (-0x8000, 0x7FFF),
        ArgType.U24: (0, 0xFFFFFF),
        ArgType.VAR_LEN: (0, 0x0FFFFFFF),
    }
    for index, (value, arg_type) in enumerate(zip(command.args, spec), 1):
        limits = ranges.get(arg_type)
        if limits is not None and not limits[0] <= int(value) <= limits[1]:
            raise ValueError(
                f"Invalid PySAR MIDI annotation '{source}': argument {index} "
                f"is outside {limits[0]}..{limits[1]}"
            )

    prefix_ranges = {
        MML.RANDOM: (-0x8000, 0x7FFF),
        MML.TIME_RANDOM: (-0x8000, 0x7FFF),
        MML.TIME: (-0x8000, 0x7FFF),
        MML.VARIABLE: (0, 0xFF),
        MML.TIME_VARIABLE: (0, 0xFF),
    }
    for prefix in command.prefixes:
        limits = prefix_ranges.get(prefix.type)
        if limits is None:
            continue
        if any(not limits[0] <= int(value) <= limits[1] for value in prefix.args):
            raise ValueError(
                f"Invalid PySAR MIDI annotation '{source}': prefix value "
                f"is outside {limits[0]}..{limits[1]}"
            )


def _collect_embedded_annotations(
        midi: MidiFile,
        used_channels: list[int],
        tick_scale: float,
        combine_tracks: bool,
) -> dict[int, list[_ChannelEvent]]:
    """Collect PySAR's targeted command syntax and friendly aliases."""
    collected: dict[int, list[_ChannelEvent]] = {channel: [] for channel in used_channels}
    order = 0

    for midi_track_index, track in enumerate(midi.tracks):
        track_channels = {
            event.channel
            for event in track.events
            if event.event_type in (
                MidiEventType.NOTE_ON, MidiEventType.NOTE_OFF,
                MidiEventType.CONTROL_CHANGE, MidiEventType.PROGRAM_CHANGE,
                MidiEventType.PITCH_BEND,
            )
        }
        absolute_tick = 0
        for event in track.events:
            absolute_tick += event.delta_time
            text = _midi_meta_text(event)
            if text is None or text.lower().startswith("@pysar/1 "):
                continue

            targeted = _PYSAR_TARGETED_RE.match(text)
            alias = _PYSAR_RE.match(text)
            if not targeted and not alias:
                if text.lower() == "@pysar":
                    raise ValueError("Empty @pysar MIDI annotation")
                if text.lower().startswith("pysar_") and ":" in text:
                    raise ValueError(
                        f"Invalid PySAR MIDI annotation target in '{text}'; "
                        "use pysar_00 through pysar_15 or pysar_all"
                    )
                continue

            target_channels: list[int]
            payload: str
            if targeted:
                target = targeted.group(1).lower()
                payload = targeted.group(2).strip()
                target_channels = (
                    list(used_channels)
                    if target == "all"
                    else [int(target)]
                )
            else:
                payload = alias.group(1).strip()
                channel_match = _PYSAR_CHANNEL_RE.search(payload)
                explicit_channel = None
                if channel_match:
                    requested_channel = int(channel_match.group(1))
                    if not 1 <= requested_channel <= 16:
                        raise ValueError(
                            f"Invalid MIDI annotation '{text}': channel must be 1..16"
                        )
                    explicit_channel = requested_channel - 1
                    payload = payload[:channel_match.start()].strip()
                target_channels = [_infer_annotation_channel(
                    track_channels, used_channels, explicit_channel, text,
                )]
                payload = _expand_pysar_alias(payload)

            if combine_tracks:
                target_channels = [0]

            for channel in target_channels:
                if not combine_tracks and channel not in used_channels:
                    # pysar_all emits nothing for an unused track. An explicit
                    # unused pysar_NN is likewise a
                    # no-op instead of manufacturing an empty sequence track.
                    continue
                expanded = re.sub(
                    r"\$([A-Za-z_][A-Za-z0-9_]*)",
                    lambda match: f"Track_{channel}_{match.group(1)}",
                    payload,
                )
                annotation = _parse_embedded_annotation(expanded, text)
                tick = max(0, int(absolute_tick * tick_scale + 0.5))
                collected.setdefault(channel, []).append(
                    _ChannelEvent(tick, "embedded", [annotation], order)
                )
                order += 1

    return collected


def midi_to_brseq(
        midi: MidiFile,
        timebase: int = DEFAULT_TIMEBASE,
        combine_tracks: bool = False,
) -> BrseqData:
    """
    Convert MIDI to BRSEQ in the style of Nintendo's *smfconv* tool.

    Produces an smfconv-compatible RSEQ structure:

    * ``notewait_off`` mode (explicit WAITs advance time)
    * Loop markers from ``[`` / ``]``
    * Per-channel tracks with setup preambles
    * Subroutine extraction (CALL / RET) for repeated phrases
    * Conductor track with ``alloctrack`` and ``opentrack``
    """
    tick_scale = timebase / midi.ticks_per_beat

    # 1. Scan MIDI for global info

    # Loop markers
    loop_start_midi: int | None = None
    loop_end_midi: int | None = None
    loop_start_count = 0
    loop_end_count = 0
    for track in midi.tracks:
        abs_tick = 0
        for event in track.events:
            abs_tick += event.delta_time
            if event.status == 0xFF and event.data and event.data[0] == MidiMetaType.MARKER:
                text = event.data[1:].decode("latin-1", errors="replace").strip()
                if text.lower() in ("[", "loop_start"):
                    loop_start_midi = abs_tick
                    loop_start_count += 1
                elif text.lower() in ("]", "loop_end"):
                    loop_end_midi = abs_tick

                    loop_end_count += 1

    if loop_start_count != loop_end_count:
        raise ValueError("Whole-sequence MIDI loop needs one start and one end marker")
    if loop_start_count > 1:
        raise ValueError("Only one whole-sequence MIDI loop marker pair is supported")
    if (
            loop_start_midi is not None
            and loop_end_midi is not None
            and loop_end_midi <= loop_start_midi
    ):
        raise ValueError("Whole-sequence MIDI loop end must follow its start")

    has_loop = loop_start_midi is not None and loop_end_midi is not None

    # 2. Gather events per MIDI channel

    channel_events: dict[int, list[tuple[int, MidiEvent]]] = {}
    tempo_changes: list[tuple[int, int]] = []

    for track in midi.tracks:
        abs_tick = 0
        for event in track.events:
            abs_tick += event.delta_time
            if event.status == 0xFF and event.data and event.data[0] == MidiMetaType.SET_TEMPO:
                if len(event.data) >= 4:
                    us = (event.data[1] << 16) | (event.data[2] << 8) | event.data[3]
                    if us > 0:
                        tempo_changes.append((abs_tick, int(round(60_000_000 / us))))
            if event.event_type in (
                MidiEventType.NOTE_ON, MidiEventType.NOTE_OFF,
                MidiEventType.CONTROL_CHANGE, MidiEventType.PROGRAM_CHANGE,
                MidiEventType.PITCH_BEND,
            ):
                ch = event.channel
                if ch not in channel_events:
                    channel_events[ch] = []
                channel_events[ch].append((abs_tick, event))

    # Multiple tracks may carry a tempo event at the same absolute tick;
    # standard MIDI defines one global tempo map, with the last event at a
    # tick taking effect.
    tempo_by_tick: dict[int, int] = {}
    for tick, bpm in tempo_changes:
        tempo_by_tick[tick] = bpm
    tempo_changes = sorted(tempo_by_tick.items())
    tempo = tempo_by_tick.get(0, DEFAULT_TEMPO)

    if not channel_events:
        if not tempo_changes:
            return _make_empty_brseq(timebase, tempo)
        # Preserve a tempo-only conductor file instead of discarding its map.
        channel_events = {0: []}

    if combine_tracks:
        merged: list[tuple[int, MidiEvent]] = []
        for evts in channel_events.values():
            merged.extend(evts)
        merged.sort(key=lambda x: x[0])
        channel_events = {0: merged}

    embedded_events = _collect_embedded_annotations(
        midi,
        sorted(channel_events.keys()),
        tick_scale,
        combine_tracks,
    )

    # 3. Channel-to-track index mapping.  MIDI and NW4R both expose sixteen
    # channels/tracks; keep this bijective (the previous special case mapped
    # both MIDI channels 8 and 9 to BRSEQ track 9).

    sorted_channels = sorted(channel_events.keys())

    def _ch_to_idx(ch: int) -> int:
        return ch

    track_indices = {ch: _ch_to_idx(ch) for ch in sorted_channels}
    used_indices = sorted(set(track_indices.values()))

    # 4. Build tracks

    all_tracks: dict[str, Track] = {}
    all_labels: list[Label] = []
    label_refs: list[tuple[str, int, int, str]] = []
    extra_label_positions: list[tuple[str, int, str]] = []

    # Conductor preamble: alloctrack + opentrack for each child
    track_mask = 0
    for idx in used_indices:
        track_mask |= (1 << idx)

    conductor_preamble: list[Command] = [
        Command(opcode=MML.ALLOC_TRACK, args=[track_mask]),
    ]

    child_indices = [i for i in used_indices if i != used_indices[0]]
    # Record (cmd_index_in_preamble, target_label) for later label_ref registration
    open_track_info: list[tuple[int, str]] = []
    for ci in child_indices:
        cmd = Command(opcode=MML.OPEN_TRACK, args=[ci, 0])
        conductor_preamble.append(cmd)
        open_track_info.append((len(conductor_preamble) - 1, f"Track_{ci}"))

    # Per-channel tracks
    for ch in sorted_channels:
        tidx = track_indices[ch]
        evts = channel_events[ch]
        is_cond = (tidx == used_indices[0])

        _build_channel_tracks(
            track_idx=tidx, events=evts,
            embedded_events=embedded_events.get(ch, ()),
            tick_scale=tick_scale, tempo=tempo,
            tempo_changes=tempo_changes if is_cond else (),
            loop_start_midi=loop_start_midi, loop_end_midi=loop_end_midi,
            is_conductor=is_cond,
            conductor_preamble=conductor_preamble if is_cond else None,
            all_tracks=all_tracks, all_labels=all_labels,
            label_refs=label_refs,
            extra_label_positions=extra_label_positions,
        )

        # Register OPEN_TRACK label_refs under the conductor track name
        if is_cond:
            for cmd_idx, target in open_track_info:
                label_refs.append(("main", cmd_idx, 1, target))

    # 5. Resolve label offsets
    command_stream = _resolve_label_offsets(
        all_tracks,
        all_labels,
        label_refs,
        extra_label_positions,
    )

    return BrseqData(
        version=0x0100,
        labels=all_labels,
        tracks=all_tracks,
        command_stream=command_stream,
    )


# Channel conversion

def _build_channel_tracks(
    *, track_idx, events, embedded_events, tick_scale, tempo, tempo_changes,
    loop_start_midi, loop_end_midi,
    is_conductor, conductor_preamble,
    all_tracks, all_labels, label_refs, extra_label_positions,
):
    """Build Track objects for a single MIDI channel.

    For the conductor track, *conductor_preamble* (ALLOC_TRACK + OPEN_TRACKs)
    is prepended so that the conductor becomes a single ``main`` track that
    matches smfconv's output layout.
    """
    has_loop = loop_start_midi is not None and loop_end_midi is not None

    # Pair note-on/off events and convert CC/pitchbend into intermediate events.
    output = _collect_channel_events(events, tick_scale)
    output.extend(embedded_events)
    for midi_tick, bpm in tempo_changes:
        output.append(_ChannelEvent(
            max(0, int(midi_tick * tick_scale + 0.5)),
            'tempo',
            [bpm],
        ))
    output.sort(key=lambda event: (
        event.brseq_tick,
        0 if event.type == 'tempo' else (1 if event.type == 'embedded' else 2),
        event.source_order,
    ))
    _validate_track_loop_events(output, track_idx)

    # Initial setup from events before the loop, or at tick 0.

    cutoff = int(loop_start_midi * tick_scale + 0.5) if has_loop else 0
    init = {}
    for ev in output:
        if ev.brseq_tick > cutoff:
            break
        if ev.type != 'embedded' and ev.type not in init:
            init[ev.type] = ev.args

    setup: list[Command] = [Command(opcode=MML.NOTE_WAIT, args=[0])]
    if is_conductor:
        setup.append(Command(opcode=MML.TEMPO, args=[tempo]))
    if 'program' in init:
        setup.append(Command(opcode=MML.PRG, args=[init['program'][0]]))
    if 'volume' in init:
        setup.append(Command(opcode=MML.VOLUME, args=[init['volume'][0] & 0x7F]))
    if 'pan' in init:
        setup.append(Command(opcode=MML.PAN, args=[init['pan'][0] & 0x7F]))
    if 'bendrange' in init:
        setup.append(Command(opcode=MML.BEND_RANGE, args=[init['bendrange'][0] & 0x7F]))
    if 'bend' in init:
        setup.append(Command(opcode=MML.PITCH_BEND, args=[init['bend'][0]]))
    if 'mod_speed' in init:
        setup.append(Command(opcode=MML.MOD_SPEED, args=[init['mod_speed'][0] & 0x7F]))
    if 'mod' in init:
        setup.append(Command(opcode=MML.MOD_DEPTH, args=[init['mod'][0] & 0x7F]))

    initial_prg = init.get('program', [None])[0]

    # Split events

    if has_loop:
        ls = int(loop_start_midi * tick_scale + 0.5)
        le = int(loop_end_midi * tick_scale + 0.5)

        intro_evts = [e for e in output if e.brseq_tick < ls]
        loop_evts = []
        for e in output:
            if ls <= e.brseq_tick < le:
                loop_evts.append(_ChannelEvent(e.brseq_tick - ls, e.type, list(e.args)))

    else:
        intro_evts = []
        loop_evts = output

    # Filter out redundant setup events at tick 0 (already in preamble)
    def _redundant(e: _ChannelEvent) -> bool:
        setup_types = {
            'program', 'volume', 'pan', 'bendrange', 'bend',
            'mod_speed', 'mod', 'tempo',
        }
        if e.brseq_tick != 0 or e.type not in setup_types:
            return False
        return e.type in init and e.args == init[e.type]

    intro_evts = [e for e in intro_evts if not _redundant(e)]
    loop_evts = [e for e in loop_evts if not _redundant(e)]

    # Convert events to commands

    intro_cmds, intro_labels, intro_refs = (
        _events_to_commands(intro_evts, initial_prg)
        if intro_evts else ([], [], [])
    )
    loop_cmds, loop_labels, loop_refs = _events_to_commands(loop_evts, initial_prg)

    # The marker positions define segment lengths even when their tails are
    # silent.  Without these waits, a 480/960 MIDI loop with its last event at
    # 720 would become a 0/240 BRSEQ loop on export.
    if has_loop:
        intro_end = int(loop_start_midi * tick_scale + 0.5)
        loop_end = int((loop_end_midi - loop_start_midi) * tick_scale + 0.5)
        _pad_commands_to_segment_end(intro_cmds, intro_evts, intro_end)
        _pad_commands_to_segment_end(loop_cmds, loop_evts, loop_end)

    # Extract repeated subroutines

    subroutines: dict[str, list[Command]] = {}
    structural_opcodes = {
        MML.OPEN_TRACK, MML.JUMP, MML.CALL, MML.LOOP_START,
        MML.LOOP_END, MML.RET, MML.FIN,
    }
    has_embedded_structure = bool(intro_labels or intro_refs or loop_labels or loop_refs)
    has_embedded_structure = has_embedded_structure or any(
        event.type in ('track_loop_start', 'track_loop_end')
        or (
            event.type == 'embedded'
            and event.args[0].command is not None
            and event.args[0].command.get_mml() in structural_opcodes
        )
        for event in loop_evts
    )
    if has_embedded_structure:
        sub_refs = []
    else:
        loop_cmds, sub_refs = _extract_subroutines(
            loop_cmds, f"Track_{track_idx}", subroutines,
        )

    # Assemble Track objects

    # Conductor uses "main" as its track name; children use "Track_N"
    tname = "main" if is_conductor else f"Track_{track_idx}"

    # Prepend conductor preamble (ALLOC_TRACK + OPEN_TRACKs) if present
    preamble = list(conductor_preamble) if conductor_preamble else []

    setup_track = Track(
        label=Label(name=tname, offset=0),
        commands=preamble + setup + intro_cmds,
    )
    all_tracks[tname] = setup_track
    all_labels.append(Label(name=tname, offset=0))

    intro_prefix = len(preamble) + len(setup)
    for cmd_index, label_name in intro_labels:
        extra_label_positions.append((tname, intro_prefix + cmd_index, label_name))
        all_labels.append(Label(name=label_name, offset=0))
    for cmd_index, arg_index, target in intro_refs:
        label_refs.append((tname, intro_prefix + cmd_index, arg_index, target))

    if has_loop:
        loop_label = f"{tname}_LoopStart"
        jump = Command(opcode=MML.JUMP, args=[0])
        loop_track = Track(
            label=Label(name=loop_label, offset=0),
            commands=loop_cmds + [jump],
        )
        all_tracks[loop_label] = loop_track
        all_labels.append(Label(name=loop_label, offset=0))
        label_refs.append((loop_label, len(loop_track.commands) - 1, 0, loop_label))

        for cmd_index, label_name in loop_labels:
            extra_label_positions.append((loop_label, cmd_index, label_name))
            all_labels.append(Label(name=label_name, offset=0))
        for cmd_index, arg_index, target in loop_refs:
            label_refs.append((loop_label, cmd_index, arg_index, target))

        for cmd_index, sub_target in sub_refs:
            label_refs.append((loop_label, cmd_index, 0, sub_target))
    else:
        loop_prefix = len(preamble) + len(setup) + len(intro_cmds)
        setup_track.commands.extend(loop_cmds)
        setup_track.commands.append(Command(opcode=MML.FIN))
        for cmd_index, label_name in loop_labels:
            extra_label_positions.append((tname, loop_prefix + cmd_index, label_name))
            all_labels.append(Label(name=label_name, offset=0))
        for cmd_index, arg_index, target in loop_refs:
            label_refs.append((tname, loop_prefix + cmd_index, arg_index, target))
        for cmd_index, sub_target in sub_refs:
            actual_idx = loop_prefix + cmd_index
            label_refs.append((tname, actual_idx, 0, sub_target))

    # Subroutine tracks
    for sname, scmds in subroutines.items():
        # Safety net: resolve any nested CALL targets within subroutine bodies.
        # These would otherwise stay at offset=0 and call the sequence start,
        # re-opening all tracks and causing infinite loops.
        for ci, cmd in enumerate(scmds):
            target = getattr(cmd, '_call_target', None)
            if target is not None:
                label_refs.append((sname, ci, 0, target))
                delattr(cmd, '_call_target')

        sub_track = Track(
            label=Label(name=sname, offset=0),
            commands=scmds + [Command(opcode=MML.RET)],
        )
        all_tracks[sname] = sub_track
        all_labels.append(Label(name=sname, offset=0))


def _validate_track_loop_events(events: list[_ChannelEvent], track_idx: int) -> None:
    """Reject mismatched counted-loop annotations before serialization."""
    depth = 0
    for event in events:
        is_start = event.type == 'track_loop_start'
        is_end = event.type == 'track_loop_end'
        if event.type == 'embedded':
            annotation: _EmbeddedAnnotation = event.args[0]
            if annotation.command is not None:
                mml = annotation.command.get_mml()
                is_start = mml == MML.LOOP_START
                is_end = mml == MML.LOOP_END
        if is_start:
            depth += 1
            if depth > 3:
                raise ValueError(
                    f"PySAR MIDI track {track_idx} exceeds NW4R's "
                    "three-entry loop/call stack"
                )
        elif is_end:
            if depth == 0:
                raise ValueError(
                    f"PySAR MIDI track {track_idx} has loop_end without loop_start"
                )
            depth -= 1
    if depth:
        raise ValueError(
            f"PySAR MIDI track {track_idx} has {depth} unclosed loop_start "
            f"annotation{'s' if depth != 1 else ''}"
        )


def _pad_commands_to_segment_end(
        commands: list[Command],
        events: list[_ChannelEvent],
        end_tick: int,
) -> None:
    """Preserve an intro/loop boundary after the segment's last event."""
    last_event_tick = max((event.brseq_tick for event in events), default=0)
    trailing_ticks = end_tick - last_event_tick
    if trailing_ticks > 0:
        commands.append(Command(opcode=MML.WAIT, args=[trailing_ticks]))


# Event collection and conversion

def _collect_channel_events(
    events: list[tuple[int, MidiEvent]], tick_scale: float,
) -> list[_ChannelEvent]:
    """Pair note-on/off events and convert CC/program/bend into intermediate events."""
    out: list[_ChannelEvent] = []
    active: dict[int, _PendingNote] = {}

    for abs_tick, ev in events:
        bt = max(0, int(abs_tick * tick_scale + 0.5))

        if ev.is_note_on():
            n, v = ev.note, ev.velocity
            if n in active:
                p = active.pop(n)
                out.append(_ChannelEvent(p.start_tick, 'note',
                                         [p.note, p.velocity, max(1, bt - p.start_tick)]))
            active[n] = _PendingNote(n, v, bt)

        elif ev.is_note_off():
            n = ev.note
            if n in active:
                p = active.pop(n)
                out.append(_ChannelEvent(p.start_tick, 'note',
                                         [p.note, p.velocity, max(1, bt - p.start_tick)]))

        elif ev.event_type == MidiEventType.PROGRAM_CHANGE:
            out.append(_ChannelEvent(bt, 'program', [ev.data[0] if ev.data else 0]))

        elif ev.event_type == MidiEventType.CONTROL_CHANGE and len(ev.data) >= 2:
            cc, val = ev.data[0], ev.data[1]
            mapping = {
                MidiCC.VOLUME: 'volume', MidiCC.PAN: 'pan',
                MidiCC.MOD_WHEEL: 'mod', MidiCC.BEND_RANGE: 'bendrange',
                MidiCC.MOD_SPEED: 'mod_speed', MidiCC.SUSTAIN: 'damper',
                MidiCC.TRACK_LOOP_START: 'track_loop_start',
                MidiCC.TRACK_LOOP_END: 'track_loop_end',
            }
            if cc in mapping:
                v = (1 if val >= 64 else 0) if cc == MidiCC.SUSTAIN else val
                out.append(_ChannelEvent(bt, mapping[cc], [v]))

        elif ev.event_type == MidiEventType.PITCH_BEND and len(ev.data) >= 2:
            bend = (ev.data[1] << 7) | ev.data[0]
            out.append(_ChannelEvent(bt, 'bend',
                                     [max(-128, min(127, int((bend - 8192) / 64)))]))

    # Close remaining notes
    if active:
        ft = max((e.brseq_tick for e in out), default=0) + 48
        for p in active.values():
            out.append(_ChannelEvent(p.start_tick, 'note',
                                     [p.note, p.velocity, max(1, ft - p.start_tick)]))

    order = {'program': 0, 'volume': 1, 'pan': 2, 'bendrange': 3,
             'mod_speed': 4, 'mod': 5, 'bend': 6, 'damper': 7,
             'track_loop_start': 8, 'track_loop_end': 9,
             'tempo': 10, 'note': 11}
    out.sort(key=lambda e: (e.brseq_tick, order.get(e.type, 99)))
    return out


def _events_to_commands(
    events: list[_ChannelEvent], initial_prg: int | None,
) -> tuple[
    list[Command],
    list[tuple[int, str]],
    list[tuple[int, int, str]],
]:
    """Convert intermediate events to BRSEQ commands (notewait_off mode)."""
    cmds: list[Command] = []
    labels: list[tuple[int, str]] = []
    refs: list[tuple[int, int, str]] = []
    cur_tick = 0
    cur_prg = initial_prg

    for ev in events:
        gap = ev.brseq_tick - cur_tick
        if gap > 0:
            cmds.append(Command(opcode=MML.WAIT, args=[gap]))
            cur_tick += gap

        if ev.type == 'note':
            n, v, d = ev.args
            cmds.append(Command(opcode=n & 0x7F, args=[v & 0x7F, max(1, d)]))
        elif ev.type == 'program':
            p = ev.args[0]
            if p != cur_prg:
                cur_prg = p
                cmds.append(Command(opcode=MML.PRG, args=[p]))
        elif ev.type == 'volume':
            cmds.append(Command(opcode=MML.VOLUME, args=[ev.args[0] & 0x7F]))
        elif ev.type == 'pan':
            cmds.append(Command(opcode=MML.PAN, args=[ev.args[0] & 0x7F]))
        elif ev.type == 'mod':
            cmds.append(Command(opcode=MML.MOD_DEPTH, args=[ev.args[0] & 0x7F]))
        elif ev.type == 'mod_speed':
            cmds.append(Command(opcode=MML.MOD_SPEED, args=[ev.args[0] & 0x7F]))
        elif ev.type == 'bendrange':
            cmds.append(Command(opcode=MML.BEND_RANGE, args=[ev.args[0] & 0x7F]))
        elif ev.type == 'damper':
            cmds.append(Command(opcode=MML.DAMPER, args=[ev.args[0]]))
        elif ev.type == 'bend':
            cmds.append(Command(opcode=MML.PITCH_BEND, args=[ev.args[0]]))
        elif ev.type == 'tempo':
            cmds.append(Command(opcode=MML.TEMPO, args=[ev.args[0]]))
        elif ev.type == 'track_loop_start':
            cmds.append(Command(opcode=MML.LOOP_START, args=[ev.args[0] & 0x7F]))
        elif ev.type == 'track_loop_end':
            cmds.append(Command(opcode=MML.LOOP_END))
        elif ev.type == 'embedded':
            annotation: _EmbeddedAnnotation = ev.args[0]
            if annotation.label is not None:
                labels.append((len(cmds), annotation.label))
            elif annotation.command is not None:
                cmds.append(annotation.command)
                if (
                        annotation.target_arg is not None
                        and annotation.target_label is not None
                ):
                    refs.append((
                        len(cmds) - 1,
                        annotation.target_arg,
                        annotation.target_label,
                    ))

    return cmds, labels, refs


# Subroutine extraction

def _cmd_sig(cmd: Command) -> tuple:
    """Hashable signature for pattern matching."""
    return (cmd.opcode, tuple(cmd.args))


def _extract_subroutines(
    commands: list[Command],
    track_prefix: str,
    subroutines: dict[str, list[Command]],
) -> tuple[list[Command], list[tuple[int, str]]]:
    """
    Find repeated command sequences (>= _MIN_SUBROUTINE_CMDS commands,
    appearing 2+ times) and extract them as CALL/RET subroutines.

    Returns (modified_commands, call_refs) where call_refs is a list of
    (cmd_index_in_modified, subroutine_label_name) pairs.
    """
    call_refs: list[tuple[int, str]] = []

    if len(commands) < _MIN_SUBROUTINE_CMDS * 2:
        return commands, call_refs

    _TAG = '_call_target'
    result = list(commands)
    sub_count = len(subroutines)

    # Deduplication lookup
    _existing_sigs: dict[tuple, str] = {}
    for sname, scmds in subroutines.items():
        sig = tuple(_cmd_sig(c) for c in scmds)
        _existing_sigs[sig] = sname

    changed = True
    while changed:
        changed = False
        sigs = [_cmd_sig(c) for c in result]
        # Track which positions contain CALL commands so we don't create
        # nested subroutines (matches smfconv behavior).
        is_call = [c.opcode == MML.CALL for c in result]
        call_prefix = [0]
        for value in is_call:
            call_prefix.append(call_prefix[-1] + value)

        def contains_call(start: int, length: int) -> bool:
            return call_prefix[start + length] != call_prefix[start]

        # A repeated window longer than the minimum necessarily has a
        # repeated minimum-length prefix.  Most real-world MIDI command
        # streams do not, so avoid scanning every length from 64 down to 12.
        # This turns the common no-subroutine case from roughly 53 full
        # passes (and millions of temporary tuples) into one linear pass.
        # Retain only hashes here: a collision merely triggers the exact
        # search below, while keeping memory compact for very dense files.
        minimum_seen: set[int] = set()
        has_candidate = False
        for i in range(len(sigs) - _MIN_SUBROUTINE_CMDS + 1):
            if contains_call(i, _MIN_SUBROUTINE_CMDS):
                continue
            key_hash = hash(tuple(sigs[i:i + _MIN_SUBROUTINE_CMDS]))
            if key_hash in minimum_seen:
                has_candidate = True
                break
            minimum_seen.add(key_hash)
        if not has_candidate:
            break

        # "There is a non-overlapping repeat of length N" is monotonic: the
        # same two positions also repeat for every shorter length.  Binary
        # search therefore finds the longest eligible phrase in at most six
        # passes instead of scanning all 53 possible lengths.  The final pass
        # below still uses insertion order, preserving the old deterministic
        # choice when several phrases have the same maximum length.
        best_len = 0
        low = _MIN_SUBROUTINE_CMDS
        high = min(len(sigs) // 2, 64)
        while low <= high:
            slen = (low + high) // 2
            if _has_non_overlapping_repeat(sigs, call_prefix, slen):
                best_len = slen
                low = slen + 1
            else:
                high = slen - 1

        best_pos = (
            _first_non_overlapping_repeat(sigs, call_prefix, best_len)
            if best_len >= _MIN_SUBROUTINE_CMDS
            else []
        )

        if best_len < _MIN_SUBROUTINE_CMDS or len(best_pos) < 2:
            break

        # Dedup: reuse existing subroutine if identical
        new_sig = tuple(_cmd_sig(result[i]) for i in range(best_pos[0], best_pos[0] + best_len))
        existing_name = _existing_sigs.get(new_sig)

        if existing_name:
            sname = existing_name
        else:
            sname = f"{track_prefix}_{sub_count}"
            subroutines[sname] = list(result[best_pos[0]:best_pos[0] + best_len])
            _existing_sigs[new_sig] = sname
            sub_count += 1

        for pos in reversed(best_pos):
            call_cmd = Command(opcode=MML.CALL, args=[0])
            setattr(call_cmd, _TAG, sname)
            result[pos:pos + best_len] = [call_cmd]

        changed = True

    # Collect call references
    for i, cmd in enumerate(result):
        target = getattr(cmd, _TAG, None)
        if target is not None:
            call_refs.append((i, target))
            delattr(cmd, _TAG)

    return result, call_refs


def _has_non_overlapping_repeat(
        sigs: list[tuple],
        call_prefix: list[int],
        length: int,
) -> bool:
    """Return whether an eligible command window repeats without overlap."""
    first_positions: dict[tuple, int] = {}
    for i in range(len(sigs) - length + 1):
        if call_prefix[i + length] != call_prefix[i]:
            continue
        key = tuple(sigs[i:i + length])
        first = first_positions.get(key)
        if first is None:
            first_positions[key] = i
        elif i >= first + length:
            return True
    return False


def _first_non_overlapping_repeat(
        sigs: list[tuple],
        call_prefix: list[int],
        length: int,
) -> list[int]:
    """Return all positions for the first maximum-length repeated phrase."""
    seen: dict[tuple, list[int]] = {}
    for i in range(len(sigs) - length + 1):
        if call_prefix[i + length] != call_prefix[i]:
            continue
        key = tuple(sigs[i:i + length])
        seen.setdefault(key, []).append(i)

    for positions in seen.values():
        if len(positions) < 2:
            continue
        non_overlapping = _non_overlapping(positions, length)
        if len(non_overlapping) >= 2:
            return non_overlapping
    return []


def _non_overlapping(positions: list[int], length: int) -> list[int]:
    """Select non-overlapping positions from a sorted list."""
    out = []
    last_end = -1
    for p in sorted(positions):
        if p >= last_end:
            out.append(p)
            last_end = p + length
    return out


# Label offset resolution

def _resolve_label_offsets(
    all_tracks: dict[str, Track],
    all_labels: list[Label],
    label_refs: list[tuple[str, int, int, str]],
    extra_label_positions: list[tuple[str, int, str]] | None = None,
) -> list[Command]:
    """
    Resolve label offsets by doing a real serialization pass through the
    writer, capturing the exact byte positions it computes, then patching
    JUMP / CALL / OPEN_TRACK args to match.

    This is the only reliable approach because the writer may reorder
    tracks (it sorts by ``start_offset``). By serializing first, we get
    the writer's actual layout and can then set ``start_offset`` on each
    track so the final write reproduces the same order.
    """
    from pysar.core.format.rseq.writer import BrseqWriter

    writer = BrseqWriter()
    extra_label_positions = extra_label_positions or []

    label_names = [label.name for label in all_labels]
    duplicates = sorted({name for name in label_names if label_names.count(name) > 1})
    if duplicates:
        raise ValueError(f"Duplicate PySAR MIDI label(s): {', '.join(duplicates)}")

    # Pass 1: serialize with placeholder arguments.
    # Build a temporary BrseqData just for this serialization.
    temp_model = BrseqData(
        version=0x0100,
        labels=all_labels,
        tracks=all_tracks,
    )
    buf = io.BytesIO()
    label_to_offset = writer._write_data_block(buf, temp_model)

    # Keep the in-memory model executable immediately.  Previously the MIDI
    # importer left every command at placeholder offset zero, so the player
    # de-duplicated the entire sequence down to its first command until the
    # caller serialized and reopened it.
    command_stream = writer._ordered_commands(temp_model)
    command_offset = 0
    for command in command_stream:
        command.offset = command_offset
        command_offset += writer._command_size(command)
    temp_model.command_stream = list(command_stream)

    # Set start_offset on every track so the final write keeps the same order.
    for name, track in all_tracks.items():
        if isinstance(name, str) and name in label_to_offset:
            track.start_offset = label_to_offset[name]
            if track.label is not None:
                track.label.offset = label_to_offset[name]

    # Labels embedded in MIDI can sit in the middle of a generated track.
    # Resolve them against the exact command byte offsets from the writer's
    # first pass, without forcing the composer to know a numeric address.
    for track_name, cmd_idx, label_name in extra_label_positions:
        track = all_tracks.get(track_name)
        if track is None or not 0 <= cmd_idx < len(track.commands):
            raise ValueError(
                f"PySAR MIDI label '{label_name}' has no command at its position"
            )
        label_to_offset[label_name] = track.commands[cmd_idx].offset

    # Patch JUMP, CALL, and OPEN_TRACK arguments with real offsets.
    for track_name, cmd_idx, arg_idx, target in label_refs:
        trk = all_tracks.get(track_name)
        if target not in label_to_offset:
            raise ValueError(f"PySAR MIDI command refers to undefined label '{target}'")
        if trk and cmd_idx < len(trk.commands):
            trk.commands[cmd_idx].args[arg_idx] = label_to_offset[target]

    # Update Label objects used by the LABL block.
    for label in all_labels:
        if label.name in label_to_offset:
            label.offset = label_to_offset[label.name]

    return list(command_stream)


# Misc utilities

def _make_empty_brseq(timebase: int, tempo: int) -> BrseqData:
    """Minimal single-track BrseqData."""
    t = Track(label=Label(name="main", offset=0), commands=[
        Command(opcode=MML.TIMEBASE, args=[timebase]),
        Command(opcode=MML.TEMPO, args=[tempo]),
        Command(opcode=MML.FIN),
    ])
    return BrseqData(version=0x0100, labels=[Label(name="main", offset=0)], tracks={"main": t})
