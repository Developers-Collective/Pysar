import wave
from io import BytesIO
from pathlib import Path
import struct
from typing import BinaryIO, Self

import numpy as np

from pysar.core.base import EditorBase, DirtyFlags
from pysar.core.model.brwav import (
    BrwavData,
    WaveInfo,
    ChannelInfo,
    AdpcmParams
)
from pysar.core.format.rwav.reader import BrwavReader
from pysar.core.format.rwav.writer import BrwavWriter

from pysar.core.codec.encode_adpcm import dsp_encode
from pysar.core.codec.decode_adpcm import decode_adpcm_block
from pysar.core.codec.brstm_adpcm import compute_loop_context

from pysar.core.codec.pcm import encode_pcm8, encode_pcm16, decode_pcm8_block, decode_pcm16_block
from pysar.core.types import AudioCodec


def _read_wav_as_pcm16_samples(wav: wave.Wave_read) -> tuple[list[int], int]:
    sample_rate = wav.getframerate()
    n_channels = wav.getnchannels()
    n_samples = wav.getnframes()
    sample_width = wav.getsampwidth()

    if wav.getcomptype() != "NONE":
        raise ValueError("Only uncompressed PCM WAV files are supported")

    if n_channels != 1:
        raise ValueError("BRWAV only supports mono audio. Use BRSTM for multi-channel.")

    frames = wav.readframes(n_samples)
    frame_size = sample_width * n_channels
    complete_frames = len(frames) // frame_size if frame_size else 0
    if complete_frames <= 0:
        raise ValueError("WAV contains no readable sample data")

    # Some tools write a frame count that is larger than the actual data chunk.
    # Decode the complete frames that are present instead of handing a short
    # buffer to struct.unpack().
    if complete_frames != n_samples:
        frames = frames[:complete_frames * frame_size]
        n_samples = complete_frames

    if sample_width == 1:
        return [((sample - 128) << 8) for sample in frames], sample_rate

    if sample_width == 2:
        return list(struct.unpack(f"<{n_samples}h", frames)), sample_rate

    if sample_width == 3:
        samples = []
        for i in range(0, len(frames), 3):
            value = int.from_bytes(frames[i:i + 3], "little", signed=False)
            if value & 0x800000:
                value -= 0x1000000
            samples.append(max(-32768, min(32767, value >> 8)))
        return samples, sample_rate

    if sample_width == 4:
        samples_32 = struct.unpack(f"<{n_samples}i", frames)
        return [max(-32768, min(32767, sample >> 16)) for sample in samples_32], sample_rate

    raise ValueError(f"Unsupported WAV sample width: {sample_width * 8}-bit")


class Brwav(EditorBase):
    """
    BRWAV file implementation.

    Provides the ability for loading, converting, decoding, and saving
    BRWAV audio files.
    """

    def __init__(self, data: BrwavData | None = None):
        super().__init__()
        self._data = data or BrwavData()
        self._decoded_pcm: bytes | None = None
        self._decoded_channels: tuple[bytes, ...] | None = None

    #
    # Factory methods
    #

    @classmethod
    def open(cls, path: str | Path) -> Self:
        """Open a BRWAV file from disk."""
        reader = BrwavReader()
        data = reader.from_file(str(path))
        return cls(data)

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        """Load a BRWAV from raw bytes."""
        reader = BrwavReader()
        data = reader.read(BytesIO(raw))
        return cls(data)

    @classmethod
    def from_stream(cls, stream: BinaryIO) -> Self:
        """Load a BRWAV from a binary stream."""
        reader = BrwavReader()
        data = reader.read(stream)
        return cls(data)

    @classmethod
    def from_wav(
            cls,
            wav_path: str | Path,
            *,
            encoding: AudioCodec = AudioCodec.ADPCM,
            loop_start: int = -1,
            loop_end: int = -1,
    ) -> Self:
        """
        Create a BRWAV from a WAV file.

        Args:
            wav_path: Path to the input WAV file
            encoding: Target encoding format (ADPCM, PCM16, or PCM8)
            loop_start: Loop start sample (-1 for no loop)
            loop_end: Loop end sample / total length (-1 for full length)

        Returns:
            A new BrwavEditor with the encoded audio
        """
        with wave.open(str(wav_path), "rb") as wav:
            samples, sample_rate = _read_wav_as_pcm16_samples(wav)

        return cls.from_pcm(
            samples,
            sample_rate,
            encoding=encoding,
            loop_start=loop_start,
            loop_end=loop_end,
        )

    @classmethod
    def from_pcm(
            cls,
            pcm_samples: list[int],
            sample_rate: int,
            *,
            encoding: AudioCodec = AudioCodec.ADPCM,
            loop_start: int = -1,
            loop_end: int = -1,
    ) -> Self:
        """
        Create a BRWAV from raw PCM samples.

        Args:
            pcm_samples: List of signed 16-bit PCM samples
            sample_rate: Sample rate in Hz
            encoding: Target encoding format
            loop_start: Loop start sample (-1 for no loop)
            loop_end: Loop end sample / total length (-1 for full length)

        Returns:
            A new BrwavEditor with the encoded audio
        """
        n_samples = len(pcm_samples)

        # Determine actual total samples (this is the loop end point)
        if loop_end <= 0 or loop_end > n_samples:
            total_samples = n_samples
        else:
            total_samples = loop_end

        # Truncate PCM data to total_samples
        pcm_samples = pcm_samples[:total_samples]

        # Determine if looping is enabled
        is_looped = loop_start >= 0

        # Validate loop_start
        if loop_start >= total_samples:
            loop_start = -1
            is_looped = False

        sample_data, wave_info = cls._encode_samples(
            pcm_samples,
            sample_rate,
            encoding,
            loop_start=loop_start if is_looped else -1,
            total_samples=total_samples,
        )

        data = BrwavData(
            wave_info=wave_info,
            sample_data=sample_data,
        )

        editor = cls(data)
        editor.mark_dirty(DirtyFlags.ALL)
        return editor

    #
    # Properties
    #

    @property
    def data(self) -> BrwavData:
        """Access the underlying data model."""
        return self._data

    @property
    def encoding(self) -> AudioCodec:
        """Audio encoding type."""
        return self._data.wave_info.encoding

    @property
    def sample_rate(self) -> int:
        """Sample rate in Hz."""
        return self._data.wave_info.actual_sample_rate

    @property
    def n_channels(self) -> int:
        """Number of audio channels."""
        return self._data.wave_info.n_channels

    @property
    def n_samples(self) -> int:
        """Number of samples (per channel)."""
        return self._data.wave_info.get_n_samples_actual()

    @property
    def is_looped(self) -> bool:
        """Whether the audio loops."""
        return self._data.wave_info.is_looped

    @property
    def loop_start(self) -> int:
        """Loop start sample."""
        return self._data.wave_info.get_loop_start_samples()

    def set_loop(self, looped: bool, loop_start: int = 0) -> None:
        """Update loop metadata, including the ADPCM decoder loop context."""
        info = self._data.wave_info
        enabled = bool(looped)
        start = max(0, int(loop_start))
        if enabled and (self.n_samples <= 0 or start >= self.n_samples):
            raise ValueError(
                f"Loop start must be between 0 and {max(0, self.n_samples - 1)}"
            )
        info.is_looped = enabled
        info.loop_start = (
            WaveInfo.sample_to_nibble(start)
            if info.encoding == AudioCodec.ADPCM
            else start
        ) if enabled else 0

        if info.encoding == AudioCodec.ADPCM:
            for channel_index, params in enumerate(info.adpcm_params):
                offset = (
                    int(info.channels[channel_index].data_offset)
                    if channel_index < len(info.channels) else 0
                )
                channel_data = self._data.sample_data[max(0, offset):]
                if enabled and start > 0:
                    coefficient_pairs = list(zip(params.coefs[::2], params.coefs[1::2]))
                    _scale, loop_yn1, loop_yn2 = compute_loop_context(
                        channel_data, start, coefficient_pairs,
                    )
                    header_offset = (start // 14) * 8
                    params.loop_pred_scale = (
                        int(channel_data[header_offset])
                        if header_offset < len(channel_data) else int(params.pred_scale)
                    )
                    params.loop_yn1 = loop_yn1
                    params.loop_yn2 = loop_yn2
                else:
                    params.loop_pred_scale = int(params.pred_scale)
                    params.loop_yn1 = 0
                    params.loop_yn2 = 0
        # Untouched BRWAVs may preserve their original raw blob. Metadata edits
        # must force the writer to rebuild it.
        self._data.raw_bytes = None
        self.mark_dirty(DirtyFlags.ALL)

    @property
    def duration(self) -> float:
        """Duration in seconds."""
        if self.sample_rate == 0:
            return 0.0
        return self.n_samples / self.sample_rate

    #
    # Conversion
    #

    def convert_to(self, target_encoding: AudioCodec) -> Self:
        """Convert the audio to a different encoding format.

        The encoder currently emits mono BRWAVs. Multi-channel input is
        therefore downmixed by frame before conversion; treating interleaved
        PCM values as one mono stream would double the duration and move loop
        points to the wrong frame.
        """
        if self.encoding == target_encoding:
            return self

        decoded_channels = self.decode_channels()
        arrays = [np.frombuffer(channel, dtype="<i2").astype(np.int32) for channel in decoded_channels]
        n_samples = min((len(channel) for channel in arrays), default=0)
        if len(arrays) == 1:
            samples = arrays[0][:n_samples].astype(np.int16).tolist()
        else:
            mixed = sum(channel[:n_samples] for channel in arrays) // len(arrays)
            samples = np.clip(mixed, -32768, 32767).astype(np.int16).tolist()

        # Preserve loop info
        is_looped = self.is_looped
        loop_start = self.loop_start if is_looped else -1

        # Re-encode (n_samples is already the correct total)
        sample_data, wave_info = self._encode_samples(
            samples,
            self.sample_rate,
            target_encoding,
            loop_start=loop_start,
            total_samples=n_samples,
        )

        # Update data
        self._data.wave_info = wave_info
        self._data.sample_data = sample_data
        self._decoded_pcm = None
        self._decoded_channels = None
        self.mark_dirty(DirtyFlags.ALL)

        return self

    #
    # Decoding
    #

    def decode(self, *, force: bool = False) -> bytes:
        """
        Decode the audio to 16-bit PCM.

        Returns the raw PCM bytes (little-endian signed 16-bit).
        Results are cached unless force=True.
        """
        if self._decoded_pcm is not None and not force:
            return self._decoded_pcm

        channels = self.decode_channels(force=force)
        if len(channels) == 1:
            pcm = channels[0]
        else:
            # SF2/playback consumers expect ordinary frame-interleaved PCM.
            # Nintendo stores each BRWAV channel in a separate data span.
            arrays = [np.frombuffer(channel, dtype="<i2") for channel in channels]
            frame_count = min(len(channel) for channel in arrays)
            pcm = np.stack([channel[:frame_count] for channel in arrays], axis=1).astype("<i2").tobytes()

        self._decoded_pcm = pcm
        return pcm

    def decode_channels(self, *, force: bool = False) -> tuple[bytes, ...]:
        """Decode each Nintendo channel to separate little-endian PCM16."""
        if self._decoded_channels is not None and not force:
            return self._decoded_channels

        info = self._data.wave_info
        sample_data = self._data.sample_data
        n_samples = info.get_n_samples_actual()
        n_channels = max(1, int(info.n_channels))
        decoded: list[bytes] = []
        for channel_index in range(n_channels):
            if channel_index < len(info.channels):
                start = max(0, int(info.channels[channel_index].data_offset))
            else:
                bytes_per_sample = 2 if info.encoding == AudioCodec.PCM16 else 1
                start = channel_index * n_samples * bytes_per_sample
            channel_data = sample_data[start:]
            if info.encoding == AudioCodec.PCM8:
                pcm = decode_pcm8_block(channel_data, n_samples, 1)
            elif info.encoding == AudioCodec.PCM16:
                pcm = decode_pcm16_block(channel_data, n_samples, 1)
            elif info.encoding == AudioCodec.ADPCM:
                if channel_index >= len(info.adpcm_params):
                    raise ValueError(f"No ADPCM parameters found for channel {channel_index}")
                adpcm = info.adpcm_params[channel_index]
                pcm = decode_adpcm_block(
                    channel_data,
                    n_samples,
                    1,
                    list(adpcm.coefs),
                    adpcm.yn1,
                    adpcm.yn2,
                )
            else:
                raise ValueError(f"Unknown encoding: {info.encoding}")
            decoded.append(pcm)

        self._decoded_channels = tuple(decoded)
        return self._decoded_channels

    def decode_to_wav(self, output_path: str | Path) -> Path:
        """
        Decode and save as a WAV file.

        Args:
            output_path: Path for the output WAV file

        Returns:
            Path to the created WAV file
        """
        output_path = Path(output_path)
        pcm = self.decode()

        with wave.open(str(output_path), "wb") as wav:
            wav.setnchannels(self.n_channels)
            wav.setsampwidth(2)  # 16-bit
            wav.setframerate(self.sample_rate)
            wav.writeframes(pcm)

        return output_path

    #
    # Saving
    #

    def to_bytes(self) -> bytes:
        """Serialize to BRWAV bytes."""
        writer = BrwavWriter()
        return writer.to_bytes(self._data)

    def save(self, path: str | Path) -> Self:
        """Save to a BRWAV file."""
        Path(path).write_bytes(self.to_bytes())
        self.clear_dirty()
        return self

    #
    # Decoding methods
    #

    def _decode_adpcm(self, data: bytes, n_samples: int, n_channels: int) -> bytes:
        """Decode DSP-ADPCM to PCM16."""

        # Get ADPCM params for the first channel (BRWAV is mono)
        if not self._data.wave_info.adpcm_params:
            raise ValueError("No ADPCM parameters found in wave info")

        adpcm = self._data.wave_info.adpcm_params[0]
        coefs = list(adpcm.coefs)
        yn1 = adpcm.yn1
        yn2 = adpcm.yn2

        return decode_adpcm_block(data, n_samples, n_channels, coefs, yn1, yn2)

    #
    # Encoding methods
    #


    @classmethod
    def _encode_samples(
            cls,
            pcm_samples: list[int],
            sample_rate: int,
            encoding: AudioCodec,
            loop_start: int = -1,
            total_samples: int | None = None,
    ) -> tuple[bytes, WaveInfo]:
        """
        Encode PCM samples to the specified format.

        Args:
            pcm_samples: PCM samples (already truncated to desired length)
            sample_rate: Sample rate in Hz
            encoding: Target encoding
            loop_start: Loop start sample (-1 for no loop)
            total_samples: Total sample count (defaults to len(pcm_samples))

        Returns:
            Tuple of (encoded sample data, WaveInfo)
        """
        if total_samples is None:
            total_samples = len(pcm_samples)

        is_looped = loop_start >= 0

        if encoding == AudioCodec.ADPCM:
            return cls._encode_as_adpcm(
                pcm_samples, total_samples, sample_rate, is_looped, loop_start
            )
        elif encoding == AudioCodec.PCM16:
            return cls._encode_as_pcm16(
                pcm_samples, total_samples, sample_rate, is_looped, loop_start
            )
        elif encoding == AudioCodec.PCM8:
            return cls._encode_as_pcm8(
                pcm_samples, total_samples, sample_rate, is_looped, loop_start
            )
        else:
            raise ValueError(f"Unsupported encoding: {encoding}")

    @classmethod
    def _encode_as_adpcm(
            cls,
            pcm_samples: list[int],
            n_samples: int,
            sample_rate: int,
            is_looped: bool,
            loop_start: int
    ) -> tuple[bytes, WaveInfo]:
        coefs, adpcm_data = dsp_encode(pcm_samples, n_samples)

        # AX can start normal decoding from the first packet header, but a
        # hardware loop jump also needs the predictor/scale and the two
        # reconstructed history samples immediately preceding loop_start.
        # BRWAV stores the original packed DSP frame header alongside those
        # reconstructed histories.
        initial_pred_scale = int(adpcm_data[0]) if adpcm_data else 0
        loop_pred_scale = initial_pred_scale
        loop_yn1 = 0
        loop_yn2 = 0
        if is_looped and loop_start > 0:
            coefficient_pairs = list(zip(coefs[::2], coefs[1::2]))
            _, loop_yn1, loop_yn2 = compute_loop_context(
                adpcm_data,
                loop_start,
                coefficient_pairs,
            )
            # At an exact 14-sample frame boundary the helper has consumed
            # the preceding frame but has not read the next header yet, so
            # source the packed predictor/scale directly from the target
            # frame in all cases.
            loop_pred_scale = int(adpcm_data[(loop_start // 14) * 8])

        # Calculate nibble addresses for ADPCM
        if is_looped and loop_start >= 0:
            loop_start_nibble = WaveInfo.sample_to_nibble(loop_start)
        else:
            loop_start_nibble = WaveInfo.sample_to_nibble(0)

        n_samples_nibble = WaveInfo.sample_to_nibble(max(0, n_samples - 1)) if n_samples > 0 else 0

        wave_info = WaveInfo(
            encoding=AudioCodec.ADPCM,
            is_looped=is_looped,
            n_channels=1,
            sample_rate=sample_rate & 0xFFFF,
            sample_rate_ext=(sample_rate >> 16) & 0xFF,
            loop_start=loop_start_nibble,
            n_samples=n_samples_nibble,
            channels=[ChannelInfo()],
            adpcm_params=[AdpcmParams(
                coefs=tuple(coefs),
                pred_scale=initial_pred_scale,
                loop_pred_scale=loop_pred_scale,
                loop_yn1=loop_yn1,
                loop_yn2=loop_yn2,
            )],
        )

        return adpcm_data, wave_info

    @classmethod
    def _encode_as_pcm16(
            cls,
            pcm_samples: list[int],
            n_samples: int,
            sample_rate: int,
            is_looped: bool,
            loop_start: int
    ) -> tuple[bytes, WaveInfo]:
        pcm16_data = encode_pcm16(pcm_samples, n_samples)

        wave_info = WaveInfo(
            encoding=AudioCodec.PCM16,
            is_looped=is_looped,
            n_channels=1,
            sample_rate=sample_rate & 0xFFFF,
            sample_rate_ext=(sample_rate >> 16) & 0xFF,
            loop_start=loop_start if is_looped else 0,
            n_samples=n_samples,
            channels=[ChannelInfo()],
            adpcm_params=[],
        )

        return pcm16_data, wave_info

    @classmethod
    def _encode_as_pcm8(
            cls,
            pcm_samples: list[int],
            n_samples: int,
            sample_rate: int,
            is_looped: bool,
            loop_start: int
    ) -> tuple[bytes, WaveInfo]:
        pcm8_data = encode_pcm8(pcm_samples, n_samples)

        wave_info = WaveInfo(
            encoding=AudioCodec.PCM8,
            is_looped=is_looped,
            n_channels=1,
            sample_rate=sample_rate & 0xFFFF,
            sample_rate_ext=(sample_rate >> 16) & 0xFF,
            loop_start=loop_start if is_looped else 0,
            n_samples=n_samples,
            channels=[ChannelInfo()],
            adpcm_params=[],
        )

        return pcm8_data, wave_info

    def __str__(self) -> str:
        return (
            f"Brwav({self.encoding.name}, {self.sample_rate}Hz, "
            f"{self.n_channels}ch, {self.n_samples} samples, "
            f"{'looped' if self.is_looped else 'not looped'})"
        )

    def __repr__(self) -> str:
        return self.__str__()
