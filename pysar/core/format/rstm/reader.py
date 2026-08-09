import struct
from typing import BinaryIO

from pysar.core.base import ReaderBase
from pysar.core.exceptions import NW4RInvalidFileError
from pysar.core.model.brstm import (
    BrstmData,
    AdpcmChannelInfo,
    TrackInfo,
)
from pysar.core.types import AudioCodec, FileTag

# holy smokes! this is NOT canonic python code!!!!
# yes because I stole big parts of it from BrawlCrate and openrevolution
# and I cannot be bothered to make it any better, it's good enough, idc anymore man

class BrstmReader(ReaderBase):
    EXPECTED_MAGIC = FileTag.BRSTM
    SUPPORTED_VERSIONS = {0x0100, 0x0101, 0x0102}

    def read(self, data: BinaryIO) -> BrstmData:
        """Read a BRSTM file from a binary stream."""
        base_offset = data.tell()

        # Read file header (0x40 bytes)
        hdr = data.read(0x40)
        if len(hdr) < 0x40 or hdr[:4] != b"RSTM":
            raise NW4RInvalidFileError("Not a valid BRSTM file")

        (
            bom, version, file_size, header_size, n_chunks,
            off_head, sz_head,
            off_adpc, sz_adpc,
            off_data, sz_data,
        ) = struct.unpack(">HHIHHIIIIII", hdr[4:40])

        # Read HEAD chunk
        data.seek(base_offset + off_head)
        head = data.read(sz_head)
        if head[:4] != b"HEAD":
            raise NW4RInvalidFileError("HEAD chunk missing")

        # Parse HEAD sections
        BASE = 0x08
        _, off_p1, _, off_p2, _, off_p3 = struct.unpack(">IIIIII", head[0x08:0x20])
        p1_off = off_p1 + BASE
        p2_off = off_p2 + BASE
        p3_off = off_p3 + BASE

        # Part 1: Stream info
        p1 = head[p1_off:]
        codec = AudioCodec(p1[0])
        loop_flag = p1[1]
        n_channels = p1[2]
        sample_rate = struct.unpack(">H", p1[4:6])[0]
        loop_start = struct.unpack(">I", p1[8:12])[0]
        total_samples = struct.unpack(">I", p1[12:16])[0]
        block_count = struct.unpack(">I", p1[20:24])[0]
        block_size = struct.unpack(">I", p1[24:28])[0]
        block_samples = struct.unpack(">I", p1[28:32])[0]
        final_block_size = struct.unpack(">I", p1[32:36])[0]
        final_block_samples = struct.unpack(">I", p1[36:40])[0]
        final_block_size_padded = struct.unpack(">I", p1[40:44])[0]
        adpc_interval = struct.unpack(">I", p1[44:48])[0]
        adpc_bpe = struct.unpack(">I", p1[48:52])[0]

        # Part 2: actual track-to-channel mappings. TrackInfoEx also carries
        # the per-track volume/pan applied by NW4R's StrmPlayer.
        tracks: list[TrackInfo] = []
        track_table_valid = False
        if 0 <= p2_off <= len(head) - 4:
            track_count = head[p2_off]
            track_data_type = head[p2_off + 1]
            refs_end = p2_off + 4 + track_count * 8
            if track_count > 0 and track_data_type in {0, 1} and refs_end <= len(head):
                parsed_tracks: list[TrackInfo] = []
                track_table_valid = True
                for track_index in range(track_count):
                    ref_offset = p2_off + 4 + track_index * 8
                    descriptor_offset = struct.unpack(">I", head[ref_offset + 4:ref_offset + 8])[0] + BASE
                    prefix_size = 1 if track_data_type == 0 else 9
                    if not 0 <= descriptor_offset <= len(head) - prefix_size:
                        track_table_valid = False
                        break
                    if track_data_type == 0:
                        volume, pan = 0x7F, 0x40
                        channel_count = head[descriptor_offset]
                        channel_offset = descriptor_offset + 1
                    else:
                        volume = head[descriptor_offset]
                        pan = head[descriptor_offset + 1]
                        channel_count = head[descriptor_offset + 8]
                        channel_offset = descriptor_offset + 9
                    if (
                        channel_count <= 0
                        or channel_offset + channel_count > len(head)
                    ):
                        track_table_valid = False
                        break
                    channels = [int(value) for value in head[channel_offset:channel_offset + channel_count]]
                    if any(channel < 0 or channel >= n_channels for channel in channels):
                        track_table_valid = False
                        break
                    parsed_tracks.append(TrackInfo(
                        volume=int(volume),
                        pan=int(pan),
                        channel_count=len(channels),
                        left_channel_id=channels[0],
                        right_channel_id=channels[1] if len(channels) > 1 else channels[0],
                        channel_indices=channels,
                    ))
                if track_table_valid:
                    tracks = parsed_tracks
        if not track_table_valid:
            tracks = []
            for start_channel in range(0, n_channels, 2):
                channels = list(range(start_channel, min(start_channel + 2, n_channels)))
                tracks.append(TrackInfo(
                    channel_count=len(channels),
                    left_channel_id=channels[0],
                    right_channel_id=channels[-1],
                    channel_indices=channels,
                ))

        # Part 3: Channel info (ADPCM)
        adpcm_channels = []
        if codec == AudioCodec.ADPCM:
            p3 = head[p3_off:]
            ch_count = p3[0]

            for ci in range(ch_count):
                entry = p3[4 + ci * 8: 4 + (ci + 1) * 8]
                _, off_chinfo_rel = struct.unpack(">II", entry)
                chinfo_off = off_chinfo_rel + BASE
                ch = head[chinfo_off:]

                _, off_coefs_rel = struct.unpack(">II", ch[:8])
                coefs_off = off_coefs_rel + BASE
                raw_coefs = head[coefs_off:coefs_off + 0x20]
                coefs_flat = struct.unpack(">16h", raw_coefs)
                coefs = tuple((coefs_flat[i], coefs_flat[i + 1]) for i in range(0, 16, 2))

                gain = struct.unpack(">H", ch[0x28:0x2A])[0]
                init_ps = struct.unpack(">H", ch[0x2A:0x2C])[0]
                init_h1 = struct.unpack(">h", ch[0x2C:0x2E])[0]
                init_h2 = struct.unpack(">h", ch[0x2E:0x30])[0]
                loop_ps = struct.unpack(">H", ch[0x30:0x32])[0]
                loop_h1 = struct.unpack(">h", ch[0x32:0x34])[0]
                loop_h2 = struct.unpack(">h", ch[0x34:0x36])[0]

                adpcm_channels.append(AdpcmChannelInfo(
                    coefs=coefs,
                    gain=gain,
                    init_ps=init_ps,
                    init_hist1=init_h1,
                    init_hist2=init_h2,
                    loop_ps=loop_ps,
                    loop_hist1=loop_h1,
                    loop_hist2=loop_h2,
                ))

        # ADPC contains the decoder histories at the start of every audio
        # block.  NW4R's StrmFileLoader indexes this table directly when a
        # stream starts in the middle, so retaining it lets us do the same
        # without decoding every preceding block first.
        adpc_entries: list[list[tuple[int, int]]] = []
        if codec == AudioCodec.ADPCM and off_adpc and sz_adpc >= 8:
            data.seek(base_offset + off_adpc)
            adpc = data.read(sz_adpc)
            if len(adpc) >= 8 and adpc[:4] == b"ADPC":
                declared_size = struct.unpack(">I", adpc[4:8])[0]
                # A block may be padded in the file, while its own size field
                # stops before that padding.  Never consume beyond either.
                payload_end = min(len(adpc), max(8, declared_size))
                payload = adpc[8:payload_end]
                row_size = n_channels * 4
                required_size = block_count * row_size
                if row_size > 0 and len(payload) >= required_size:
                    for block_index in range(block_count):
                        row_offset = block_index * row_size
                        row = []
                        for channel in range(n_channels):
                            entry_offset = row_offset + channel * 4
                            row.append(struct.unpack(">hh", payload[entry_offset:entry_offset + 4]))
                        adpc_entries.append(row)

        # Read DATA chunk
        data.seek(base_offset + off_data)
        data_hdr = data.read(0x20)
        if data_hdr[:4] != b"DATA":
            raise NW4RInvalidFileError("DATA chunk missing")

        data_payload = data.read(sz_data - 0x20)

        # De-interleave blocks into per-channel data
        channel_data = [bytearray() for _ in range(n_channels)]
        pos = 0
        for bi in range(block_count):
            is_final = bi == block_count - 1
            this_block_size = final_block_size if is_final else block_size
            stored_block_size = max(final_block_size, final_block_size_padded) if is_final else block_size
            for ch in range(n_channels):
                channel_data[ch] += data_payload[pos:pos + this_block_size]
                pos += stored_block_size

        # Capture raw bytes
        data.seek(base_offset)
        raw_bytes = data.read(file_size)

        return BrstmData(
            version=version,
            file_size=file_size,
            codec=codec,
            is_looped=bool(loop_flag),
            n_channels=n_channels,
            sample_rate=sample_rate,
            loop_start=loop_start,
            total_samples=total_samples,
            block_count=block_count,
            block_size=block_size,
            block_samples=block_samples,
            final_block_size=final_block_size,
            final_block_samples=final_block_samples,
            final_block_size_padded=final_block_size_padded,
            adpc_table_interval=adpc_interval,
            adpc_bytes_per_entry=adpc_bpe,
            adpcm_channels=adpcm_channels,
            tracks=tracks,
            channel_data=[bytes(ch) for ch in channel_data],
            adpc_entries=adpc_entries,
            raw_bytes=raw_bytes,
        )
