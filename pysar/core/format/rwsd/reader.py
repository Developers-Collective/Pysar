import struct
from typing import BinaryIO

from pysar.core import NW4RInvalidFileError, NW4RUnsupportedVersionError
from pysar.core.base import ReaderBase
from pysar.core.model.brwsd import (
    BrwsdData, WaveSound, WsdInfo, TrackInfo, NoteEvent, NoteInfo,
)
from pysar.core.types import FileTag
from pysar.io.binary import read_reference


class BrwsdReader(ReaderBase):
    EXPECTED_MAGIC = FileTag.BRWSD
    SUPPORTED_VERSIONS = {0x0103}

    def read(self, data: BinaryIO) -> BrwsdData:
        """Read a BRWSD file from a binary stream."""
        base_offset = data.tell()

        # File header (0x20 bytes)
        header = data.read(0x20)
        if len(header) < 0x20 or header[:4] != b"RWSD":
            raise NW4RInvalidFileError("Not a valid BRWSD file")

        (
            bom, version, file_size, header_size, n_sections,
            data_offset, data_size,
            wave_offset, wave_size,
        ) = struct.unpack(">HHIHH IIII", header[4:0x20])

        if version not in self.SUPPORTED_VERSIONS:
            raise NW4RUnsupportedVersionError(
                f"Unsupported BRWSD version: 0x{version:04X}"
            )

        # DATA section
        data.seek(base_offset + data_offset)
        section_magic = data.read(4)
        if section_magic != b"DATA":
            raise NW4RInvalidFileError("DATA section missing")
        section_size = struct.unpack(">I", data.read(4))[0]

        content_base = data.tell()  # position right after the 8-byte section header

        # WSD count
        n_wsd = struct.unpack(">I", data.read(4))[0]

        # WSD refs
        wsd_refs = []
        for _ in range(n_wsd):
            ref = read_reference(data)
            wsd_refs.append(ref)

        # Parse each WSD entry
        wave_sounds: list[WaveSound] = []
        for ref in wsd_refs:
            if ref.flag == 0:
                wave_sounds.append(WaveSound())
                continue

            data.seek(content_base + ref.offset)
            wsd = self._read_wsd(data, content_base)
            wave_sounds.append(wsd)

        return BrwsdData(version=version, wave_sounds=wave_sounds)

    def _read_wsd(self, data: BinaryIO, content_base: int) -> WaveSound:
        """Read a single WSD entry (3 refs: info, tracks, notes)."""
        info_ref = read_reference(data)
        track_ref = read_reference(data)
        note_ref = read_reference(data)

        # Read WsdInfo
        info = WsdInfo()
        if info_ref.flag != 0:
            data.seek(content_base + info_ref.offset)
            info = self._read_wsd_info(data)

        # Read track table
        tracks: list[TrackInfo] = []
        if track_ref.flag != 0:
            data.seek(content_base + track_ref.offset)
            tracks = self._read_track_table(data, content_base)

        # Read note table
        notes: list[NoteInfo] = []
        if note_ref.flag != 0:
            data.seek(content_base + note_ref.offset)
            notes = self._read_note_table(data, content_base)

        return WaveSound(info=info, tracks=tracks, notes=notes)

    def _read_wsd_info(self, data: BinaryIO) -> WsdInfo:
        """Read WsdInfo structure."""
        raw = data.read(12)
        pitch, pan, surround_pan, fx_a, fx_b, fx_c, main_send, _pad = struct.unpack(
            ">fBBBBBBH", raw
        )
        # Skip 2 null refs + 4 bytes padding
        data.read(8 + 8 + 4)

        return WsdInfo(
            pitch=pitch,
            pan=pan,
            surround_pan=surround_pan,
            fx_send_a=fx_a,
            fx_send_b=fx_b,
            fx_send_c=fx_c,
            main_send=main_send,
        )

    def _read_track_table(
            self, data: BinaryIO, content_base: int,
    ) -> list[TrackInfo]:
        n = struct.unpack(">I", data.read(4))[0]

        refs = []
        for _ in range(n):
            refs.append(read_reference(data))

        tracks: list[TrackInfo] = []
        for ref in refs:
            if ref.flag == 0:
                tracks.append(TrackInfo())
                continue
            data.seek(content_base + ref.offset)
            tracks.append(self._read_track_info(data, content_base))

        return tracks

    def _read_track_info(
            self, data: BinaryIO, content_base: int,
    ) -> TrackInfo:
        """Read a single track: ref to note event table."""
        evt_ref = read_reference(data)

        events: list[NoteEvent] = []
        if evt_ref.flag != 0:
            data.seek(content_base + evt_ref.offset)
            n_events = struct.unpack(">I", data.read(4))[0]

            evt_refs = []
            for _ in range(n_events):
                evt_refs.append(read_reference(data))

            for er in evt_refs:
                if er.flag == 0:
                    events.append(NoteEvent())
                    continue
                data.seek(content_base + er.offset)
                events.append(self._read_note_event(data))

        return TrackInfo(note_events=events)

    def _read_note_event(self, data: BinaryIO) -> NoteEvent:
        """Read NoteEvent (16 bytes)."""
        position, length, note_index, _pad = struct.unpack(">ffII", data.read(16))
        return NoteEvent(position=position, length=length, note_index=note_index)

    def _read_note_table(
            self, data: BinaryIO, content_base: int,
    ) -> list[NoteInfo]:
        n = struct.unpack(">I", data.read(4))[0]

        refs = []
        for _ in range(n):
            refs.append(read_reference(data))

        notes: list[NoteInfo] = []
        for ref in refs:
            if ref.flag == 0:
                notes.append(NoteInfo())
                continue
            data.seek(content_base + ref.offset)
            notes.append(self._read_note_info(data))

        return notes

    def _read_note_info(self, data: BinaryIO) -> NoteInfo:
        """Read NoteInfo: 20 bytes data + 3 null refs (24) + 4 pad = 48 bytes total."""
        # Format from writer: '>iBBBBB3sBBBBf' = 20 bytes
        raw = data.read(20)
        (
            wave_index,
            attack, decay, sustain, release, hold,
            _pad3,
            original_key, volume, pan, surround_pan,
            pitch,
        ) = struct.unpack(">iBBBBB3sBBBBf", raw)

        # Skip 3 null refs + 4 bytes padding
        data.read(8 * 3 + 4)

        return NoteInfo(
            wave_index=wave_index,
            attack=attack,
            decay=decay,
            sustain=sustain,
            release=release,
            hold=hold,
            original_key=original_key,
            volume=volume,
            pan=pan,
            surround_pan=surround_pan,
            pitch=pitch,
        )
