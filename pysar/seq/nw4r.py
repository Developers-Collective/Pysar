from dataclasses import dataclass
import math


# These constants are a practical reconstruction of
# NW4R/AX behavior based on reverse-engineered runtime behavior and
# older project experiments.
# So some values are engine-like constants, some are measured or
# reconstructed tables, and some are ordinary just math.

# Generally speaking, this is a (hopefully) pretty good approximation
# of the engine behavior, but there are known but miniscule differences.

AUDIO_FRAME_INTERVAL_MS = 3
AX_MAX_VOICES = 96
ENV_FLOOR_DB = -90.4
ENV_FLOOR_DB_TENTHS = ENV_FLOOR_DB * 10.0
PAN_MODE_DUAL = 0
PAN_MODE_BALANCE = 1
PAN_CURVE_SQRT = 0
PAN_CURVE_SQRT_0DB = 1
PAN_CURVE_SQRT_0DB_CLAMP = 2
PAN_CURVE_SINCOS = 3
PAN_CURVE_SINCOS_0DB = 4
PAN_CURVE_SINCOS_0DB_CLAMP = 5
PAN_CURVE_LINEAR = 6
PAN_CURVE_LINEAR_0DB = 7
PAN_CURVE_LINEAR_0DB_CLAMP = 8
BIQUAD_FILTER_TYPE_NONE = 0
BIQUAD_FILTER_TYPE_LPF = 1
BIQUAD_FILTER_TYPE_HPF = 2
BIQUAD_FILTER_TYPE_BPF512 = 3
BIQUAD_FILTER_TYPE_BPF1024 = 4
BIQUAD_FILTER_TYPE_BPF2048 = 5
LFO_TARGET_PITCH = 0
LFO_TARGET_VOLUME = 1
LFO_TARGET_PAN = 2
PITCH_DIVISION_BIT = 8
PITCH_DIVISION_RANGE = 1 << PITCH_DIVISION_BIT

# Equal-temperament pitch helpers. These are derived mathematically.
_NOTE_TABLE = tuple(2.0 ** (index / 12.0) for index in range(12))
_PITCH_TABLE = tuple(2.0 ** (index / (12.0 * PITCH_DIVISION_RANGE)) for index in range(PITCH_DIVISION_RANGE))

# Attack progression table reconstructed to match the envelope curve used by
# NW4R closely enough for playback and rendering.
_ENV_ATTACK_TABLE = (
    0.9992175, 0.9984326, 0.9976452, 0.9968553, 0.9960629, 0.9952679, 0.9944704, 0.9936704,
    0.9928677, 0.9920625, 0.9912546, 0.9904441, 0.9896309, 0.9888151, 0.9879965, 0.9871752,
    0.9863512, 0.9855244, 0.9846949, 0.9838625, 0.9830273, 0.9821893, 0.9813483, 0.9805045,
    0.9796578, 0.9788081, 0.9779555, 0.9770999, 0.9762413, 0.9753797, 0.9745150, 0.9736472,
    0.9727763, 0.9719023, 0.9710251, 0.9701448, 0.9692612, 0.9683744, 0.9674844, 0.9665910,
    0.9656944, 0.9647944, 0.9638910, 0.9629842, 0.9620740, 0.9611604, 0.9602433, 0.9593226,
    0.9583984, 0.9574706, 0.9565392, 0.9556042, 0.9546655, 0.9537231, 0.9527769, 0.9518270,
    0.9508732, 0.9499157, 0.9489542, 0.9479888, 0.9470195, 0.9460462, 0.9450689, 0.9440875,
    0.9431020, 0.9421124, 0.9411186, 0.9401206, 0.9391184, 0.9381118, 0.9371009, 0.9360856,
    0.9350659, 0.9340417, 0.9330131, 0.9319798, 0.9309420, 0.9298995, 0.9288523, 0.9278004,
    0.9267436, 0.9256821, 0.9246156, 0.9235442, 0.9224678, 0.9213864, 0.9202998, 0.9192081,
    0.9181112, 0.9170091, 0.9159016, 0.9147887, 0.9136703, 0.9125465, 0.9114171, 0.9102821,
    0.9091414, 0.9079949, 0.9068427, 0.9056845, 0.9045204, 0.9033502, 0.9021740, 0.9009916,
    0.8998029, 0.8986080, 0.8974066, 0.8961988, 0.8949844, 0.8900599, 0.8824622, 0.8759247,
    0.8691861, 0.8636406, 0.8535788, 0.8430189, 0.8286135, 0.8149099, 0.8002172, 0.7780663,
    0.7554750, 0.7242125, 0.6828239, 0.6329169, 0.5592135, 0.4551411, 0.3298770, 0.0,
)

# Sustain values are handled in tenths of dB in the original engine, so this is
# the practical lookup we use for the same square-law style response.
_ENV_DECIBEL_SQUARE_TABLE = (
    -723, -722, -721, -651, -601, -562, -530, -503,
    -480, -460, -442, -425, -410, -396, -383, -371,
    -360, -349, -339, -330, -321, -313, -305, -297,
    -289, -282, -276, -269, -263, -257, -251, -245,
    -239, -234, -229, -224, -219, -214, -210, -205,
    -201, -196, -192, -188, -184, -180, -176, -173,
    -169, -165, -162, -158, -155, -152, -149, -145,
    -142, -139, -136, -133, -130, -127, -125, -122,
    -119, -116, -114, -111, -109, -106, -103, -101,
    -99, -96, -94, -91, -89, -87, -85, -82,
    -80, -78, -76, -74, -72, -70, -68, -66,
    -64, -62, -60, -58, -56, -54, -52, -50,
    -49, -47, -45, -43, -42, -40, -38, -36,
    -35, -33, -31, -30, -28, -27, -25, -23,
    -22, -20, -19, -17, -16, -14, -13, -11,
    -10, -8, -7, -6, -4, -3, -1, 0,
)

# Small quarter-wave sine table used by the runtime-style LFO.
_LFO_SIN_TABLE = (
    0, 6, 12, 19, 25, 31, 37, 43,
    49, 54, 60, 65, 71, 76, 81, 85,
    90, 94, 98, 102, 106, 109, 112, 115,
    117, 120, 122, 123, 125, 126, 126, 127,
    127,
)

# Low-pass cutoff lookup. The table itself is reconstructed, the interpolation
# threshold below is a project-side fit so the control value lands in the right
# part of the curve.
_LPF_FREQ_TABLE = (
    80, 100, 128, 160, 200, 256, 320, 400,
    500, 640, 800, 1000, 1280, 1600, 2000, 2560,
    3200, 4000, 5120, 6400, 8000, 10240, 12800, 16000,
)
_CALC_LPF_FREQ_INTERCEPT = 0.135614381
_NW4R_RANDOM_SEED = 0x12345678


@dataclass(slots=True)
class Nw4rRandomState:
    state: int = _NW4R_RANDOM_SEED

    def calc_random(self) -> int:
        self.state = ((self.state * 1664525) + 1013904223) & 0xFFFFFFFF
        return (self.state >> 16) & 0xFFFF

    def randint(self, minimum: int, maximum: int) -> int:
        minimum = int(minimum)
        maximum = int(maximum)
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        value = self.calc_random()
        value *= (maximum - minimum) + 1
        value >>= 16
        return value + minimum


def calc_pan_ratio(pan: float, curve: int) -> float:
    pan = (max(-1.0, min(1.0, float(pan))) + 1.0) * 0.5
    center_zero = curve in {
        PAN_CURVE_SQRT_0DB,
        PAN_CURVE_SQRT_0DB_CLAMP,
        PAN_CURVE_SINCOS_0DB,
        PAN_CURVE_SINCOS_0DB_CLAMP,
        PAN_CURVE_LINEAR_0DB,
        PAN_CURVE_LINEAR_0DB_CLAMP,
    }
    zero_clamp = curve in {
        PAN_CURVE_SQRT_0DB_CLAMP,
        PAN_CURVE_SINCOS_0DB_CLAMP,
        PAN_CURVE_LINEAR_0DB_CLAMP,
    }
    if curve in {PAN_CURVE_LINEAR, PAN_CURVE_LINEAR_0DB, PAN_CURVE_LINEAR_0DB_CLAMP}:
        ratio = pan
        center = 0.5
    elif curve in {PAN_CURVE_SINCOS, PAN_CURVE_SINCOS_0DB, PAN_CURVE_SINCOS_0DB_CLAMP}:
        ratio = math.sin(pan * (math.pi / 2.0))
        center = math.sin(math.pi / 4.0)
    else:
        ratio = math.sqrt(pan)
        center = math.sqrt(0.5)
    if center_zero and center > 0.0:
        ratio /= center
    upper = 1.0 if zero_clamp else 2.0
    return max(0.0, min(upper, ratio))


def calc_lpf_freq(scale: float) -> int:
    scale = max(0.0, min(1.0, float(scale)))
    if scale < _CALC_LPF_FREQ_INTERCEPT:
        return _LPF_FREQ_TABLE[0]
    if scale >= 0.9:
        return _LPF_FREQ_TABLE[-1]
    index = int((scale - _CALC_LPF_FREQ_INTERCEPT) / (0.1 / 3.0))
    index = max(0, min(len(_LPF_FREQ_TABLE) - 1, index))
    return _LPF_FREQ_TABLE[index]


def calc_pitch_ratio(pitch: int) -> float:
    pitch = int(pitch)
    octave = 0
    octave_ratio = 1.0
    octave_span = PITCH_DIVISION_RANGE * 12
    while pitch < 0:
        octave -= 1
        pitch += octave_span
    while pitch >= octave_span:
        octave += 1
        pitch -= octave_span
    if octave > 0:
        octave_ratio *= 2.0 ** octave
    elif octave < 0:
        octave_ratio /= 2.0 ** (-octave)
    note = pitch // PITCH_DIVISION_RANGE
    fine = pitch % PITCH_DIVISION_RANGE
    ratio = octave_ratio
    if note:
        ratio *= _NOTE_TABLE[note]
    if fine:
        ratio *= _PITCH_TABLE[fine]
    return ratio


def lpf_alpha(scale: float, sample_rate: int) -> float | None:
    cutoff = calc_lpf_freq(scale)
    if cutoff >= 16000 or sample_rate <= 0:
        return None
    decay = math.exp((-2.0 * math.pi * cutoff) / sample_rate)
    return 1.0 - decay


def biquad_coefficients(filter_type: int, value: float, sample_rate: int) -> tuple[float, float, float, float, float] | None:
    filter_type = int(filter_type)
    if filter_type == BIQUAD_FILTER_TYPE_NONE or sample_rate <= 0:
        return None
    value = max(0.0, min(1.0, float(value)))
    if filter_type == BIQUAD_FILTER_TYPE_LPF:
        freq = calc_lpf_freq(max(value, _CALC_LPF_FREQ_INTERCEPT))
        q = 0.7071067811865476
    elif filter_type == BIQUAD_FILTER_TYPE_HPF:
        freq = calc_lpf_freq(max(value, _CALC_LPF_FREQ_INTERCEPT))
        q = 0.7071067811865476
    elif filter_type == BIQUAD_FILTER_TYPE_BPF512:
        freq = 512.0
        q = 0.5 + (value * (2.0 - value)) * 7.5
    elif filter_type == BIQUAD_FILTER_TYPE_BPF1024:
        freq = 1024.0
        q = 0.5 + (value * (2.0 - value)) * 7.5
    elif filter_type == BIQUAD_FILTER_TYPE_BPF2048:
        freq = 2048.0
        q = 0.5 + (value * (2.0 - value)) * 7.5
    else:
        return None
    nyquist = sample_rate * 0.5
    freq = max(10.0, min(nyquist * 0.95, float(freq)))
    omega = 2.0 * math.pi * freq / sample_rate
    sin_omega = math.sin(omega)
    cos_omega = math.cos(omega)
    alpha = sin_omega / (2.0 * max(1.0e-6, q))
    if filter_type == BIQUAD_FILTER_TYPE_LPF:
        b0 = (1.0 - cos_omega) * 0.5
        b1 = 1.0 - cos_omega
        b2 = b0
    elif filter_type == BIQUAD_FILTER_TYPE_HPF:
        b0 = (1.0 + cos_omega) * 0.5
        b1 = -(1.0 + cos_omega)
        b2 = b0
    else:
        b0 = alpha
        b1 = 0.0
        b2 = -alpha
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_omega
    a2 = 1.0 - alpha
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def attack_coefficient(value: int) -> float:
    return float(_ENV_ATTACK_TABLE[_clamp_7bit(value)])


def hold_ms(value: int) -> int:
    value = _clamp_7bit(value)
    return int(((value + 1) * (value + 1)) / 4)


def release_rate(value: int) -> float:
    value = _clamp_7bit(value)
    if value == 127:
        return 65535.0
    if value == 126:
        return 24.0
    if value < 50:
        return (((value << 1) + 1) / 128.0) / 5.0
    return (60.0 / (126 - value)) / 5.0


def sustain_db(value: int) -> float:
    return _ENV_DECIBEL_SQUARE_TABLE[_clamp_7bit(value)] / 10.0


def db_to_ratio(db: float) -> float:
    if db <= ENV_FLOOR_DB:
        return 0.0
    return float(10.0 ** (db / 20.0))


def velocity_gain(value: int) -> float:
    velocity = _clamp_7bit(value) / 127.0
    return float(velocity * velocity)


@dataclass(slots=True)
class EnvelopeState:
    status: str = "attack"
    value_tenths_db: float = ENV_FLOOR_DB_TENTHS
    attack: float = 0.0
    hold: int = 0
    hold_counter: int = 0
    decay: float = 0.0
    sustain_tenths_db: float = 0.0
    release: float = 0.0

    def reset(self, init_db: float = ENV_FLOOR_DB) -> None:
        self.value_tenths_db = init_db * 10.0
        self.status = "attack"
        self.hold_counter = self.hold

    def configure(
        self,
        *,
        attack: int,
        hold: int,
        decay: int,
        sustain: int,
        release: int,
        reset_hold: bool = True,
    ) -> None:
        self.attack = attack_coefficient(attack)
        self.hold = hold_ms(hold)
        if reset_hold:
            self.hold_counter = self.hold
        self.decay = release_rate(decay)
        self.sustain_tenths_db = sustain_db(sustain) * 10.0
        self.release = release_rate(release)

    def note_off(self) -> None:
        self.status = "release"

    def current_gain(self) -> float:
        if self.status == "attack" and self.attack == 0.0:
            return 1.0
        return db_to_ratio(self.value_tenths_db / 10.0)

    def update(self, msec: int) -> None:
        if msec <= 0:
            return
        if self.status == "attack":
            while msec > 0:
                self.value_tenths_db *= self.attack
                msec -= 1
                if self.value_tenths_db > -(1.0 / 32.0):
                    self.value_tenths_db = 0.0
                    self.status = "hold"
                    self.hold_counter = self.hold
                    break
            return
        if self.status == "hold":
            if msec < self.hold_counter:
                self.hold_counter -= msec
                return
            msec -= self.hold_counter
            self.hold_counter = 0
            self.status = "decay"
        if self.status == "decay":
            self.value_tenths_db -= self.decay * msec
            if self.value_tenths_db < self.sustain_tenths_db:
                self.value_tenths_db = self.sustain_tenths_db
                self.status = "sustain"
            return
        if self.status == "sustain":
            return
        if self.status == "release":
            self.value_tenths_db -= self.release * msec


@dataclass(slots=True)
class LfoState:
    depth: float = 0.0
    range: int = 1
    speed: float = 6.25
    delay: int = 0
    counter: float = 0.0
    delay_counter: int = 0

    def reset(self) -> None:
        self.counter = 0.0
        self.delay_counter = 0

    def configure(self, *, depth: float, range: int, speed: float, delay: int, reset_phase: bool = True) -> None:
        self.depth = max(0.0, float(depth))
        self.range = max(0, int(range))
        self.speed = max(0.0, float(speed))
        self.delay = max(0, int(delay))
        if reset_phase:
            self.reset()

    def update(self, msec: int) -> None:
        if msec <= 0:
            return
        if self.delay_counter < self.delay:
            if self.delay_counter + msec <= self.delay:
                self.delay_counter += msec
                return
            msec -= self.delay - self.delay_counter
            self.delay_counter = self.delay
        self.counter += self.speed * msec / 1000.0
        self.counter -= int(self.counter)

    def value(self) -> float:
        if self.depth == 0.0 or self.delay_counter < self.delay:
            return 0.0
        index = int(self.counter * 128.0)
        if index >= 128:
            index = 127
        return (_lfo_sin_index(index) / 127.0) * self.depth * self.range


def _lfo_sin_index(index: int) -> int:
    index = max(0, min(127, int(index)))
    if index < 32:
        return _LFO_SIN_TABLE[index]
    if index < 64:
        return _LFO_SIN_TABLE[32 - (index - 32)]
    if index < 96:
        return -_LFO_SIN_TABLE[index - 64]
    return -_LFO_SIN_TABLE[32 - (index - 96)]


def _clamp_7bit(value: int) -> int:
    return max(0, min(127, int(value)))


