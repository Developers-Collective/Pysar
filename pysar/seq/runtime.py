from dataclasses import dataclass, field
from enum import IntEnum

from pysar.core.format.rseq.mml import MAX_TRACKS, SEQ_GLOBAL_VAR_COUNT, SEQ_LOCAL_VAR_COUNT, SEQ_TRACK_VAR_COUNT, SEQ_VAR_DEFAULT
from pysar.seq.movevalue import MoveValue


INVALID_ENVELOPE = 255


def _to_s16(value: int) -> int:
    value = int(value) & 0xFFFF
    if value >= 0x8000:
        value -= 0x10000
    return value


class PlayerState(IntEnum):
    STOPPED = 0
    PLAYING = 1
    PAUSED = 2


@dataclass(slots=True)
class CallFrame:
    return_index: int
    loop_start_index: int = -1
    loop_count: int = 0


@dataclass(slots=True)
class ActiveNote:
    note: int
    velocity: int
    start_tick: int
    command_offset: int
    duration_ticks: int
    program: int
    natural_end_tick: int | None = None
    sweep_pitch: float = 0.0
    sweep_ticks: int = 0
    sweep_start_tick: int = 0

    def sweep_value(self, tick: int) -> float:
        if self.sweep_ticks <= 0 or self.sweep_pitch == 0.0:
            return 0.0
        elapsed = tick - self.sweep_start_tick
        if elapsed <= 0:
            return self.sweep_pitch
        if elapsed >= self.sweep_ticks:
            return 0.0
        return self.sweep_pitch * (self.sweep_ticks - elapsed) / self.sweep_ticks


@dataclass(slots=True)
class TrackState:
    track_no: int = 0
    command_index: int = 0
    active: bool = False
    finished: bool = False
    started_notes: bool = False
    wait_ticks: int = 0
    note_wait: bool = True
    note_finish_wait: bool = False
    tie: bool = False
    tie_command_offset: int = -1
    monophonic: bool = False
    porta: bool = False
    damper: bool = False
    muted: bool = False
    cmp_flag: bool = True

    program: int = 0
    bank: int = 0
    volume: MoveValue = field(default_factory=lambda: MoveValue(127))
    volume2: int = 127
    main_volume: int = 127
    pan: MoveValue = field(default_factory=lambda: MoveValue(64))
    surround_pan: MoveValue = field(default_factory=lambda: MoveValue(0))
    pitch_bend: MoveValue = field(default_factory=lambda: MoveValue(0))
    pitch_bend_range: int = 2
    init_pan: int = 64
    transpose: int = 0
    velocity_range: int = 127
    priority: int = 64
    porta_key: int = 60
    porta_time: int = 0
    sweep_pitch: float = 0.0

    attack: int = INVALID_ENVELOPE
    decay: int = INVALID_ENVELOPE
    sustain: int = INVALID_ENVELOPE
    release: int = INVALID_ENVELOPE
    hold: int = INVALID_ENVELOPE

    mod_depth: int = 0
    mod_speed: int = 16
    mod_type: int = 0
    mod_delay: int = 0
    mod_range: int = 1

    main_send: int = 127
    fx_send_a: int = 0
    fx_send_b: int = 0
    fx_send_c: int = 0
    lpf_cutoff: int = 64
    biquad_type: int = 0
    biquad_value: int = 0

    loop_start_index: int = -1
    loop_count: int = 0
    loop_iterations: dict[int, int] = field(default_factory=dict)
    jump_iterations: dict[int, int] = field(default_factory=dict)
    call_stack: list[CallFrame] = field(default_factory=list)
    variables: list[int] = field(default_factory=lambda: [SEQ_VAR_DEFAULT] * SEQ_TRACK_VAR_COUNT)
    active_notes: list[ActiveNote] = field(default_factory=list)


@dataclass(slots=True)
class PlayerContext:
    state: PlayerState = PlayerState.STOPPED
    tempo: int = 120
    timebase: int = 48
    tick_counter: int = 0
    tempo_ratio: float = 1.0
    local_vars: list[int] = field(default_factory=lambda: [SEQ_VAR_DEFAULT] * SEQ_LOCAL_VAR_COUNT)
    global_vars: list[int] = field(default_factory=lambda: [SEQ_VAR_DEFAULT] * SEQ_GLOBAL_VAR_COUNT)
    tracks: list[TrackState] = field(default_factory=lambda: [TrackState(track_no=i) for i in range(MAX_TRACKS)])

    def get_variable(self, var_no: int, track: TrackState) -> int:
        if 0 <= var_no < 16:
            return self.local_vars[var_no]
        if 16 <= var_no < 32:
            return self.global_vars[var_no - 16]
        if 32 <= var_no < 48:
            return track.variables[var_no - 32]
        return SEQ_VAR_DEFAULT

    def set_variable(self, var_no: int, value: int, track: TrackState) -> None:
        value = _to_s16(value)
        if 0 <= var_no < 16:
            self.local_vars[var_no] = value
        elif 16 <= var_no < 32:
            self.global_vars[var_no - 16] = value
        elif 32 <= var_no < 48:
            track.variables[var_no - 32] = value
