import io
from pathlib import Path
from typing import BinaryIO, Self, Iterator

from pysar.core.base import EditorBase, DirtyFlags
from pysar.core.model.brwsd import (
    BrwsdData, WaveSound, WsdInfo, TrackInfo, NoteEvent, NoteInfo,
)
from pysar.core.format.rwsd.reader import BrwsdReader
from pysar.core.format.rwsd.writer import BrwsdWriter


class Brwsd(EditorBase):
    """High-level editor for BRWSD (Wave Sound Data) files."""

    def __init__(self, data: BrwsdData | None = None):
        super().__init__()
        self._data = data or BrwsdData()

    #
    # Factory functions
    #

    @classmethod
    def new(cls) -> Self:
        """Create a new empty BRWSD."""
        editor = cls(BrwsdData())
        editor.mark_dirty(DirtyFlags.ALL)
        return editor

    @classmethod
    def open(cls, path: str | Path) -> Self:
        """Open a BRWSD file from disk."""
        reader = BrwsdReader()
        data = reader.from_file(str(path))
        return cls(data)

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        """Load a BRWSD from raw bytes."""
        reader = BrwsdReader()
        data = reader.read(io.BytesIO(raw))
        return cls(data)

    @classmethod
    def from_stream(cls, stream: BinaryIO) -> Self:
        """Load a BRWSD from a binary stream."""
        reader = BrwsdReader()
        data = reader.read(stream)
        return cls(data)

    #
    # Properties
    #

    @property
    def data(self) -> BrwsdData:
        """The underlying data model."""
        return self._data

    @property
    def version(self) -> int:
        return self._data.version

    @version.setter
    def version(self, value: int) -> None:
        self._data.version = value
        self.mark_dirty(DirtyFlags.ALL)

    @property
    def wave_sounds(self) -> list[WaveSound]:
        return self._data.wave_sounds

    #
    # Access functions
    #

    def __len__(self) -> int:
        return len(self._data.wave_sounds)

    def __getitem__(self, index: int) -> WaveSound:
        return self._data.wave_sounds[index]

    def __setitem__(self, index: int, value: WaveSound) -> None:
        self._data.wave_sounds[index] = value
        self.mark_dirty(DirtyFlags.ALL)

    def __delitem__(self, index: int) -> None:
        del self._data.wave_sounds[index]
        self.mark_dirty(DirtyFlags.ALL)

    def __iter__(self) -> Iterator[WaveSound]:
        return iter(self._data.wave_sounds)

    def __contains__(self, item: WaveSound) -> bool:
        return item in self._data.wave_sounds

    #
    # Operations
    #

    def add_wave_sound(
        self,
        wave_index: int = 0,
        *,
        pitch: float = 1.0,
        pan: int = 64,
        volume: int = 127,
        original_key: int = 60,
        attack: int = 127,
        decay: int = 127,
        sustain: int = 127,
        release: int = 127,
    ) -> WaveSound:
        """
        Add a new wave sound entry.

        Creates a minimal WSD with one track and one note event.

        Args:
            wave_index: Index into the BRWAR wave archive.
            pitch: Pitch multiplier (1.0 = normal).
            pan: Panning (0=left, 64=center, 127=right).
            volume: Note volume (0-127).
            original_key: MIDI note for unity pitch (default 60).
            attack/decay/sustain/release: ADSR envelope values.

        Returns:
            The newly created WaveSound.
        """
        note = NoteInfo(
            wave_index=wave_index,
            attack=attack, decay=decay,
            sustain=sustain, release=release,
            original_key=original_key,
            volume=volume, pan=pan,
            pitch=pitch,
        )

        event = NoteEvent(position=0.0, length=0.0, note_index=0)
        track = TrackInfo(note_events=[event])

        info = WsdInfo(pitch=pitch, pan=pan)

        wsd = WaveSound(info=info, tracks=[track], notes=[note])
        self._data.wave_sounds.append(wsd)
        self.mark_dirty(DirtyFlags.ALL)
        return wsd

    def remove_wave_sound(self, index: int) -> WaveSound:
        """Remove and return the wave sound at the given index."""
        wsd = self._data.wave_sounds.pop(index)
        self.mark_dirty(DirtyFlags.ALL)
        return wsd

    def insert_wave_sound(self, index: int, wsd: WaveSound) -> None:
        """Insert a wave sound at the given index."""
        self._data.wave_sounds.insert(index, wsd)
        self.mark_dirty(DirtyFlags.ALL)

    def clear(self) -> None:
        """Remove all wave sounds."""
        self._data.wave_sounds.clear()
        self.mark_dirty(DirtyFlags.ALL)

    #
    # Note info
    #

    def get_note_infos(self, wsd_index: int) -> list[NoteInfo]:
        """Get the note info table for a wave sound entry."""
        return self._data.wave_sounds[wsd_index].notes

    def get_wave_indices(self) -> list[int]:
        """Get all wave archive indices referenced by this file."""
        indices: list[int] = []
        for wsd in self._data.wave_sounds:
            for note in wsd.notes:
                if note.wave_index not in indices:
                    indices.append(note.wave_index)
        return indices

    def set_wave_index(self, wsd_index: int, note_index: int, wave_index: int) -> None:
        """Change the wave archive index for a specific note."""
        self._data.wave_sounds[wsd_index].notes[note_index].wave_index = wave_index
        self.mark_dirty(DirtyFlags.ALL)

    #
    # Serialization
    #

    def to_bytes(self) -> bytes:
        """Serialize to BRWSD binary."""
        writer = BrwsdWriter()
        return writer.to_bytes(self._data)

    def save(self, path: str | Path) -> Self:
        """Save to a BRWSD file."""
        Path(path).write_bytes(self.to_bytes())
        self.clear_dirty()
        return self

    #
    # Info / Debug
    #

    def summary(self) -> str:
        """Get a human-readable summary."""
        lines = [
            f"BRWSD Wave Sound Data",
            f"  Version: 0x{self._data.version:04X}",
            f"  Wave Sounds: {len(self._data.wave_sounds)}",
            "",
        ]

        for i, wsd in enumerate(self._data.wave_sounds):
            lines.append(f"  [{i}] pitch={wsd.info.pitch:.4f} pan={wsd.info.pan} "
                         f"main_send={wsd.info.main_send}")
            lines.append(f"      tracks={len(wsd.tracks)} notes={len(wsd.notes)}")
            for j, note in enumerate(wsd.notes):
                lines.append(
                    f"      note[{j}]: wave={note.wave_index} key={note.original_key} "
                    f"vol={note.volume} pan={note.pan} pitch={note.pitch:.4f} "
                    f"ADSR=({note.attack},{note.decay},{note.sustain},{note.release})"
                )

        return "\n".join(lines)

    def __str__(self) -> str:
        n = len(self._data.wave_sounds)
        return f"Brwsd(v0x{self._data.version:04X}, {n} sound{'s' if n != 1 else ''})"

    def __repr__(self) -> str:
        return self.__str__()