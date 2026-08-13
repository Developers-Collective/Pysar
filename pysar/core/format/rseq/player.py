import math
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable

from pysar.core.format.rseq.mml import (
    MML, MMLEX, is_note,
    DEFAULT_TEMPO, DEFAULT_TIMEBASE, MAX_TRACKS,
    SEQ_VAR_DEFAULT, SEQ_LOCAL_VAR_COUNT, SEQ_GLOBAL_VAR_COUNT, SEQ_TRACK_VAR_COUNT,
    CALL_STACK_DEPTH,
)
from pysar.core.model.brseq import BrseqData, Command


# =============================================================================
# Player State
# =============================================================================

class PlayerState(IntEnum):
    """Player state."""
    STOPPED = 0
    PLAYING = 1
    PAUSED = 2


@dataclass
class CallStackEntry:
    """Entry in the call stack for CALL/RET."""
    return_flat_index: int
    loop_start_cmd: int = -1
    loop_count: int = 0


class MoveValue:
    """Nintendo-style linear move value updated once per sequence tick."""

    def __init__(self, value: int = 0):
        self._origin = value
        self._target = value
        self._frames = 0
        self._counter = 0

    def init_value(self, value: int) -> None:
        self._origin = value
        self._target = value
        self._frames = 0
        self._counter = 0

    def set_target(self, target: int, frames: int) -> None:
        self._origin = self.get_value()
        self._target = target
        self._frames = max(0, frames)
        self._counter = 0

    def update(self) -> None:
        if self._counter < self._frames:
            self._counter += 1

    def get_value(self) -> int:
        if self._counter >= self._frames or self._frames == 0:
            return self._target
        return int(self._origin + (self._target - self._origin) * self._counter / self._frames)

    @property
    def target(self) -> int:
        return self._target


@dataclass
class NoteRuntimeInfo:
    """Optional runtime metadata resolved from bank/wave information."""

    natural_duration_seconds: float | None = None
    ignore_note_off: bool = False


@dataclass
class ActiveNote:
    """An active (playing) note."""
    note: int
    velocity: int
    channel: int
    start_tick: int
    duration: int  # In ticks, -1 for no auto note-off
    program: int = 0
    natural_end_tick: int | None = None
    sweep_pitch: float = 0.0
    sweep_ticks: int = 0
    sweep_start_tick: int = 0

    def current_sweep_pitch(self, tick: int) -> float:
        """Current portamento/sweep offset in semitones."""
        if self.sweep_ticks <= 0 or self.sweep_pitch == 0.0:
            return 0.0
        elapsed = tick - self.sweep_start_tick
        if elapsed <= 0:
            return self.sweep_pitch
        if elapsed >= self.sweep_ticks:
            return 0.0
        return self.sweep_pitch * (self.sweep_ticks - elapsed) / self.sweep_ticks


@dataclass
class TrackState:
    """State of a single track."""
    # Position in the flat command list.
    track_index: int = 0
    flat_cmd_index: int = 0

    # Timing
    wait_ticks: int = 0

    # Flags
    active: bool = False
    finished: bool = False
    note_wait: bool = True
    note_finish_wait: bool = False
    tie: bool = False
    monophonic: bool = False
    porta: bool = False
    damper: bool = False
    muted: bool = False

    # Parameters
    program: int = 0
    bank: int = 0
    volume: MoveValue = field(default_factory=lambda: MoveValue(127))
    volume2: int = 127
    main_volume: int = 127
    pan: MoveValue = field(default_factory=lambda: MoveValue(64))
    surround_pan: MoveValue = field(default_factory=lambda: MoveValue(0))
    init_pan: int = 64
    transpose: int = 0
    pitch_bend: MoveValue = field(default_factory=lambda: MoveValue(0))
    bend_range: int = 2
    priority: int = 64
    velocity_range: int = 127
    sweep_pitch: float = 0.0

    # Modulation
    mod_depth: int = 0
    mod_speed: int = 16
    mod_type: int = 0
    mod_delay: int = 0
    mod_range: int = 1

    # Envelope
    attack: int = 0
    decay: int = 0
    sustain: int = 127
    release: int = 0
    env_hold: int = 0

    # Effects
    main_send: int = 127
    fx_send_a: int = 0
    fx_send_b: int = 0
    fx_send_c: int = 0
    lpf_cutoff: int = 64
    biquad_type: int = 0
    biquad_value: int = 0

    # Portamento
    porta_key: int = 60
    porta_time: int = 0

    # Call stack for CALL/RET commands.
    call_stack: list[CallStackEntry] = field(default_factory=list)

    # Loop (inline loop_start / loop_end)
    loop_start_cmd: int = -1
    loop_count: int = 0

    # Loop iteration tracking for non-realtime rendering.
    # Maps loop_start flat_cmd_index to the number of completed loops.
    loop_iterations: dict[int, int] = field(default_factory=dict)
    # Same idea for JUMP targets, where backwards jumps can loop forever.
    jump_iterations: dict[int, int] = field(default_factory=dict)

    # Variables
    variables: list[int] = field(default_factory=lambda: [SEQ_VAR_DEFAULT] * SEQ_TRACK_VAR_COUNT)

    # Active notes on this track
    active_notes: list[ActiveNote] = field(default_factory=list)


@dataclass
class PlayerContext:
    """Complete player context."""
    # Global state
    state: PlayerState = PlayerState.STOPPED

    # Timing
    tempo: int = DEFAULT_TEMPO
    timebase: int = DEFAULT_TIMEBASE
    tick_counter: int = 0
    tempo_ratio: float = 1.0

    # Fractional tick accumulator
    _tick_accumulator: float = 0.0

    # Tracks
    tracks: list[TrackState] = field(default_factory=lambda: [TrackState() for _ in range(MAX_TRACKS)])

    # Variables
    local_vars: list[int] = field(default_factory=lambda: [SEQ_VAR_DEFAULT] * SEQ_LOCAL_VAR_COUNT)
    global_vars: list[int] = field(default_factory=lambda: [SEQ_VAR_DEFAULT] * SEQ_GLOBAL_VAR_COUNT)

    # Compare flag (for IF commands)
    cmp_flag: bool = False

    # Callbacks
    note_on_callback: Callable[[int, int, int, int, int], None] | None = None   # track, note, vel, program, bank
    note_off_callback: Callable[[int, int], None] | None = None                 # track, note
    note_change_callback: Callable[
        [int, int, int, int, int, int, bool, int, int], None
    ] | None = None  # track, old_note, new_note, vel, program, bank, porta, porta_time, porta_key
    control_change_callback: Callable[[int, int, int], None] | None = None      # track, cc, value
    program_change_callback: Callable[[int, int], None] | None = None           # track, program

    def get_variable(self, index: int, track_state: TrackState) -> int:
        if 0 <= index < 16:
            return self.local_vars[index]
        elif 16 <= index < 32:
            return self.global_vars[index - 16]
        elif 32 <= index < 48:
            return track_state.variables[index - 32]
        return SEQ_VAR_DEFAULT

    def set_variable(self, index: int, value: int, track_state: TrackState) -> None:
        if 0 <= index < 16:
            self.local_vars[index] = value
        elif 16 <= index < 32:
            self.global_vars[index - 16] = value
        elif 32 <= index < 48:
            track_state.variables[index - 32] = value


# Sequence player

class SequencePlayer:
    """
    Software BRSEQ sequence player.

    Matches the C# reference implementation behavior:
      - Builds a FLAT command list sorted by offset (like C# ParseCommands)
      - Player navigates sequentially (like C# NextNode traversal)
      - Labels are pass-through (not track boundaries)
      - OPEN_TRACK starts a child at the given offset in the flat list
      - JUMP/CALL navigate to offset positions in the flat list

    Usage:
        player = SequencePlayer()
        player.load(brseq_data)
        player.set_note_callbacks(on_note_on=..., on_note_off=...)
        player.play_blocking()
    """

    def __init__(self):
        self._brseq: BrseqData | None = None
        self._ctx = PlayerContext()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._note_runtime_resolver: Callable[[TrackState, int, int], NoteRuntimeInfo | None] | None = None

        # Flat command list, similar to C# RSEQ.Commands.
        # All commands sorted by offset, labels are non-blocking markers.
        # The player walks this list sequentially using flat_cmd_index.
        self._flat_commands: list[Command] = []

        # Offset-to-flat-index lookup.
        self._offset_to_flat_index: dict[int, int] = {}

        # Loop limiting for non-realtime rendering.
        # 0 = unlimited (real-time playback), >0 = max iterations for infinite loops
        self._max_loop_iterations: int = 0

        # Set while a command is executing so offline renderers can bind a
        # performed note back to its semantic BRSEQ source command.
        self._render_source_offset: int | None = None

    @property
    def is_playing(self) -> bool:
        return self._ctx.state == PlayerState.PLAYING

    @property
    def is_paused(self) -> bool:
        return self._ctx.state == PlayerState.PAUSED

    @property
    def tick(self) -> int:
        return self._ctx.tick_counter

    @property
    def tempo(self) -> int:
        return self._ctx.tempo

    @property
    def timebase(self) -> int:
        return self._ctx.timebase

    def load(
            self,
            brseq: BrseqData,
            start_label: str | None = None,
            start_offset: int | None = None,
    ) -> None:
        """
        Load a sequence for playback.

        This builds a flat command list from all tracks, sorted by offset,
        matching the C# RSEQ.ParseCommands() behavior where the player
        walks sequentially through the entire command stream via NextNode.

        Args:
            brseq: The sequence data to load.
            start_label: Optional label name to start from.
            start_offset: Optional byte offset to start from (takes priority
                          over start_label). Used by BRSAR's seq_label_offset.
        """
        with self._lock:
            self._brseq = brseq
            self._ctx = PlayerContext()

            # Build the flat command list.
            # Collect ALL commands from ALL tracks, keyed by offset.
            # This matches C#: Commands.AddRange(Data.Labels + Data.Commands)
            # then OrderBy(offset).
            seen_offsets: set[int] = set()
            all_cmds: list[Command] = []

            for track in brseq.tracks.values():
                for cmd in track.commands:
                    off = cmd.offset if cmd.offset is not None else -1
                    if off >= 0 and off not in seen_offsets:
                        seen_offsets.add(off)
                        all_cmds.append(cmd)
                    elif off < 0:
                        all_cmds.append(cmd)

            # Sort by offset (commands without offset go to the end)
            all_cmds.sort(key=lambda c: c.offset if c.offset is not None else 0x7FFFFFFF)
            self._flat_commands = all_cmds

            # Build the offset-to-index lookup.
            self._offset_to_flat_index.clear()
            for i, cmd in enumerate(self._flat_commands):
                if cmd.offset is not None and cmd.offset not in self._offset_to_flat_index:
                    self._offset_to_flat_index[cmd.offset] = i

            # Find the starting position.
            start_flat_idx = 0

            if start_offset is not None and start_offset > 0:
                idx = self._offset_to_flat_index.get(start_offset)
                if idx is not None:
                    start_flat_idx = idx
            elif start_label:
                for label in brseq.labels:
                    if label.name == start_label:
                        idx = self._offset_to_flat_index.get(label.offset)
                        if idx is not None:
                            start_flat_idx = idx
                        break

            # Set up track 0 as the main track
            self._ctx.tracks[0].flat_cmd_index = start_flat_idx
            self._ctx.tracks[0].active = True
            self._ctx.tracks[0].track_index = 0

    def play(self) -> None:
        """Start playback (call update() in a loop after this)."""
        with self._lock:
            if self._brseq is None:
                return
            self._ctx.state = PlayerState.PLAYING

    def pause(self) -> None:
        with self._lock:
            if self._ctx.state == PlayerState.PLAYING:
                self._ctx.state = PlayerState.PAUSED

    def resume(self) -> None:
        with self._lock:
            if self._ctx.state == PlayerState.PAUSED:
                self._ctx.state = PlayerState.PLAYING

    def stop(self) -> None:
        """Stop playback and release all notes."""
        with self._lock:
            self._ctx.state = PlayerState.STOPPED
            for track_state in self._ctx.tracks:
                self._release_track_notes(track_state)
                track_state.active = False
                track_state.finished = True

    def set_note_callbacks(
            self,
            on_note_on: Callable[[int, int, int, int, int], None] | None = None,
            on_note_off: Callable[[int, int], None] | None = None,
            on_note_change: Callable[
                [int, int, int, int, int, int, bool, int, int], None
            ] | None = None,
    ) -> None:
        """Set callbacks for note events.

        Args:
            on_note_on: Called with (track_index, note, velocity, program, bank)
            on_note_off: Called with (track_index, note)
            on_note_change: Called with
                (track_index, old_note, new_note, velocity, program, bank,
                 porta_enabled, porta_time, porta_key)
        """
        self._ctx.note_on_callback = on_note_on
        self._ctx.note_off_callback = on_note_off
        self._ctx.note_change_callback = on_note_change

    def set_control_callbacks(
            self,
            on_control_change: Callable[[int, int, int], None] | None = None,
            on_program_change: Callable[[int, int], None] | None = None,
    ) -> None:
        self._ctx.control_change_callback = on_control_change
        self._ctx.program_change_callback = on_program_change

    def set_note_runtime_resolver(
            self,
            resolver: Callable[[TrackState, int, int], NoteRuntimeInfo | None] | None,
    ) -> None:
        """Attach optional bank/wave metadata for note lifetime estimation."""
        self._note_runtime_resolver = resolver

    def _release_track_notes(self, track_state: TrackState) -> None:
        for note in track_state.active_notes:
            if self._ctx.note_off_callback:
                self._ctx.note_off_callback(track_state.track_index, note.note)
        track_state.active_notes.clear()
        track_state.note_finish_wait = False

    def _resolve_note_runtime(
            self,
            track_state: TrackState,
            note: int,
            velocity: int,
    ) -> NoteRuntimeInfo:
        if self._note_runtime_resolver is None:
            return NoteRuntimeInfo()
        info = self._note_runtime_resolver(track_state, note, velocity)
        return info if info is not None else NoteRuntimeInfo()

    @staticmethod
    def _events_to_timed_events(
            events: list[dict],
            timebase: int,
    ) -> list[tuple[float, dict]]:
        """Convert tick events into absolute seconds with a real tempo map."""
        if timebase <= 0:
            timebase = DEFAULT_TIMEBASE

        timed_events: list[tuple[float, dict]] = []
        tempo = DEFAULT_TEMPO
        last_tick = 0
        current_time = 0.0

        for ev in events:
            tick = ev.get('tick', last_tick)
            delta_ticks = max(0, tick - last_tick)
            current_time += delta_ticks * 60.0 / (max(1, tempo) * timebase)
            last_tick = tick
            timed_events.append((current_time, ev))
            if ev.get('type') == 'tempo':
                tempo = ev.get('tempo', tempo) or tempo

        return timed_events

    def _get_track_pitch_state(self, track_state: TrackState) -> tuple[float, int]:
        """Return current pitch offset in semitones and the bend range needed to render it."""
        pitch = (track_state.pitch_bend.get_value() / 128.0) * track_state.bend_range
        bend_range = max(1, track_state.bend_range)

        if len(track_state.active_notes) == 1:
            note = track_state.active_notes[0]
            pitch += note.current_sweep_pitch(self._ctx.tick_counter)
            bend_range = max(bend_range, int(math.ceil(abs(note.sweep_pitch))) or 1)

        return pitch, bend_range

    @staticmethod
    def _midi_pitch_bend_value(semitones: float, bend_range: int) -> int:
        if bend_range <= 0:
            return 8192
        normalized = max(-1.0, min(1.0, semitones / bend_range))
        return max(0, min(16383, 8192 + int(round(normalized * 8191))))

    @staticmethod
    def _set_pitch_bend_range(fs, channel: int, semitones: int) -> None:
        semitones = max(0, min(127, semitones))
        fs.cc(channel, 101, 0)
        fs.cc(channel, 100, 0)
        fs.cc(channel, 6, semitones)
        fs.cc(channel, 38, 0)
        fs.cc(channel, 101, 127)
        fs.cc(channel, 100, 127)

    @staticmethod
    def _apply_pitch_bend(fs, channel: int, value: int) -> None:
        try:
            fs.pitch_bend(channel, value)
            return
        except Exception:
            pass
        try:
            fs.pitch_bend(channel, value - 8192)
        except Exception:
            pass

    def update(self, delta_ms: float) -> bool:
        """
        Update the player state.

        Converts wall-clock time into ticks using NW4R formula:
            ticks_per_second = (timebase * tempo) / 60
        Accumulates fractional ticks so no time is lost.
        """
        with self._lock:
            if self._ctx.state != PlayerState.PLAYING:
                return self._ctx.state != PlayerState.STOPPED

            if self._brseq is None:
                return False

            ticks_per_ms = (self._ctx.timebase * self._ctx.tempo * self._ctx.tempo_ratio) / 60000.0
            self._ctx._tick_accumulator += delta_ms * ticks_per_ms

            whole_ticks = int(self._ctx._tick_accumulator)
            self._ctx._tick_accumulator -= whole_ticks

            for _ in range(whole_ticks):
                self._process_tick()
                self._ctx.tick_counter += 1

                if all(not t.active or t.finished for t in self._ctx.tracks):
                    if any(t.active_notes for t in self._ctx.tracks):
                        continue
                    self._ctx.state = PlayerState.STOPPED
                    return False

            return True

    def play_blocking(self, update_interval_ms: float = 1.0) -> None:
        """Play the sequence, blocking until finished."""
        self.play()
        last_time = time.perf_counter()
        while self.is_playing:
            current_time = time.perf_counter()
            delta_ms = (current_time - last_time) * 1000.0
            last_time = current_time
            if not self.update(delta_ms):
                break
            time.sleep(update_interval_ms / 1000.0)

    def play_threaded(self) -> None:
        """Play the sequence in a background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.play_blocking, daemon=True)
        self._thread.start()

    def play_audio_blocking(self, sample_rate: int = 44100, loop_count: int = 2) -> None:
        """
        Play the sequence as audio using sounddevice (sine wave synthesis).

        This renders the entire sequence to events first, then synthesizes
        audio in real-time using simple sine oscillators per active note.

        Args:
            sample_rate: Audio sample rate (default 44100)
            loop_count: How many times loops repeat in render
        """
        import numpy as np
        try:
            import sounddevice as sd
        except ImportError:
            raise ImportError(
                "sounddevice is required for audio playback. "
                "Install it with: pip install sounddevice"
            )

        # Render all events first
        events = self.render_events(max_ticks=5_000_000, max_loops=loop_count)
        if not events:
            return

        # Get timebase
        timebase = self._ctx.timebase if self._ctx.timebase > 0 else DEFAULT_TIMEBASE
        timed_events = self._events_to_timed_events(events, timebase)

        # Build a timeline of note_on / note_off with absolute seconds
        # Using the C# timing model: seconds = ticks / 96.0, then scaled by 120/tempo
        note_events: list[tuple[float, str, int, int, int]] = []  # (time_sec, type, track, note, velocity)
        tempo = DEFAULT_TEMPO

        for ev in events:
            if ev['type'] == 'tempo':
                tempo = ev['tempo']
                continue

            tick = ev['tick']
            # C# model: TimeSpan.FromSeconds(ticks / 96.0) * (120.0 / tempo)
            time_sec = (tick / 96.0) * (120.0 / tempo)

            if ev['type'] == 'note_on':
                note_events.append((time_sec, 'on', ev['track'], ev['note'], ev['velocity']))
            elif ev['type'] == 'note_off':
                note_events.append((time_sec, 'off', ev['track'], ev['note'], 0))

        if not note_events:
            return

        note_events.sort(key=lambda x: x[0])
        total_seconds = note_events[-1][0] + 1.0  # 1 second of tail

        # Synthesize audio
        total_samples = int(total_seconds * sample_rate)
        audio = np.zeros(total_samples, dtype=np.float32)

        # Process note events: for each note_on, find its note_off, generate sine
        active: dict[tuple[int, int], list[tuple[float, int]]] = {}  # (track, note) -> [(start_time, velocity)]
        all_note_spans: list[tuple[float, float, int, int]] = []  # (start, end, midi_note, velocity)

        for time_sec, etype, track, note, vel in note_events:
            key = (track, note)
            if etype == 'on':
                if key not in active:
                    active[key] = []
                active[key].append((time_sec, vel))
            elif etype == 'off':
                if key in active and active[key]:
                    start_time, start_vel = active[key].pop(0)
                    all_note_spans.append((start_time, time_sec, note, start_vel))
                    if not active[key]:
                        del active[key]

        # Close remaining active notes
        for key, entries in active.items():
            _, note_num = key
            for start_time, vel in entries:
                all_note_spans.append((start_time, total_seconds - 0.5, note_num, vel))

        # Render each note span as a sine wave with ADSR envelope
        for start, end, midi_note, velocity in all_note_spans:
            freq = 440.0 * (2.0 ** ((midi_note - 69) / 12.0))
            amplitude = (velocity / 127.0) * 0.15  # Scale down to avoid clipping

            start_sample = int(start * sample_rate)
            end_sample = min(int(end * sample_rate), total_samples)
            if start_sample >= end_sample:
                continue

            n_samples = end_sample - start_sample
            t = np.arange(n_samples, dtype=np.float32) / sample_rate

            # Simple sine with attack/release envelope
            wave = np.sin(2.0 * np.pi * freq * t) * amplitude

            # Quick attack (5ms), sustain, quick release (20ms)
            attack_samples = min(int(0.005 * sample_rate), n_samples)
            release_samples = min(int(0.02 * sample_rate), n_samples)

            if attack_samples > 0:
                wave[:attack_samples] *= np.linspace(0, 1, attack_samples, dtype=np.float32)
            if release_samples > 0:
                wave[-release_samples:] *= np.linspace(1, 0, release_samples, dtype=np.float32)

            audio[start_sample:end_sample] += wave

        # Clip to prevent distortion
        peak = np.max(np.abs(audio))
        if peak > 0.9:
            audio *= 0.9 / peak

        # Play
        sd.play(audio, samplerate=sample_rate, blocking=True)

    def play_with_soundfont(
            self,
            sf2_path: str,
            sample_rate: int = 44100,
            loop_count: int = 1,
            audio_driver: str | None = None,
            default_program: int | dict[int, int] | None = None,
            note_override: int | None = None,
    ) -> None:
        """
        Play the sequence using a SoundFont via FluidSynth.

        Renders events first, then plays them in real-time through FluidSynth
        with the provided SF2 for game-accurate instrument playback.

        Args:
            sf2_path: Path to the SF2 soundfont file.
            sample_rate: Audio sample rate (default 44100).
            loop_count: How many times loops repeat in render.
            audio_driver: FluidSynth audio driver (None = auto-detect).
            default_program: Program to use when the sequence has variable-based PRG.
                Can be a single int (applied to track 0) or a dict mapping
                track_no -> program for per-track defaults.
            note_override: If set, override all note values with this MIDI note.
                          Used to select a specific key-range variation.
        """
        try:
            import fluidsynth
        except ImportError as exc:
            err = str(exc)
            if "Couldn't find the FluidSynth library" in err:
                raise ImportError(
                    "pyfluidsynth is installed, but the native FluidSynth library "
                    "(libfluidsynth) was not found. Please install it first."
                ) from exc
            raise ImportError(
                "pyfluidsynth is required for soundfont playback. "
                "Install it with: pip install pyfluidsynth"
            ) from exc

        events = self.render_events(max_ticks=5_000_000, max_loops=loop_count)
        if not events:
            return

        timebase = self._ctx.timebase if self._ctx.timebase > 0 else DEFAULT_TIMEBASE

        # Build list of audio drivers to try
        if audio_driver is not None:
            drivers_to_try = [audio_driver]
        else:
            import sys
            if sys.platform == 'win32':
                drivers_to_try = ['wasapi', 'dsound', 'waveout']
            elif sys.platform == 'darwin':
                drivers_to_try = ['coreaudio']
            else:
                drivers_to_try = ['pulseaudio', 'alsa', 'jack']

        fs = fluidsynth.Synth(samplerate=float(sample_rate), gain=20.0)
        try:
            # Try each driver until one works
            started = False
            active_driver = None
            for drv in drivers_to_try:
                try:
                    fs.start(driver=drv)
                    started = True
                    active_driver = drv
                    break
                except Exception:
                    continue
            if not started:
                raise RuntimeError(
                    f"Could not start FluidSynth audio. Tried drivers: {drivers_to_try}. "
                    f"Make sure an audio output device is available."
                )
            sfid = fs.sfload(sf2_path)
            if sfid < 0:
                raise RuntimeError(f"FluidSynth failed to load SoundFont: {sf2_path}")

            # Track current program per channel for program_select
            channel_programs: dict[int, int] = {}
            bend_ranges: dict[int, int] = {}

            # Build per-channel default program map from the dict or scalar
            default_programs_map: dict[int, int] = {}
            if isinstance(default_program, dict):
                default_programs_map = default_program
            elif default_program is not None:
                default_programs_map[0] = default_program

            timed_events = self._events_to_timed_events(events, timebase)
            adjusted_events = timed_events

            clock_start = time.perf_counter()

            for time_sec, ev in adjusted_events:
                elapsed = time.perf_counter() - clock_start
                wait = time_sec - elapsed
                if wait > 0:
                    time.sleep(wait)

                self._dispatch_fluidsynth_event(
                    fs,
                    sfid,
                    ev,
                    channel_programs=channel_programs,
                    default_programs_map=default_programs_map,
                    bend_ranges=bend_ranges,
                    note_override=note_override,
                )

            # Let final notes ring out
            final_tail = adjusted_events[-1][0] if adjusted_events else 0.0
            time.sleep(min(2.0, max(0.1, final_tail * 0.1 + 0.1)))

        finally:
            fs.delete()

    def render_to_wav(
            self,
            sf2_path: str,
            output_path: str,
            sample_rate: int = 44100,
            loop_count: int = 1,
            default_program: int | dict[int, int] | None = None,
            note_override: int | None = None,
            max_ticks: int = 5_000_000,
    ) -> None:
        """
        Render the sequence to a WAV file using FluidSynth offline synthesis.

        Args:
            sf2_path: Path to the SF2 soundfont file.
            output_path: Path for the output WAV file.
            sample_rate: Audio sample rate (default 44100).
            loop_count: How many times loops repeat in render.
            default_program: Program to use when the sequence has variable-based PRG.
                Can be a single int (applied to track 0) or a dict mapping
                track_no -> program for per-track defaults.
            note_override: If set, override all note values with this MIDI note.
        """
        try:
            import fluidsynth
        except ImportError:
            raise ImportError(
                "pyfluidsynth is required for soundfont rendering. "
                "Install it with: pip install pyfluidsynth"
            )

        events = self.render_events(max_ticks=max_ticks, max_loops=loop_count)
        if not events:
            raise RuntimeError("No events to render")
        timed_events = self._events_to_timed_events(events, self._ctx.timebase if self._ctx.timebase > 0 else DEFAULT_TIMEBASE)

        fs = fluidsynth.Synth(samplerate=float(sample_rate), gain=1.0)
        try:
            sfid = fs.sfload(sf2_path)
            if sfid < 0:
                raise RuntimeError(f"FluidSynth failed to load SoundFont: {sf2_path}")

            channel_programs: dict[int, int] = {}
            bend_ranges: dict[int, int] = {}

            # Build per-channel default program map from the dict or scalar
            default_programs_map: dict[int, int] = {}
            if isinstance(default_program, dict):
                default_programs_map = default_program
            elif default_program is not None:
                default_programs_map[0] = default_program

            max_time = 0.0
            for time_sec, _ in timed_events:
                max_time = max(max_time, time_sec)
            # Small buffer so the very last sample doesn't clip
            total_duration = max_time + 0.25
            total_samples = int(total_duration * sample_rate)
            import numpy as np
            all_samples = np.zeros(total_samples * 2, dtype=np.float32)  # stereo interleaved
            chunk_size = 1024
            current_sample = 0

            processed_events = [{**ev, '_time': time_sec} for time_sec, ev in timed_events]

            event_idx = 0
            tempo = DEFAULT_TEMPO

            while current_sample < total_samples:
                current_time = current_sample / sample_rate

                # Process all events up to current_time
                while event_idx < len(processed_events):
                    ev = processed_events[event_idx]
                    ev_time = ev.get('_time', 0.0)
                    if ev_time > current_time:
                        break

                    self._dispatch_fluidsynth_event(
                        fs,
                        sfid,
                        ev,
                        channel_programs=channel_programs,
                        default_programs_map=default_programs_map,
                        bend_ranges=bend_ranges,
                        note_override=note_override,
                    )

                    event_idx += 1

                # Synthesize a chunk
                samples_left = total_samples - current_sample
                n = min(chunk_size, samples_left)
                chunk = fs.get_samples(n)
                # chunk is a numpy array of interleaved stereo float samples
                all_samples[current_sample * 2:(current_sample + n) * 2] = chunk[:n * 2]
                current_sample += n

            # Convert to 16-bit PCM and write WAV
            # FluidSynth get_samples() returns float samples in int16 range (-32768..32767)
            import wave
            pcm_int16 = np.clip(all_samples, -32768, 32767).astype(np.int16)

            with wave.open(output_path, 'wb') as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(pcm_int16.tobytes())

        finally:
            fs.delete()

    # Tick-level processing

    def _process_tick(self) -> None:
        """Process one tick of the sequence."""
        for track_state in self._ctx.tracks:
            if not track_state.active:
                continue

            # 1. Expire notes whose duration has elapsed.
            #    even on finished tracks so notes sustain naturally.
            track_state.volume.update()
            track_state.pan.update()
            track_state.surround_pan.update()
            track_state.pitch_bend.update()
            self._update_notes(track_state)

            if track_state.finished:
                continue

            if track_state.note_finish_wait:
                if track_state.active_notes:
                    continue
                track_state.note_finish_wait = False

            # 2. If we're waiting, decrement and skip command processing
            #    When wait reaches 0, fall through to execute on this tick
            #    (WAIT N means "delay N ticks", not "delay N+1 ticks")
            if track_state.wait_ticks > 0:
                track_state.wait_ticks -= 1
                if track_state.wait_ticks > 0:
                    continue

            # 3. Execute commands until we hit a wait, note, or end.
            safety = 0
            while (
                    not track_state.finished
                    and track_state.wait_ticks <= 0
                    and not track_state.note_finish_wait
            ):
                if not self._execute_next_command(track_state):
                    break
                safety += 1
                if safety > 10000:
                    track_state.finished = True
                    break

    def _update_notes(self, track_state: TrackState) -> None:
        """Release notes whose duration has expired.

        Duration semantics:
          > 0: release after that many ticks
            0: "fire and forget" - release when the track finishes
                (the sample plays out naturally via the sound engine)
           -1: tied note, never auto-released
        """
        finished_notes: list[ActiveNote] = []
        for note in track_state.active_notes:
            natural_end = note.natural_end_tick is not None and self._ctx.tick_counter >= note.natural_end_tick
            auto_release = note.duration > 0 and (self._ctx.tick_counter - note.start_tick) >= note.duration
            if natural_end or auto_release:
                finished_notes.append(note)

        for note in finished_notes:
            track_state.active_notes.remove(note)
            if self._ctx.note_off_callback:
                self._ctx.note_off_callback(track_state.track_index, note.note)

    # Command execution using the flat command list

    def _execute_next_command(self, track_state: TrackState) -> bool:
        """Execute the next command from the flat list.

        Returns False if the track has ended or should stop executing
        commands this tick.
        """
        idx = track_state.flat_cmd_index

        # End of flat command list
        if idx >= len(self._flat_commands):
            track_state.finished = True
            return False

        cmd = self._flat_commands[idx]
        self._render_source_offset = cmd.offset
        track_state.flat_cmd_index = idx + 1

        # Handle IF prefix: skip if compare flag is false
        if cmd.has_if and not self._ctx.cmp_flag:
            return True

        # RANDOM/VARIABLE apply to the command's primary argument.
        def get_arg(index: int, default: int = 0) -> int:
            if index >= len(cmd.args):
                return default
            if index == 0:
                for prefix in cmd.prefixes:
                    if prefix.type == MML.RANDOM and prefix.args and len(prefix.args) >= 2:
                        import random
                        return random.randint(prefix.args[0], prefix.args[1])
                    if prefix.type == MML.VARIABLE and prefix.args:
                        return self._ctx.get_variable(prefix.args[0], track_state)
            return cmd.args[index]

        def get_note_length(default: int) -> int:
            if len(cmd.args) < 2:
                return default
            for prefix in cmd.prefixes:
                if prefix.type == MML.RANDOM and prefix.args and len(prefix.args) >= 2:
                    import random
                    return random.randint(prefix.args[0], prefix.args[1])
                if prefix.type == MML.VARIABLE and prefix.args:
                    return self._ctx.get_variable(prefix.args[0], track_state)
            return cmd.args[1]

        def get_time_arg(default: int = 0) -> int:
            for prefix in cmd.prefixes:
                if prefix.type == MML.TIME and prefix.args:
                    return prefix.args[0]
                if prefix.type == MML.TIME_RANDOM and prefix.args and len(prefix.args) >= 2:
                    import random
                    return random.randint(prefix.args[0], prefix.args[1])
                if prefix.type == MML.TIME_VARIABLE and prefix.args:
                    return self._ctx.get_variable(prefix.args[0], track_state)
            return default

        mml = cmd.get_mml()

        def emit_cc(cc: int, value: int) -> None:
            if self._ctx.control_change_callback:
                self._ctx.control_change_callback(
                    track_state.track_index,
                    cc,
                    max(0, min(127, value)),
                )

        # Notes (opcode 0x00-0x7F)
        if is_note(cmd.opcode):
            velocity = cmd.args[0] if cmd.args else 100
            duration = get_note_length(self._ctx.timebase)
            self._handle_note(track_state, cmd.opcode, velocity, duration)
            return True

        # Timing
        if mml == MML.WAIT:
            wait = get_arg(0, 0)
            if wait > 0:
                track_state.wait_ticks = wait
            return True

        # Program / instrument
        elif mml == MML.PRG:
            track_state.program = get_arg(0, 0)
            if self._ctx.program_change_callback:
                self._ctx.program_change_callback(track_state.track_index, track_state.program)

        elif mml == MML.TEMPO:
            self._ctx.tempo = get_arg(0, DEFAULT_TEMPO)

        elif mml == MML.TIMEBASE:
            self._ctx.timebase = get_arg(0, DEFAULT_TIMEBASE)

        # Volume / pan / expression
        elif mml == MML.VOLUME:
            vol_value = get_arg(0, 127)
            time_arg = get_time_arg(0)
            track_state.volume.set_target(vol_value, time_arg)
            if time_arg <= 0:
                emit_cc(11, vol_value)  # Emit CC11 (Expression) for dynamic volume control

        elif mml == MML.VOLUME2:
            track_state.volume2 = get_arg(0, 127)
            emit_cc(11, track_state.volume2)

        elif mml == MML.MAIN_VOLUME:
            track_state.main_volume = get_arg(0, 127)
            emit_cc(7, track_state.main_volume)

        elif mml == MML.PAN:
            pan_value = get_arg(0, 64)
            time_arg = get_time_arg(0)
            track_state.pan.set_target(pan_value, time_arg)
            if time_arg <= 0:
                emit_cc(10, pan_value)  # Emit CC10 (Pan) for stereo positioning

        elif mml == MML.INIT_PAN:
            track_state.init_pan = get_arg(0, 64)

        elif mml == MML.SURROUND_PAN:
            track_state.surround_pan.set_target(get_arg(0, 0), get_time_arg(0))

        elif mml == MML.TRANSPOSE:
            track_state.transpose = get_arg(0, 0)

        elif mml == MML.PITCH_BEND:
            track_state.pitch_bend.set_target(get_arg(0, 0), get_time_arg(0))

        elif mml == MML.BEND_RANGE:
            track_state.bend_range = get_arg(0, 2)

        elif mml == MML.VELOCITY_RANGE:
            track_state.velocity_range = get_arg(0, 127)

        elif mml == MML.PRIO:
            track_state.priority = get_arg(0, 64)

        # Flags and toggles
        elif mml == MML.NOTE_WAIT:
            track_state.note_wait = get_arg(0, 1) != 0

        elif mml == MML.TIE:
            track_state.tie = get_arg(0, 0) != 0
            self._release_track_notes(track_state)

        elif mml == MML.MONOPHONIC:
            track_state.monophonic = get_arg(0, 0) != 0
            if track_state.monophonic:
                self._release_track_notes(track_state)

        elif mml == MML.PORTA:
            track_state.porta_key = max(0, min(127, get_arg(0, 0) + track_state.transpose))
            track_state.porta = True

        elif mml == MML.PORTA_SW:
            track_state.porta = get_arg(0, 0) != 0
            emit_cc(65, 127 if track_state.porta else 0)

        elif mml == MML.PORTA_TIME:
            track_state.porta_time = get_arg(0, 0)
            emit_cc(5, track_state.porta_time)

        elif mml == MML.DAMPER:
            track_state.damper = get_arg(0, 0) != 0
            emit_cc(64, 127 if track_state.damper else 0)

        elif mml == MML.MUTE:
            track_state.muted = get_arg(0, 0) != 0

        # Modulation
        elif mml == MML.MOD_DEPTH:
            track_state.mod_depth = get_arg(0, 0)
            emit_cc(1, track_state.mod_depth)

        elif mml == MML.MOD_SPEED:
            track_state.mod_speed = get_arg(0, 16)

        elif mml == MML.MOD_TYPE:
            track_state.mod_type = get_arg(0, 0)

        elif mml == MML.MOD_DELAY:
            track_state.mod_delay = get_arg(0, 0)

        elif mml == MML.MOD_RANGE:
            track_state.mod_range = get_arg(0, 1)

        # Envelope (ADSR)
        elif mml == MML.ATTACK:
            track_state.attack = get_arg(0, 0)

        elif mml == MML.DECAY:
            track_state.decay = get_arg(0, 0)

        elif mml == MML.SUSTAIN:
            track_state.sustain = get_arg(0, 127)

        elif mml == MML.RELEASE:
            track_state.release = get_arg(0, 0)

        elif mml == MML.ENV_HOLD:
            track_state.env_hold = get_arg(0, 0)

        elif mml == MML.ENV_RESET:
            track_state.attack = 0
            track_state.decay = 0
            track_state.sustain = 127
            track_state.release = 0
            track_state.env_hold = 0

        elif mml == MML.SWEEP_PITCH:
            track_state.sweep_pitch = get_arg(0, 0) / 64.0

        # Effect sends
        elif mml == MML.MAINSEND:
            track_state.main_send = get_arg(0, 127)

        elif mml == MML.FXSEND_A:
            track_state.fx_send_a = get_arg(0, 0)
            emit_cc(91, track_state.fx_send_a)

        elif mml == MML.FXSEND_B:
            track_state.fx_send_b = get_arg(0, 0)
            emit_cc(93, track_state.fx_send_b)

        elif mml == MML.FXSEND_C:
            track_state.fx_send_c = get_arg(0, 0)
            emit_cc(94, track_state.fx_send_c)

        elif mml == MML.LPF_CUTOFF:
            track_state.lpf_cutoff = get_arg(0, 64)
            emit_cc(74, track_state.lpf_cutoff)

        elif mml == MML.BIQUAD_TYPE:
            track_state.biquad_type = get_arg(0, 0)

        elif mml == MML.BIQUAD_VALUE:
            track_state.biquad_value = get_arg(0, 0)
            emit_cc(71, track_state.biquad_value)

        # Loops
        elif mml == MML.LOOP_START:
            count = get_arg(0, 0)
            track_state.loop_start_cmd = track_state.flat_cmd_index
            track_state.loop_count = count  # 0 = infinite

        elif mml == MML.LOOP_END:
            if track_state.loop_start_cmd >= 0:
                if track_state.loop_count == 0:
                    # Infinite loop, check the render limit.
                    if self._max_loop_iterations > 0:
                        key = track_state.loop_start_cmd
                        iters = track_state.loop_iterations.get(key, 0) + 1
                        track_state.loop_iterations[key] = iters
                        if iters > self._max_loop_iterations:
                            # Break out of infinite loop
                            track_state.loop_start_cmd = -1
                            track_state.loop_iterations.pop(key, None)
                        else:
                            track_state.flat_cmd_index = track_state.loop_start_cmd
                    else:
                        track_state.flat_cmd_index = track_state.loop_start_cmd
                else:
                    track_state.loop_count -= 1
                    if track_state.loop_count > 0:
                        track_state.flat_cmd_index = track_state.loop_start_cmd
                    else:
                        track_state.loop_start_cmd = -1

        # Track control
        elif mml == MML.OPEN_TRACK:
            track_no = get_arg(0, 0)
            offset = get_arg(1, 0)
            self._open_track(track_no, offset)

        elif mml == MML.JUMP:
            offset = get_arg(0, 0)
            target_idx = self._offset_to_flat_index.get(offset, -1)
            # Backward jump = potential infinite loop
            if self._max_loop_iterations > 0 and target_idx >= 0 and target_idx <= track_state.flat_cmd_index:
                key = target_idx
                iters = track_state.jump_iterations.get(key, 0) + 1
                track_state.jump_iterations[key] = iters
                if iters > self._max_loop_iterations:
                    # Break out and treat this like FIN.
                    track_state.finished = True
                    return False
            self._jump_to_offset(track_state, offset)

        elif mml == MML.CALL:
            offset = get_arg(0, 0)
            if len(track_state.call_stack) < CALL_STACK_DEPTH:
                track_state.call_stack.append(CallStackEntry(
                    return_flat_index=track_state.flat_cmd_index,
                    loop_start_cmd=track_state.loop_start_cmd,
                    loop_count=track_state.loop_count,
                ))
                self._jump_to_offset(track_state, offset)

        elif mml == MML.RET:
            if track_state.call_stack:
                entry = track_state.call_stack.pop()
                track_state.flat_cmd_index = entry.return_flat_index
                track_state.loop_start_cmd = entry.loop_start_cmd
                track_state.loop_count = entry.loop_count

        elif mml == MML.ALLOC_TRACK:
            pass  # Informational only

        elif mml == MML.FIN:
            self._release_track_notes(track_state)
            track_state.finished = True
            return False

        # Extended commands (variable ops / comparisons)
        elif mml == MMLEX.SETVAR:
            var_idx = get_arg(0, 0)
            value = get_arg(1, 0)
            self._ctx.set_variable(var_idx, value, track_state)

        elif mml == MMLEX.ADDVAR:
            var_idx = get_arg(0, 0)
            value = get_arg(1, 0)
            current = self._ctx.get_variable(var_idx, track_state)
            self._ctx.set_variable(var_idx, current + value, track_state)

        elif mml == MMLEX.SUBVAR:
            var_idx = get_arg(0, 0)
            value = get_arg(1, 0)
            current = self._ctx.get_variable(var_idx, track_state)
            self._ctx.set_variable(var_idx, current - value, track_state)

        elif mml == MMLEX.MULVAR:
            var_idx = get_arg(0, 0)
            value = get_arg(1, 0)
            current = self._ctx.get_variable(var_idx, track_state)
            self._ctx.set_variable(var_idx, current * value, track_state)

        elif mml == MMLEX.DIVVAR:
            var_idx = get_arg(0, 0)
            value = get_arg(1, 0)
            current = self._ctx.get_variable(var_idx, track_state)
            if value != 0:
                self._ctx.set_variable(var_idx, current // value, track_state)

        elif mml == MMLEX.MODVAR:
            var_idx = get_arg(0, 0)
            value = get_arg(1, 0)
            current = self._ctx.get_variable(var_idx, track_state)
            if value != 0:
                self._ctx.set_variable(var_idx, current % value, track_state)

        # Comparisons
        elif mml == MMLEX.CMP_EQ:
            var_idx = get_arg(0, 0)
            value = get_arg(1, 0)
            self._ctx.cmp_flag = self._ctx.get_variable(var_idx, track_state) == value

        elif mml == MMLEX.CMP_GE:
            var_idx = get_arg(0, 0)
            value = get_arg(1, 0)
            self._ctx.cmp_flag = self._ctx.get_variable(var_idx, track_state) >= value

        elif mml == MMLEX.CMP_GT:
            var_idx = get_arg(0, 0)
            value = get_arg(1, 0)
            self._ctx.cmp_flag = self._ctx.get_variable(var_idx, track_state) > value

        elif mml == MMLEX.CMP_LE:
            var_idx = get_arg(0, 0)
            value = get_arg(1, 0)
            self._ctx.cmp_flag = self._ctx.get_variable(var_idx, track_state) <= value

        elif mml == MMLEX.CMP_LT:
            var_idx = get_arg(0, 0)
            value = get_arg(1, 0)
            self._ctx.cmp_flag = self._ctx.get_variable(var_idx, track_state) < value

        elif mml == MMLEX.CMP_NE:
            var_idx = get_arg(0, 0)
            value = get_arg(1, 0)
            self._ctx.cmp_flag = self._ctx.get_variable(var_idx, track_state) != value

        # Unknown commands are silently ignored (like the real engine)

        return True

    # =========================================================================
    # Note handling
    # =========================================================================

    def _handle_note(
            self,
            track_state: TrackState,
            note: int,
            velocity: int,
            duration: int,
    ) -> None:
        """Handle a note event, mirroring NW4R NoteOn behavior."""
        velocity = max(0, min(127, velocity * track_state.velocity_range // 127))
        actual_note = note + track_state.transpose
        actual_note = max(0, min(127, actual_note))

        runtime = self._resolve_note_runtime(track_state, actual_note, velocity)
        natural_end_tick = None
        if runtime.natural_duration_seconds is not None:
            ticks_per_second = (self._ctx.timebase * self._ctx.tempo * self._ctx.tempo_ratio) / 60.0
            if ticks_per_second > 0:
                natural_ticks = max(1, int(round(runtime.natural_duration_seconds * ticks_per_second)))
                natural_end_tick = self._ctx.tick_counter + natural_ticks

        def compute_sweep() -> tuple[float, int]:
            sweep_pitch = track_state.sweep_pitch
            if track_state.porta:
                sweep_pitch += track_state.porta_key - actual_note
            if sweep_pitch == 0.0:
                return 0.0, 0
            if track_state.porta and track_state.porta_time > 0:
                length_ms = int((track_state.porta_time * track_state.porta_time * abs(sweep_pitch)) / 32.0)
                length_ms *= 5
                tick_msec = 60000.0 / max(1.0, self._ctx.tempo * self._ctx.timebase)
                return sweep_pitch, max(1, int(round(length_ms / tick_msec)))
            return sweep_pitch, max(1, duration if duration > 0 else 1)

        def update_voice(existing: ActiveNote, indefinite: bool) -> None:
            old_note = existing.note
            existing.note = actual_note
            existing.velocity = velocity
            existing.start_tick = self._ctx.tick_counter
            existing.duration = -1 if indefinite or runtime.ignore_note_off or duration <= 0 else duration
            existing.program = track_state.program
            existing.natural_end_tick = natural_end_tick
            existing.sweep_pitch, existing.sweep_ticks = compute_sweep()
            existing.sweep_start_tick = self._ctx.tick_counter
            if old_note != actual_note:
                if self._ctx.note_change_callback:
                    self._ctx.note_change_callback(
                        track_state.track_index,
                        old_note,
                        actual_note,
                        velocity,
                        track_state.program,
                        track_state.bank,
                        track_state.porta,
                        track_state.porta_time,
                        track_state.porta_key,
                    )
                else:
                    if self._ctx.note_off_callback:
                        self._ctx.note_off_callback(track_state.track_index, old_note)
                    if self._ctx.note_on_callback:
                        self._ctx.note_on_callback(
                            track_state.track_index,
                            actual_note,
                            velocity,
                            track_state.program,
                            track_state.bank,
                        )

        if track_state.tie and track_state.active_notes:
            update_voice(track_state.active_notes[-1], indefinite=True)
            track_state.porta_key = actual_note
            if track_state.note_wait:
                track_state.wait_ticks = duration
                if duration == 0:
                    track_state.note_finish_wait = True
            return

        if track_state.monophonic and track_state.active_notes:
            update_voice(track_state.active_notes[-1], indefinite=False)
            track_state.porta_key = actual_note
            if track_state.note_wait:
                track_state.wait_ticks = duration
                if duration == 0:
                    track_state.note_finish_wait = True
            return

        active_note = ActiveNote(
            note=actual_note,
            velocity=velocity,
            channel=track_state.track_index,
            start_tick=self._ctx.tick_counter,
            duration=-1 if track_state.tie or runtime.ignore_note_off or duration <= 0 else duration,
            program=track_state.program,
            natural_end_tick=natural_end_tick,
        )
        active_note.sweep_pitch, active_note.sweep_ticks = compute_sweep()
        active_note.sweep_start_tick = self._ctx.tick_counter
        track_state.active_notes.append(active_note)

        if self._ctx.note_on_callback:
            self._ctx.note_on_callback(
                track_state.track_index,
                actual_note,
                velocity,
                track_state.program,
                track_state.bank,
            )

        track_state.porta_key = actual_note
        if track_state.note_wait:
            track_state.wait_ticks = duration
            if duration == 0:
                track_state.note_finish_wait = True

    @staticmethod
    def _apply_note_change(
            fs,
            channel: int,
            old_note: int,
            new_note: int,
            velocity: int,
    ) -> None:
        """Retune an active note; continuous sweep is emitted separately as pitch-bend events."""
        fs.noteoff(channel, old_note)
        fs.noteon(channel, new_note, velocity)

    def _dispatch_fluidsynth_event(
            self,
            fs,
            sfid: int,
            ev: dict,
            *,
            channel_programs: dict[int, int],
            default_programs_map: dict[int, int],
            bend_ranges: dict[int, int],
            note_override: int | None = None,
    ) -> None:
        event_type = ev.get('type')
        if event_type == 'tempo':
            return

        channel = ev.get('track', 0) % 16

        if event_type in {'note_on', 'note_change'}:
            program = ev.get('program', 0)
            if program < 0:
                program = default_programs_map.get(channel, 0)
            if channel_programs.get(channel) != program:
                fs.program_select(channel, sfid, 0, program)
                channel_programs[channel] = program

        if event_type == 'note_on':
            note = note_override if note_override is not None else ev['note']
            fs.noteon(channel, note, ev['velocity'])
            return

        if event_type == 'note_change':
            old_note = note_override if note_override is not None else ev['old_note']
            note = note_override if note_override is not None else ev['note']
            self._apply_note_change(
                fs,
                channel,
                old_note,
                note,
                ev['velocity'],
            )
            return

        if event_type == 'note_off':
            note = note_override if note_override is not None else ev['note']
            fs.noteoff(channel, note)
            return

        if event_type == 'control_change':
            fs.cc(channel, ev['cc'], ev['value'])
            return

        if event_type == 'program_change':
            program = ev.get('program', 0)
            if program < 0:
                program = default_programs_map.get(channel, 0)
            if channel_programs.get(channel) != program:
                fs.program_select(channel, sfid, 0, program)
                channel_programs[channel] = program
            return

        if event_type == 'pitch_bend':
            bend_range = max(1, ev.get('range', 2))
            if bend_ranges.get(channel) != bend_range:
                self._set_pitch_bend_range(fs, channel, bend_range)
                bend_ranges[channel] = bend_range
            self._apply_pitch_bend(
                fs,
                channel,
                self._midi_pitch_bend_value(ev.get('semitones', 0.0), bend_range),
            )

    # Track navigation based on offsets in the flat command list

    def _open_track(self, track_no: int, offset: int) -> None:
        """
        Open a child track on the given track slot.

        Matches C# OpenTrackCommand.PreviewExecute:
            channelID = TrackNumber + ((TrackNumber >= 9) ? 1 : 0)
        """
        if track_no < 0 or track_no >= MAX_TRACKS:
            return

        flat_idx = self._offset_to_flat_index.get(offset)
        if flat_idx is None:
            return

        ts = TrackState(track_index=track_no)
        ts.flat_cmd_index = flat_idx
        ts.active = True
        self._ctx.tracks[track_no] = ts

    def _jump_to_offset(self, track_state: TrackState, offset: int) -> None:
        """Jump execution to the given offset in the flat command list."""
        flat_idx = self._offset_to_flat_index.get(offset)
        if flat_idx is not None:
            track_state.flat_cmd_index = flat_idx
            return

        # Fallback: scan flat commands for matching offset
        for i, cmd in enumerate(self._flat_commands):
            if cmd.offset == offset:
                track_state.flat_cmd_index = i
                return

    # Non-realtime rendering

    def _render_idle_skip(self, limit: int) -> int:
        if limit <= 0:
            return 0

        current_tick = self._ctx.tick_counter
        skip = limit

        for track_state in self._ctx.tracks:
            if not track_state.active:
                continue

            # These parameters can emit a MIDI event on every tick while
            # interpolating.  Tick through the move rather than dropping its
            # intermediate values.
            for value in (
                    track_state.volume,
                    track_state.pan,
                    track_state.surround_pan,
                    track_state.pitch_bend,
            ):
                if value._counter < value._frames:
                    return 0

            if not track_state.finished:
                if track_state.note_finish_wait:
                    # The next command becomes runnable as soon as the last
                    # note expires, handled by the deadline calculation below.
                    if not track_state.active_notes:
                        return 0
                else:
                    # WAIT N runs the next command on the tick that changes
                    # the counter from 1 to 0, so only N-1 ticks are idle.
                    if track_state.wait_ticks <= 1:
                        return 0
                    skip = min(skip, track_state.wait_ticks - 1)

            for note in track_state.active_notes:
                sweep_end = note.sweep_start_tick + note.sweep_ticks
                if note.sweep_pitch != 0.0 and current_tick < sweep_end:
                    return 0

                deadlines = []
                if note.duration > 0:
                    deadlines.append(note.start_tick + note.duration)
                if note.natural_end_tick is not None:
                    deadlines.append(note.natural_end_tick)
                if deadlines:
                    remaining = min(deadlines) - current_tick
                    if remaining <= 0:
                        return 0
                    skip = min(skip, remaining)

        return max(0, skip)

    def render_events(
            self,
            max_ticks: int = 1_000_000,
            max_loops: int = 2,
    ) -> list[dict]:
        """
        Render the entire sequence into a list of events (non-realtime).

        Args:
            max_ticks: Safety limit on total ticks to process
            max_loops: How many times to unroll infinite loops (0 = unlimited).
                       Also limits backward JUMPs. Default 1.

        Returns events like:
            {'type': 'note_on',  'tick': 0, 'track': 0, 'note': 60, 'vel': 100, 'program': 0}
            {'type': 'note_off', 'tick': 48, 'track': 0, 'note': 60}
            {'type': 'tempo',    'tick': 0, 'tempo': 120}
        """
        self._max_loop_iterations = max_loops
        events: list[dict] = []

        def emit_track_runtime(track: int) -> None:
            ts = self._ctx.tracks[track]
            track_state = track_render_state.setdefault(
                ts.track_index,
                {
                    'volume': max(0, min(127, int(round(
                        ts.volume.get_value() * ts.volume2 * ts.main_volume / (127.0 * 127.0)
                    )))),
                    'pan': ts.pan.get_value(),
                    'pitch': 0.0,
                    'range': max(1, ts.bend_range),
                },
            )

            volume = max(0, min(127, int(round(
                ts.volume.get_value() * ts.volume2 * ts.main_volume / (127.0 * 127.0)
            ))))
            if volume != track_state['volume']:
                events.append({
                    'type': 'control_change',
                    'tick': self._ctx.tick_counter,
                    'track': ts.track_index,
                    'cc': 7,
                    'value': volume,
                })
                track_state['volume'] = volume

            pan = ts.pan.get_value()
            if pan != track_state['pan']:
                events.append({
                    'type': 'control_change',
                    'tick': self._ctx.tick_counter,
                    'track': ts.track_index,
                    'cc': 10,
                    'value': pan,
                })
                track_state['pan'] = pan

            pitch, bend_range = self._get_track_pitch_state(ts)
            if abs(pitch - track_state['pitch']) > 1e-6 or bend_range != track_state['range']:
                events.append({
                    'type': 'pitch_bend',
                    'tick': self._ctx.tick_counter,
                    'track': ts.track_index,
                    'semitones': pitch,
                    'range': bend_range,
                })
                track_state['pitch'] = pitch
                track_state['range'] = bend_range

        def on_note_on(track, note, vel, program, bank):
            emit_track_runtime(track)
            events.append({
                'type': 'note_on', 'tick': self._ctx.tick_counter,
                'track': track, 'note': note, 'velocity': vel,
                'program': program, 'bank': bank,
                'source_offset': self._render_source_offset,
            })

        def on_note_off(track, note):
            events.append({
                'type': 'note_off', 'tick': self._ctx.tick_counter,
                'track': track, 'note': note,
            })

        def on_note_change(track, old_note, note, vel, program, bank, porta, porta_time, porta_key):
            emit_track_runtime(track)
            events.append({
                'type': 'note_change', 'tick': self._ctx.tick_counter,
                'track': track, 'old_note': old_note, 'note': note,
                'velocity': vel, 'program': program, 'bank': bank,
                'porta': porta, 'porta_time': porta_time, 'porta_key': porta_key,
                'source_offset': self._render_source_offset,
            })

        def on_control_change(track, cc, value):
            state = track_render_state.setdefault(track, {
                'volume': 127,
                'pan': 64,
                'pitch': 0.0,
                'range': 2,
            })
            if cc == 7:
                state['volume'] = value
            elif cc == 10:
                state['pan'] = value
            events.append({
                'type': 'control_change', 'tick': self._ctx.tick_counter,
                'track': track, 'cc': cc, 'value': value,
            })

        def on_program_change(track, program):
            events.append({
                'type': 'program_change', 'tick': self._ctx.tick_counter,
                'track': track, 'program': program,
            })

        old_on = self._ctx.note_on_callback
        old_off = self._ctx.note_off_callback
        old_change = self._ctx.note_change_callback
        old_cc = self._ctx.control_change_callback
        old_pc = self._ctx.program_change_callback
        self._ctx.note_on_callback = on_note_on
        self._ctx.note_off_callback = on_note_off
        self._ctx.note_change_callback = on_note_change
        self._ctx.control_change_callback = on_control_change
        self._ctx.program_change_callback = on_program_change

        track_render_state = {
            ts.track_index: {
                'volume': max(0, min(127, int(round(
                    ts.volume.get_value() * ts.volume2 * ts.main_volume / (127.0 * 127.0)
                )))),
                'pan': ts.pan.get_value(),
                'pitch': 0.0,
                'range': max(1, ts.bend_range),
            }
            for ts in self._ctx.tracks
        }

        events.append({'type': 'tempo', 'tick': 0, 'tempo': self._ctx.tempo})

        self._ctx.state = PlayerState.PLAYING
        prev_tempo = self._ctx.tempo

        ticks_processed = 0
        while ticks_processed < max_ticks:
            if self._ctx.state != PlayerState.PLAYING:
                break

            self._process_tick()

            for ts in self._ctx.tracks:
                if not ts.active:
                    continue
                emit_track_runtime(ts.track_index)

            self._ctx.tick_counter += 1
            ticks_processed += 1

            if self._ctx.tempo != prev_tempo:
                events.append({'type': 'tempo', 'tick': self._ctx.tick_counter, 'tempo': self._ctx.tempo})
                prev_tempo = self._ctx.tempo

            if all(not t.active or t.finished for t in self._ctx.tracks):
                # All command processing is done, but keep going if notes
                # are still sustaining so they expire at their natural time.
                if not any(t.active_notes for t in self._ctx.tracks):
                    break

            idle_ticks = self._render_idle_skip(max_ticks - ticks_processed)
            if idle_ticks:
                for ts in self._ctx.tracks:
                    if ts.active and not ts.finished and not ts.note_finish_wait:
                        ts.wait_ticks = max(0, ts.wait_ticks - idle_ticks)
                self._ctx.tick_counter += idle_ticks
                ticks_processed += idle_ticks

        # Release remaining notes
        for ts in self._ctx.tracks:
            for note in ts.active_notes:
                events.append({
                    'type': 'note_off', 'tick': self._ctx.tick_counter,
                    'track': ts.track_index, 'note': note.note,
                })

        self._ctx.note_on_callback = old_on
        self._ctx.note_off_callback = old_off
        self._ctx.note_change_callback = old_change
        self._ctx.control_change_callback = old_cc
        self._ctx.program_change_callback = old_pc
        self._max_loop_iterations = 0  # Reset for real-time playback

        events.sort(key=lambda e: e['tick'])
        return events
