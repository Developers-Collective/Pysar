from dataclasses import dataclass, field, replace
import math

import numba as nb
import numpy as np

from pysar.seq.nw4r import (
    AUDIO_FRAME_INTERVAL_MS,
    AX_MAX_VOICES,
    BIQUAD_FILTER_TYPE_NONE,
    ENV_FLOOR_DB,
    EnvelopeState,
    LFO_TARGET_PITCH,
    LFO_TARGET_PAN,
    LFO_TARGET_VOLUME,
    LfoState,
    calc_pan_ratio,
    calc_pitch_ratio,
    biquad_coefficients,
    db_to_ratio as _nw_db_to_ratio,
    lpf_alpha,
    velocity_gain,
)
from pysar.seq.player import NoteRuntimeInfo, SequencePlayer
from pysar.seq.runtime import INVALID_ENVELOPE
from pysar.seq.resolver import BankWaveResolver, ResolvedRegion, midi_ratio, note_off_ignores_release
from pysar.seq.types import PlaybackContext, RenderOptions, RenderedAudio

#
# use jitting as an excuse to say Python was a good choice
#

# Pre-built lookup tables for numba
_NOTE_TABLE_NB = np.array(tuple(2.0 ** (i / 12.0) for i in range(12)), dtype=np.float64)
_PITCH_TABLE_NB = np.array(tuple(2.0 ** (i / (12.0 * 256)) for i in range(256)), dtype=np.float64)
_LFO_SIN_TABLE_NB = np.array([
    0, 6, 12, 19, 25, 31, 37, 43, 49, 54, 60, 65, 71, 76, 81, 85,
    90, 94, 98, 102, 106, 109, 112, 115, 117, 120, 122, 123, 125, 126, 126, 127, 127,
], dtype=np.int32)

# Envelope status as integers for numba
_ENV_ST_ATTACK = 0
_ENV_ST_HOLD = 1
_ENV_ST_DECAY = 2
_ENV_ST_SUSTAIN = 3
_ENV_ST_RELEASE = 4
_ENV_STATUS_TO_INT = {"attack": 0, "hold": 1, "decay": 2, "sustain": 3, "release": 4}
_ENV_STATUS_TO_STR = ("attack", "hold", "decay", "sustain", "release")
_ENV_FLOOR = -90.4
_AF_MS = 3  # AUDIO_FRAME_INTERVAL_MS

# Voice-state array layout
_VS_POS = 0
_VS_AGE = 1
_VS_DONE = 2
_VS_RELEASED = 3
_VS_CTRL_MS = 4
_VS_SW_AGE = 5
_VS_SW_PITCH = 6
_VS_SW_FRAMES = 7
_VS_LFO_CTR = 8
_VS_LFO_DCTR = 9
_VS_LFO_DELAY = 10
_VS_LFO_DEPTH = 11
_VS_LFO_RANGE = 12
_VS_LFO_SPEED = 13
_VS_ENV_ST = 14
_VS_ENV_VAL = 15
_VS_ENV_ATK = 16
_VS_ENV_HCTR = 17
_VS_ENV_DECAY = 18
_VS_ENV_SUS = 19
_VS_ENV_REL = 20
_VS_ENV_HOLD = 21
_VS_SIZE = 22


@nb.njit(cache=True, nogil=True)
def _db_to_ratio_nb(db):
    if db <= _ENV_FLOOR:
        return 0.0
    return 10.0 ** (db / 20.0)


@nb.njit(cache=True, nogil=True)
def _env_gain_nb(status, val, attack):
    if status == _ENV_ST_ATTACK and attack == 0.0:
        return 1.0
    return _db_to_ratio_nb(val / 10.0)


@nb.njit(cache=True, nogil=True)
def _env_update_nb(status, val, attack, hold_ctr, decay, sus, release, hold, msec):
    if msec <= 0:
        return status, val, hold_ctr
    if status == _ENV_ST_ATTACK:
        for _ in range(msec):
            val *= attack
            if val > -(1.0 / 32.0):
                val = 0.0
                status = _ENV_ST_HOLD
                hold_ctr = hold
                break
        return status, val, hold_ctr
    if status == _ENV_ST_HOLD:
        if msec < hold_ctr:
            hold_ctr -= msec
            return status, val, hold_ctr
        msec -= hold_ctr
        hold_ctr = 0
        status = _ENV_ST_DECAY
    if status == _ENV_ST_DECAY:
        val -= decay * msec
        if val < sus:
            val = sus
            status = _ENV_ST_SUSTAIN
        return status, val, hold_ctr
    if status == _ENV_ST_SUSTAIN:
        return status, val, hold_ctr
    if status == _ENV_ST_RELEASE:
        val -= release * msec
    return status, val, hold_ctr


@nb.njit(cache=True, nogil=True)
def _lfo_sin_nb(index, table):
    index = max(0, min(127, index))
    if index < 32:
        return table[index]
    if index < 64:
        return table[32 - (index - 32)]
    if index < 96:
        return -table[index - 64]
    return -table[32 - (index - 96)]


@nb.njit(cache=True, nogil=True)
def _lfo_value_nb(depth, rng, counter, delay_ctr, delay, table):
    if depth == 0.0 or delay_ctr < delay:
        return 0.0
    idx = int(counter * 128.0)
    if idx >= 128:
        idx = 127
    return (_lfo_sin_nb(idx, table) / 127.0) * depth * rng


@nb.njit(cache=True, nogil=True)
def _lfo_update_nb(counter, delay_ctr, delay, speed, msec):
    if msec <= 0:
        return counter, delay_ctr
    if delay_ctr < delay:
        if delay_ctr + msec <= delay:
            return counter, delay_ctr + msec
        msec -= delay - delay_ctr
        delay_ctr = delay
    counter += speed * msec / 1000.0
    counter -= int(counter)
    return counter, delay_ctr


@nb.njit(cache=True, nogil=True)
def _pitch_ratio_nb(pitch, note_table, pitch_table):
    octave = 0
    ratio = 1.0
    span = 256 * 12
    while pitch < 0:
        octave -= 1
        pitch += span
    while pitch >= span:
        octave += 1
        pitch -= span
    if octave > 0:
        ratio = 2.0 ** octave
    elif octave < 0:
        ratio = 1.0 / (2.0 ** (-octave))
    note = pitch // 256
    fine = pitch % 256
    if note:
        ratio *= note_table[note]
    if fine:
        ratio *= pitch_table[fine]
    return ratio


@nb.njit(cache=True, nogil=True)
def _pan_ratio_nb(pan, curve):
    pan_n = (max(-1.0, min(1.0, pan)) + 1.0) * 0.5
    center_zero = (curve == 1 or curve == 2 or curve == 4
                   or curve == 5 or curve == 7 or curve == 8)
    zero_clamp = (curve == 2 or curve == 5 or curve == 8)
    if curve == 6 or curve == 7 or curve == 8:
        ratio = pan_n
        center = 0.5
    elif curve == 3 or curve == 4 or curve == 5:
        ratio = math.sin(pan_n * (math.pi / 2.0))
        center = math.sin(math.pi / 4.0)
    else:
        ratio = math.sqrt(pan_n)
        center = math.sqrt(0.5)
    if center_zero and center > 0.0:
        ratio /= center
    upper = 1.0 if zero_clamp else 2.0
    return max(0.0, min(upper, ratio))


@nb.njit(cache=True, nogil=True)
def _sample_at_nb(samples, position, tension):
    n = len(samples)
    if position <= 0.0:
        return float(samples[0])
    if position >= n - 1:
        return float(samples[n - 1])
    base = int(position)
    frac = position - base
    y0 = float(samples[max(0, base - 1)])
    y1 = float(samples[base])
    y2 = float(samples[min(n - 1, base + 1)])
    y3 = float(samples[min(n - 1, base + 2)])
    a = tension
    c0 = (-a * y0) + ((2.0 - a) * y1) + ((a - 2.0) * y2) + (a * y3)
    c1 = (2.0 * a * y0) + ((a - 3.0) * y1) + ((3.0 - 2.0 * a) * y2) - (a * y3)
    c2 = (-a * y0) + (a * y2)
    c3 = y1
    return ((c0 * frac + c1) * frac + c2) * frac + c3


@nb.njit(cache=True, nogil=True)
def _render_voice_loop(
    out, send_a, send_b, send_c,
    samples, vstate,
    lpf_st, bq_x1, bq_x2, bq_y1, bq_y2,
    base_gain, base_ratio, region_sr, out_sr,
    track_pitch, track_pan, pan_curve,
    main_send, fx_a, fx_b, fx_c,
    lfo_target, note_pan,
    lpf_alpha, has_lpf,
    bq_b0, bq_b1, bq_b2, bq_a1, bq_a2, has_bq,
    is_looped, loop_start, loop_end,
    tension, note_table, pitch_table, lfo_sin_table,
):
    position = vstate[_VS_POS]
    age = vstate[_VS_AGE]
    done = vstate[_VS_DONE] > 0.5
    released = vstate[_VS_RELEASED] > 0.5
    ctrl_ms = vstate[_VS_CTRL_MS]
    sw_age = vstate[_VS_SW_AGE]
    sw_pitch = vstate[_VS_SW_PITCH]
    sw_frames = vstate[_VS_SW_FRAMES]
    lfo_ctr = vstate[_VS_LFO_CTR]
    lfo_dctr = int(vstate[_VS_LFO_DCTR])
    lfo_delay = int(vstate[_VS_LFO_DELAY])
    lfo_depth = vstate[_VS_LFO_DEPTH]
    lfo_rng = vstate[_VS_LFO_RANGE]
    lfo_speed = vstate[_VS_LFO_SPEED]
    env_st = int(vstate[_VS_ENV_ST])
    env_val = vstate[_VS_ENV_VAL]
    env_atk = vstate[_VS_ENV_ATK]
    env_hctr = int(vstate[_VS_ENV_HCTR])
    env_decay = vstate[_VS_ENV_DECAY]
    env_sus = vstate[_VS_ENV_SUS]
    env_rel = vstate[_VS_ENV_REL]
    env_hold = int(vstate[_VS_ENV_HOLD])

    n_samples = len(samples)
    frames = len(out)
    sr_ratio = region_sr / max(1.0, float(out_sr))
    inv_sr = 1000.0 / max(1.0, float(out_sr))
    note_p = 0.0 if note_pan == 64 else (note_pan - 64.0) / 63.0

    for i in range(frames):
        if done:
            break

        # LFO
        lfo_val = _lfo_value_nb(lfo_depth, lfo_rng, lfo_ctr, lfo_dctr, lfo_delay, lfo_sin_table)

        # Sweep
        sweep_val = 0.0
        if sw_frames > 0 and sw_pitch != 0.0 and sw_age < sw_frames:
            sweep_val = sw_pitch * (sw_frames - sw_age) / sw_frames

        # Pitch
        p_off = track_pitch + sweep_val
        if lfo_target == 0:
            p_off += lfo_val
        p_ratio = _pitch_ratio_nb(int(p_off * 256.0 + 0.5 if p_off >= 0 else p_off * 256.0 - 0.5),
                                  note_table, pitch_table)
        step = sr_ratio * base_ratio * p_ratio

        # Gain
        n_gain = base_gain
        if lfo_target == 1:
            n_gain *= _db_to_ratio_nb(lfo_val * 6.0)

        # Pan
        pan_lfo = lfo_val if lfo_target == 2 else 0.0
        c_pan = max(-1.0, min(1.0, track_pan + pan_lfo + note_p))
        l_gain = _pan_ratio_nb(-c_pan, pan_curve)
        r_gain = _pan_ratio_nb(c_pan, pan_curve)

        # Envelope
        e_gain = _env_gain_nb(env_st, env_val, env_atk)
        gain = e_gain * n_gain
        if gain <= 0.0 and done:
            break

        # Sample interpolation
        smp = _sample_at_nb(samples, position, tension)
        left = smp * gain * l_gain
        right = smp * gain * r_gain

        # LPF filter (scalar, no array allocation)
        if has_lpf:
            lpf_st[0] += (left - lpf_st[0]) * lpf_alpha
            lpf_st[1] += (right - lpf_st[1]) * lpf_alpha
            left = lpf_st[0]
            right = lpf_st[1]

        # Biquad filter (scalar)
        if has_bq:
            bq_ol = bq_b0 * left + bq_b1 * bq_x1[0] + bq_b2 * bq_x2[0] - bq_a1 * bq_y1[0] - bq_a2 * bq_y2[0]
            bq_or = bq_b0 * right + bq_b1 * bq_x1[1] + bq_b2 * bq_x2[1] - bq_a1 * bq_y1[1] - bq_a2 * bq_y2[1]
            bq_x2[0] = bq_x1[0]; bq_x2[1] = bq_x1[1]
            bq_x1[0] = left; bq_x1[1] = right
            bq_y2[0] = bq_y1[0]; bq_y2[1] = bq_y1[1]
            bq_y1[0] = bq_ol; bq_y1[1] = bq_or
            left = bq_ol
            right = bq_or

        # Write outputs
        out[i, 0] = left * main_send
        out[i, 1] = right * main_send
        send_a[i, 0] = left * fx_a
        send_a[i, 1] = right * fx_a
        send_b[i, 0] = left * fx_b
        send_b[i, 1] = right * fx_b
        send_c[i, 0] = left * fx_c
        send_c[i, 1] = right * fx_c

        # Advance state
        position += step
        age += 1
        if sw_age < sw_frames:
            sw_age += 1

        # Step control
        ctrl_ms += inv_sr
        while ctrl_ms >= _AF_MS:
            lfo_ctr, lfo_dctr = _lfo_update_nb(lfo_ctr, lfo_dctr, lfo_delay, lfo_speed, _AF_MS)
            env_st, env_val, env_hctr = _env_update_nb(
                env_st, env_val, env_atk, env_hctr,
                env_decay, env_sus, env_rel, env_hold, _AF_MS,
            )
            ctrl_ms -= _AF_MS
            if released and _env_gain_nb(env_st, env_val, env_atk) == 0.0:
                done = True
                break

        # Loop / end-of-sample
        if not done:
            if is_looped and not released:
                if position >= loop_end:
                    position = loop_start + (position - loop_end)
            elif position >= n_samples - 1:
                done = True

    # Pack state back
    vstate[_VS_POS] = position
    vstate[_VS_AGE] = age
    vstate[_VS_DONE] = 1.0 if done else 0.0
    vstate[_VS_CTRL_MS] = ctrl_ms
    vstate[_VS_SW_AGE] = sw_age
    vstate[_VS_LFO_CTR] = lfo_ctr
    vstate[_VS_LFO_DCTR] = float(lfo_dctr)
    vstate[_VS_ENV_ST] = float(env_st)
    vstate[_VS_ENV_VAL] = env_val
    vstate[_VS_ENV_HCTR] = float(env_hctr)


@dataclass(slots=True)
class _TrackMix:
    volume: float = 1.0
    pan: float = 0.0
    pitch: float = 0.0
    program: int = 0
    priority: int = 128
    main_send: float = 1.0
    fx_send_a: float = 0.0
    fx_send_b: float = 0.0
    fx_send_c: float = 0.0
    lpf_scale: float = 1.0
    biquad_type: int = BIQUAD_FILTER_TYPE_NONE
    biquad_value: float = 0.0
    pan_curve: int = 0


@dataclass(slots=True)
class _Voice:
    track_no: int
    note: int
    region: ResolvedRegion
    base_ratio: float
    velocity_gain: float
    position: float = 0.0
    age_frames: int = 0
    released: bool = False
    done: bool = False

    ignore_note_off: bool = False
    priority: int = 128
    release_priority_fix: bool = False
    physical_voice_count: int = 1

    lfo_target: int = 0
    env: EnvelopeState = field(default_factory=EnvelopeState)
    lfo: LfoState = field(default_factory=LfoState)
    control_progress_ms: float = 0.0
    lpf_state: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=np.float32))
    biquad_x1: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=np.float32))
    biquad_x2: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=np.float32))
    biquad_y1: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=np.float32))
    biquad_y2: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=np.float32))
    sweep_pitch: float = 0.0
    sweep_frames: int = 0
    sweep_age_frames: int = 0

    def current_gain(self) -> float:
        if self.done:
            return 0.0
        return self.env.current_gain()

    def start_release(self) -> None:
        if self.ignore_note_off or self.released or self.done:
            return
        self.released = True
        self.env.note_off()
        if not self.release_priority_fix:
            self.priority = 1

    def lfo_value(self) -> float:
        return self.lfo.value()

    def sweep_value(self) -> float:
        if self.sweep_frames <= 0 or self.sweep_pitch == 0.0:
            return 0.0
        if self.sweep_age_frames >= self.sweep_frames:
            return 0.0
        return self.sweep_pitch * (self.sweep_frames - self.sweep_age_frames) / self.sweep_frames

    def step_control(self, sample_rate: int) -> None:
        if self.done:
            return
        self.control_progress_ms += 1000.0 / max(1, sample_rate)
        while self.control_progress_ms >= AUDIO_FRAME_INTERVAL_MS:
            self.lfo.update(AUDIO_FRAME_INTERVAL_MS)
            self.env.update(AUDIO_FRAME_INTERVAL_MS)
            self.control_progress_ms -= AUDIO_FRAME_INTERVAL_MS
            if self.released and self.env.current_gain() == 0.0:
                self.done = True
                break


class SequenceRenderer:
    _AX_INTERPOLATION_TENSION = -1.325

    def __init__(self) -> None:
        self._resolver: BankWaveResolver | None = None
        self.last_sequence_truncated = False

    def render(self, context: PlaybackContext, options: RenderOptions | None = None) -> RenderedAudio:
        settings = options or RenderOptions()
        player = self.make_sequence_player(context, settings)
        events = player.render_events(
            max_ticks=settings.max_ticks,
            loop_count=settings.loop_count,
            one_shot=settings.one_shot,
        )
        if not events:
            return RenderedAudio(samples=np.zeros((0, 2), dtype=np.float32), sample_rate=settings.sample_rate)

        frame_events = self._event_frames(events, player.timebase, settings.sample_rate)
        return self._render_frame_events(context, frame_events, settings)

    def stream(self, context: PlaybackContext, options: RenderOptions | None = None, *, start_frame: int = 0):
        settings = options or RenderOptions()
        start_frame = max(0, int(start_frame))

        player = self.make_sequence_player(context, settings)
        self.last_sequence_truncated = False
        seek_state = {"suppress": start_frame > 0}
        frame_events = self._iter_tick_frame_events(
            player.iter_event_ticks(
                max_ticks=settings.max_ticks,
                loop_count=settings.loop_count,
                one_shot=settings.one_shot,
                suppress_track_state=lambda: seek_state["suppress"],
            ),
            player.timebase,
            settings.sample_rate,
            start_frame=start_frame,
            seek_state=seek_state,
            snapshot_events=player.snapshot_track_events,
        )
        try:
            yield from self._stream_tick_event_iter(context, frame_events, settings, start_frame=start_frame)
        finally:
            self.last_sequence_truncated = player.truncated

    def _try_stream_fast_single_voice_sequence(
        self,
        context: PlaybackContext,
        events: list[dict],
        timebase: int,
        settings: RenderOptions,
        *,
        start_frame: int = 0,
    ):
        if not events or self._resolver is None:
            return None

        frame_events = self._event_frames(events, timebase, settings.sample_rate)
        note_events = [
            (frame, event)
            for frame, event in frame_events
            if event.get("type") in ("note_on", "note_change")
        ]
        if len(note_events) != 1 or note_events[0][1].get("type") != "note_on":
            return None

        note_frame, note_event = note_events[0]
        track_no = int(note_event.get("track", 0))
        note = int(note_event.get("note", 0))
        velocity = int(note_event.get("velocity", 127))
        program = int(note_event.get("program", 0))
        if any(int(note_event.get(key, 0) or 0) != 0 for key in ("mod_depth", "mod_delay")):
            return None
        if float(note_event.get("sweep_pitch", 0.0) or 0.0) != 0.0:
            return None

        track = self._default_track_mix(context)
        end_frame: int | None = None
        for frame, event in frame_events:
            event_type = event.get("type")
            if frame > note_frame:
                if (
                    event_type == "note_off"
                    and int(event.get("track", 0)) == track_no
                    and int(event.get("note", note)) == note
                ):
                    end_frame = frame
                    continue
                if event_type not in ("tempo", "render_marker"):
                    return None
                continue

            if event_type == "control_change":
                cc = int(event.get("cc", -1))
                if cc == 7:
                    gain = event.get("gain")
                    track.volume = float(gain if gain is not None else (event.get("value", 127) / 127.0)) * context.archive_volume
                elif cc == 10:
                    track.pan = ((event.get("value", 64) - 64) / 63.0) if event.get("value", 64) != 64 else 0.0
                continue
            if event_type == "program_change":
                program = int(event.get("program", program))
                continue
            if event_type == "pitch_bend":
                if float(event.get("semitones", 0.0) or 0.0) != 0.0:
                    return None
                continue
            if event_type == "track_param":
                track.main_send = max(0.0, min(1.0, int(event.get("main_send", 127)) / 127.0))
                track.fx_send_a = max(0.0, min(1.0, int(event.get("fx_send_a", 0)) / 127.0))
                track.fx_send_b = max(0.0, min(1.0, int(event.get("fx_send_b", 0)) / 127.0))
                track.fx_send_c = max(0.0, min(1.0, int(event.get("fx_send_c", 0)) / 127.0))
                track.lpf_scale = max(0.0, min(1.0, int(event.get("lpf_cutoff", 64)) / 64.0))
                track.biquad_type = int(event.get("biquad_type", BIQUAD_FILTER_TYPE_NONE))
                track.biquad_value = max(0.0, min(1.0, int(event.get("biquad_value", 0)) / 127.0))

        if end_frame is None or end_frame <= note_frame:
            return None
        if track.fx_send_a or track.fx_send_b or track.fx_send_c:
            return None
        if track.lpf_scale != 1.0 or track.biquad_type != BIQUAD_FILTER_TYPE_NONE or track.biquad_value != 0.0:
            return None

        region = self._resolver.resolve(program, note, velocity)
        if region is None or len(region.samples) == 0:
            return None
        region = _apply_event_overrides(region, note_event)
        if not self._has_simple_preview_envelope(region):
            return None

        step = (region.sample_rate / max(1, settings.sample_rate)) * midi_ratio(
            note,
            region.param.original_key,
            region.param.pitch,
        )
        if step <= 0.0:
            return None

        samples = region.samples
        source = np.arange(len(samples), dtype=np.float64)
        pan = self._combine_pan(track.pan, region.param.pan)
        left_gain, right_gain = self._pan_gains(pan, track.pan_curve)
        gain = (region.param.volume / 127.0) * velocity_gain(velocity) * track.volume * track.main_send
        chunk_frames = max(settings.block_frames * 2, 1024)
        start = max(start_frame, note_frame)
        stop = max(start, end_frame)

        def chunks():
            cursor = start
            while cursor < stop:
                frames = min(chunk_frames, stop - cursor)
                relative = np.arange(cursor - note_frame, cursor - note_frame + frames, dtype=np.float64)
                positions = relative * step
                if region.is_looped:
                    loop_start = max(0, min(region.loop_start, len(samples) - 1))
                    loop_end = max(loop_start + 1, min(region.loop_end, len(samples)))
                    loop_len = max(1, loop_end - loop_start)
                    mask = positions >= loop_end
                    if np.any(mask):
                        positions = positions.copy()
                        positions[mask] = loop_start + np.mod(positions[mask] - loop_start, loop_len)
                else:
                    positions = np.minimum(positions, len(samples) - 1)

                mono = np.interp(positions, source, samples).astype(np.float32, copy=False)
                out = np.column_stack((mono * gain * left_gain, mono * gain * right_gain))
                self._apply_master_gain(out, settings.master_gain)
                yield out.astype(np.float32, copy=False)
                cursor += frames

        return chunks()

    def make_sequence_player(self, context: PlaybackContext, settings: RenderOptions) -> SequencePlayer:
        if context.brseq is None or context.brbnk is None or context.brwar is None:
            raise ValueError("sequence context is incomplete")

        self._resolver = BankWaveResolver(context.brbnk, context.brwar)
        program_override = _clamp_program(settings.seq_program_override)
        note_override = _clamp_midi_note(settings.seq_note_override)
        player = SequencePlayer()
        player.set_note_runtime_resolver(
            self._make_runtime_resolver(
                program_override,
                note_override,
                include_looped_natural=settings.one_shot,
            )
        )
        player.set_random_overrides(settings.seq_random_overrides)
        player.load(
            context.brseq.data,
            start_label=context.start_label,
            start_offset=context.start_offset,
            default_programs=context.default_programs,
        )
        return player

    def render_wave_sound(self, context: PlaybackContext, options: RenderOptions | None = None) -> RenderedAudio:
        settings = options or RenderOptions()
        if context.brwsd is None or context.brwar is None:
            raise ValueError("wave context is incomplete")
        wave_index = int(context.extras.get("wave_sound_index", -1))
        if wave_index < 0 and getattr(context.entry, "sound_info", None) is not None:
            wave_index = int(getattr(context.entry.sound_info, "wave_index", -1))
        if wave_index < 0 or wave_index >= len(context.brwsd):
            return RenderedAudio(samples=np.zeros((0, 2), dtype=np.float32), sample_rate=settings.sample_rate)

        wave_sound = context.brwsd[wave_index]
        resolver = BankWaveResolver(_DirectBankAdapter(), context.brwar)
        note_regions: list[ResolvedRegion | None] = []
        note_reference_frames: list[int] = []

        # NW4R's WsdPlayer always requests note index 0 when starting its one
        # channel.  Track events and additional note-table entries are stored
        # in the file, but are not runtime variations.
        for note_info in wave_sound.notes[:1]:
            region = resolver.resolve(note_info.wave_index, note_info.original_key, 127)
            if region is None:
                note_regions.append(None)
                note_reference_frames.append(0)
                continue
            applied_region = self._apply_note_info(region, note_info)
            note_regions.append(applied_region)
            note_reference_frames.append(
                self._region_playback_frames(applied_region, settings.sample_rate, note_info.original_key)
            )

        simple_audio = self._render_simple_wave_sound(context, wave_sound, note_regions, settings)
        if simple_audio is not None:
            return simple_audio

        frame_events = self._wave_frame_events(wave_sound, note_regions, note_reference_frames)
        if not frame_events:
            return RenderedAudio(samples=np.zeros((0, 2), dtype=np.float32), sample_rate=settings.sample_rate)

        self._resolver = resolver
        frame_events.sort(key=lambda item: (item[0], 0 if item[1].get("type") == "note_off" else 1))
        return self._render_frame_events(context, frame_events, settings)

    def stream_wave_sound(
        self,
        context: PlaybackContext,
        options: RenderOptions | None = None,
        *,
        start_frame: int = 0,
    ):
        settings = options or RenderOptions()
        if context.brwsd is None or context.brwar is None:
            raise ValueError("wave context is incomplete")
        wave_index = int(context.extras.get("wave_sound_index", -1))
        if wave_index < 0 and getattr(context.entry, "sound_info", None) is not None:
            wave_index = int(getattr(context.entry.sound_info, "wave_index", -1))
        if wave_index < 0 or wave_index >= len(context.brwsd):
            return

        wave_sound = context.brwsd[wave_index]
        resolver = BankWaveResolver(_DirectBankAdapter(), context.brwar)
        note_regions: list[ResolvedRegion | None] = []
        note_reference_frames: list[int] = []

        for note_info in wave_sound.notes[:1]:
            region = resolver.resolve(note_info.wave_index, note_info.original_key, 127)
            if region is None:
                note_regions.append(None)
                note_reference_frames.append(0)
                continue
            applied_region = self._apply_note_info(region, note_info)
            note_regions.append(applied_region)
            note_reference_frames.append(
                self._region_playback_frames(applied_region, settings.sample_rate, note_info.original_key)
            )

        simple_chunks = self._stream_simple_wave_sound(
            context,
            wave_sound,
            note_regions,
            settings,
            start_frame=max(0, int(start_frame)),
        )
        if simple_chunks is not None:
            yield from simple_chunks
            return

        frame_events = self._wave_frame_events(wave_sound, note_regions, note_reference_frames)
        if not frame_events:
            return
        self._resolver = resolver
        frame_events.sort(key=lambda item: (item[0], 0 if item[1].get("type") == "note_off" else 1))
        yield from self._stream_frame_events(
            context,
            frame_events,
            settings,
            start_frame=max(0, int(start_frame)),
        )

    def render_stream_sound(
            self,
            context: PlaybackContext,
            options: RenderOptions | None = None,
            track_indices: list[int] | tuple[int, ...] | None = None,
    ) -> RenderedAudio:
        settings = options or RenderOptions()
        if context.brstm is None:
            raise ValueError("stream context is incomplete")
        pcm = context.brstm.decode().astype(np.float32) / 32768.0
        if pcm.ndim == 1:
            pcm = np.repeat(pcm[:, None], 2, axis=1)
        else:
            pcm = self._mix_stream_tracks(pcm, track_indices, context.brstm.data.tracks)
        if context.brstm.sample_rate != settings.sample_rate and pcm.shape[0] > 0:
            pcm = self._resample_stereo(pcm, context.brstm.sample_rate, settings.sample_rate)
        pcm *= context.archive_volume
        self._apply_master_gain(pcm, settings.master_gain)
        return RenderedAudio(samples=pcm.astype(np.float32, copy=False), sample_rate=settings.sample_rate)

    def stream_stream_sound(
            self,
            context: PlaybackContext,
            options: RenderOptions | None = None,
            track_indices: list[int] | tuple[int, ...] | None = None,
            start_frame: int = 0,
            end_frame: int | None = None,
    ):
        settings = options or RenderOptions()
        if context.brstm is None:
            raise ValueError("stream context is incomplete")
        # The HTTP STRM path requests the BRSTM's native sample rate. Keep the
        # existing full-render fallback for callers that explicitly request a
        # different rate, where block-local resampling would introduce seams.
        if int(context.brstm.sample_rate) != int(settings.sample_rate):
            audio = self.render_stream_sound(context, settings, track_indices=track_indices)
            ratio = settings.sample_rate / max(1, int(context.brstm.sample_rate))
            first = max(0, min(len(audio.samples), round(int(start_frame) * ratio)))
            last = len(audio.samples) if end_frame is None else max(
                first,
                min(len(audio.samples), round(int(end_frame) * ratio)),
            )
            yield from self._chunk_audio(audio.samples[first:last], max(settings.block_frames * 8, 4096))
            return
        for pcm16 in context.brstm.iter_decoded_blocks(
            start_frame=max(0, int(start_frame)),
            end_frame=end_frame,
        ):
            pcm = pcm16.astype(np.float32) / 32768.0
            if pcm.ndim == 1:
                pcm = np.repeat(pcm[:, None], 2, axis=1)
            else:
                pcm = self._mix_stream_tracks(pcm, track_indices, context.brstm.data.tracks)
            pcm *= context.archive_volume
            self._apply_master_gain(pcm, settings.master_gain)
            yield pcm.astype(np.float32, copy=False)

    @staticmethod
    def _mix_stream_tracks(
            pcm: np.ndarray,
            track_indices: list[int] | tuple[int, ...] | None,
            tracks=None,
    ) -> np.ndarray:
        """Downmix selected authored BRSTM tracks into stereo preview output."""
        channel_count = int(pcm.shape[1])
        mappings = []
        for authored_index, track in enumerate(list(tracks or ())):
            channels = (
                track.resolved_channel_indices()
                if hasattr(track, "resolved_channel_indices")
                else [track.left_channel_id, track.right_channel_id][:max(1, int(track.channel_count))]
            )
            channels = [int(channel) for channel in channels if 0 <= int(channel) < channel_count]
            if channels:
                mappings.append((
                    authored_index,
                    channels,
                    max(0, min(255, int(getattr(track, "volume", 127)))),
                    max(0, min(255, int(getattr(track, "pan", 64)))),
                ))
        if not mappings:
            for track_index, start_channel in enumerate(range(0, channel_count, 2)):
                mappings.append((
                    track_index,
                    list(range(start_channel, min(start_channel + 2, channel_count))),
                    127,
                    64,
                ))
        mapping_by_index = {
            track_index: (channels, volume, pan)
            for track_index, channels, volume, pan in mappings
        }
        if track_indices is None:
            selected = list(mapping_by_index)
        else:
            selected_set: set[int] = set()
            for index in track_indices:
                try:
                    track_index = int(index)
                except (TypeError, ValueError):
                    continue
                if track_index in mapping_by_index:
                    selected_set.add(track_index)
            selected = [track_index for track_index in mapping_by_index if track_index in selected_set]
            # The UI prevents an empty selection. Keep playback audible if an
            # out-of-date client sends no valid track indices.
            if not selected:
                selected = list(mapping_by_index)

        mixed = np.zeros((pcm.shape[0], 2), dtype=np.float32)
        for track_index in selected:
            channels, volume, pan = mapping_by_index[track_index]
            left_channels = channels[0::2]
            right_channels = channels[1::2]
            left = np.mean(pcm[:, left_channels], axis=1)
            right = (
                np.mean(pcm[:, right_channels], axis=1)
                if right_channels
                else left
            )
            gain = volume / 127.0
            pan_value = ((pan - 63) if pan <= 1 else (pan - 64)) / 63.0
            pan_value = max(-1.0, min(1.0, pan_value))
            left_gain = 1.0 - max(0.0, pan_value)
            right_gain = 1.0 + min(0.0, pan_value)
            mixed[:, 0] += left * gain * left_gain
            mixed[:, 1] += right * gain * right_gain
        return mixed / len(selected)

    def _make_runtime_resolver(
        self,
        program_override: int | None = None,
        note_override: int | None = None,
        *,
        include_looped_natural: bool = False,
    ):
        resolver = self._resolver
        runtime_cache: dict[tuple[int, ...], NoteRuntimeInfo] = {}

        def inner(track, note: int, velocity: int) -> NoteRuntimeInfo:
            assert resolver is not None
            resolved_program = program_override if program_override is not None else track.program
            resolved_note = note_override if note_override is not None else note
            cache_key = (
                int(resolved_program),
                int(resolved_note),
                int(velocity),
                int(track.attack),
                int(track.decay),
                int(track.sustain),
                int(track.release),
                int(track.hold),
            )
            cached = runtime_cache.get(cache_key)
            if cached is not None:
                return cached
            info = resolver.resolve_info(resolved_program, resolved_note, velocity)
            if info is None:
                result = NoteRuntimeInfo()
                runtime_cache[cache_key] = result
                return result
            param = _apply_track_param_overrides(info.param, track)
            ratio = midi_ratio(resolved_note, param.original_key, param.pitch)
            if ratio <= 0:
                result = NoteRuntimeInfo()
                runtime_cache[cache_key] = result
                return result
            natural_duration = self._sample_playback_duration_seconds(
                sample_rate=info.sample_rate,
                sample_count=info.sample_count,
                is_looped=info.is_looped,
                loop_end=info.loop_end,
                ratio=ratio,
                include_looped_natural=include_looped_natural,
            )
            result = NoteRuntimeInfo(
                natural_duration_seconds=natural_duration,
                ignore_note_off=note_off_ignores_release(param, info.is_looped),
            )
            runtime_cache[cache_key] = result
            return result

        return inner

    @staticmethod
    def _region_playback_duration_seconds(
        region: ResolvedRegion,
        ratio: float,
        include_looped_natural: bool,
    ) -> float | None:
        return SequenceRenderer._sample_playback_duration_seconds(
            sample_rate=region.sample_rate,
            sample_count=len(region.samples),
            is_looped=region.is_looped,
            loop_end=region.loop_end,
            ratio=ratio,
            include_looped_natural=include_looped_natural,
        )

    @staticmethod
    def _sample_playback_duration_seconds(
        *,
        sample_rate: int,
        sample_count: int,
        is_looped: bool,
        loop_end: int,
        ratio: float,
        include_looped_natural: bool,
    ) -> float | None:
        if ratio <= 0 or sample_rate <= 0 or sample_count <= 0:
            return None
        if not is_looped:
            return (sample_count / sample_rate) / ratio
        if not include_looped_natural:
            return None
        end_sample = max(1, min(int(loop_end or sample_count), sample_count) - 1)
        return (end_sample / sample_rate) / ratio

    def _event_frames(self, events: list[dict], timebase: int, sample_rate: int) -> list[tuple[int, dict]]:
        if timebase <= 0:
            timebase = 48
        current_tempo = 120
        last_tick = 0
        current_time = 0.0
        frame_events: list[tuple[int, dict]] = []
        for event in events:
            tick = int(event.get("tick", last_tick))
            delta_ticks = max(0, tick - last_tick)
            current_time += delta_ticks * 60.0 / (max(1, current_tempo) * timebase)
            last_tick = tick
            frame_events.append((round(current_time * sample_rate), event))
            if event.get("type") == "tempo":
                current_tempo = int(event.get("tempo", current_tempo) or current_tempo)
        return frame_events

    def _iter_event_frames(self, events, timebase: int, sample_rate: int):
        if timebase <= 0:
            timebase = 48
        current_tempo = 120
        last_tick = 0
        current_frame = 0.0
        for event in events:
            tick = int(event.get("tick", last_tick))
            delta_ticks = max(0, tick - last_tick)
            current_frame += delta_ticks * 60.0 * sample_rate / (max(1, current_tempo) * timebase)
            last_tick = tick
            yield round(current_frame), event
            if event.get("type") == "tempo":
                current_tempo = int(event.get("tempo", current_tempo) or current_tempo)

    def _iter_tick_frame_events(
        self,
        tick_events,
        timebase: int,
        sample_rate: int,
        *,
        start_frame: int = 0,
        seek_state: dict[str, bool] | None = None,
        snapshot_events=None,
    ):
        if timebase <= 0:
            timebase = 48
        current_tempo = 120
        last_tick = 0
        current_frame = 0.0
        active_notes: dict[tuple[int, int], list[tuple[int, int, dict]]] = {}
        note_order = 0
        seeking = start_frame > 0
        for tick, events in tick_events:
            tick = int(tick)
            delta_ticks = max(0, tick - last_tick)
            current_frame += delta_ticks * 60.0 * sample_rate / (max(1, current_tempo) * timebase)
            last_tick = tick
            frame = round(current_frame)
            if seeking and frame < start_frame:
                for event in events:
                    event_type = event.get("type")
                    track = int(event.get("track", 0))
                    note = int(event.get("note", 0))
                    if event_type == "note_on":
                        active_notes.setdefault((track, note), []).append((frame, note_order, event))
                        note_order += 1
                    elif event_type == "note_change":
                        old_key = (track, int(event.get("old_note", note)))
                        prior = active_notes.get(old_key)
                        if prior:
                            prior.pop()
                            if not prior:
                                active_notes.pop(old_key, None)
                        active_notes.setdefault((track, note), []).append(
                            (frame, note_order, {**event, "type": "note_on"})
                        )
                        note_order += 1
                    elif event_type == "note_off":
                        active = active_notes.get((track, note))
                        if active:
                            active.pop(0)
                            if not active:
                                active_notes.pop((track, note), None)
            else:
                if seeking:
                    seeking = False
                    if seek_state is not None:
                        seek_state["suppress"] = False
                    active = (item for notes in active_notes.values() for item in notes)
                    for note_frame, _, note_event in sorted(active, key=lambda item: (item[0], item[1])):
                        yield note_frame, [note_event]
                    controls = snapshot_events() if snapshot_events is not None else []
                    if controls:
                        events = [*controls, *events]
                yield frame, events
            for event in events:
                if event.get("type") == "tempo":
                    current_tempo = int(event.get("tempo", current_tempo) or current_tempo)

    def _render_frame_events(
        self,
        context: PlaybackContext,
        frame_events: list[tuple[int, dict]],
        settings: RenderOptions,
    ) -> RenderedAudio:
        buffer = np.zeros((max(1, frame_events[-1][0] + round(settings.tail_seconds * settings.sample_rate)), 2), dtype=np.float32)
        fx_a = np.zeros_like(buffer)
        fx_b = np.zeros_like(buffer)
        fx_c = np.zeros_like(buffer)
        tracks: dict[int, _TrackMix] = {}
        voices: list[_Voice] = []
        cursor = 0

        for frame, event in frame_events:
            if frame > len(buffer):
                buffer = self._grow_buffer(buffer, frame)
                fx_a = self._grow_buffer(fx_a, frame)
                fx_b = self._grow_buffer(fx_b, frame)
                fx_c = self._grow_buffer(fx_c, frame)
            if frame > cursor:
                self._mix_into(buffer, fx_a, fx_b, fx_c, voices, tracks, cursor, frame - cursor, settings.sample_rate)
                cursor = frame
            self._apply_event(context, event, tracks, voices, settings, frame)

        tail_frames = round(settings.tail_seconds * settings.sample_rate)
        end_frame = min(len(buffer), cursor + tail_frames)
        if end_frame > cursor:
            self._mix_into(buffer, fx_a, fx_b, fx_c, voices, tracks, cursor, end_frame - cursor, settings.sample_rate)
            cursor = end_frame

        while any(not voice.done for voice in voices):
            if cursor + settings.block_frames > len(buffer):
                buffer = self._grow_buffer(buffer, cursor + settings.block_frames)
                fx_a = self._grow_buffer(fx_a, cursor + settings.block_frames)
                fx_b = self._grow_buffer(fx_b, cursor + settings.block_frames)
                fx_c = self._grow_buffer(fx_c, cursor + settings.block_frames)
            self._mix_into(buffer, fx_a, fx_b, fx_c, voices, tracks, cursor, settings.block_frames, settings.sample_rate)
            cursor += settings.block_frames
            if cursor >= len(buffer) - settings.block_frames:
                break

        if cursor < len(buffer):
            buffer = buffer[:cursor]
            fx_a = fx_a[:cursor]
            fx_b = fx_b[:cursor]
            fx_c = fx_c[:cursor]
        self._apply_fx_returns(buffer, fx_a, fx_b, fx_c, settings.sample_rate)
        self._apply_master_gain(buffer, settings.master_gain)
        return RenderedAudio(samples=buffer, sample_rate=settings.sample_rate)

    def _stream_frame_events(
        self,
        context: PlaybackContext,
        frame_events: list[tuple[int, dict]],
        settings: RenderOptions,
        *,
        start_frame: int = 0,
    ):
        if not frame_events:
            return

        chunk_frames = max(settings.block_frames * 2, 1024)
        tail_frames = round(settings.tail_seconds * settings.sample_rate)
        final_event_frame = max(frame for frame, _ in frame_events)
        hard_end = final_event_frame + tail_frames
        tracks: dict[int, _TrackMix] = {}
        voices: list[_Voice] = []
        cursor = max(0, int(start_frame))
        event_index = 0
        frame_events = sorted(frame_events, key=lambda item: (item[0], 0 if item[1].get("type") == "note_off" else 1))

        while event_index < len(frame_events) or voices:
            while event_index < len(frame_events) and frame_events[event_index][0] <= cursor:
                event_frame, event = frame_events[event_index]
                self._apply_event(
                    context,
                    event,
                    tracks,
                    voices,
                    settings,
                    event_frame,
                    current_frame=cursor if event_frame < cursor else None,
                )
                event_index += 1
            if start_frame:
                voices[:] = [voice for voice in voices if not voice.done]

            if event_index < len(frame_events):
                next_event_frame = max(cursor, frame_events[event_index][0])
                frames = min(chunk_frames, max(1, next_event_frame - cursor))
            else:
                if cursor >= hard_end and not voices:
                    break
                frames = chunk_frames

            buffer = np.zeros((frames, 2), dtype=np.float32)
            fx_a = np.zeros_like(buffer)
            fx_b = np.zeros_like(buffer)
            fx_c = np.zeros_like(buffer)
            self._mix_into(buffer, fx_a, fx_b, fx_c, voices, tracks, 0, frames, settings.sample_rate)
            self._apply_fx_returns(buffer, fx_a, fx_b, fx_c, settings.sample_rate)
            self._apply_master_gain(buffer, settings.master_gain)
            if np.any(buffer):
                yield buffer
            else:
                yield buffer
            cursor += frames

            if event_index >= len(frame_events) and not voices:
                break
            if cursor > hard_end + settings.sample_rate * 300:
                break

    def _stream_frame_event_iter(
        self,
        context: PlaybackContext,
        frame_events,
        settings: RenderOptions,
    ):
        chunk_frames = max(settings.block_frames * 2, 1024)
        tracks: dict[int, _TrackMix] = {}
        voices: list[_Voice] = []
        cursor = 0
        pending = next(frame_events, None)

        while pending is not None or voices:
            chunk_end = cursor + chunk_frames
            buffer = np.zeros((chunk_frames, 2), dtype=np.float32)
            fx_a = np.zeros_like(buffer)
            fx_b = np.zeros_like(buffer)
            fx_c = np.zeros_like(buffer)
            local_cursor = 0

            while local_cursor < chunk_frames:
                absolute_frame = cursor + local_cursor
                while pending is not None and pending[0] <= absolute_frame:
                    event_frame, event = pending
                    self._apply_event(context, event, tracks, voices, settings, event_frame)
                    pending = next(frame_events, None)

                next_frame = chunk_end if pending is None else min(chunk_end, max(absolute_frame, pending[0]))
                frames = next_frame - absolute_frame
                if frames <= 0:
                    continue

                self._mix_into(buffer, fx_a, fx_b, fx_c, voices, tracks, local_cursor, frames, settings.sample_rate)
                local_cursor += frames

            self._apply_fx_returns(buffer, fx_a, fx_b, fx_c, settings.sample_rate)
            self._apply_master_gain(buffer, settings.master_gain)
            yield buffer
            cursor = chunk_end

    def _stream_tick_event_iter(
        self,
        context: PlaybackContext,
        frame_events,
        settings: RenderOptions,
        *,
        start_frame: int = 0,
    ):
        chunk_frames = max(settings.block_frames * 2, 1024)
        tracks: dict[int, _TrackMix] = {}
        voices: list[_Voice] = []
        cursor = max(0, int(start_frame))
        pending = next(frame_events, None)

        while pending is not None or voices:
            chunk_end = cursor + chunk_frames
            buffer = np.zeros((chunk_frames, 2), dtype=np.float32)
            fx_a = np.zeros_like(buffer)
            fx_b = np.zeros_like(buffer)
            fx_c = np.zeros_like(buffer)
            local_cursor = 0

            while local_cursor < chunk_frames:
                absolute_frame = cursor + local_cursor
                while pending is not None and pending[0] <= absolute_frame:
                    event_frame, events = pending
                    for event in events:
                        self._apply_event(
                            context,
                            event,
                            tracks,
                            voices,
                            settings,
                            event_frame,
                            current_frame=absolute_frame,
                        )
                    pending = next(frame_events, None)

                next_frame = chunk_end if pending is None else min(chunk_end, max(absolute_frame, pending[0]))
                frames = next_frame - absolute_frame
                if frames <= 0:
                    continue

                self._mix_into(buffer, fx_a, fx_b, fx_c, voices, tracks, local_cursor, frames, settings.sample_rate)
                local_cursor += frames

            self._apply_fx_returns(buffer, fx_a, fx_b, fx_c, settings.sample_rate)
            self._apply_master_gain(buffer, settings.master_gain)
            yield buffer
            cursor = chunk_end

    def _wave_frame_events(
        self,
        wave_sound,
        note_regions: list[ResolvedRegion | None],
        note_reference_frames: list[int],
    ) -> list[tuple[int, dict]]:
        if not wave_sound.notes or not note_regions or note_regions[0] is None:
            return []
        note_info = wave_sound.notes[0]
        reference_frames = note_reference_frames[0]
        frame_events: list[tuple[int, dict]] = [(0, {
            "type": "note_on",
            "track": 0,
            "note": note_info.original_key,
            "velocity": 127,
            "program": note_info.wave_index,
            "note_info": note_info,
        })]
        if reference_frames > 0:
            frame_events.append((reference_frames, {"type": "render_marker"}))
        return frame_events

    def _apply_event(
        self,
        context: PlaybackContext,
        event: dict,
        tracks: dict[int, _TrackMix],
        voices: list[_Voice],
        settings: RenderOptions,
        frame: int,
        current_frame: int | None = None,
    ) -> None:
        event_type = event.get("type")
        if event_type in ("tempo", "render_marker"):
            return

        track_no = int(event.get("track", 0))
        track = tracks.setdefault(track_no, self._default_track_mix(context))
        program_override = _clamp_program(settings.seq_program_override)
        note_override = _clamp_midi_note(settings.seq_note_override)

        if event_type == "control_change":
            cc = int(event.get("cc", -1))
            if cc == 7:
                gain = event.get("gain")
                track.volume = float(gain if gain is not None else (event.get("value", 127) / 127.0)) * context.archive_volume
            elif cc == 10:
                track.pan = ((event.get("value", 64) - 64) / 63.0) if event.get("value", 64) != 64 else 0.0
            return

        if event_type == "pitch_bend":
            track.pitch = float(event.get("semitones", 0.0))
            return

        if event_type == "program_change":
            track.program = int(event.get("program", 0))
            return

        if event_type == "track_param":
            track.priority = self._channel_priority(context) + int(event.get("priority", 64))
            track.main_send = max(0.0, min(1.0, int(event.get("main_send", 127)) / 127.0))
            track.fx_send_a = max(0.0, min(1.0, int(event.get("fx_send_a", 0)) / 127.0))
            track.fx_send_b = max(0.0, min(1.0, int(event.get("fx_send_b", 0)) / 127.0))
            track.fx_send_c = max(0.0, min(1.0, int(event.get("fx_send_c", 0)) / 127.0))
            track.lpf_scale = max(0.0, min(1.0, int(event.get("lpf_cutoff", 64)) / 64.0))
            track.biquad_type = int(event.get("biquad_type", BIQUAD_FILTER_TYPE_NONE))
            track.biquad_value = max(0.0, min(1.0, int(event.get("biquad_value", 0)) / 127.0))
            return

        if event_type == "note_off":
            note = int(note_override if note_override is not None else event.get("note", 0))
            for voice in voices:
                if voice.track_no == track_no and voice.note == note and not voice.done and not voice.released:
                    voice.start_release()
                    break
            return

        resolver = self._resolver
        assert resolver is not None
        program = int(program_override if program_override is not None else event.get("program", track.program))
        note = int(note_override if note_override is not None else event.get("note", 0))
        velocity = int(event.get("velocity", 127))
        region = resolver.resolve(program, note, velocity)
        if region is None:
            return
        note_info = event.get("note_info")
        if note_info is not None:
            region = self._apply_note_info(region, note_info)
        region = _apply_event_overrides(region, event)

        if event_type == "note_change":
            old_note = int(note_override if note_override is not None else event.get("old_note", note))
            for voice in reversed(voices):
                if voice.track_no == track_no and voice.note == old_note and not voice.done:
                    voice.note = note
                    voice.region = region
                    voice.base_ratio = midi_ratio(note, region.param.original_key, region.param.pitch)
                    voice.velocity_gain = velocity_gain(velocity)
                    voice.priority = track.priority
                    voice.release_priority_fix = self._release_priority_fix(context)
                    self._configure_voice(voice, region, settings.sample_rate, event=event, reset_phase=False, reset_envelope=False)
                    if current_frame is not None and current_frame > frame:
                        self._fast_forward_voice(voice, track, current_frame - frame, settings.sample_rate)
                    return

        alternate = int(region.param.alternate_assign)
        if alternate:
            for voice in voices:
                if voice.track_no == track_no and int(voice.region.param.alternate_assign) == alternate and not voice.done:
                    voice.start_release()

        voice = self._make_voice(track_no, note, velocity, region, track, context, settings.sample_rate, event=event)
        if self._admit_voice(voices, voice, settings.max_physical_voices):
            if current_frame is not None and current_frame > frame:
                self._fast_forward_voice(voice, track, current_frame - frame, settings.sample_rate)
            if not voice.done:
                voices.append(voice)

    @staticmethod
    def _apply_note_info(region: ResolvedRegion, note_info) -> ResolvedRegion:
        return ResolvedRegion(
            program=region.program,
            note=region.note,
            velocity=region.velocity,
            param=_note_info_to_inst_param(note_info, region.param),
            samples=region.samples,
            sample_rate=region.sample_rate,
            is_looped=region.is_looped,
            loop_start=region.loop_start,
            loop_end=region.loop_end,
        )

    @staticmethod
    def _region_reference_frames(region: ResolvedRegion, sample_rate: int) -> int:
        if region.sample_rate <= 0 or len(region.samples) == 0:
            return 0
        return max(1, round(len(region.samples) * (sample_rate / region.sample_rate)))

    @staticmethod
    def _region_playback_frames(region: ResolvedRegion, sample_rate: int, note: int) -> int:
        reference_frames = SequenceRenderer._region_reference_frames(region, sample_rate)
        if reference_frames == 0:
            return 0
        ratio = midi_ratio(note, region.param.original_key, region.param.pitch)
        return max(1, round(reference_frames / ratio))

    def _render_simple_wave_sound(
        self,
        context: PlaybackContext,
        wave_sound,
        note_regions: list[ResolvedRegion | None],
        settings: RenderOptions,
    ) -> RenderedAudio | None:
        if not wave_sound.notes or not note_regions:
            return None

        note_index = 0
        note_info = wave_sound.notes[note_index]
        region = note_regions[note_index]
        if region is None or region.is_looped or len(region.samples) == 0:
            return None

        step = (region.sample_rate / max(1, settings.sample_rate)) * midi_ratio(
            note_info.original_key,
            region.param.original_key,
            region.param.pitch,
        )
        if step <= 0.0:
            return None

        out_frames = max(1, int(np.ceil(len(region.samples) / step)))
        if abs(step - 1.0) < 1.0e-9:
            mono = region.samples.astype(np.float32, copy=True)
        else:
            positions = np.arange(out_frames, dtype=np.float64) * step
            source = np.arange(len(region.samples), dtype=np.float64)
            mono = np.interp(positions, source, region.samples).astype(np.float32)
        mono *= self._simple_envelope_gains(region, len(mono), settings.sample_rate)

        pan = self._combine_pan(0.0, region.param.pan)
        left_gain, right_gain = self._pan_gains(pan, int(getattr(context.entry, "pan_curve", 0)))
        gain = (region.param.volume / 127.0) * context.archive_volume
        main_send = max(0.0, min(1.0, int(getattr(wave_sound.info, "main_send", 127)) / 127.0))
        samples = np.column_stack((mono * gain * left_gain * main_send, mono * gain * right_gain * main_send))

        tail_frames = max(0, round(settings.tail_seconds * settings.sample_rate))
        if tail_frames:
            samples = np.pad(samples, ((0, tail_frames), (0, 0)))
        self._apply_master_gain(samples, settings.master_gain)
        return RenderedAudio(samples=samples.astype(np.float32, copy=False), sample_rate=settings.sample_rate)

    def _stream_simple_wave_sound(
        self,
        context: PlaybackContext,
        wave_sound,
        note_regions: list[ResolvedRegion | None],
        settings: RenderOptions,
        *,
        start_frame: int = 0,
    ):
        if not wave_sound.notes or not note_regions:
            return None

        note_info = wave_sound.notes[0]
        region = note_regions[0]
        if region is None or region.is_looped or len(region.samples) == 0:
            return None

        def chunks():
            step = (region.sample_rate / max(1, settings.sample_rate)) * midi_ratio(
                note_info.original_key,
                region.param.original_key,
                region.param.pitch,
            )
            if step <= 0.0:
                return
            out_frames = max(1, int(np.ceil(len(region.samples) / step)))
            chunk_frames = max(settings.block_frames * 8, 4096)
            source = np.arange(len(region.samples), dtype=np.float64)
            env = EnvelopeState()
            env.configure(
                attack=region.param.attack,
                hold=region.param.hold,
                decay=region.param.decay,
                sustain=region.param.sustain,
                release=region.param.release,
            )
            env.reset(ENV_FLOOR_DB)
            control_frames = max(1, round(AUDIO_FRAME_INTERVAL_MS * settings.sample_rate / 1000.0))
            pan = self._combine_pan(0.0, region.param.pan)
            left_gain, right_gain = self._pan_gains(pan, int(getattr(context.entry, "pan_curve", 0)))
            gain = (region.param.volume / 127.0) * context.archive_volume
            main_send = max(0.0, min(1.0, int(getattr(wave_sound.info, "main_send", 127)) / 127.0))

            cursor = max(0, min(out_frames, int(start_frame)))
            for _ in range(cursor // control_frames):
                env.update(AUDIO_FRAME_INTERVAL_MS)
            while cursor < out_frames:
                frames = min(chunk_frames, out_frames - cursor)
                positions = (np.arange(cursor, cursor + frames, dtype=np.float64) * step)
                mono = np.interp(positions, source, region.samples).astype(np.float32)
                gains = np.empty(frames, dtype=np.float32)
                gain_cursor = 0
                while gain_cursor < frames:
                    gain_end = min(frames, gain_cursor + control_frames)
                    gains[gain_cursor:gain_end] = env.current_gain()
                    env.update(AUDIO_FRAME_INTERVAL_MS)
                    gain_cursor = gain_end
                mono *= gains
                samples = np.column_stack((mono * gain * left_gain * main_send, mono * gain * right_gain * main_send))
                self._apply_master_gain(samples, settings.master_gain)
                yield samples.astype(np.float32, copy=False)
                cursor += frames

        return chunks()

    @staticmethod
    def _simple_envelope_gains(region: ResolvedRegion, frames: int, sample_rate: int) -> np.ndarray:
        env = EnvelopeState()
        env.configure(
            attack=region.param.attack,
            hold=region.param.hold,
            decay=region.param.decay,
            sustain=region.param.sustain,
            release=region.param.release,
        )
        env.reset(ENV_FLOOR_DB)
        block_frames = max(1, round(AUDIO_FRAME_INTERVAL_MS * sample_rate / 1000.0))
        gains = np.empty(frames, dtype=np.float32)
        cursor = 0
        while cursor < frames:
            end = min(frames, cursor + block_frames)
            gains[cursor:end] = env.current_gain()
            env.update(AUDIO_FRAME_INTERVAL_MS)
            cursor = end
        return gains

    @staticmethod
    def _has_simple_preview_envelope(region: ResolvedRegion) -> bool:
        param = region.param
        return (
            int(param.attack) in (INVALID_ENVELOPE, 127)
            and int(param.decay) in (INVALID_ENVELOPE, 127)
            and int(param.sustain) in (INVALID_ENVELOPE, 127)
            and int(param.hold) in (INVALID_ENVELOPE, 0, 127)
        )

    @staticmethod
    def _chunk_audio(samples: np.ndarray, chunk_frames: int):
        for cursor in range(0, len(samples), chunk_frames):
            yield samples[cursor:cursor + chunk_frames]

    @staticmethod
    def _wsd_value_to_frames(value: float, reference_frames: int) -> int:
        if reference_frames > 0 and 0.0 <= value <= 1.0:
            return max(0, round(value * reference_frames))
        return max(0, round(value))

    @staticmethod
    def _channel_priority(context: PlaybackContext) -> int:
        sound_info = getattr(context.entry, "sound_info", None)
        return int(getattr(sound_info, "channel_priority", 64))

    @staticmethod
    def _release_priority_fix(context: PlaybackContext) -> bool:
        sound_info = getattr(context.entry, "sound_info", None)
        return bool(getattr(sound_info, "release_priority_fix", 0))

    def _default_track_mix(self, context: PlaybackContext) -> _TrackMix:
        return _TrackMix(
            volume=context.archive_volume,
            priority=self._channel_priority(context) + 64,
            main_send=1.0,
            lpf_scale=1.0,
            pan_curve=int(getattr(context.entry, "pan_curve", 0)),
        )

    @staticmethod
    def _admit_voice(voices: list[_Voice], voice: _Voice, limit: int) -> bool:
        limit = max(1, int(limit or AX_MAX_VOICES))
        current = sum(active.physical_voice_count for active in voices if not active.done)
        required = max(1, voice.physical_voice_count)
        if current + required <= limit:
            return True
        candidates = sorted(
            (active for active in voices if not active.done),
            key=lambda item: (item.priority, -item.age_frames),
        )
        remaining = current
        for candidate in candidates:
            if candidate.priority > voice.priority:
                return False
            candidate.done = True
            remaining -= candidate.physical_voice_count
            if remaining + required <= limit:
                return True
        return remaining + required <= limit

    def _make_voice(
        self,
        track_no: int,
        note: int,
        velocity: int,
        region: ResolvedRegion,
        track: _TrackMix,
        context: PlaybackContext,
        sample_rate: int,
        event: dict | None = None,
    ) -> _Voice:
        voice = _Voice(
            track_no=track_no,
            note=note,
            region=region,
            base_ratio=midi_ratio(note, region.param.original_key, region.param.pitch),
            velocity_gain=velocity_gain(velocity),
            priority=track.priority,
            release_priority_fix=self._release_priority_fix(context),
        )
        self._configure_voice(voice, region, sample_rate, event=event)
        return voice

    def _configure_voice(
        self,
        voice: _Voice,
        region: ResolvedRegion,
        sample_rate: int,
        event: dict | None = None,
        *,
        reset_phase: bool = True,
        reset_envelope: bool = True,
    ) -> None:
        voice.region = region
        voice.ignore_note_off = note_off_ignores_release(region.param, region.is_looped)
        voice.physical_voice_count = 1
        voice.env.configure(
            attack=region.param.attack,
            hold=region.param.hold,
            decay=region.param.decay,
            sustain=region.param.sustain,
            release=region.param.release,
            reset_hold=reset_envelope,
        )
        if reset_envelope:
            if voice.released:
                voice.env.note_off()
            else:
                voice.env.reset(ENV_FLOOR_DB)
            voice.control_progress_ms = 0.0
        depth = max(0.0, min(1.0, int(event.get("mod_depth", 0)) / 128.0)) if event is not None else 0.0
        speed = max(0.0, int(event.get("mod_speed", 16)) * 0.390625) if event is not None else 6.25
        delay = max(0, int(event.get("mod_delay", 0)) * 5) if event is not None else 0
        lfo_range = max(0, int(event.get("mod_range", 1))) if event is not None else 1
        voice.lfo_target = int(event.get("mod_type", 0)) if event is not None else 0
        voice.lfo.configure(depth=depth, range=lfo_range, speed=speed, delay=delay, reset_phase=reset_phase)
        if event is not None:
            voice.sweep_pitch = float(event.get("sweep_pitch", 0.0) or 0.0)
            sweep_seconds = max(0.0, float(event.get("sweep_seconds", 0.0) or 0.0))
            voice.sweep_frames = max(0, round(sweep_seconds * sample_rate))
        else:
            voice.sweep_pitch = 0.0
            voice.sweep_frames = 0
        voice.sweep_age_frames = 0

    def _mix_into(
        self,
        buffer: np.ndarray,
        fx_a: np.ndarray,
        fx_b: np.ndarray,
        fx_c: np.ndarray,
        voices: list[_Voice],
        tracks: dict[int, _TrackMix],
        start: int,
        frames: int,
        sample_rate: int,
    ) -> None:
        end = min(len(buffer), start + frames)
        if end <= start:
            return

        for voice in list(voices):
            if voice.done:
                continue
            track = tracks.get(voice.track_no, _TrackMix())
            dry, send_a, send_b, send_c = self._render_voice(voice, track, end - start, sample_rate=sample_rate)
            buffer[start:end, :] += dry
            fx_a[start:end, :] += send_a
            fx_b[start:end, :] += send_b
            fx_c[start:end, :] += send_c
        voices[:] = [voice for voice in voices if not voice.done]

    def _fast_forward_voice(self, voice: _Voice, track: _TrackMix, frames: int, sample_rate: int) -> None:
        frames = max(0, int(frames))
        if frames <= 0 or voice.done:
            return

        pitch_offset = track.pitch + voice.sweep_value()
        pitch_ratio = calc_pitch_ratio(round(pitch_offset * 256.0))
        step = (voice.region.sample_rate / max(1, sample_rate)) * voice.base_ratio * pitch_ratio
        if step <= 0.0:
            return

        samples = voice.region.samples
        if len(samples) == 0:
            voice.done = True
            return

        voice.position += frames * step
        voice.age_frames += frames
        if voice.sweep_age_frames < voice.sweep_frames:
            voice.sweep_age_frames = min(voice.sweep_frames, voice.sweep_age_frames + frames)

        # A one-shot voice that has run past the end of its sample is already
        # finished, so settle it *before* touching the envelope.  Seeking skips
        # over thousands of long-dead notes, and stepping each one's envelope
        # across the whole skipped span is what made seeking take seconds.
        looping = voice.region.is_looped and not voice.released
        if not looping and voice.position >= len(samples) - 1:
            voice.done = True
            return

        voice.control_progress_ms += frames * 1000.0 / max(1, sample_rate)
        steps = int(voice.control_progress_ms // AUDIO_FRAME_INTERVAL_MS)
        if steps <= 0:
            return
        voice.control_progress_ms -= steps * AUDIO_FRAME_INTERVAL_MS

        # Attack is the only envelope stage whose update() can complete partway
        # through a span and discard the remainder, so it has to be stepped at
        # the real control rate.  Hold/decay/sustain/release all consume the
        # whole span linearly, so once attack is done the rest of the skip
        # collapses into a single update - which is what makes seeking past a
        # long sequence cheap instead of one iteration per 3ms skipped.
        while steps > 0 and voice.env.status == "attack":
            voice.lfo.update(AUDIO_FRAME_INTERVAL_MS)
            voice.env.update(AUDIO_FRAME_INTERVAL_MS)
            steps -= 1
            if voice.released and voice.env.current_gain() == 0.0:
                voice.done = True
                return

        if steps > 0:
            remaining_ms = steps * AUDIO_FRAME_INTERVAL_MS
            voice.lfo.update(remaining_ms)
            voice.env.update(remaining_ms)
            if voice.released and voice.env.current_gain() == 0.0:
                voice.done = True
                return

        if looping:
            loop_start = max(0, min(voice.region.loop_start, len(samples) - 1))
            loop_end = max(loop_start + 1, min(voice.region.loop_end, len(samples)))
            loop_len = max(1, loop_end - loop_start)
            if voice.position >= loop_end:
                voice.position = loop_start + ((voice.position - loop_start) % loop_len)

    def _render_voice(self, voice: _Voice, track: _TrackMix, frames: int, *, sample_rate: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        out = np.zeros((frames, 2), dtype=np.float32)
        send_a = np.zeros((frames, 2), dtype=np.float32)
        send_b = np.zeros((frames, 2), dtype=np.float32)
        send_c = np.zeros((frames, 2), dtype=np.float32)
        samples = voice.region.samples
        if len(samples) == 0:
            voice.done = True
            return out, send_a, send_b, send_c

        base_gain = (voice.region.param.volume / 127.0) * voice.velocity_gain * track.volume
        lpf = lpf_alpha(track.lpf_scale, sample_rate)
        biquad = biquad_coefficients(track.biquad_type, track.biquad_value, sample_rate)

        loop_start = max(0, min(voice.region.loop_start, len(samples) - 1))
        loop_end = max(loop_start + 1, min(voice.region.loop_end, len(samples)))

        # Pack mutable voice state into a float64 array for the numba kernel
        vstate = np.empty(_VS_SIZE, dtype=np.float64)
        vstate[_VS_POS] = voice.position
        vstate[_VS_AGE] = float(voice.age_frames)
        vstate[_VS_DONE] = 1.0 if voice.done else 0.0
        vstate[_VS_RELEASED] = 1.0 if voice.released else 0.0
        vstate[_VS_CTRL_MS] = voice.control_progress_ms
        vstate[_VS_SW_AGE] = float(voice.sweep_age_frames)
        vstate[_VS_SW_PITCH] = voice.sweep_pitch
        vstate[_VS_SW_FRAMES] = float(voice.sweep_frames)
        vstate[_VS_LFO_CTR] = voice.lfo.counter
        vstate[_VS_LFO_DCTR] = float(voice.lfo.delay_counter)
        vstate[_VS_LFO_DELAY] = float(voice.lfo.delay)
        vstate[_VS_LFO_DEPTH] = voice.lfo.depth
        vstate[_VS_LFO_RANGE] = float(voice.lfo.range)
        vstate[_VS_LFO_SPEED] = voice.lfo.speed
        vstate[_VS_ENV_ST] = float(_ENV_STATUS_TO_INT.get(voice.env.status, 0))
        vstate[_VS_ENV_VAL] = voice.env.value_tenths_db
        vstate[_VS_ENV_ATK] = voice.env.attack
        vstate[_VS_ENV_HCTR] = float(voice.env.hold_counter)
        vstate[_VS_ENV_DECAY] = voice.env.decay
        vstate[_VS_ENV_SUS] = voice.env.sustain_tenths_db
        vstate[_VS_ENV_REL] = voice.env.release
        vstate[_VS_ENV_HOLD] = float(voice.env.hold)

        has_lpf = lpf is not None
        lpf_val = float(lpf) if has_lpf else 0.0
        has_bq = biquad is not None
        bq_b0 = biquad[0] if has_bq else 0.0
        bq_b1 = biquad[1] if has_bq else 0.0
        bq_b2 = biquad[2] if has_bq else 0.0
        bq_a1 = biquad[3] if has_bq else 0.0
        bq_a2 = biquad[4] if has_bq else 0.0

        _render_voice_loop(
            out, send_a, send_b, send_c,
            samples, vstate,
            voice.lpf_state, voice.biquad_x1, voice.biquad_x2, voice.biquad_y1, voice.biquad_y2,
            base_gain, voice.base_ratio,
            float(voice.region.sample_rate), float(sample_rate),
            track.pitch, track.pan, int(track.pan_curve),
            track.main_send, track.fx_send_a, track.fx_send_b, track.fx_send_c,
            int(voice.lfo_target), int(voice.region.param.pan),
            lpf_val, has_lpf,
            bq_b0, bq_b1, bq_b2, bq_a1, bq_a2, has_bq,
            voice.region.is_looped, loop_start, loop_end,
            self._AX_INTERPOLATION_TENSION,
            _NOTE_TABLE_NB, _PITCH_TABLE_NB, _LFO_SIN_TABLE_NB,
        )

        # Unpack state back to the voice object.
        voice.position = vstate[_VS_POS]
        voice.age_frames = int(vstate[_VS_AGE])
        voice.done = vstate[_VS_DONE] > 0.5
        voice.control_progress_ms = vstate[_VS_CTRL_MS]
        voice.sweep_age_frames = int(vstate[_VS_SW_AGE])
        voice.lfo.counter = vstate[_VS_LFO_CTR]
        voice.lfo.delay_counter = int(vstate[_VS_LFO_DCTR])
        env_st_idx = int(vstate[_VS_ENV_ST])
        voice.env.status = _ENV_STATUS_TO_STR[env_st_idx] if 0 <= env_st_idx < len(_ENV_STATUS_TO_STR) else "attack"
        voice.env.value_tenths_db = vstate[_VS_ENV_VAL]
        voice.env.hold_counter = int(vstate[_VS_ENV_HCTR])

        return out, send_a, send_b, send_c

    @staticmethod
    def _apply_fx_returns(buffer: np.ndarray, fx_a: np.ndarray, fx_b: np.ndarray, fx_c: np.ndarray, sample_rate: int) -> None:
        if buffer.size == 0:
            return
        if np.any(fx_a):
            buffer += SequenceRenderer._reverb_return(
                fx_a,
                sample_rate,
                delays_ms=(17, 29, 41),
                gains=(0.20, 0.14, 0.10),
                lpf_alpha=0.28,
            )
        if np.any(fx_b):
            buffer += SequenceRenderer._reverb_return(
                fx_b,
                sample_rate,
                delays_ms=(31, 47, 73, 109),
                gains=(0.22, 0.16, 0.12, 0.08),
                lpf_alpha=0.22,
            )
        if np.any(fx_c):
            buffer += SequenceRenderer._reverb_return(
                fx_c,
                sample_rate,
                delays_ms=(43, 67, 97, 149, 211),
                gains=(0.24, 0.176, 0.128, 0.088, 0.064),
                lpf_alpha=0.10,
            )

    @staticmethod
    def _reverb_return(
        source: np.ndarray,
        sample_rate: int,
        *,
        delays_ms: tuple[int, ...],
        gains: tuple[float, ...],
        lpf_alpha: float,
    ) -> np.ndarray:
        wet = np.zeros_like(source)
        for index, (delay_ms, gain) in enumerate(zip(delays_ms, gains, strict=False)):
            delay = max(1, round(sample_rate * delay_ms / 1000.0))
            if delay >= len(source):
                continue
            delayed = source[:-delay]
            if index % 2:
                wet[delay:, 0] += delayed[:, 1] * gain
                wet[delay:, 1] += delayed[:, 0] * gain
            else:
                wet[delay:, :] += delayed * gain

        if lpf_alpha <= 0.0:
            return wet
        state = np.zeros(2, dtype=np.float32)
        for i in range(len(wet)):
            state += (wet[i] - state) * lpf_alpha
            wet[i] = state
        return wet

    @staticmethod
    def _sample_at(samples: np.ndarray, position: float) -> float:
        if position <= 0:
            return float(samples[0])
        if position >= len(samples) - 1:
            return float(samples[-1])
        base = int(position)
        frac = position - base
        y0 = float(samples[max(0, base - 1)])
        y1 = float(samples[base])
        y2 = float(samples[min(len(samples) - 1, base + 1)])
        y3 = float(samples[min(len(samples) - 1, base + 2)])

        # AX uses a 4-tap polyphase SRC. A sharper cubic-convolution kernel is
        # a noticeably closer approximation for transposed one-shot SFX than the
        # softer Catmull-Rom kernel we used before.
        a = SequenceRenderer._AX_INTERPOLATION_TENSION
        c0 = (-a * y0) + ((2.0 - a) * y1) + ((a - 2.0) * y2) + (a * y3)
        c1 = (2.0 * a * y0) + ((a - 3.0) * y1) + ((3.0 - 2.0 * a) * y2) - (a * y3)
        c2 = (-a * y0) + (a * y2)
        c3 = y1
        return ((c0 * frac + c1) * frac + c2) * frac + c3

    @staticmethod
    def _combine_pan(track_pan: float, note_pan: int) -> float:
        note = 0.0 if note_pan == 64 else (note_pan - 64) / 63.0
        return max(-1.0, min(1.0, track_pan + note))

    @staticmethod
    def _pan_gains(pan: float, pan_curve: int) -> tuple[float, float]:
        left = calc_pan_ratio(-pan, pan_curve)
        right = calc_pan_ratio(pan, pan_curve)
        return float(left), float(right)

    @staticmethod
    def _apply_filters(
        voice: _Voice,
        left: float,
        right: float,
        lpf: float | None,
        biquad: tuple[float, float, float, float, float] | None,
    ) -> tuple[float, float]:
        values = np.array([left, right], dtype=np.float32)
        if lpf is not None:
            voice.lpf_state += (values - voice.lpf_state) * lpf
            values = voice.lpf_state.copy()
        if biquad is not None:
            b0, b1, b2, a1, a2 = biquad
            output = (
                (b0 * values)
                + (b1 * voice.biquad_x1)
                + (b2 * voice.biquad_x2)
                - (a1 * voice.biquad_y1)
                - (a2 * voice.biquad_y2)
            ).astype(np.float32)
            voice.biquad_x2 = voice.biquad_x1.copy()
            voice.biquad_x1 = values.copy()
            voice.biquad_y2 = voice.biquad_y1.copy()
            voice.biquad_y1 = output.copy()
            values = output
        return float(values[0]), float(values[1])

    @staticmethod
    def _grow_buffer(buffer: np.ndarray, target: int) -> np.ndarray:
        grown = np.zeros((target + 2048, 2), dtype=np.float32)
        grown[: len(buffer)] = buffer
        return grown

    @staticmethod
    def _apply_master_gain(buffer: np.ndarray, gain: float) -> None:
        if buffer.size == 0:
            return
        buffer *= gain
        peak = np.max(np.abs(buffer))
        if peak > 0.999:
            buffer *= 0.999 / peak

    @staticmethod
    def _resample_stereo(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
        if samples.shape[0] == 0 or src_rate == dst_rate:
            return samples
        ratio = dst_rate / src_rate
        new_len = max(1, round(samples.shape[0] * ratio))
        src_pos = np.linspace(0.0, samples.shape[0] - 1, new_len, dtype=np.float64)
        left = np.interp(src_pos, np.arange(samples.shape[0]), samples[:, 0])
        right = np.interp(src_pos, np.arange(samples.shape[0]), samples[:, 1])
        return np.column_stack((left, right)).astype(np.float32)


def _note_info_to_inst_param(note_info, base_param):
    base_param = replace(base_param)
    base_param.attack = note_info.attack
    base_param.decay = note_info.decay
    base_param.sustain = note_info.sustain
    base_param.release = note_info.release
    base_param.hold = note_info.hold
    base_param.original_key = note_info.original_key
    base_param.volume = note_info.volume
    base_param.pan = note_info.pan
    base_param.pitch = note_info.pitch
    return base_param


class _DirectBankAdapter:
    def get_inst_param(self, program: int, key: int = 60, velocity: int = 127):
        from pysar.core.model.brbnk import InstParam

        return InstParam(wave_index=program)


def _apply_track_overrides(region: ResolvedRegion, track) -> ResolvedRegion:
    param = _apply_track_param_overrides(region.param, track)
    if param is region.param:
        return region
    return replace(region, param=param)


def _apply_track_param_overrides(param, track):
    original = param
    param = replace(param)
    changed = False
    if getattr(track, "attack", INVALID_ENVELOPE) != INVALID_ENVELOPE:
        param.attack = int(track.attack)
        changed = True
    if getattr(track, "decay", INVALID_ENVELOPE) != INVALID_ENVELOPE:
        param.decay = int(track.decay)
        changed = True
    if getattr(track, "sustain", INVALID_ENVELOPE) != INVALID_ENVELOPE:
        param.sustain = int(track.sustain)
        changed = True
    if getattr(track, "release", INVALID_ENVELOPE) != INVALID_ENVELOPE:
        param.release = int(track.release)
        changed = True
    if getattr(track, "hold", INVALID_ENVELOPE) != INVALID_ENVELOPE:
        param.hold = int(track.hold)
        changed = True
    return param if changed else original


def _apply_event_overrides(region: ResolvedRegion, event: dict) -> ResolvedRegion:
    param = replace(region.param)
    changed = False
    for key in ("attack", "decay", "sustain", "release", "hold"):
        value = int(event.get(key, INVALID_ENVELOPE))
        if value != INVALID_ENVELOPE:
            setattr(param, key, value)
            changed = True
    if not changed:
        return region
    return replace(region, param=param)


def _clamp_midi_note(value: int | None) -> int | None:
    if value is None:
        return None
    return max(0, min(127, int(value)))


def _clamp_program(value: int | None) -> int | None:
    if value is None:
        return None
    return max(0, int(value))


def _db_to_ratio(db: float) -> float:
    return _nw_db_to_ratio(db)
