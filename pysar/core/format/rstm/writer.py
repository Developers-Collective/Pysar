import io
import struct
from typing import BinaryIO

from pysar.core.base import WriterBase
from pysar.core.model.brstm import BrstmData
from pysar.core.types import AudioCodec, CANONICAL_ADPCM_COEFS


def _pad32(x: int) -> int:
    return (x + 0x1F) & ~0x1F

# holy smokes! this is NOT canonic python code!!!!
# yes because I stole big parts of it from BrawlCrate and openrevolution
# and I cannot be bothered to make it any better, it's good enough, idc anymore man

class BrstmWriter(WriterBase):
    def write(self, model: BrstmData, output: BinaryIO) -> None:
        """Write a BRSTM file to a binary stream."""
        C = model.n_channels

        # Build HEAD chunk
        head = io.BytesIO()
        head.write(b"HEAD")
        head.write(b"\x00\x00\x00\x00")  # Size placeholder

        BASE = 0x08
        head.write(struct.pack(">I", 0x01000000))  # Marker
        off_p1_pos = head.tell()
        head.write(struct.pack(">I", 0))  # Part 1 offset placeholder
        head.write(struct.pack(">I", 0x01000000))
        off_p2_pos = head.tell()
        head.write(struct.pack(">I", 0))  # Part 2 offset placeholder
        head.write(struct.pack(">I", 0x01000000))
        off_p3_pos = head.tell()
        head.write(struct.pack(">I", 0))  # Part 3 offset placeholder

        # Part 1: Stream info
        off_p1 = head.tell()
        p1 = io.BytesIO()
        p1.write(bytes([model.codec & 0xFF]))
        p1.write(bytes([1 if model.is_looped else 0]))
        p1.write(bytes([model.n_channels & 0xFF]))
        p1.write(b"\x00")
        p1.write(struct.pack(">H", model.sample_rate))
        p1.write(b"\x00\x00")
        p1.write(struct.pack(">I", model.loop_start))
        p1.write(struct.pack(">I", model.total_samples))
        p1_data_abs_pos = p1.tell()
        p1.write(struct.pack(">I", 0))  # Data offset placeholder
        p1.write(struct.pack(">I", model.block_count))
        p1.write(struct.pack(">I", model.block_size))
        p1.write(struct.pack(">I", model.block_samples))
        p1.write(struct.pack(">I", model.final_block_size))
        p1.write(struct.pack(">I", model.final_block_samples))
        p1.write(struct.pack(">I", model.final_block_size_padded))
        p1.write(struct.pack(">I", model.adpc_table_interval))
        p1.write(struct.pack(">I", model.adpc_bytes_per_entry))
        head.write(p1.getvalue())

        # Part 2: Track info
        off_p2 = head.tell()
        track_rows = []
        for track in model.tracks:
            channels = (
                track.resolved_channel_indices()
                if hasattr(track, "resolved_channel_indices")
                else [track.left_channel_id, track.right_channel_id][:max(1, int(track.channel_count))]
            )
            channels = [int(channel) for channel in channels]
            if not channels or any(channel < 0 or channel >= C for channel in channels):
                continue
            track_rows.append((
                max(0, min(0xFF, int(track.volume))),
                max(0, min(0xFF, int(track.pan))),
                channels[:0xFF],
            ))
        if not track_rows:
            for start_channel in range(0, C, 2):
                channels = list(range(start_channel, min(start_channel + 2, C)))
                if channels:
                    track_rows.append((0x7F, 0x40, channels))
        track_rows = track_rows[:0xFF]

        p2 = io.BytesIO()
        p2.write(bytes([len(track_rows)]))
        p2.write(bytes([1]))  # TrackInfoEx retains volume and pan.
        p2.write(b"\x00\x00")
        track_ref_positions = []
        for _ in track_rows:
            p2.write(bytes([1, 1]))
            p2.write(b"\x00\x00")
            track_ref_positions.append(p2.tell())
            p2.write(struct.pack(">I", 0))

        track_descriptor_offsets = []
        for volume, pan, channels in track_rows:
            while p2.tell() % 4:
                p2.write(b"\x00")
            track_descriptor_offsets.append(p2.tell())
            p2.write(bytes([volume, pan]))
            p2.write(b"\x00\x00")
            p2.write(b"\x00\x00\x00\x00")
            p2.write(bytes([len(channels)]))
            p2.write(bytes(channels))

        part2 = bytearray(p2.getvalue())
        for ref_position, descriptor_offset in zip(track_ref_positions, track_descriptor_offsets):
            struct.pack_into(">I", part2, ref_position, (off_p2 - BASE) + descriptor_offset)
        head.write(part2)

        # Part 3: Channel info
        off_p3 = head.tell()
        p3 = io.BytesIO()
        p3.write(bytes([C]))
        p3.write(b"\x00\x00\x00")

        if model.codec == AudioCodec.ADPCM:
            table_positions = []
            for _ in range(C):
                p3.write(struct.pack(">I", 0x01000000))
                table_positions.append(p3.tell())
                p3.write(struct.pack(">I", 0))

            chinfo_rel_offsets = []
            for c in range(C):
                ch_start_rel = p3.tell()
                chinfo_rel_offsets.append(ch_start_rel)
                chb = io.BytesIO()
                chb.write(struct.pack(">I", 0x01000000))
                coefs_ptr = (off_p3 - BASE) + ch_start_rel + 8
                chb.write(struct.pack(">I", coefs_ptr))

                # Write coefficients
                ch_info = model.adpcm_channels[c] if c < len(model.adpcm_channels) else None
                coefs = ch_info.coefs if ch_info else CANONICAL_ADPCM_COEFS
                for a, b in coefs:
                    chb.write(struct.pack(">hh", a, b))

                # Write ADPCM context
                gain = ch_info.gain if ch_info else 0
                init_ps = ch_info.init_ps if ch_info else 0
                init_h1 = ch_info.init_hist1 if ch_info else 0
                init_h2 = ch_info.init_hist2 if ch_info else 0
                loop_ps = ch_info.loop_ps if ch_info else 0
                loop_h1 = ch_info.loop_hist1 if ch_info else 0
                loop_h2 = ch_info.loop_hist2 if ch_info else 0

                chb.write(struct.pack(">H", gain))
                chb.write(struct.pack(">H", init_ps))
                chb.write(struct.pack(">h", init_h1))
                chb.write(struct.pack(">h", init_h2))
                chb.write(struct.pack(">H", loop_ps))
                chb.write(struct.pack(">h", loop_h1))
                chb.write(struct.pack(">h", loop_h2))
                chb.write(b"\x00\x00")  # padding
                p3.write(chb.getvalue())

            p3_bytes = bytearray(p3.getvalue())
            for idx, rel_off in enumerate(chinfo_rel_offsets):
                struct.pack_into(">I", p3_bytes, table_positions[idx], (off_p3 - BASE) + rel_off)
            head.write(p3_bytes)
        else:
            # PCM: no detailed channel info needed
            for _ in range(C):
                p3.write(struct.pack(">I", 0x01000000))
                p3.write(struct.pack(">I", 0))
            head.write(p3.getvalue())

        # Finalize HEAD
        head_end = head.tell()
        head_bytes = bytearray(head.getvalue())
        struct.pack_into(">I", head_bytes, 4, head_end)
        struct.pack_into(">I", head_bytes, 0x0C, off_p1 - BASE)
        struct.pack_into(">I", head_bytes, 0x14, off_p2 - BASE)
        struct.pack_into(">I", head_bytes, 0x1C, off_p3 - BASE)
        head_bytes += b"\x00" * (_pad32(len(head_bytes)) - len(head_bytes))

        # Build ADPC chunk (ADPCM only)
        adpc_bytes = b""
        if model.codec == AudioCodec.ADPCM and model.adpc_entries:
            adpc = io.BytesIO()
            adpc.write(b"ADPC")
            adpc.write(b"\x00\x00\x00\x00")  # Size placeholder
            for row in model.adpc_entries:
                for h1, h2 in row:
                    adpc.write(struct.pack(">hh", h1, h2))
            adpc_bytes = adpc.getvalue()
            adpc_bytes = adpc_bytes[:4] + struct.pack(">I", len(adpc_bytes)) + adpc_bytes[8:]
            adpc_bytes += b"\x00" * (_pad32(len(adpc_bytes)) - len(adpc_bytes))

        # Build DATA chunk
        data_chunk = io.BytesIO()
        data_chunk.write(b"DATA")
        data_chunk.write(b"\x00\x00\x00\x00")  # Size placeholder
        data_chunk.write(struct.pack(">I", 0x18))
        data_chunk.write(b"\x00" * 0x14)

        # Get data payload
        if model.data_payload is not None:
            payload = model.data_payload
        else:
            # Interleave channel data
            payload = self._interleave_blocks(model)

        data_chunk.write(payload)
        data_bytes = bytearray(data_chunk.getvalue())
        data_bytes = data_bytes[:4] + struct.pack(">I", len(data_bytes)) + data_bytes[8:]
        data_bytes += b"\x00" * (_pad32(len(data_bytes)) - len(data_bytes))

        # Calculate offsets
        off_head = 0x40
        sz_head = len(head_bytes)
        if model.codec == AudioCodec.ADPCM and adpc_bytes:
            off_adpc = off_head + sz_head
            sz_adpc = len(adpc_bytes)
            off_data = off_adpc + sz_adpc
        else:
            off_adpc = 0
            sz_adpc = 0
            off_data = off_head + sz_head

        # Update data absolute offset in HEAD
        data_abs_off = off_data + 0x20
        struct.pack_into(">I", head_bytes, off_p1 + p1_data_abs_pos, data_abs_off)

        # Write file header
        n_sections = 2 if model.codec == AudioCodec.ADPCM else 1
        out = io.BytesIO()
        out.write(b"RSTM")
        out.write(struct.pack(">HBB", 0xFEFF, 1, 0))
        out.write(struct.pack(">I", 0))  # File size placeholder
        out.write(struct.pack(">H", 0x40))
        out.write(struct.pack(">H", n_sections))
        out.write(struct.pack(">II", off_head, len(head_bytes)))
        out.write(struct.pack(">II", off_adpc, sz_adpc))
        out.write(struct.pack(">II", off_data, len(data_bytes)))

        # Pad header to 0x40
        if out.tell() < 0x40:
            out.write(b"\x00" * (0x40 - out.tell()))

        # Write chunks
        out.write(head_bytes)
        if model.codec == AudioCodec.ADPCM and adpc_bytes:
            out.write(adpc_bytes)
        out.write(data_bytes)

        # Update file size
        final = out.getvalue()
        final = final[:8] + struct.pack(">I", len(final)) + final[12:]

        output.write(final)

    def _interleave_blocks(self, model: BrstmData) -> bytes:
        parts = []
        pos = [0] * model.n_channels

        for bi in range(model.block_count):
            this_size = model.block_size if bi < model.block_count - 1 else model.final_block_size
            for c in range(model.n_channels):
                ch_data = model.channel_data[c]
                block = ch_data[pos[c]:pos[c] + this_size]
                parts.append(block)
                if bi == model.block_count - 1:
                    padded_size = max(this_size, model.final_block_size_padded)
                    parts.append(b"\x00" * (padded_size - len(block)))
                pos[c] += this_size

        return b"".join(parts)
