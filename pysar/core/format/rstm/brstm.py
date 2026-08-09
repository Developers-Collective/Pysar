import wave
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Self

import numpy as np

from pysar.core.base import EditorBase, DirtyFlags
from pysar.core.codec.brstm_adpcm import (
    encode_adpcm_channel,
    decode_adpcm_channel,
    decode_adpcm_channel_chunk,
    compute_loop_context,
    adpcm_bytes_for_samples,
)

from pysar.core.format.rstm.reader import BrstmReader
from pysar.core.format.rstm.writer import BrstmWriter
from pysar.core.model.brstm import (
    BrstmData,
    AdpcmChannelInfo,
    TrackInfo,
)
from pysar.core.types import AudioCodec, CANONICAL_ADPCM_COEFS


def _pad32(x: int) -> int:
    return (x + 0x1F) & ~0x1F


def _clip_i16(x: int) -> int:
    return max(-32768, min(32767, x))


def _default_tracks(channel_count: int) -> list[TrackInfo]:
    tracks = []
    for start_channel in range(0, max(0, int(channel_count)), 2):
        channels = list(range(start_channel, min(start_channel + 2, channel_count)))
        tracks.append(TrackInfo(
            channel_count=len(channels),
            left_channel_id=channels[0],
            right_channel_id=channels[-1],
            channel_indices=channels,
        ))
    return tracks


class Brstm(EditorBase):
    def __init__(self, data: BrstmData | None = None):
        super().__init__()
        self._data = data or BrstmData()
        self._decoded_pcm: np.ndarray | None = None

    #
    # Factory methods
    #

    @classmethod
    def open(cls, path: str | Path) -> Self:
        """Open a BRSTM file from disk."""
        reader = BrstmReader()
        data = reader.from_file(str(path))
        return cls(data)

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        """Load a BRSTM from raw bytes."""
        reader = BrstmReader()
        data = reader.read(BytesIO(raw))
        return cls(data)

    @classmethod
    def from_stream(cls, stream: BinaryIO) -> Self:
        """Load a BRSTM from a binary stream."""
        reader = BrstmReader()
        data = reader.read(stream)
        return cls(data)

    @classmethod
    def from_wav(
            cls,
            wav_path: str | Path,
            *,
            codec: AudioCodec = AudioCodec.ADPCM,
            loop_start: int = 0,
            loop_end: int | None = None,
            block_samples: int = 14336,
    ) -> Self:
        """
        Create a BRSTM from a WAV file.

        Args:
            wav_path: Path to input WAV file
            codec: Target codec (ADPCM, PCM16, PCM8)
            loop_start: Loop start sample (0 = no loop if loop_end is None)
            loop_end: Loop end sample (None = end of file)
            block_samples: Samples per block (ADPCM must be multiple of 14)

        Returns:
            New Brstm with encoded audio
        """
        # Read WAV file
        pcm, sample_rate = cls._read_wav(wav_path)
        return cls.from_pcm(
            pcm,
            sample_rate,
            codec=codec,
            loop_start=loop_start,
            loop_end=loop_end,
            block_samples=block_samples,
        )

    @classmethod
    def from_pcm(
            cls,
            pcm: np.ndarray | list,
            sample_rate: int,
            *,
            codec: AudioCodec = AudioCodec.ADPCM,
            loop_start: int = 0,
            loop_end: int | None = None,
            block_samples: int = 14336,
    ) -> Self:
        """
        Create a BRSTM from PCM samples.

        Args:
            pcm: PCM samples as [N, C] array or list of lists
            sample_rate: Sample rate in Hz
            codec: Target codec
            loop_start: Loop start sample (where to jump back to)
            loop_end: Loop end sample / total length (None = full length)
            block_samples: Samples per block

        Returns:
            New Brstm with encoded audio
        """
        # Normalize to numpy array [N, C]
        if isinstance(pcm, np.ndarray):
            x = pcm
            if x.ndim == 1:
                x = x.reshape(-1, 1)
            if x.shape[1] > x.shape[0]:
                x = x.T
            N, C = int(x.shape[0]), int(x.shape[1])
            pcm_ch = [x[:, i].astype(np.int16, copy=False) for i in range(C)]
        else:
            # List handling...
            if isinstance(pcm, list) and pcm and isinstance(pcm[0], list):
                N = len(pcm)
                C = len(pcm[0])
                pcm_ch = [
                    np.array([_clip_i16(int(pcm[i][c])) for i in range(N)], dtype=np.int16)
                    for c in range(C)
                ]
            else:
                arr = [_clip_i16(int(v)) for v in pcm]
                N = len(arr)
                C = 1
                pcm_ch = [np.array(arr, dtype=np.int16)]

        # Determine actual total samples (this is the loop end point)
        if loop_end is None or loop_end > N:
            total_samples = N
        else:
            total_samples = loop_end

        # Truncate PCM data to total_samples
        pcm_ch = [ch[:total_samples] for ch in pcm_ch]

        # Determine if looping is enabled
        # Looping is enabled if loop_start > 0 OR if explicitly set loop_end
        is_looped = loop_start > 0 or (loop_end is not None and loop_end > loop_start)

        # Validate loop_start
        if loop_start >= total_samples:
            loop_start = 0
            is_looped = False

        # Ensure block_samples is valid for ADPCM
        if codec == AudioCodec.ADPCM and block_samples % 14:
            block_samples = max(14, (block_samples // 14) * 14)

        # Encode based on codec
        if codec == AudioCodec.ADPCM:
            data = cls._encode_adpcm(
                pcm_ch, total_samples, C, sample_rate,
                loop_start, is_looped, block_samples
            )
        else:
            data = cls._encode_pcm(
                pcm_ch, total_samples, C, sample_rate, codec,
                loop_start, is_looped, block_samples
            )

        editor = cls(data)
        editor.mark_dirty(DirtyFlags.ALL)
        return editor

    #
    # Properties
    #

    @property
    def data(self) -> BrstmData:
        return self._data

    @property
    def codec(self) -> AudioCodec:
        return self._data.codec

    @property
    def sample_rate(self) -> int:
        return self._data.sample_rate

    @property
    def n_channels(self) -> int:
        return self._data.n_channels

    @property
    def n_samples(self) -> int:
        return self._data.total_samples

    @property
    def is_looped(self) -> bool:
        return self._data.is_looped

    @property
    def loop_start(self) -> int:
        return self._data.loop_start

    @property
    def duration(self) -> float:
        return self._data.duration

    #
    # Decoding
    #

    def decode(self, *, force: bool = False) -> np.ndarray:
        """
        Decode the audio to PCM16.

        Returns:
            numpy array of shape [N, C] with int16 samples
        """
        if self._decoded_pcm is not None and not force:
            return self._decoded_pcm

        N = self._data.total_samples
        C = self._data.n_channels

        if self._data.codec == AudioCodec.ADPCM:
            chans = self._decode_adpcm()
        elif self._data.codec == AudioCodec.PCM16:
            chans = self._decode_pcm16()
        elif self._data.codec == AudioCodec.PCM8:
            chans = self._decode_pcm8()
        else:
            raise ValueError(f"Unknown codec: {self._data.codec}")

        out = np.zeros((N, C), dtype=np.int16)
        for c in range(C):
            out[:, c] = np.asarray(chans[c], dtype=np.int16)[:N]

        self._decoded_pcm = out
        return out

    def decode_to_wav(self, output_path: str | Path) -> Path:
        """Decode and save as a WAV file."""
        output_path = Path(output_path)
        pcm = self.decode()

        N, C = pcm.shape
        data = pcm.astype("<i2", copy=False).tobytes()

        with open(output_path, "wb") as f:
            f.write(b"RIFF")
            f.write((36 + len(data)).to_bytes(4, "little"))
            f.write(b"WAVEfmt ")
            f.write((16).to_bytes(4, "little"))  # fmt size
            f.write((1).to_bytes(2, "little"))  # PCM
            f.write(C.to_bytes(2, "little"))
            f.write(self.sample_rate.to_bytes(4, "little"))
            f.write((self.sample_rate * C * 2).to_bytes(4, "little"))
            f.write((C * 2).to_bytes(2, "little"))
            f.write((16).to_bytes(2, "little"))
            f.write(b"data")
            f.write(len(data).to_bytes(4, "little"))
            f.write(data)

        return output_path

    def iter_decoded_blocks(self, start_frame: int = 0, end_frame: int | None = None):
        """Yield a native-rate PCM16 frame range one BRSTM block at a time.

        PCM streams can jump directly to any block.  ADPCM streams do the same
        when a structurally consistent ADPC seek table is available; otherwise
        they decode preceding blocks to reconstruct the decoder history safely.
        The first and last yielded arrays are trimmed to the requested range.
        """
        data = self._data
        total = max(0, int(data.total_samples))
        start = min(total, max(0, int(start_frame)))
        end = total if end_frame is None else min(total, max(0, int(end_frame)))
        if total <= 0 or end <= start:
            return
        block_count = max(1, int(data.block_count or 0))
        block_samples = max(1, int(data.block_samples or total))
        first_block = start // block_samples
        last_block = (end - 1) // block_samples

        if self._decoded_pcm is not None:
            for block_index in range(first_block, last_block + 1):
                block_start = block_index * block_samples
                take_start = max(start, block_start)
                take_end = min(end, block_start + block_samples)
                yield self._decoded_pcm[take_start:take_end]
            return

        channels = int(data.n_channels)
        decode_from = first_block
        histories: list[tuple[int, int]] = []
        if data.codec == AudioCodec.ADPCM:
            histories = self._adpc_histories_for_block(first_block, block_count, block_samples) or []
            if not histories:
                decode_from = 0
                histories = [
                    (int(info.init_hist1), int(info.init_hist2))
                    for info in data.adpcm_channels
                ]

        for block_index in range(decode_from, min(block_count, last_block + 1)):
            block_start = block_index * block_samples
            remaining = total - block_start
            if remaining <= 0:
                break
            final = block_index == block_count - 1
            samples = min(
                remaining,
                max(0, int(data.final_block_samples)) if final else block_samples,
            )
            if samples <= 0:
                samples = min(remaining, block_samples)
            byte_count = max(0, int(data.final_block_size)) if final else max(0, int(data.block_size))
            byte_offset = block_index * max(0, int(data.block_size))
            out = np.zeros((samples, channels), dtype=np.int16)

            for channel in range(channels):
                raw = data.channel_data[channel]
                payload = raw[byte_offset:byte_offset + byte_count]
                if data.codec == AudioCodec.ADPCM:
                    h1, h2 = histories[channel]
                    pcm, h1, h2 = decode_adpcm_channel_chunk(
                        payload,
                        data.adpcm_channels[channel].coefs,
                        h1,
                        h2,
                        samples,
                    )
                    histories[channel] = (h1, h2)
                elif data.codec == AudioCodec.PCM16:
                    pcm = np.frombuffer(payload, dtype=">i2", count=min(samples, len(payload) // 2))
                elif data.codec == AudioCodec.PCM8:
                    unsigned = np.frombuffer(payload, dtype=np.uint8, count=min(samples, len(payload)))
                    signed = unsigned.astype(np.int16)
                    signed[signed > 127] -= 256
                    pcm = signed << 8
                else:
                    raise ValueError(f"Unknown codec: {data.codec}")
                count = min(samples, len(pcm))
                if count:
                    out[:count, channel] = pcm[:count]
            take_start = max(0, start - block_start)
            take_end = min(samples, end - block_start)
            if take_end > take_start:
                yield out[take_start:take_end]

    def _adpc_histories_for_block(
            self,
            block_index: int,
            block_count: int,
            block_samples: int,
    ) -> list[tuple[int, int]] | None:
        """Return a trustworthy ADPC row, or ``None`` for sequential fallback."""
        data = self._data
        channels = int(data.n_channels)
        if block_index == 0:
            if len(data.adpcm_channels) < channels:
                return None
            return [
                (int(info.init_hist1), int(info.init_hist2))
                for info in data.adpcm_channels[:channels]
            ]
        if (
                int(data.adpc_table_interval) != block_samples
                or int(data.adpc_bytes_per_entry) != 4
                or len(data.adpc_entries) < block_count
                or len(data.adpcm_channels) < channels
        ):
            return None

        initial = data.adpc_entries[0]
        row = data.adpc_entries[block_index] if block_index < len(data.adpc_entries) else []
        if len(initial) != channels or len(row) != channels:
            return None
        expected_initial = [
            (int(info.init_hist1), int(info.init_hist2))
            for info in data.adpcm_channels[:channels]
        ]
        parsed_initial = [(int(h1), int(h2)) for h1, h2 in initial]
        if parsed_initial != expected_initial:
            return None

        parsed_row = [(int(h1), int(h2)) for h1, h2 in row]
        if any(not (-32768 <= value <= 32767) for pair in parsed_row for value in pair):
            return None
        return parsed_row

    #
    # Saving
    #

    def to_bytes(self) -> bytes:
        """Serialize to BRSTM bytes."""
        writer = BrstmWriter()
        return writer.to_bytes(self._data)

    def save(self, path: str | Path) -> Self:
        """Save to a BRSTM file."""
        Path(path).write_bytes(self.to_bytes())
        self.clear_dirty()
        return self

    #
    # Private decode methods
    #

    def _decode_adpcm(self) -> list[np.ndarray]:
        chans = []
        for ch in range(self._data.n_channels):
            ch_info = self._data.adpcm_channels[ch]
            pcm = decode_adpcm_channel(
                self._data.channel_data[ch],
                ch_info.coefs,
                ch_info.init_hist1,
                ch_info.init_hist2,
                self._data.total_samples,
            )
            chans.append(pcm)
        return chans

    def _decode_pcm16(self) -> list[np.ndarray]:
        chans = []
        for ch in range(self._data.n_channels):
            buf = self._data.channel_data[ch]
            arr = np.frombuffer(buf, dtype=">i2").astype(np.int16, copy=False)
            chans.append(arr[:self._data.total_samples])
        return chans

    def _decode_pcm8(self) -> list[np.ndarray]:
        chans = []
        for ch in range(self._data.n_channels):
            b = np.frombuffer(self._data.channel_data[ch], dtype=np.uint8)[:self._data.total_samples]
            s8 = b.astype(np.int16)
            s8[s8 > 127] -= 256
            chans.append((s8 << 8).astype(np.int16))
        return chans

    # ===================
    # Private encode methods
    # ===================

    @classmethod
    def _read_wav(cls, path: str | Path) -> tuple[np.ndarray, int]:
        """Read WAV file and return (pcm array, sample_rate)."""
        with wave.open(str(path), "rb") as w:
            nch = w.getnchannels()
            sr = w.getframerate()
            sw = w.getsampwidth()
            n = w.getnframes()
            raw = w.readframes(n)

        if sw == 2:
            x = np.frombuffer(raw, dtype="<i2")
        elif sw == 1:
            u8 = np.frombuffer(raw, dtype=np.uint8)
            x = ((u8.astype(np.int16) - 128) << 8).astype(np.int16)
        elif sw == 3:
            b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
            x32 = (b[:, 0].astype(np.int32) |
                   (b[:, 1].astype(np.int32) << 8) |
                   (b[:, 2].astype(np.int32) << 16))
            x32 = (x32 << 8) >> 8  # Sign extend
            x = (x32 >> 8).astype(np.int16)
        elif sw == 4:
            x32 = np.frombuffer(raw, dtype="<i4")
            x = (x32 >> 16).astype(np.int16)
        else:
            raise NotImplementedError(f"Unsupported WAV sample width: {sw}")

        return x.reshape(-1, nch), sr

    @classmethod
    def _encode_adpcm(
            cls,
            pcm_ch: list[np.ndarray],
            total_samples: int,
            n_channels: int,
            sample_rate: int,
            loop_start: int,
            is_looped: bool,
            block_samples: int,
    ) -> BrstmData:
        N = total_samples
        C = n_channels

        adpcm_by_ch = []
        per_frame_hist = []
        init_ps_list = []
        init_hist_list = []
        loop_ps_list = []
        loop_hist_list = []

        for ch in range(C):
            adpcm_u8, ips, ih1, ih2, lps, fh = encode_adpcm_channel(pcm_ch[ch])

            adpcm_by_ch.append(adpcm_u8.tobytes())
            per_frame_hist.append(fh)
            init_ps_list.append(int(ips))
            init_hist_list.append((int(ih1), int(ih2)))

            # Compute loop context
            if is_looped and loop_start > 0:
                ps, lh1, lh2 = compute_loop_context(adpcm_u8.tobytes(), loop_start)
                loop_ps_list.append(ps)
                loop_hist_list.append((lh1, lh2))
            else:
                loop_ps_list.append(int(ips))
                loop_hist_list.append((int(ih1), int(ih2)))

        # Calculate block structure
        full_blocks = N // block_samples
        leftover = N - full_blocks * block_samples
        block_count = full_blocks + (1 if leftover > 0 else 0)
        block_size = adpcm_bytes_for_samples(block_samples)
        final_block_samples = leftover if leftover > 0 else block_samples
        final_block_size = adpcm_bytes_for_samples(final_block_samples)

        # Interleave blocks
        data_payload = cls._split_blocks(adpcm_by_ch, block_size, final_block_size, block_count, C)

        # Build ADPC entries
        adpc_entries = []
        adpc_entries.append([(h[0], h[1]) for h in init_hist_list])
        for bi in range(1, block_count):
            end_prev = bi * block_samples
            frames_prev = (end_prev + 13) // 14
            row = []
            for c in range(C):
                fh = per_frame_hist[c]
                if frames_prev > 0 and frames_prev <= len(fh):
                    h1 = int(fh[frames_prev - 1, 0])
                    h2 = int(fh[frames_prev - 1, 1])
                else:
                    h1, h2 = init_hist_list[c]
                row.append((h1, h2))
            adpc_entries.append(row)

        # Build channel info
        adpcm_channels = []
        for c in range(C):
            adpcm_channels.append(AdpcmChannelInfo(
                coefs=CANONICAL_ADPCM_COEFS,
                gain=0,
                init_ps=init_ps_list[c],
                init_hist1=init_hist_list[c][0],
                init_hist2=init_hist_list[c][1],
                loop_ps=loop_ps_list[c],
                loop_hist1=loop_hist_list[c][0],
                loop_hist2=loop_hist_list[c][1],
            ))

        return BrstmData(
            codec=AudioCodec.ADPCM,
            is_looped=is_looped,
            n_channels=C,
            sample_rate=sample_rate,
            loop_start=loop_start,
            total_samples=N,  # This IS the loop end / total length
            block_count=block_count,
            block_size=block_size,
            block_samples=block_samples,
            final_block_size=final_block_size,
            final_block_samples=final_block_samples,
            final_block_size_padded=_pad32(final_block_size),
            adpc_table_interval=block_samples,
            # NW4R defines this as the size per channel, not the full row.
            adpc_bytes_per_entry=4,
            adpcm_channels=adpcm_channels,
            tracks=_default_tracks(C),
            channel_data=adpcm_by_ch,
            adpc_entries=adpc_entries,
            data_payload=data_payload,
        )

    @classmethod
    def _encode_pcm(
            cls,
            pcm_ch: list[np.ndarray],
            total_samples: int,
            n_channels: int,
            sample_rate: int,
            codec: AudioCodec,
            loop_start: int,
            is_looped: bool,
            block_samples: int,
    ) -> BrstmData:
        N = total_samples
        C = n_channels

        if codec == AudioCodec.PCM16:
            per_ch_bytes = [np.asarray(ch, dtype=">i2").tobytes() for ch in pcm_ch]
            bytes_per_sample = 2
        else:  # PCM8
            per_ch_bytes = []
            for ch in pcm_ch:
                s8 = (np.asarray(ch, dtype=np.int16) >> 8)
                s8 = np.clip(s8, -128, 127).astype(np.int8)
                per_ch_bytes.append(s8.tobytes())
            bytes_per_sample = 1

        full_blocks = N // block_samples if block_samples > 0 else 0
        leftover = N - full_blocks * block_samples
        block_count = full_blocks + (1 if leftover > 0 else 0)
        block_size = block_samples * bytes_per_sample
        final_block_samples = leftover if leftover > 0 else block_samples
        final_block_size = final_block_samples * bytes_per_sample

        data_payload = cls._split_blocks(per_ch_bytes, block_size, final_block_size, block_count, C)

        return BrstmData(
            codec=codec,
            is_looped=is_looped,
            n_channels=C,
            sample_rate=sample_rate,
            loop_start=loop_start,
            total_samples=N,
            block_count=block_count,
            block_size=block_size,
            block_samples=block_samples,
            final_block_size=final_block_size,
            final_block_samples=final_block_samples,
            final_block_size_padded=_pad32(final_block_size),
            adpc_table_interval=0,
            adpc_bytes_per_entry=0,
            adpcm_channels=[],
            tracks=_default_tracks(C),
            channel_data=per_ch_bytes,
            adpc_entries=[],
            data_payload=data_payload,
        )

    @staticmethod
    def _split_blocks(
            per_ch_bytes: list[bytes],
            block_size: int,
            final_block_size: int,
            block_count: int,
            n_channels: int,
    ) -> bytes:
        parts = []
        pos = [0] * n_channels

        for bi in range(block_count):
            this_sz = block_size if bi < block_count - 1 else final_block_size
            for c in range(n_channels):
                block = per_ch_bytes[c][pos[c]:pos[c] + this_sz]
                parts.append(block)
                if bi == block_count - 1:
                    parts.append(b"\x00" * (_pad32(final_block_size) - len(block)))
                pos[c] += this_sz

        return b"".join(parts)

    def __str__(self) -> str:
        loop_info = f", loop={self.loop_start}" if self.is_looped else ""
        return (
            f"Brstm({self.codec.name}, {self.sample_rate}Hz, "
            f"{self.n_channels}ch, {self.n_samples} samples{loop_info})"
        )

    def __repr__(self) -> str:
        return self.__str__()
