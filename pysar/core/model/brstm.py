from dataclasses import dataclass, field
from enum import IntEnum
from pysar.core.types import AudioCodec, CANONICAL_ADPCM_COEFS


@dataclass
class AdpcmChannelInfo:
    # 8 coefficient pairs (16 values total)
    coefs: tuple[tuple[int, int], ...] = field(default_factory=lambda: CANONICAL_ADPCM_COEFS)

    # Gain (usually 0)
    gain: int = 0

    # Initial predictor/scale and history
    init_ps: int = 0
    init_hist1: int = 0
    init_hist2: int = 0

    # Loop point predictor/scale and history
    loop_ps: int = 0
    loop_hist1: int = 0
    loop_hist2: int = 0

    @property
    def init_hist(self) -> tuple[int, int]:
        return self.init_hist1, self.init_hist2

    @property
    def loop_hist(self) -> tuple[int, int]:
        return self.loop_hist1, self.loop_hist2


@dataclass
class TrackInfo:
    volume: int = 0x7F
    pan: int = 0x40

    # Channel indices for this track
    channel_count: int = 2
    left_channel_id: int = 0
    right_channel_id: int = 1
    channel_indices: list[int] = field(default_factory=list)

    def resolved_channel_indices(self) -> list[int]:
        if self.channel_indices:
            return [int(index) for index in self.channel_indices]
        if self.channel_count <= 1:
            return [int(self.left_channel_id)]
        return [int(self.left_channel_id), int(self.right_channel_id)]


@dataclass
class BrstmData:
    # File metadata
    version: int = 0x0100
    file_size: int = 0

    # Audio format
    codec: AudioCodec = AudioCodec.ADPCM
    is_looped: bool = False
    n_channels: int = 2
    sample_rate: int = 32000

    # Sample info
    loop_start: int = 0
    total_samples: int = 0

    # Block structure
    block_count: int = 0
    block_size: int = 0
    block_samples: int = 0
    final_block_size: int = 0
    final_block_samples: int = 0
    final_block_size_padded: int = 0

    # ADPC table info (for ADPCM only)
    adpc_table_interval: int = 0
    adpc_bytes_per_entry: int = 0

    # Track info
    tracks: list[TrackInfo] = field(default_factory=list)

    # Per-channel ADPCM info (for ADPCM codec)
    adpcm_channels: list[AdpcmChannelInfo] = field(default_factory=list)

    # Per-channel raw audio data (after de-interleaving blocks)
    channel_data: list[bytes] = field(default_factory=list)

    # ADPC history table entries: list of (h1, h2) per channel per block
    adpc_entries: list[list[tuple[int, int]]] = field(default_factory=list)

    # Pre-interleaved data payload (for efficient serialization)
    data_payload: bytes | None = None

    # Raw bytes for pass-through
    raw_bytes: bytes | None = None

    @property
    def n_tracks(self) -> int:
        """Number of tracks (typically channels // 2 for stereo)."""
        if self.tracks:
            return len(self.tracks)
        return max(1, (self.n_channels + 1) // 2) if self.n_channels else 0

    @property
    def duration(self) -> float:
        """Duration in seconds."""
        if self.sample_rate == 0:
            return 0.0
        return self.total_samples / self.sample_rate

    def __str__(self) -> str:
        loop_info = f", loop_start={self.loop_start}" if self.is_looped else ""
        return (
            f"BrstmData(codec={self.codec}, channels={self.n_channels}, "
            f"rate={self.sample_rate}, samples={self.total_samples}{loop_info})"
        )

    def __repr__(self) -> str:
        return self.__str__()
