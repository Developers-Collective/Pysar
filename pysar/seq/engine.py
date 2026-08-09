from pathlib import Path
from typing import Literal

from pysar.core.model.brsar import SoundType
from pysar.seq.archive import make_playback_context
from pysar.seq.renderer import SequenceRenderer
from pysar.seq.types import PlaybackContext, RenderOptions, RenderedAudio


class SoundArchiveEngine:
    def render(self, context: PlaybackContext, options: RenderOptions | None = None) -> RenderedAudio:
        renderer = SequenceRenderer()
        if context.sound_type == SoundType.SEQ:
            return renderer.render(context, options)
        if context.sound_type == SoundType.WAVE:
            return renderer.render_wave_sound(context, options)
        if context.sound_type == SoundType.STRM:
            return renderer.render_stream_sound(context, options)
        raise ValueError(f"unsupported sound type: {context.sound_type!r}")

    def render_sound(self, archive, name: str, options: RenderOptions | None = None) -> RenderedAudio:
        return self.render(make_playback_context(archive, name), options)

    def save_wav(
        self,
        context: PlaybackContext,
        output_path: str | Path,
        options: RenderOptions | None = None,
        *,
        encoding: Literal["pcm16", "pcm24", "pcm32"] = "pcm16",
    ) -> Path:
        return self.render(context, options).save_wav(output_path, encoding=encoding)

    def save_sound_wav(
        self,
        archive,
        name: str,
        output_path: str | Path,
        options: RenderOptions | None = None,
        *,
        encoding: Literal["pcm16", "pcm24", "pcm32"] = "pcm16",
    ) -> Path:
        context = make_playback_context(archive, name)
        return self.save_wav(context, output_path, options, encoding=encoding)

    def save_hq_wav(
        self,
        context: PlaybackContext,
        output_path: str | Path,
        options: RenderOptions | None = None,
    ) -> Path:
        settings = options or RenderOptions(sample_rate=48000)
        return self.save_wav(context, output_path, settings, encoding="pcm24")

    def save_sound_hq_wav(
        self,
        archive,
        name: str,
        output_path: str | Path,
        options: RenderOptions | None = None,
    ) -> Path:
        context = make_playback_context(archive, name)
        return self.save_hq_wav(context, output_path, options)

    def play(self, context: PlaybackContext, options: RenderOptions | None = None) -> RenderedAudio:
        audio = self.render(context, options)
        if audio.samples.size == 0:
            return audio
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise ImportError("sounddevice is required for playback") from exc
        sd.play(audio.samples, samplerate=audio.sample_rate, blocking=True)
        return audio

    def play_sound(self, archive, name: str, options: RenderOptions | None = None) -> RenderedAudio:
        return self.play(make_playback_context(archive, name), options)
