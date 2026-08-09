import re
from enum import IntEnum, auto


# Note mask - values 0x00-0x7F are notes
NOTE_MASK = 0x80


class MML(IntEnum):
    # Variable length parameter commands
    WAIT = 0x80
    PRG = 0x81

    # Control commands
    OPEN_TRACK = 0x88
    JUMP = 0x89
    CALL = 0x8A

    # Prefix commands (modifiers for following command)
    RANDOM = 0xA0
    VARIABLE = 0xA1
    IF = 0xA2
    TIME = 0xA3
    TIME_RANDOM = 0xA4
    TIME_VARIABLE = 0xA5

    # u8 parameter commands
    TIMEBASE = 0xB0
    ENV_HOLD = 0xB1
    MONOPHONIC = 0xB2
    VELOCITY_RANGE = 0xB3
    BIQUAD_TYPE = 0xB4
    BIQUAD_VALUE = 0xB5
    PAN = 0xC0
    VOLUME = 0xC1
    MAIN_VOLUME = 0xC2
    TRANSPOSE = 0xC3
    PITCH_BEND = 0xC4
    BEND_RANGE = 0xC5
    PRIO = 0xC6
    NOTE_WAIT = 0xC7
    TIE = 0xC8
    PORTA = 0xC9
    MOD_DEPTH = 0xCA
    MOD_SPEED = 0xCB
    MOD_TYPE = 0xCC
    MOD_RANGE = 0xCD
    PORTA_SW = 0xCE
    PORTA_TIME = 0xCF
    ATTACK = 0xD0
    DECAY = 0xD1
    SUSTAIN = 0xD2
    RELEASE = 0xD3
    LOOP_START = 0xD4
    VOLUME2 = 0xD5
    PRINTVAR = 0xD6
    SURROUND_PAN = 0xD7
    LPF_CUTOFF = 0xD8
    FXSEND_A = 0xD9
    FXSEND_B = 0xDA
    MAINSEND = 0xDB
    INIT_PAN = 0xDC
    MUTE = 0xDD
    FXSEND_C = 0xDE
    DAMPER = 0xDF

    # s16 parameter commands
    MOD_DELAY = 0xE0
    TEMPO = 0xE1
    SWEEP_PITCH = 0xE3

    # Extended command prefix
    EX_COMMAND = 0xF0

    # Other
    ENV_RESET = 0xFB
    LOOP_END = 0xFC
    RET = 0xFD
    ALLOC_TRACK = 0xFE
    FIN = 0xFF

    def __str__(self) -> str:
        return self.name.lower()


class MMLEX(IntEnum):
    # Variable operations
    SETVAR = 0x80
    ADDVAR = 0x81
    SUBVAR = 0x82
    MULVAR = 0x83
    DIVVAR = 0x84
    SHIFTVAR = 0x85
    RANDVAR = 0x86
    ANDVAR = 0x87
    ORVAR = 0x88
    XORVAR = 0x89
    NOTVAR = 0x8A
    MODVAR = 0x8B

    # Comparisons
    CMP_EQ = 0x90
    CMP_GE = 0x91
    CMP_GT = 0x92
    CMP_LE = 0x93
    CMP_LT = 0x94
    CMP_NE = 0x95

    # User procedure
    USERPROC = 0xE0

    def __str__(self) -> str:
        return self.name.lower()


class ArgType(IntEnum):
    """Argument types for MML commands."""
    NONE = 0
    U8 = 1
    S8 = 2
    U16 = 3
    S16 = 4
    U24 = 5
    VAR_LEN = 6
    RANDOM = 7      # Two s16 values (min, max)
    VARIABLE = 8    # u8 variable index


MML_ARG_SPEC: dict[int, list[ArgType]] = {
    MML.WAIT: [ArgType.VAR_LEN],
    MML.PRG: [ArgType.VAR_LEN],
    MML.OPEN_TRACK: [ArgType.U8, ArgType.U24],
    MML.JUMP: [ArgType.U24],
    MML.CALL: [ArgType.U24],
    MML.TIMEBASE: [ArgType.U8],
    MML.ENV_HOLD: [ArgType.S8],
    MML.MONOPHONIC: [ArgType.U8],
    MML.VELOCITY_RANGE: [ArgType.U8],
    MML.BIQUAD_TYPE: [ArgType.U8],
    MML.BIQUAD_VALUE: [ArgType.U8],
    MML.PAN: [ArgType.U8],
    MML.VOLUME: [ArgType.U8],
    MML.MAIN_VOLUME: [ArgType.U8],
    MML.TRANSPOSE: [ArgType.S8],
    MML.PITCH_BEND: [ArgType.S8],
    MML.BEND_RANGE: [ArgType.U8],
    MML.PRIO: [ArgType.U8],
    MML.NOTE_WAIT: [ArgType.U8],
    MML.TIE: [ArgType.U8],
    MML.PORTA: [ArgType.U8],
    MML.MOD_DEPTH: [ArgType.U8],
    MML.MOD_SPEED: [ArgType.U8],
    MML.MOD_TYPE: [ArgType.U8],
    MML.MOD_RANGE: [ArgType.U8],
    MML.PORTA_SW: [ArgType.U8],
    MML.PORTA_TIME: [ArgType.U8],
    MML.ATTACK: [ArgType.S8],
    MML.DECAY: [ArgType.S8],
    MML.SUSTAIN: [ArgType.S8],
    MML.RELEASE: [ArgType.S8],
    MML.LOOP_START: [ArgType.U8],
    MML.VOLUME2: [ArgType.U8],
    MML.PRINTVAR: [ArgType.U8],
    MML.SURROUND_PAN: [ArgType.U8],
    MML.LPF_CUTOFF: [ArgType.U8],
    MML.FXSEND_A: [ArgType.U8],
    MML.FXSEND_B: [ArgType.U8],
    MML.MAINSEND: [ArgType.U8],
    MML.INIT_PAN: [ArgType.U8],
    MML.MUTE: [ArgType.U8],
    MML.FXSEND_C: [ArgType.U8],
    MML.DAMPER: [ArgType.U8],
    MML.MOD_DELAY: [ArgType.S16],
    MML.TEMPO: [ArgType.U16],
    MML.SWEEP_PITCH: [ArgType.S16],
    MML.ALLOC_TRACK: [ArgType.U16],
    MML.ENV_RESET: [],
    MML.LOOP_END: [],
    MML.RET: [],
    MML.FIN: [],
}


MMLEX_ARG_SPEC: dict[int, list[ArgType]] = {
    MMLEX.SETVAR: [ArgType.U8, ArgType.S16],
    MMLEX.ADDVAR: [ArgType.U8, ArgType.S16],
    MMLEX.SUBVAR: [ArgType.U8, ArgType.S16],
    MMLEX.MULVAR: [ArgType.U8, ArgType.S16],
    MMLEX.DIVVAR: [ArgType.U8, ArgType.S16],
    MMLEX.SHIFTVAR: [ArgType.U8, ArgType.S16],
    MMLEX.RANDVAR: [ArgType.U8, ArgType.S16],
    MMLEX.ANDVAR: [ArgType.U8, ArgType.S16],
    MMLEX.ORVAR: [ArgType.U8, ArgType.S16],
    MMLEX.XORVAR: [ArgType.U8, ArgType.S16],
    MMLEX.NOTVAR: [ArgType.U8, ArgType.S16],
    MMLEX.MODVAR: [ArgType.U8, ArgType.S16],
    MMLEX.CMP_EQ: [ArgType.U8, ArgType.S16],
    MMLEX.CMP_GE: [ArgType.U8, ArgType.S16],
    MMLEX.CMP_GT: [ArgType.U8, ArgType.S16],
    MMLEX.CMP_LE: [ArgType.U8, ArgType.S16],
    MMLEX.CMP_LT: [ArgType.U8, ArgType.S16],
    MMLEX.CMP_NE: [ArgType.U8, ArgType.S16],
    MMLEX.USERPROC: [ArgType.U16],
}


PREFIXABLE_COMMANDS = {
    MML.WAIT, MML.PRG, MML.TEMPO, MML.VOLUME, MML.VOLUME2, MML.MAIN_VOLUME,
    MML.PITCH_BEND, MML.PAN, MML.TRANSPOSE, MML.PORTA_TIME, MML.SWEEP_PITCH,
    MML.MOD_DEPTH, MML.MOD_SPEED, MML.ATTACK, MML.DECAY, MML.SUSTAIN,
    MML.RELEASE, MML.MOD_DELAY, MML.LOOP_START, MML.MUTE,
    # Extended commands are also prefixable
}


NOTE_NAMES = ['c', 'cs', 'd', 'ds', 'e', 'f', 'fs', 'g', 'gs', 'a', 'as', 'b']


def note_to_name(note: int) -> str:
    """Convert MIDI note number to name (e.g., 60 -> 'c4')."""
    if note < 0 or note > 127:
        return f"n{note}"
    octave = (note // 12) - 1
    name = NOTE_NAMES[note % 12]
    return f"{name}{octave}"


def name_to_note(name: str) -> int:
    """Convert note name to MIDI number (e.g., 'c4' -> 60)."""
    name = name.lower().strip()

    # Handle generic note format: n60
    if name.startswith('n') and name[1:].isdigit():
        return int(name[1:])

    # Nintendo source uses ``cn4`` for C natural and ``cs4`` for C sharp.
    # Also accept the common c4/c#4/db4 spellings used by other tools.
    match = re.fullmatch(r"([a-g])([ns#b]?)(m?\d+|-\d+)?", name)
    if match:
        letter, accidental, octave_text = match.groups()
        natural_pc = {
            'c': 0, 'd': 2, 'e': 4, 'f': 5,
            'g': 7, 'a': 9, 'b': 11,
        }[letter]
        if accidental in ('s', '#'):
            natural_pc += 1
        elif accidental == 'b':
            natural_pc -= 1
        if not octave_text:
            octave = 4
        elif octave_text.startswith('m'):
            octave = -int(octave_text[1:])
        else:
            octave = int(octave_text)
        return (octave + 1) * 12 + natural_pc

    raise ValueError(f"Invalid note name: {name}")


def is_note(opcode: int) -> bool:
    """Check if opcode is a note (0x00-0x7F)."""
    return (opcode & NOTE_MASK) == 0


# Variable ranges
SEQ_LOCAL_VAR_COUNT = 16
SEQ_GLOBAL_VAR_COUNT = 16
SEQ_TRACK_VAR_COUNT = 16

SEQ_LOCAL_VAR_RANGE = range(0, 16)
SEQ_GLOBAL_VAR_RANGE = range(16, 32)
SEQ_TRACK_VAR_RANGE = range(32, 48)

SEQ_VAR_DEFAULT = -1

# Player constants
DEFAULT_TIMEBASE = 48
DEFAULT_TEMPO = 120
MAX_TRACKS = 16
CALL_STACK_DEPTH = 3


