from dataclasses import dataclass
from typing import Callable

from pysar.core.format.rseq.mml import CALL_STACK_DEPTH, DEFAULT_TEMPO, DEFAULT_TIMEBASE, MAX_TRACKS, MML, MMLEX, MMLEX_ARG_SPEC, is_note
from pysar.core.model.brseq import BrseqData, Command
from pysar.seq.nw4r import Nw4rRandomState
from pysar.seq.runtime import INVALID_ENVELOPE, ActiveNote, CallFrame, PlayerContext, PlayerState, TrackState


@dataclass(slots=True)
class NoteRuntimeInfo:
    natural_duration_seconds: float | None = None
    ignore_note_off: bool = False


TrackSnapshot = tuple[int, int, float, int, int, int, int, int, int, int, int, int]


class SequencePlayer:
    def __init__(self) -> None:
        self._brseq: BrseqData | None = None
        self._ctx = PlayerContext()
        self._flat_commands: list[Command] = []
        self._offset_to_index: dict[int, int] = {}
        self._default_programs: dict[int, int] = {}
        self._max_loop_iterations = 0
        self._one_shot = False
        self._truncated = False
        self._note_runtime_resolver: Callable[[TrackState, int, int], NoteRuntimeInfo | None] | None = None
        self._command_callback: Callable[[int, int, Command], None] | None = None
        self._rng = Nw4rRandomState()
        self._random_overrides: dict[int, int] = {}
        self._random_call_index = 0
        self._trace_random_calls = False
        self._random_calls: list[dict[str, int | bool]] = []
        self._current_command: Command | None = None
        self._suppress_track_state_events = False
        self._track_state_markers = False

    @property
    def timebase(self) -> int:
        return self._ctx.timebase

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def random_calls(self) -> tuple[dict[str, int | bool], ...]:
        return tuple(self._random_calls)

    def trace_random_calls(self, enabled: bool = True) -> None:
        self._trace_random_calls = bool(enabled)
        self._random_calls.clear()

    def snapshot_track_events(self) -> list[dict]:
        """Return the current mixer controls without advancing playback."""
        events: list[dict] = []
        snapshots: dict[int, TrackSnapshot] = {}
        suppressed = self._suppress_track_state_events
        self._suppress_track_state_events = False
        try:
            for track in self._ctx.tracks:
                if track.active or track.active_notes:
                    self._emit_track_state(track, events, snapshots)
        finally:
            self._suppress_track_state_events = suppressed
        return events

    def set_note_runtime_resolver(
        self,
        resolver: Callable[[TrackState, int, int], NoteRuntimeInfo | None] | None,
    ) -> None:
        self._note_runtime_resolver = resolver

    def set_command_callback(
        self,
        callback: Callable[[int, int, Command], None] | None,
    ) -> None:
        self._command_callback = callback

    def set_random_overrides(self, overrides: dict[int, int] | list[tuple[int, int]] | tuple[tuple[int, int], ...] | None) -> None:
        if isinstance(overrides, dict):
            items = overrides.items()
        else:
            items = overrides or []
        self._random_overrides = {int(index): int(value) for index, value in items}
        self._random_call_index = 0

    def load(
        self,
        brseq: BrseqData,
        *,
        start_label: str | None = None,
        start_offset: int | None = None,
        default_programs: dict[int, int] | None = None,
    ) -> None:
        self._brseq = brseq
        self._ctx = PlayerContext()
        self._default_programs = dict(default_programs or {})
        self._rng = Nw4rRandomState()
        self._random_call_index = 0
        self._random_calls.clear()
        self._current_command = None
        self._flat_commands = self._collect_commands(brseq)
        self._offset_to_index = {}
        for index, cmd in enumerate(self._flat_commands):
            self._offset_to_index.setdefault(cmd.offset, index)

        start_index = self._find_start_index(brseq, start_label, start_offset)
        track = self._ctx.tracks[0]
        track.track_no = 0
        track.command_index = start_index
        track.active = True
        track.finished = False
        track.program = self._default_programs.get(0, 0)

    def render_events(self, *, max_ticks: int = 1_000_000, loop_count: int = 1, one_shot: bool = False) -> list[dict]:
        if self._brseq is None:
            return []

        self._max_loop_iterations = max(0, int(loop_count))
        self._one_shot = bool(one_shot)
        self._truncated = False
        events: list[dict] = []
        track_snapshot: dict[int, TrackSnapshot] = {}
        events.append({"type": "tempo", "tick": 0, "tempo": self._ctx.tempo})
        self._ctx.state = PlayerState.PLAYING
        last_tempo = self._ctx.tempo
        completed = False

        for _ in range(max_ticks):
            if self._ctx.state != PlayerState.PLAYING:
                completed = True
                break

            self._process_tick(events, track_snapshot)
            self._ctx.tick_counter += 1

            if self._ctx.tempo != last_tempo:
                events.append({"type": "tempo", "tick": self._ctx.tick_counter, "tempo": self._ctx.tempo})
                last_tempo = self._ctx.tempo

            if all((not track.active) or track.finished for track in self._ctx.tracks):
                if not any(track.active_notes for track in self._ctx.tracks):
                    completed = True
                    break
        else:
            completed = self._ctx.state != PlayerState.PLAYING or (
                all((not track.active) or track.finished for track in self._ctx.tracks)
                and not any(track.active_notes for track in self._ctx.tracks)
            )

        self._truncated = not completed

        for track in self._ctx.tracks:
            for note in track.active_notes:
                events.append({
                    "type": "note_off",
                    "tick": self._ctx.tick_counter,
                    "track": track.track_no,
                    "note": note.note,
                })

        self._max_loop_iterations = 0
        self._one_shot = False
        events.sort(key=lambda item: (item["tick"], 0 if item["type"] == "control_change" else 1))
        return events

    def iter_events(self, *, max_ticks: int = 1_000_000, loop_count: int = 1, one_shot: bool = False):
        if self._brseq is None:
            return

        self._max_loop_iterations = max(0, int(loop_count))
        self._one_shot = bool(one_shot)
        self._truncated = False
        track_snapshot: dict[int, TrackSnapshot] = {}
        self._ctx.state = PlayerState.PLAYING
        last_tempo = self._ctx.tempo
        completed = False
        yield {"type": "tempo", "tick": 0, "tempo": self._ctx.tempo}

        for _ in range(max_ticks):
            if self._ctx.state != PlayerState.PLAYING:
                completed = True
                break

            events: list[dict] = []
            self._process_tick(events, track_snapshot)
            events.sort(key=lambda item: (item["tick"], 0 if item["type"] == "control_change" else 1))
            for event in events:
                yield event

            self._ctx.tick_counter += 1

            if self._ctx.tempo != last_tempo:
                yield {"type": "tempo", "tick": self._ctx.tick_counter, "tempo": self._ctx.tempo}
                last_tempo = self._ctx.tempo

            if all((not track.active) or track.finished for track in self._ctx.tracks):
                if not any(track.active_notes for track in self._ctx.tracks):
                    completed = True
                    break
        else:
            completed = self._ctx.state != PlayerState.PLAYING or (
                all((not track.active) or track.finished for track in self._ctx.tracks)
                and not any(track.active_notes for track in self._ctx.tracks)
            )

        self._truncated = not completed

        for track in self._ctx.tracks:
            for note in track.active_notes:
                yield {
                    "type": "note_off",
                    "tick": self._ctx.tick_counter,
                    "track": track.track_no,
                    "note": note.note,
                }

        self._max_loop_iterations = 0
        self._one_shot = False

    def iter_event_ticks(
        self,
        *,
        max_ticks: int = 1_000_000,
        loop_count: int = 1,
        one_shot: bool = False,
        suppress_track_state: Callable[[], bool] | None = None,
        track_state_markers: bool = False,
    ):
        if self._brseq is None:
            return

        self._max_loop_iterations = max(0, int(loop_count))
        self._one_shot = bool(one_shot)
        self._truncated = False
        track_snapshot: dict[int, TrackSnapshot] = {}
        self._ctx.state = PlayerState.PLAYING
        last_tempo = self._ctx.tempo
        pending_events = [{"type": "tempo", "tick": 0, "tempo": self._ctx.tempo}]
        completed = False

        for _ in range(max_ticks):
            if self._ctx.state != PlayerState.PLAYING:
                completed = True
                break

            tick = self._ctx.tick_counter
            events: list[dict] = []
            self._suppress_track_state_events = bool(suppress_track_state and suppress_track_state())
            self._track_state_markers = bool(track_state_markers)
            if pending_events:
                events.extend(pending_events)
                pending_events = []
            self._process_tick(events, track_snapshot)
            events.sort(key=lambda item: (item["tick"], 0 if item["type"] == "control_change" else 1))
            yield tick, events

            self._ctx.tick_counter += 1

            if self._ctx.tempo != last_tempo:
                pending_events.append({"type": "tempo", "tick": self._ctx.tick_counter, "tempo": self._ctx.tempo})
                last_tempo = self._ctx.tempo

            if all((not track.active) or track.finished for track in self._ctx.tracks):
                if not any(track.active_notes for track in self._ctx.tracks):
                    completed = True
                    break
        else:
            completed = self._ctx.state != PlayerState.PLAYING or (
                all((not track.active) or track.finished for track in self._ctx.tracks)
                and not any(track.active_notes for track in self._ctx.tracks)
            )

        self._truncated = not completed

        final_events = []
        for track in self._ctx.tracks:
            for note in track.active_notes:
                final_events.append({
                    "type": "note_off",
                    "tick": self._ctx.tick_counter,
                    "track": track.track_no,
                    "note": note.note,
                })
        if final_events:
            yield self._ctx.tick_counter, final_events

        self._max_loop_iterations = 0
        self._one_shot = False
        self._suppress_track_state_events = False
        self._track_state_markers = False

    def _collect_commands(self, brseq: BrseqData) -> list[Command]:
        seen: set[int] = set()
        commands: list[Command] = []
        for track in brseq.tracks.values():
            for command in track.commands:
                if command.offset in seen:
                    continue
                seen.add(command.offset)
                commands.append(command)
        commands.sort(key=lambda command: command.offset)
        return commands

    def _find_start_index(self, brseq: BrseqData, start_label: str | None, start_offset: int | None) -> int:
        if start_offset is not None:
            if start_offset in self._offset_to_index:
                return self._offset_to_index[start_offset]
            for track in brseq.tracks.values():
                if track.start_offset <= start_offset < track.end_offset:
                    for command in track.commands:
                        if command.offset >= start_offset:
                            return self._offset_to_index.get(command.offset, 0)
                    return self._offset_to_index.get(track.start_offset, 0)
        if start_label is not None:
            for label in brseq.labels:
                if label.name == start_label:
                    return self._offset_to_index.get(label.offset, 0)
        return 0

    def _process_tick(self, events: list[dict], track_snapshot: dict[int, TrackSnapshot]) -> None:
        for track in self._ctx.tracks:
            if not track.active:
                continue

            volume_changed = track.volume.tick()
            pan_changed = track.pan.tick()
            track.surround_pan.tick()
            pitch_changed = track.pitch_bend.tick()
            self._update_notes(track, events)
            if (
                not self._suppress_track_state_events
                or (
                    self._track_state_markers
                    and (
                        track.track_no not in track_snapshot
                        or volume_changed
                        or pan_changed
                        or pitch_changed
                    )
                )
            ):
                self._emit_track_state(track, events, track_snapshot)

            if track.finished:
                continue
            if track.note_finish_wait and track.active_notes:
                continue
            if track.note_finish_wait:
                track.note_finish_wait = False

            if track.wait_ticks > 0:
                track.wait_ticks -= 1
                if track.wait_ticks > 0:
                    continue

            guard = 0
            while not track.finished and track.wait_ticks <= 0 and not track.note_finish_wait:
                if not self._step_command(track, events, track_snapshot):
                    break
                guard += 1
                if guard > 10_000:
                    self._release_track(track, events)
                    track.finished = True
                    break

    def _emit_track_state(
        self,
        track: TrackState,
        events: list[dict],
        track_snapshot: dict[int, TrackSnapshot],
    ) -> None:
        if self._suppress_track_state_events and not self._track_state_markers:
            return
        combined_volume = max(
            0,
            min(127, round(track.volume.value * track.volume2 * track.main_volume / (127.0 * 127.0))),
        )
        pan = max(0, min(127, track.pan.value))
        pitch, bend_range = self._current_pitch(track)
        current = (
            combined_volume,
            pan,
            pitch,
            bend_range,
            track.priority,
            track.main_send,
            track.fx_send_a,
            track.fx_send_b,
            track.fx_send_c,
            track.lpf_cutoff,
            track.biquad_type,
            track.biquad_value,
        )
        previous = track_snapshot.get(track.track_no)
        if self._suppress_track_state_events:
            if previous is None or previous != current:
                events.append({"type": "track_state_marker", "tick": self._ctx.tick_counter})
            track_snapshot[track.track_no] = current
            return

        gain = (
            (track.volume.value / 127.0)
            * (track.volume2 / 127.0)
            * (track.main_volume / 127.0)
        )
        if previous is None or previous[0] != combined_volume:
            events.append({
                "type": "control_change",
                "tick": self._ctx.tick_counter,
                "track": track.track_no,
                "cc": 7,
                "value": combined_volume,
                "gain": gain,
            })
        if previous is None or previous[1] != pan:
            events.append({
                "type": "control_change",
                "tick": self._ctx.tick_counter,
                "track": track.track_no,
                "cc": 10,
                "value": pan,
            })
        if previous is None or abs(previous[2] - pitch) > 1.0e-6 or previous[3] != bend_range:
            events.append({
                "type": "pitch_bend",
                "tick": self._ctx.tick_counter,
                "track": track.track_no,
                "semitones": pitch,
                "range": bend_range,
            })
        if previous is None or previous[4:] != current[4:]:
            events.append({
                "type": "track_param",
                "tick": self._ctx.tick_counter,
                "track": track.track_no,
                "priority": track.priority,
                "main_send": track.main_send,
                "fx_send_a": track.fx_send_a,
                "fx_send_b": track.fx_send_b,
                "fx_send_c": track.fx_send_c,
                "lpf_cutoff": track.lpf_cutoff,
                "biquad_type": track.biquad_type,
                "biquad_value": track.biquad_value,
            })
        track_snapshot[track.track_no] = current

    def _current_pitch(self, track: TrackState) -> tuple[float, int]:
        pitch = (track.pitch_bend.value / 128.0) * max(1, track.pitch_bend_range)
        bend_range = max(1, track.pitch_bend_range)
        return pitch, bend_range

    def _update_notes(self, track: TrackState, events: list[dict]) -> None:
        finished: list[ActiveNote] = []
        for note in track.active_notes:
            expired = note.duration_ticks > 0 and (self._ctx.tick_counter - note.start_tick) >= note.duration_ticks
            natural = note.natural_end_tick is not None and self._ctx.tick_counter >= note.natural_end_tick
            if expired or natural:
                finished.append(note)
        for note in finished:
            track.active_notes.remove(note)
            events.append({
                "type": "note_off",
                "tick": self._ctx.tick_counter,
                "track": track.track_no,
                "note": note.note,
            })

    def _step_command(
        self,
        track: TrackState,
        events: list[dict],
        track_snapshot: dict[int, TrackSnapshot],
    ) -> bool:
        if track.command_index >= len(self._flat_commands):
            self._release_track(track, events)
            track.finished = True
            return False

        command = self._flat_commands[track.command_index]
        track.command_index += 1
        self._current_command = command
        command_is_extended = command.is_extended

        if command.has_if and not track.cmp_flag:
            return True

        if self._command_callback is not None:
            self._command_callback(track.track_no, self._ctx.tick_counter, command)

        def arg(index: int, default: int = 0) -> int:
            prefix_index = None
            if command_is_extended:
                try:
                    mmlex = MMLEX(command.opcode)
                except ValueError:
                    mmlex = None
                spec = MMLEX_ARG_SPEC.get(mmlex, []) if mmlex is not None else []
                if len(spec) >= 2:
                    prefix_index = 1
                elif len(spec) == 1:
                    prefix_index = 0
            else:
                prefix_index = 0

            if index == prefix_index:
                for prefix in command.prefixes:
                    if prefix.type == MML.RANDOM and len(prefix.args) >= 2:
                        return self._random_int(prefix.args[0], prefix.args[1])
                    if prefix.type == MML.VARIABLE and prefix.args:
                        return self._ctx.get_variable(prefix.args[0], track)

            if index >= len(command.args):
                return default
            return command.args[index]

        def note_length(default: int) -> int:
            for prefix in command.prefixes:
                if prefix.type == MML.RANDOM and len(prefix.args) >= 2:
                    return self._random_int(prefix.args[0], prefix.args[1])
                if prefix.type == MML.VARIABLE and prefix.args:
                    return self._ctx.get_variable(prefix.args[0], track)
            if len(command.args) < 2:
                return default
            return command.args[1]

        def time_arg(default: int = 0) -> int:
            for prefix in command.prefixes:
                if prefix.type == MML.TIME and prefix.args:
                    return prefix.args[0]
                if prefix.type == MML.TIME_RANDOM and len(prefix.args) >= 2:
                    return self._random_int(prefix.args[0], prefix.args[1])
                if prefix.type == MML.TIME_VARIABLE and prefix.args:
                    return self._ctx.get_variable(prefix.args[0], track)
            return default

        def trunc_div(lhs: int, rhs: int) -> int:
            return int(lhs / rhs)

        def trunc_mod(lhs: int, rhs: int) -> int:
            return lhs - (trunc_div(lhs, rhs) * rhs)

        mml = None
        mmlex = None
        if command_is_extended:
            try:
                mmlex = MMLEX(command.opcode)
            except ValueError:
                mmlex = None
        else:
            try:
                mml = MML(command.opcode)
            except ValueError:
                mml = None

        if is_note(command.opcode):
            velocity = command.args[0] if command.args else 127
            self._note_on(track, command.opcode, velocity, note_length(self._ctx.timebase), events, command.offset)
            return True

        if mml == MML.WAIT:
            wait = arg(0, 0)
            if wait > 0:
                track.wait_ticks = wait
            return True

        if mml == MML.PRG:
            track.program = arg(0, 0)
            events.append({
                "type": "program_change",
                "tick": self._ctx.tick_counter,
                "track": track.track_no,
                "program": track.program,
            })
            return True

        if mml == MML.TEMPO:
            self._ctx.tempo = max(1, arg(0, DEFAULT_TEMPO))
            return True

        if mml == MML.TIMEBASE:
            self._ctx.timebase = max(1, arg(0, DEFAULT_TIMEBASE))
            return True

        if mml == MML.VOLUME:
            track.volume.set_target(arg(0, 127), time_arg(0))
            self._emit_track_state(track, events, track_snapshot)
            return True

        if mml == MML.VOLUME2:
            track.volume2 = arg(0, 127)
            self._emit_track_state(track, events, track_snapshot)
            return True

        if mml == MML.MAIN_VOLUME:
            track.main_volume = arg(0, 127)
            self._emit_track_state(track, events, track_snapshot)
            return True

        if mml == MML.PAN:
            track.pan.set_target(arg(0, 64), time_arg(0))
            self._emit_track_state(track, events, track_snapshot)
            return True

        if mml == MML.SURROUND_PAN:
            track.surround_pan.set_target(arg(0, 0), time_arg(0))
            return True

        if mml == MML.INIT_PAN:
            track.init_pan = arg(0, 64)
            return True

        if mml == MML.TRANSPOSE:
            track.transpose = arg(0, 0)
            return True

        if mml == MML.PITCH_BEND:
            track.pitch_bend.set_target(arg(0, 0), time_arg(0))
            self._emit_track_state(track, events, track_snapshot)
            return True

        if mml == MML.BEND_RANGE:
            track.pitch_bend_range = max(1, arg(0, 2))
            self._emit_track_state(track, events, track_snapshot)
            return True

        if mml == MML.VELOCITY_RANGE:
            track.velocity_range = arg(0, 127)
            return True

        if mml == MML.PRIO:
            track.priority = arg(0, 64)
            self._emit_track_state(track, events, track_snapshot)
            return True

        if mml == MML.NOTE_WAIT:
            track.note_wait = arg(0, 1) != 0
            return True

        if mml == MML.TIE:
            track.tie = arg(0, 0) != 0
            track.tie_command_offset = command.offset if track.tie else -1
            if not track.tie:
                self._release_track(track, events)
            return True

        if mml == MML.MONOPHONIC:
            track.monophonic = arg(0, 0) != 0
            if track.monophonic and len(track.active_notes) > 1:
                while len(track.active_notes) > 1:
                    note = track.active_notes.pop(0)
                    events.append({
                        "type": "note_off",
                        "tick": self._ctx.tick_counter,
                        "track": track.track_no,
                        "note": note.note,
                    })
            return True

        if mml == MML.PORTA:
            track.porta_key = max(0, min(127, arg(0, 0) + track.transpose))
            track.porta = True
            return True

        if mml == MML.PORTA_SW:
            track.porta = arg(0, 0) != 0
            return True

        if mml == MML.PORTA_TIME:
            track.porta_time = arg(0, 0)
            return True

        if mml == MML.DAMPER:
            track.damper = arg(0, 0) != 0
            return True

        if mml == MML.MUTE:
            track.muted = arg(0, 0) != 0
            return True

        if mml == MML.MOD_DEPTH:
            track.mod_depth = arg(0, 0)
            return True

        if mml == MML.MOD_SPEED:
            track.mod_speed = arg(0, 16)
            return True

        if mml == MML.MOD_TYPE:
            track.mod_type = arg(0, 0)
            return True

        if mml == MML.MOD_DELAY:
            track.mod_delay = arg(0, 0)
            return True

        if mml == MML.MOD_RANGE:
            track.mod_range = arg(0, 1)
            return True

        if mml == MML.ATTACK:
            track.attack = arg(0, 127)
            return True

        if mml == MML.DECAY:
            track.decay = arg(0, 127)
            return True

        if mml == MML.SUSTAIN:
            track.sustain = arg(0, 127)
            return True

        if mml == MML.RELEASE:
            track.release = arg(0, 127)
            return True

        if mml == MML.ENV_HOLD:
            track.hold = arg(0, 0)
            return True

        if mml == MML.ENV_RESET:
            track.attack = track.decay = track.sustain = track.release = INVALID_ENVELOPE
            track.hold = INVALID_ENVELOPE
            return True

        if mml == MML.SWEEP_PITCH:
            track.sweep_pitch = arg(0, 0) / 64.0
            return True

        if mml == MML.MAINSEND:
            track.main_send = arg(0, 127)
            self._emit_track_state(track, events, track_snapshot)
            return True

        if mml == MML.FXSEND_A:
            track.fx_send_a = arg(0, 0)
            self._emit_track_state(track, events, track_snapshot)
            return True

        if mml == MML.FXSEND_B:
            track.fx_send_b = arg(0, 0)
            self._emit_track_state(track, events, track_snapshot)
            return True

        if mml == MML.FXSEND_C:
            track.fx_send_c = arg(0, 0)
            self._emit_track_state(track, events, track_snapshot)
            return True

        if mml == MML.LPF_CUTOFF:
            track.lpf_cutoff = arg(0, 64)
            self._emit_track_state(track, events, track_snapshot)
            return True

        if mml == MML.BIQUAD_TYPE:
            track.biquad_type = arg(0, 0)
            self._emit_track_state(track, events, track_snapshot)
            return True

        if mml == MML.BIQUAD_VALUE:
            track.biquad_value = arg(0, 0)
            self._emit_track_state(track, events, track_snapshot)
            return True

        if mml == MML.LOOP_START:
            track.loop_start_index = track.command_index
            track.loop_count = arg(0, 0)
            return True

        if mml == MML.LOOP_END:
            if not self._handle_loop_end(track):
                self._release_track(track, events)
                track.finished = True
                return False
            return True

        if mml == MML.OPEN_TRACK:
            self._open_track(arg(0, 0), arg(1, 0))
            return True

        if mml == MML.JUMP:
            if not self._jump(track, arg(0, 0)):
                self._release_track(track, events)
                track.finished = True
                return False
            return True

        if mml == MML.CALL:
            target = arg(0, 0)
            if len(track.call_stack) < CALL_STACK_DEPTH:
                track.call_stack.append(
                    CallFrame(
                        return_index=track.command_index,
                        loop_start_index=track.loop_start_index,
                        loop_count=track.loop_count,
                    )
                )
                self._jump(track, target, apply_loop_limit=False)
            return True

        if mml == MML.RET:
            if track.call_stack:
                frame = track.call_stack.pop()
                track.command_index = frame.return_index
                track.loop_start_index = frame.loop_start_index
                track.loop_count = frame.loop_count
            else:
                self._release_track(track, events)
                track.finished = True
                return False
            return True

        if mml == MML.ALLOC_TRACK:
            return True

        if mml == MML.FIN:
            self._release_track(track, events)
            track.finished = True
            return False

        if mmlex == MMLEX.SETVAR:
            self._ctx.set_variable(arg(0, 0), arg(1, 0), track)
            return True

        if mmlex == MMLEX.ADDVAR:
            var_no = arg(0, 0)
            self._ctx.set_variable(var_no, self._ctx.get_variable(var_no, track) + arg(1, 0), track)
            return True

        if mmlex == MMLEX.SUBVAR:
            var_no = arg(0, 0)
            self._ctx.set_variable(var_no, self._ctx.get_variable(var_no, track) - arg(1, 0), track)
            return True

        if mmlex == MMLEX.MULVAR:
            var_no = arg(0, 0)
            self._ctx.set_variable(var_no, self._ctx.get_variable(var_no, track) * arg(1, 0), track)
            return True

        if mmlex == MMLEX.DIVVAR:
            var_no = arg(0, 0)
            divisor = arg(1, 1)
            if divisor:
                self._ctx.set_variable(var_no, trunc_div(self._ctx.get_variable(var_no, track), divisor), track)
            return True

        if mmlex == MMLEX.SHIFTVAR:
            var_no = arg(0, 0)
            amount = arg(1, 0)
            current = self._ctx.get_variable(var_no, track)
            if amount >= 0:
                self._ctx.set_variable(var_no, current << amount, track)
            else:
                self._ctx.set_variable(var_no, current >> (-amount), track)
            return True

        if mmlex == MMLEX.RANDVAR:
            var_no = arg(0, 0)
            limit = arg(1, 0)
            negative = limit < 0
            limit = abs(limit)
            value = self._random_int(0, limit)
            if negative:
                value = -value
            self._ctx.set_variable(var_no, value, track)
            return True

        if mmlex == MMLEX.ANDVAR:
            var_no = arg(0, 0)
            self._ctx.set_variable(var_no, self._ctx.get_variable(var_no, track) & arg(1, 0), track)
            return True

        if mmlex == MMLEX.ORVAR:
            var_no = arg(0, 0)
            self._ctx.set_variable(var_no, self._ctx.get_variable(var_no, track) | arg(1, 0), track)
            return True

        if mmlex == MMLEX.XORVAR:
            var_no = arg(0, 0)
            self._ctx.set_variable(var_no, self._ctx.get_variable(var_no, track) ^ arg(1, 0), track)
            return True

        if mmlex == MMLEX.NOTVAR:
            self._ctx.set_variable(arg(0, 0), ~((arg(1, 0)) & 0xFFFF), track)
            return True

        if mmlex == MMLEX.MODVAR:
            var_no = arg(0, 0)
            divisor = arg(1, 1)
            if divisor:
                self._ctx.set_variable(var_no, trunc_mod(self._ctx.get_variable(var_no, track), divisor), track)
            return True

        if mmlex in {MMLEX.CMP_EQ, MMLEX.CMP_GE, MMLEX.CMP_GT, MMLEX.CMP_LE, MMLEX.CMP_LT, MMLEX.CMP_NE}:
            lhs = self._ctx.get_variable(arg(0, 0), track)
            rhs = arg(1, 0)
            if mmlex == MMLEX.CMP_EQ:
                track.cmp_flag = lhs == rhs
            elif mmlex == MMLEX.CMP_GE:
                track.cmp_flag = lhs >= rhs
            elif mmlex == MMLEX.CMP_GT:
                track.cmp_flag = lhs > rhs
            elif mmlex == MMLEX.CMP_LE:
                track.cmp_flag = lhs <= rhs
            elif mmlex == MMLEX.CMP_LT:
                track.cmp_flag = lhs < rhs
            else:
                track.cmp_flag = lhs != rhs
            return True

        if mmlex == MMLEX.USERPROC:
            return True

        return True

    def _note_on(
        self,
        track: TrackState,
        note: int,
        velocity: int,
        duration: int,
        events: list[dict],
        command_offset: int,
    ) -> None:
        velocity = max(0, min(127, velocity * track.velocity_range // 127))
        actual_note = max(0, min(127, note + track.transpose))
        track.started_notes = True
        runtime = self._resolve_note_runtime(track, actual_note, velocity)
        natural_end_tick = None
        if runtime.natural_duration_seconds is not None:
            ticks_per_second = (self._ctx.timebase * self._ctx.tempo * self._ctx.tempo_ratio) / 60.0
            if ticks_per_second > 0:
                natural_end_tick = self._ctx.tick_counter + max(1, round(runtime.natural_duration_seconds * ticks_per_second))

        def sweep() -> tuple[float, int, float]:
            semitones = track.sweep_pitch
            if track.porta:
                semitones += track.porta_key - actual_note
            if semitones == 0.0:
                return 0.0, 0, 0.0
            if track.porta and track.porta_time > 0:
                length_ms = int((track.porta_time * track.porta_time * abs(semitones)) / 32.0) * 5
                tick_ms = 60000.0 / max(1.0, self._ctx.tempo * self._ctx.timebase)
                sweep_ticks = max(1, round(length_ms / tick_ms))
                return semitones, sweep_ticks, length_ms / 1000.0
            sweep_ticks = max(1, duration if duration > 0 else 1)
            ticks_per_second = (self._ctx.timebase * self._ctx.tempo * self._ctx.tempo_ratio) / 60.0
            sweep_seconds = (sweep_ticks / ticks_per_second) if ticks_per_second > 0 else 0.0
            return semitones, sweep_ticks, sweep_seconds

        def effective_duration(sweep_ticks: int) -> int:
            if track.tie or duration <= 0:
                return -1
            if track.porta and track.porta_time > 0:
                return max(duration, sweep_ticks)
            return duration

        def replace_note(current: ActiveNote, indefinite: bool) -> None:
            old_note = current.note
            current.note = actual_note
            current.velocity = velocity
            current.start_tick = self._ctx.tick_counter
            current.command_offset = command_offset
            current.sweep_pitch, current.sweep_ticks, sweep_seconds = sweep()
            current.duration_ticks = -1 if indefinite else effective_duration(current.sweep_ticks)
            current.program = track.program
            current.natural_end_tick = natural_end_tick
            current.sweep_start_tick = self._ctx.tick_counter
            events.append({
                "type": "note_change",
                "tick": self._ctx.tick_counter,
                "track": track.track_no,
                "old_note": old_note,
                "note": actual_note,
                "velocity": velocity,
                "program": track.program,
                "bank": track.bank,
                "porta": track.porta,
                "porta_time": track.porta_time,
                "porta_key": track.porta_key,
                "sweep_pitch": current.sweep_pitch,
                "sweep_seconds": sweep_seconds,
                **self._note_event_metadata(track),
            })

        if track.tie and track.active_notes:
            replace_note(track.active_notes[-1], indefinite=True)
            track.porta_key = actual_note
            if track.note_wait:
                track.wait_ticks = duration
                if duration == 0:
                    track.note_finish_wait = True
            return

        if track.monophonic and track.active_notes:
            replace_note(track.active_notes[-1], indefinite=False)
            track.porta_key = actual_note
            if track.note_wait:
                track.wait_ticks = duration
                if duration == 0:
                    track.note_finish_wait = True
            return

        voice = ActiveNote(
            note=actual_note,
            velocity=velocity,
            start_tick=self._ctx.tick_counter,
            command_offset=command_offset,
            duration_ticks=0,
            program=track.program,
            natural_end_tick=natural_end_tick,
        )
        voice.sweep_pitch, voice.sweep_ticks, sweep_seconds = sweep()
        voice.duration_ticks = effective_duration(voice.sweep_ticks)
        voice.sweep_start_tick = self._ctx.tick_counter
        track.active_notes.append(voice)
        events.append({
            "type": "note_on",
            "tick": self._ctx.tick_counter,
            "track": track.track_no,
            "note": actual_note,
            "velocity": velocity,
            "program": track.program,
            "bank": track.bank,
            "sweep_pitch": voice.sweep_pitch,
            "sweep_seconds": sweep_seconds,
            **self._note_event_metadata(track),
        })

        track.porta_key = actual_note
        if track.note_wait:
            track.wait_ticks = duration
            if duration == 0:
                track.note_finish_wait = True

    def _resolve_note_runtime(self, track: TrackState, note: int, velocity: int) -> NoteRuntimeInfo:
        if self._note_runtime_resolver is None:
            return NoteRuntimeInfo()
        info = self._note_runtime_resolver(track, note, velocity)
        return info if info is not None else NoteRuntimeInfo()

    def _random_int(self, minimum: int, maximum: int) -> int:
        minimum = int(minimum)
        maximum = int(maximum)
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        call_index = self._random_call_index
        self._random_call_index += 1
        sampled = self._rng.randint(minimum, maximum)
        if self._trace_random_calls:
            command = self._current_command
            self._random_calls.append({
                "index": call_index,
                "minimum": minimum,
                "maximum": maximum,
                "opcode": int(command.opcode) if command is not None else -1,
                "extended": bool(command.is_extended) if command is not None else False,
                "offset": int(command.offset or 0) if command is not None else -1,
            })
        if call_index in self._random_overrides:
            value = self._random_overrides[call_index]
            return max(minimum, min(maximum, int(value)))
        return sampled

    @staticmethod
    def _note_event_metadata(track: TrackState) -> dict[str, int]:
        return {
            "priority": track.priority,
            "attack": track.attack,
            "decay": track.decay,
            "sustain": track.sustain,
            "release": track.release,
            "hold": track.hold,
            "mod_depth": track.mod_depth,
            "mod_speed": track.mod_speed,
            "mod_type": track.mod_type,
            "mod_delay": track.mod_delay,
            "mod_range": track.mod_range,
        }

    def _handle_loop_end(self, track: TrackState) -> bool:
        if track.loop_start_index < 0:
            return True
        if self._one_shot:
            track.loop_start_index = -1
            track.loop_iterations.clear()
            return False
        if track.loop_count == 0:
            if self._max_loop_iterations > 0:
                count = track.loop_iterations.get(track.loop_start_index, 0) + 1
                track.loop_iterations[track.loop_start_index] = count
                if count > self._max_loop_iterations:
                    track.loop_start_index = -1
                    track.loop_iterations.clear()
                    return True
            track.command_index = track.loop_start_index
            return True
        track.loop_count -= 1
        if track.loop_count > 0:
            track.command_index = track.loop_start_index
        else:
            track.loop_start_index = -1
        return True

    def _open_track(self, track_no: int, offset: int) -> None:
        if not 0 <= track_no < MAX_TRACKS:
            return
        target = self._offset_to_index.get(offset)
        if target is None:
            return
        opened = TrackState(track_no=track_no)
        opened.command_index = target
        opened.active = True
        opened.program = self._default_programs.get(track_no, self._default_programs.get(0, 0))
        self._ctx.tracks[track_no] = opened

    def _jump(self, track: TrackState, offset: int, *, apply_loop_limit: bool = True) -> bool:
        target = self._offset_to_index.get(offset)
        if target is None:
            return False
        if (
            apply_loop_limit
            and self._max_loop_iterations > 0
            and target <= track.command_index
            and (self._one_shot or self._should_bound_backward_jump(track, offset))
        ):
            if self._one_shot and track.started_notes:
                return False
            count = track.jump_iterations.get(target, 0) + 1
            track.jump_iterations[target] = count
            if count > self._max_loop_iterations:
                return False
        track.command_index = target
        return True

    @staticmethod
    def _should_bound_backward_jump(track: TrackState, target_offset: int) -> bool:
        active_notes = track.active_notes
        if not active_notes:
            return True
        anchor_offset = max(note.command_offset for note in active_notes)
        if track.tie and track.tie_command_offset >= 0:
            anchor_offset = min(anchor_offset, track.tie_command_offset)
        return target_offset <= anchor_offset

    def _release_track(self, track: TrackState, events: list[dict]) -> None:
        for note in list(track.active_notes):
            events.append({
                "type": "note_off",
                "tick": self._ctx.tick_counter,
                "track": track.track_no,
                "note": note.note,
            })
        track.active_notes.clear()
        track.note_finish_wait = False
