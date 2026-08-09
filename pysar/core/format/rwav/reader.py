import struct
from typing import BinaryIO

from pysar.core.base import ReaderBase
from pysar.core.types import FileTag, AudioCodec
from pysar.io.binary import read_file_header, read_section_header
from pysar.core.model.brwav import (
    BrwavData,
    WaveInfo,
    ChannelInfo,
    AdpcmParams,
    LocationType,
)


class BrwavReader(ReaderBase):
    EXPECTED_MAGIC = FileTag.BRWAV
    SUPPORTED_VERSIONS = {0x0100, 0x0101, 0x0102}

    def read(self, data: BinaryIO) -> BrwavData:
        """Read a BRWAV file from a binary stream."""
        base_offset = data.tell()

        header = read_file_header(data)
        self._validate(header)

        info_offset, info_size, data_offset, data_size = struct.unpack(
            ">IIII", data.read(16)
        )

        # Parse INFO section
        data.seek(base_offset + info_offset)
        wave_info = self._read_info_section(data)

        # Parse DATA section
        data.seek(base_offset + data_offset)
        sample_data = self._read_data_section(data)

        # Capture raw bytes for potential pass-through
        data.seek(base_offset)
        raw_bytes = data.read(header.file_size)

        # Restore stream position
        data.seek(base_offset + header.file_size)

        return BrwavData(
            version=header.version,
            file_size=header.file_size,
            info_offset=info_offset,
            info_size=info_size,
            data_offset=data_offset,
            data_size=data_size,
            wave_info=wave_info,
            sample_data=sample_data,
            raw_bytes=raw_bytes,
        )

    def _read_info_section(self, data: BinaryIO) -> WaveInfo:
        section_header = read_section_header(data)
        if section_header.magic != "INFO":
            raise ValueError(f"Expected INFO section, got {section_header.magic}")

        return self._read_wave_info(data)

    def _read_wave_info(self, data: BinaryIO) -> WaveInfo:
        wave_base = data.tell()

        # Read wave info header (28 bytes)
        (
            encoding,
            is_looped,
            n_channels,
            sample_rate_ext,
            sample_rate,
            location_type,
            _pad,
            loop_start,
            n_samples,
            channel_table_offset,
            data_location,
            _reserved,
        ) = struct.unpack(">BBBBHBBIIIII", data.read(28))

        wave_info = WaveInfo(
            encoding=AudioCodec(encoding),
            is_looped=bool(is_looped),
            n_channels=n_channels,
            sample_rate=sample_rate,
            sample_rate_ext=sample_rate_ext,
            location_type=LocationType(location_type),
            loop_start=loop_start,
            n_samples=n_samples,
            channel_table_offset=channel_table_offset,
            data_location=data_location,
        )

        # Read channel table
        data.seek(wave_base + channel_table_offset)
        channel_offsets = struct.unpack(f">{n_channels}I", data.read(n_channels * 4))

        # Read channel info for each channel
        for offset in channel_offsets:
            data.seek(wave_base + offset)
            channel = self._read_channel_info(data)
            wave_info.channels.append(channel)

        # Read ADPCM params for each channel
        for channel in wave_info.channels:
            if wave_info.location_type == LocationType.OFFSET:
                data.seek(wave_base + channel.adpcm_offset)
            else:
                data.seek(channel.adpcm_offset)

            adpcm = self._read_adpcm_params(data)
            wave_info.adpcm_params.append(adpcm)

        return wave_info

    def _read_channel_info(self, data: BinaryIO) -> ChannelInfo:
        (
            data_offset,
            adpcm_offset,
            volume_fl,
            volume_fr,
            volume_bl,
            volume_br,
            _reserved,
        ) = struct.unpack(">IIIIIII", data.read(28))

        return ChannelInfo(
            data_offset=data_offset,
            adpcm_offset=adpcm_offset,
            volume_fl=volume_fl,
            volume_fr=volume_fr,
            volume_bl=volume_bl,
            volume_br=volume_br,
        )

    def _read_adpcm_params(self, data: BinaryIO) -> AdpcmParams:
        coefs = struct.unpack(">16h", data.read(32))
        (
            gain,
            pred_scale,
            yn1,
            yn2,
            loop_pred_scale,
            loop_yn1,
            loop_yn2,
            _reserved,
        ) = struct.unpack(">hhhhhhhh", data.read(16))

        return AdpcmParams(
            coefs=coefs,
            gain=gain,
            pred_scale=pred_scale,
            yn1=yn1,
            yn2=yn2,
            loop_pred_scale=loop_pred_scale,
            loop_yn1=loop_yn1,
            loop_yn2=loop_yn2,
        )

    def _read_data_section(self, data: BinaryIO) -> bytes:
        section_header = read_section_header(data)
        if section_header.magic != "DATA":
            raise ValueError(f"Expected DATA section, got {section_header.magic}")

        # Read sample data (section size minus 8-byte header)
        return data.read(section_header.size - 8)
