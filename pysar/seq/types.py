from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
import wave

import numpy as np

from pysar.core.format.rbnk import Brbnk
from pysar.core.format.rseq import Brseq
from pysar.core.format.rstm import Brstm
from pysar.core.format.rwar import Brwar
from pysar.core.format.rwsd import Brwsd
from pysar.core.model import SoundType, SoundDataEntry


@dataclass(slots=True)
class RenderOptions:
    sample_rate: int = 32000
    loop_count: int = 1
    max_ticks: int = 300_000
    tail_seconds: float = 0.45
    block_frames: int = 512
    master_gain: float = 0.85
    max_physical_voices: int = 96
    one_shot: bool = True
    seq_program_override: int | None = None
    seq_note_override: int | None = None
    seq_random_overrides: tuple[tuple[int, int], ...] = ()


@dataclass(slots=True)
class PlaybackContext:
    name: str
    sound_type: "SoundType"
    entry: "SoundDataEntry"
    archive_volume: float = 1.0
    brseq: Brseq | None = None
    brbnk: Brbnk | None = None
    brwar: Brwar | None = None
    brwsd: Brwsd | None = None
    brstm: Brstm | None = None
    start_label: str | None = None
    start_offset: int | None = None
    default_programs: dict[int, int] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RenderedAudio:
    samples: np.ndarray
    sample_rate: int

    _WavEncoding = Literal["pcm16", "pcm24", "pcm32"]

    @property
    def duration(self) -> float:
        if self.samples.size == 0:
            return 0.0
        return float(len(self.samples) / self.sample_rate)

    @property
    def peak(self) -> float:
        if self.samples.size == 0:
            return 0.0
        return float(np.max(np.abs(self.samples)))

    def save_wav(self, path: str | Path, *, encoding: _WavEncoding = "pcm16") -> Path:
        out_path = Path(path)
        pcm = np.clip(self.samples, -1.0, 1.0)
        encoding = encoding.lower()
        if encoding == "pcm16":
            sample_width = 2
            payload = np.round(pcm * 32767.0).astype("<i2").tobytes()
        elif encoding == "pcm24":
            sample_width = 3
            scaled = np.round(pcm * 8388607.0).astype(np.int32)
            payload = self._pcm24_bytes(scaled)
        elif encoding == "pcm32":
            sample_width = 4
            payload = np.round(pcm * 2147483647.0).astype("<i4").tobytes()
        else:
            raise ValueError(f"unsupported WAV encoding: {encoding!r}")
        with wave.open(str(out_path), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(sample_width)
            handle.setframerate(self.sample_rate)
            handle.writeframes(payload)
        return out_path

    @staticmethod
    def _pcm24_bytes(samples: np.ndarray) -> bytes:
        packed = np.empty((samples.shape[0], samples.shape[1], 3), dtype=np.uint8)
        packed[..., 0] = samples & 0xFF
        packed[..., 1] = (samples >> 8) & 0xFF
        packed[..., 2] = (samples >> 16) & 0xFF
        return packed.reshape(-1).tobytes()
