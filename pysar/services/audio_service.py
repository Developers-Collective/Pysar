from pathlib import Path
from typing import Literal

import numpy as np

from pysar.core.format.rsar import Brsar
from pysar.seq import RenderedAudio
from pysar.seq.engine import SoundArchiveEngine

from pysar.services.errors import InvalidSelectionError, NoProjectOpenError, PlaybackFailedError
from pysar.services.models import PreviewOptions, ProjectSession

class AudioService:
    def __init__(self, engine: SoundArchiveEngine | None = None) -> None:
        self._engine = engine or SoundArchiveEngine()

    def render_preview(
        self,
        session: ProjectSession,
        sound_id: int,
        options: PreviewOptions | None = None,
    ) -> "RenderedAudio":
        archive = self._require_archive(session)
        sound_name = self._sound_name(archive, sound_id)
        render_options = options.to_render_options() if options is not None else None
        try:
            return self._engine.render_sound(archive, sound_name, render_options)
        except Exception as exc:  # pragma: no cover - defensive wrapper
            raise PlaybackFailedError(f"could not render preview for {sound_name!r}") from exc

    def save_preview_wav(
        self,
        session: ProjectSession,
        sound_id: int,
        output_path: str | Path,
        options: PreviewOptions | None = None,
        *,
        encoding: Literal["pcm16", "pcm24", "pcm32"] = "pcm16",
    ) -> Path:
        audio = self.render_preview(session, sound_id, options)
        return audio.save_wav(output_path, encoding=encoding)

    def play_sound(
        self,
        session: ProjectSession,
        sound_id: int,
        options: PreviewOptions | None = None,
        *,
        offset_ms: int = 0,
        volume: float = 1.0,
    ) -> "RenderedAudio":
        audio = self.render_preview(session, sound_id, options)
        start_frame = max(0, min(len(audio.samples), round(offset_ms * audio.sample_rate / 1000)))
        samples = audio.samples[start_frame:]
        samples = np.clip(samples * max(0.0, min(1.0, float(volume))), -1.0, 1.0)
        try:
            import sounddevice as sd
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise PlaybackFailedError("sounddevice is required for playback") from exc
        try:
            sd.play(samples, samplerate=audio.sample_rate, blocking=False)
        except Exception as exc:  # pragma: no cover - depends on local audio setup
            raise PlaybackFailedError(f"could not play sound id {sound_id!r}") from exc
        return audio

    def play_wave_sample(
        self,
        session: ProjectSession,
        archive_file_id: int,
        wave_index: int,
        *,
        offset_ms: int = 0,
        volume: float = 1.0,
        playback_rate: float = 1.0,
    ) -> dict:
        archive = self._require_archive(session)
        embedded = archive.data.embedded_files.get(int(archive_file_id))
        if embedded is None or embedded.magic != "RWAR":
            raise InvalidSelectionError(f"file_id {archive_file_id} is not an RWAR")

        from pysar.core.format.rwar import Brwar

        brwar = Brwar.from_bytes(embedded.raw_data)
        if wave_index < 0 or wave_index >= len(brwar):
            raise InvalidSelectionError(f"wave index {wave_index} is out of range")

        brwav = brwar[int(wave_index)]
        pcm = np.frombuffer(brwav.decode(), dtype="<i2").astype(np.float32) / 32768.0
        channels = max(1, int(brwav.n_channels))
        if channels > 1:
            pcm = pcm.reshape(-1, channels)
        else:
            pcm = pcm.reshape(-1, 1)
        rate = max(1.0e-6, float(playback_rate))
        output_frames = max(1, int(np.ceil(len(pcm) / rate))) if len(pcm) else 0
        start_frame = max(0, min(output_frames, round(offset_ms * int(brwav.sample_rate) / 1000)))
        if len(pcm) and abs(rate - 1.0) >= 1.0e-9:
            positions = np.arange(start_frame, output_frames, dtype=np.float64) * rate
            left = np.floor(positions).astype(np.int64)
            right = np.minimum(left + 1, len(pcm) - 1)
            fraction = (positions - left).astype(np.float32)[:, None]
            pcm = (pcm[left] * (1.0 - fraction) + pcm[right] * fraction).astype(np.float32, copy=False)
        else:
            pcm = pcm[start_frame:]
        pcm = np.clip(pcm * max(0.0, min(1.0, float(volume))), -1.0, 1.0)
        try:
            import sounddevice as sd
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise PlaybackFailedError("sounddevice is required for playback") from exc
        try:
            sd.play(pcm, samplerate=int(brwav.sample_rate), blocking=False)
        except Exception as exc:  # pragma: no cover - depends on local audio setup
            raise PlaybackFailedError(f"could not play wave sample {wave_index}") from exc
        return {
            "sampleRate": int(brwav.sample_rate),
            "durationMs": int(round(output_frames * 1000 / max(1, int(brwav.sample_rate)))),
        }

    def stop(self) -> None:
        try:
            import sounddevice as sd
        except ImportError:
            return
        sd.stop()

    @staticmethod
    def _require_archive(session: ProjectSession) -> Brsar:
        if session.archive is None:
            raise NoProjectOpenError("no archive is open")
        archive: Brsar = session.archive
        return archive

    @staticmethod
    def _sound_name(archive, sound_id: int) -> str:
        if sound_id < 0 or sound_id >= len(archive.data.sound_entries):
            raise InvalidSelectionError(f"sound id {sound_id} is out of range")
        entry = archive.data.sound_entries[sound_id]
        if 0 <= entry.file_name_index < len(archive.data.names):
            return archive.data.names[entry.file_name_index]
        raise InvalidSelectionError(f"sound id {sound_id} has no valid name")
