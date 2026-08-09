from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Iterator, Self, overload

from pysar.core.base import EditorBase, DirtyFlags
from pysar.core.format.rwar.reader import BrwarReader
from pysar.core.format.rwar.writer import BrwarWriter
from pysar.core.format.rwav.brwav import Brwav
from pysar.core.model.brwar import BrwarData, BrwarEntry
from pysar.core.types import AudioCodec


class Brwar(EditorBase):
    def __init__(self, data: BrwarData | None = None):
        super().__init__()
        self._data = data or BrwarData()
        # Cache of Brwav instances (lazy-loaded)
        self._editor_cache: dict[int, Brwav] = {}

    #
    # Factory methods
    #

    @classmethod
    def new(cls) -> Self:
        """Create a new empty BRWAR archive."""
        editor = cls(BrwarData())
        editor.mark_dirty(DirtyFlags.ALL)
        return editor

    @classmethod
    def open(cls, path: str | Path) -> Self:
        """Open a BRWAR file from disk."""
        reader = BrwarReader()
        data = reader.from_file(str(path))
        return cls(data)

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        """Load a BRWAR from raw bytes."""
        reader = BrwarReader()
        data = reader.read(BytesIO(raw))
        return cls(data)

    @classmethod
    def from_stream(cls, stream: BinaryIO) -> Self:
        """Load a BRWAR from a binary stream."""
        reader = BrwarReader()
        data = reader.read(stream)
        return cls(data)

    @classmethod
    def from_wav_files(
            cls,
            wav_paths: list[str | Path],
            *,
            encoding: AudioCodec = AudioCodec.ADPCM,
    ) -> Self:
        """
        Create a BRWAR from multiple WAV files.

        Args:
            wav_paths: List of paths to WAV files
            encoding: Encoding to use for all entries

        Returns:
            New Brwar containing all WAVs
        """
        archive = cls.new()
        for path in wav_paths:
            archive.add_from_wav(path, encoding=encoding)
        return archive

    @classmethod
    def from_folder(
            cls,
            folder_path: str | Path,
            *,
            pattern: str = "*.wav",
            encoding: AudioCodec = AudioCodec.ADPCM,
    ) -> Self:
        """
        Create a BRWAR from all WAV files in a folder.

        Args:
            folder_path: Path to folder containing WAV files
            pattern: Glob pattern for WAV files
            encoding: Encoding to use for all entries

        Returns:
            New Brwar containing all matching WAVs
        """
        folder = Path(folder_path)
        wav_files = sorted(folder.glob(pattern))
        return cls.from_wav_files(wav_files, encoding=encoding)

    # ===================
    # Properties
    # ===================

    @property
    def data(self) -> BrwarData:
        """Access the underlying data model."""
        return self._data

    @property
    def n_entries(self) -> int:
        """Number of BRWAV entries in the archive."""
        return len(self._data.entries)

    # ===================
    # Entry access
    # ===================

    def __len__(self) -> int:
        return len(self._data.entries)

    @overload
    def __getitem__(self, index: int) -> Brwav:
        ...

    @overload
    def __getitem__(self, index: slice) -> list[Brwav]:
        ...

    def __getitem__(self, index: int | slice) -> Brwav | list[Brwav]:
        """
        Get a Brwav for the entry at the given index.
        """
        if isinstance(index, slice):
            indices = range(*index.indices(len(self)))
            return [self._get_editor(i) for i in indices]

        if index < 0:
            index = len(self) + index
        if not (0 <= index < len(self)):
            raise IndexError(f"Index {index} out of range for archive with {len(self)} entries")

        return self._get_editor(index)

    def __setitem__(self, index: int, wave: Brwav) -> None:
        """Replace the entry at the given index."""
        if index < 0:
            index = len(self) + index
        if not (0 <= index < len(self)):
            raise IndexError(f"Index {index} out of range for archive with {len(self)} entries")

        self._data.entries[index] = BrwarEntry(brwav=wave.data)
        self._editor_cache[index] = wave
        self.mark_dirty(DirtyFlags.ALL)

    def __iter__(self) -> Iterator[Brwav]:
        """Iterate over all entries as Brwavs."""
        for i in range(len(self)):
            yield self._get_editor(i)

    def __contains__(self, editor: Brwav) -> bool:
        """Check if a Brwav is in this archive."""
        for i in range(len(self)):
            if self._data.entries[i].brwav is editor.data:
                return True
        return False

    def _get_editor(self, index: int) -> Brwav:
        """Get or create a Brwav for the given index."""
        if index not in self._editor_cache:
            self._editor_cache[index] = Brwav(self._data.entries[index].brwav)
        return self._editor_cache[index]

    def get_by_offset(self, offset: int) -> tuple[int, Brwav] | None:
        """
        Find an entry by its original offset in the DATA section.

        Args:
            offset: Original offset to search for

        Returns:
            Tuple of (index, Brwav) or None if not found
        """
        for i, entry in enumerate(self._data.entries):
            if entry.original_offset == offset:
                return i, self._get_editor(i)
        return None

    #
    # Modification methods
    #

    def add(self, wave: Brwav) -> int:
        """
        Add a BRWAV to the archive.

        Args:
            wave: BrwavEditor to add

        Returns:
            Index of the new entry
        """
        index = len(self._data.entries)
        self._data.entries.append(BrwarEntry(brwav=wave.data))
        self._editor_cache[index] = wave
        self.mark_dirty(DirtyFlags.ALL)
        return index

    def add_from_wav(
            self,
            wav_path: str | Path,
            *,
            encoding: AudioCodec = AudioCodec.ADPCM,
            loop_start: int = -1,
            loop_end: int = -1,
    ) -> int:
        """
        Add a WAV file to the archive.

        Args:
            wav_path: Path to the WAV file
            encoding: Target encoding (ADPCM, PCM16, PCM8)
            loop_start: Loop start sample (-1 for no loop)
            loop_end: Loop end sample (-1 for full length)

        Returns:
            Index of the new entry
        """
        editor = Brwav.from_wav(
            wav_path,
            encoding=encoding,
            loop_start=loop_start,
            loop_end=loop_end,
        )
        return self.add(editor)

    def add_from_pcm(
            self,
            pcm_samples: list[int],
            sample_rate: int,
            *,
            encoding: AudioCodec = AudioCodec.ADPCM,
            loop_start: int = -1,
            loop_end: int = -1,
    ) -> int:
        """
        Add PCM samples to the archive as a new BRWAV.

        Args:
            pcm_samples: List of signed 16-bit PCM samples
            sample_rate: Sample rate in Hz
            encoding: Target encoding
            loop_start: Loop start sample (-1 for no loop)
            loop_end: Loop end sample (-1 for full length)

        Returns:
            Index of the new entry
        """
        editor = Brwav.from_pcm(
            pcm_samples,
            sample_rate,
            encoding=encoding,
            loop_start=loop_start,
            loop_end=loop_end,
        )
        return self.add(editor)

    def remove(self, index: int) -> Brwav:
        """
        Remove and return the entry at the given index.

        Args:
            index: Index of entry to remove

        Returns:
            The removed Brwav
        """
        if index < 0:
            index = len(self) + index
        if not (0 <= index < len(self)):
            raise IndexError(f"Index {index} out of range")

        # Get editor before removal
        editor = self._get_editor(index)

        # Remove from data
        del self._data.entries[index]

        # Update cache (shift indices)
        new_cache = {}
        for i, ed in self._editor_cache.items():
            if i < index:
                new_cache[i] = ed
            elif i > index:
                new_cache[i - 1] = ed
        self._editor_cache = new_cache

        self.mark_dirty(DirtyFlags.ALL)
        return editor

    def clear(self) -> None:
        """Remove all entries from the archive."""
        self._data.entries.clear()
        self._editor_cache.clear()
        self.mark_dirty(DirtyFlags.ALL)

    def insert(self, index: int, wave: Brwav) -> None:
        """
        Insert a BRWAV at the given index.

        Args:
            index: Index to insert at
            wave: Brwav to insert
        """
        if index < 0:
            index = len(self) + index
        index = max(0, min(index, len(self)))

        self._data.entries.insert(index, BrwarEntry(brwav=wave.data))

        # Update cache (shift indices)
        new_cache = {}
        for i, ed in self._editor_cache.items():
            if i < index:
                new_cache[i] = ed
            else:
                new_cache[i + 1] = ed
        new_cache[index] = wave
        self._editor_cache = new_cache

        self.mark_dirty(DirtyFlags.ALL)

    def replace(self, index: int, wave: Brwav) -> Brwav:
        """
        Replace the entry at the given index.

        Args:
            index: Index of entry to replace
            wave: New Brwav

        Returns:
            The replaced Brwav
        """
        old = self._get_editor(index)
        self[index] = wave
        return old

    # ===================
    # Batch operations
    # ===================

    def decode_all(self) -> list[bytes]:
        """
        Decode all entries to PCM.

        Returns:
            List of PCM bytes for each entry
        """
        return [self._get_editor(i).decode() for i in range(len(self))]

    def decode_all_to_wav(
            self,
            output_folder: str | Path,
            *,
            prefix: str = "sound_",
            start_index: int = 0,
    ) -> list[Path]:
        """
        Decode all entries to WAV files.

        Args:
            output_folder: Folder to save WAV files
            prefix: Filename prefix
            start_index: Starting index for numbering

        Returns:
            List of paths to created WAV files
        """
        output_folder = Path(output_folder)
        output_folder.mkdir(parents=True, exist_ok=True)

        paths = []
        for i in range(len(self)):
            filename = f"{prefix}{start_index + i:04d}.wav"
            path = output_folder / filename
            self._get_editor(i).decode_to_wav(path)
            paths.append(path)

        return paths

    def get_info(self) -> list[dict]:
        """
        Get information about all entries.

        Returns:
            List of dicts with entry info
        """
        info = []
        for i in range(len(self)):
            editor = self._get_editor(i)
            info.append({
                "index": i,
                "encoding": editor.encoding.name,
                "sample_rate": editor.sample_rate,
                "n_samples": editor.n_samples,
                "duration": editor.duration,
                "is_looped": editor.is_looped,
                "loop_start": editor.loop_start if editor.is_looped else None,
            })
        return info

    #
    # Serialization
    #

    def to_bytes(self) -> bytes:
        """Serialize to BRWAR bytes."""
        # Fast path: if nothing was modified and we have original raw bytes,
        # return them directly for a byte-identical round-trip.
        has_dirty_editors = any(ed.is_dirty for ed in self._editor_cache.values())
        if not self.is_dirty and not has_dirty_editors and self._data.raw_bytes is not None:
            return self._data.raw_bytes

        # Sync any modified editors back to data
        for i, editor in self._editor_cache.items():
            if editor.is_dirty:
                self._data.entries[i] = BrwarEntry(brwav=editor.data)

        writer = BrwarWriter()
        return writer.to_bytes(self._data)

    def save(self, path: str | Path) -> Self:
        """Save to a BRWAR file."""
        Path(path).write_bytes(self.to_bytes())
        self.clear_dirty()
        # Clear dirty flags on all cached editors
        for editor in self._editor_cache.values():
            editor.clear_dirty()
        return self

    def __str__(self) -> str:
        return f"Brwar({len(self)} entries)"

    def __repr__(self) -> str:
        return self.__str__()

    def summary(self) -> str:
        """Get a detailed summary of the archive contents."""
        lines = [f"BRWAR Archive: {len(self)} entries", "=" * 40]

        for i in range(len(self)):
            editor = self._get_editor(i)
            loop_info = f", loop@{editor.loop_start}" if editor.is_looped else ""
            lines.append(
                f"  [{i:3d}] {editor.encoding.name:6s} "
                f"{editor.sample_rate:5d}Hz "
                f"{editor.duration:6.2f}s "
                f"({editor.n_samples:7d} samples){loop_info}"
            )

        return "\n".join(lines)