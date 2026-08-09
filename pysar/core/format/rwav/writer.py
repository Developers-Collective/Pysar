import struct
from typing import BinaryIO

from pysar.core.base import WriterBase, NW4RFileHeader, NW4RSectionHeader
from pysar.core.model.brwav import BrwavData, WaveInfo, ChannelInfo, AdpcmParams
from pysar.core.types import FileTag, ByteOrder, AudioCodec
from pysar.io.binary import (
    write_file_header,
    write_section_header,
    align_up,
)


class BrwavWriter(WriterBase):
    VERSION = 0x0102
    HEADER_SIZE = 0x20  # 32 bytes

    def write(self, model: BrwavData, output: BinaryIO) -> None:
        """Write a BRWAV file to a binary stream."""

        # Calculate sizes
        info_size = self._calculate_info_size(model.wave_info)
        # DATA sections, like the INFO section and the complete RWAV, are
        # 0x20-aligned.  Keep codec output tightly packed and put structural
        # padding here, after all encoded packets.  Padding inside the ADPCM
        # stream shifts every packet that follows it and corrupts playback.
        data_size = align_up(len(model.sample_data) + 8, 0x20)

        info_offset = self.HEADER_SIZE
        data_offset = info_offset + info_size
        file_size = data_offset + data_size

        # Write file header
        header = NW4RFileHeader(
            magic=FileTag.BRWAV,
            byte_order=ByteOrder.BIG_ENDIAN,
            version=model.version or self.VERSION,
            file_size=file_size,
            header_size=self.HEADER_SIZE,
            n_sections=2,
        )
        write_file_header(header, output)

        # Write section table
        output.write(
            struct.pack(">IIII", info_offset, info_size, data_offset, data_size)
        )

        self._write_info_section(model.wave_info, info_size, data_offset, output)
        self._write_data_section(model.sample_data, data_size, output)

    def _calculate_info_size(self, wave_info: WaveInfo) -> int:
        n_channels = wave_info.n_channels
        has_adpcm = wave_info.encoding == AudioCodec.ADPCM

        base_size = (
                8  # INFO section header
                + 28  # WaveInfo
                + n_channels * 4  # Channel table
                + n_channels * 28  # ChannelInfo structures
        )

        # Only include ADPCM params for ADPCM encoding
        if has_adpcm:
            base_size += n_channels * 48  # AdpcmParams structures

        # Align to 32 bytes
        return align_up(base_size, 0x20)

    def _write_info_section(
            self, wave_info: WaveInfo, info_size: int, data_offset: int, output: BinaryIO
    ) -> None:
        section_start = output.tell()
        has_adpcm = wave_info.encoding == AudioCodec.ADPCM

        # Write section header
        section_header = NW4RSectionHeader(magic="INFO", size=info_size)
        write_section_header(section_header, output)

        # Write wave info
        wave_info_start = output.tell()
        self._write_wave_info(wave_info, data_offset, output)

        # Write channel table
        output.seek(wave_info_start + wave_info.channel_table_offset)
        n_channels = wave_info.n_channels

        # Channel info starts after: WaveInfo(28) + ChannelTable(n*4)
        channel_info_base = 0x1C + n_channels * 4

        for i in range(n_channels):
            channel_offset = channel_info_base + i * 0x1C
            output.write(struct.pack(">I", channel_offset))

        # Write channel info structures
        for i in range(n_channels):
            channel = wave_info.channels[i] if i < len(wave_info.channels) else ChannelInfo()
            output.seek(wave_info_start + channel_info_base + i * 0x1C)
            self._write_channel_info(channel, wave_info, i, has_adpcm, output)

        # Write ADPCM params (only for ADPCM encoding)
        if has_adpcm:
            adpcm_base = channel_info_base + n_channels * 0x1C
            for i in range(n_channels):
                adpcm = wave_info.adpcm_params[i] if i < len(wave_info.adpcm_params) else AdpcmParams()
                output.seek(wave_info_start + adpcm_base + i * 0x30)
                self._write_adpcm_params(adpcm, output)

        # Pad to alignment
        output.seek(section_start + info_size)

    def _write_wave_info(
            self, wave_info: WaveInfo, data_offset: int, output: BinaryIO
    ) -> None:
        wave_info_offset = output.tell()
        # For a standalone RWAV, NW4R treats dataLocation as an offset from
        # WaveInfo to the DATA block header, then advances past that header.
        # Writing the absolute file offset makes the Wii resolve beyond DATA.
        data_location = data_offset - wave_info_offset
        if not 0 <= data_location <= 0xFFFFFFFF:
            raise ValueError("RWAV DATA section cannot be addressed from WaveInfo")
        output.write(
            struct.pack(
                ">BBBBHBBIIIII",
                wave_info.encoding.value,
                1 if wave_info.is_looped else 0,
                wave_info.n_channels,
                wave_info.sample_rate_ext,
                wave_info.sample_rate,
                wave_info.location_type.value,
                0,  # padding
                wave_info.loop_start,
                wave_info.n_samples,
                wave_info.channel_table_offset,
                data_location,
                0,  # reserved
            )
        )

    def _write_channel_info(
            self,
            channel: ChannelInfo,
            wave_info: WaveInfo,
            index: int,
            has_adpcm: bool,
            output: BinaryIO,
    ) -> None:
        if has_adpcm:
            # Calculate ADPCM offset relative to wave info start
            adpcm_offset = (
                    0x1C  # WaveInfo size
                    + wave_info.n_channels * 4  # Channel table
                    + wave_info.n_channels * 0x1C  # All ChannelInfo structures
                    + index * 0x30  # This channel's ADPCM params
            )
        else:
            # No ADPCM params for PCM formats
            adpcm_offset = 0

        output.write(
            struct.pack(
                ">IIIIIII",
                channel.data_offset,
                adpcm_offset,
                channel.volume_fl,
                channel.volume_fr,
                channel.volume_bl,
                channel.volume_br,
                0,  # reserved
            )
        )

    def _write_adpcm_params(self, adpcm: AdpcmParams, output: BinaryIO) -> None:
        output.write(struct.pack(">16h", *adpcm.coefs))
        output.write(
            struct.pack(
                ">hhhhhhhh",
                adpcm.gain,
                adpcm.pred_scale,
                adpcm.yn1,
                adpcm.yn2,
                adpcm.loop_pred_scale,
                adpcm.loop_yn1,
                adpcm.loop_yn2,
                0,  # reserved
            )
        )

    def _write_data_section(
            self, sample_data: bytes, data_size: int, output: BinaryIO
    ) -> None:
        section_header = NW4RSectionHeader(magic="DATA", size=data_size)
        write_section_header(section_header, output)
        output.write(sample_data)
        padding = data_size - 8 - len(sample_data)
        if padding > 0:
            output.write(b"\x00" * padding)
