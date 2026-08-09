from dataclasses import dataclass, field
from enum import IntEnum

from pysar.core.types import AudioCodec


AudioEncoding = AudioCodec


class LocationType(IntEnum):
    OFFSET = 0   # Relative address
    ADDRESS = 1  # Absolute address


@dataclass
class AdpcmParams:
    coefs: tuple[int, ...] = field(default_factory=lambda: tuple([0] * 16))
    gain: int = 0
    pred_scale: int = 0
    yn1: int = 0
    yn2: int = 0

    loop_pred_scale: int = 0
    loop_yn1: int = 0
    loop_yn2: int = 0

    def __post_init__(self):
        if len(self.coefs) != 16:
            raise ValueError(f"ADPCM requires exactly 16 coefficients, got {len(self.coefs)}")

    def __str__(self) -> str:
        return f"AdpcmParams(yn1={self.yn1}, yn2={self.yn2})"


@dataclass
class ChannelInfo:
    data_offset: int = 0
    adpcm_offset: int = 0

    # Volume levels for surround sound (front-left, front-right, back-left, back-right)
    volume_fl: int = 1
    volume_fr: int = 1
    volume_bl: int = 1
    volume_br: int = 1


@dataclass
class WaveInfo:
    encoding: AudioCodec = AudioCodec.ADPCM
    is_looped: bool = False
    n_channels: int = 1
    sample_rate: int = 32000
    sample_rate_ext: int = 0

    location_type: LocationType = LocationType.OFFSET

    loop_start: int = 0
    n_samples: int = 0

    data_location: int = 0

    channel_table_offset: int = 0x1C

    channels: list[ChannelInfo] = field(default_factory=list)
    adpcm_params: list[AdpcmParams] = field(default_factory=list)

    @property
    def actual_sample_rate(self) -> int:
        return (self.sample_rate_ext << 16) | self.sample_rate

    @staticmethod
    def nibble_to_samples(nibble_addr: int) -> int:
        nibble_addr = max(0, int(nibble_addr))
        if nibble_addr < 2:
            return 0
        return (nibble_addr // 16) * 14 + (nibble_addr % 16) - 2

    @staticmethod
    def sample_to_nibble(sample: int) -> int:
        sample = max(0, int(sample))
        return (sample // 14) * 16 + (sample % 14) + 2

    @staticmethod
    def samples_to_nibble(samples: int) -> int:
        return WaveInfo.sample_to_nibble(samples)

    def get_loop_start_samples(self) -> int:
        if self.encoding == AudioCodec.ADPCM:
            return self.nibble_to_samples(self.loop_start)
        return self.loop_start

    def get_n_samples_actual(self) -> int:
        if self.encoding == AudioCodec.ADPCM:
            if self.n_samples <= 0:
                return 0
            return self.nibble_to_samples(self.n_samples) + 1
        return self.n_samples

    def __str__(self) -> str:
        loop_info = f", loop_start={self.get_loop_start_samples()}" if self.is_looped else ""
        return (
            f"WaveInfo(encoding={self.encoding}, looped={self.is_looped}, "
            f"channels={self.n_channels}, rate={self.actual_sample_rate}, "
            f"samples={self.get_n_samples_actual()}{loop_info})"
        )


@dataclass
class BrwavData:
    """
    Complete BRWAV file data model.

    Contains all parsed information from a BRWAV file,
    separated into metadata (wave_info) and audio data (samples).
    """
    version: int = 0x0102
    file_size: int = 0

    info_offset: int = 0
    info_size: int = 0
    data_offset: int = 0
    data_size: int = 0

    wave_info: WaveInfo = field(default_factory=WaveInfo)
    sample_data: bytes = b""

    raw_bytes: bytes | None = None

    @property
    def encoding(self) -> AudioCodec:
        return self.wave_info.encoding

    @property
    def sample_rate(self) -> int:
        return self.wave_info.actual_sample_rate

    @property
    def n_channels(self) -> int:
        return self.wave_info.n_channels

    @property
    def is_looped(self) -> bool:
        return self.wave_info.is_looped

    def __str__(self) -> str:
        return f"BrwavData(version=0x{self.version:04X}, {self.wave_info})"

    def __repr__(self) -> str:
        return self.__str__()
