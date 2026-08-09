from enum import IntEnum, StrEnum
from typing import NamedTuple

class FileTag(StrEnum):
    BRSAR = "RSAR"
    BRSEQ = "RSEQ"
    BRWAR = "RWAR"
    BRWAV = "RWAV"
    BRBNK = "RBNK"
    BRSTM = "RSTM"
    BRWSD = "RWSD"


class ByteOrder(IntEnum):
    BIG_ENDIAN = 0xFEFF
    LITTLE_ENDIAN = 0xFFFE


class RefType(IntEnum):
    ABSOLUTE = 0
    RELATIVE = 1


class Reference(NamedTuple):
    flag: int
    data_type: int
    offset: int


NW4R_SIZE_OF_REFERENCE = 8
NW4R_EMPTY_REFERENCE = b"\x00" * NW4R_SIZE_OF_REFERENCE


#
# Audio-related types
#

class AudioCodec(IntEnum):
    PCM8 = 0
    PCM16 = 1
    ADPCM = 2

    def __str__(self) -> str:
        return self.name


# Standard predictor coefficient pairs used by BRSTM and related stream formats.
# BRWAV stores its own coefficients.
CANONICAL_ADPCM_COEFS: tuple[tuple[int, int], ...] = (
    (0x0000, 0x0000),
    (0x0800, 0x0000),
    (0x0400, 0x0400),
    (0x0300, 0x0100),
    (0x0380, 0x0100),
    (0x03C0, 0x0100),
    (0x0400, 0x0000),
    (0x03C0, 0x0040),
)