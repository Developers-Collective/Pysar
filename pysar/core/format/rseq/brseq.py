from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Self

from pysar.core.base import EditorBase, DirtyFlags
from pysar.core.model.brseq import BrseqData, Label, Track, Command, Prefix
from pysar.core.format.rseq.reader import BrseqReader
from pysar.core.format.rseq.writer import BrseqWriter
from pysar.core.format.rseq.text import to_text, from_text
from pysar.core.format.rseq.midi import (
    MidiFile, brseq_to_midi, midi_to_brseq,
)
from pysar.core.format.rseq.player import SequencePlayer
from pysar.core.format.rseq.mml import MML, MMLEX, DEFAULT_TEMPO, DEFAULT_TIMEBASE, is_note


class Brseq(EditorBase):
    def __init__(self, data: BrseqData | None = None):
        super().__init__()
        self._data = data or BrseqData()
        self._player: SequencePlayer | None = None

    #
    # Factory methods
    #

    @classmethod
    def new(cls) -> Self:
        """Create a new empty sequence."""
        editor = cls(BrseqData())
        editor.mark_dirty(DirtyFlags.ALL)
        return editor

    @classmethod
    def open(cls, path: str | Path) -> Self:
        """Open a BRSEQ file from disk."""
        reader = BrseqReader()
        data = reader.from_file(str(path))
        return cls(data)

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        """Load a BRSEQ from raw bytes."""
        reader = BrseqReader()
        data = reader.read(BytesIO(raw))
        return cls(data)

    @classmethod
    def from_stream(cls, stream: BinaryIO) -> Self:
        """Load a BRSEQ from a binary stream."""
        reader = BrseqReader()
        data = reader.read(stream)
        return cls(data)

    @classmethod
    def from_midi(
            cls,
            path: str | Path,
            timebase: int = DEFAULT_TIMEBASE,
            combine_tracks: bool = False,
            annotations: str = "auto",
            merge_policy: str = "safe",
    ) -> Self:
        """
        Create a BRSEQ from a MIDI file.

        Args:
            path: Path to MIDI file
            timebase: BRSEQ timebase (default 48)
            combine_tracks: If True, merge all MIDI tracks into one
        Returns:
            BrseqEditor instance
        """
        if annotations not in {"auto", "ignore"}:
            raise ValueError("annotations must be 'auto' or 'ignore'")
        if annotations == "auto":
            from pysar.core.format.rseq.midi_profile import NintendoMidiProfile
            data = NintendoMidiProfile.import_file(
                path,
                timebase=timebase,
                combine_tracks=combine_tracks,
                merge_policy=merge_policy,
            ).data
        else:
            midi = MidiFile.from_file(path)
            data = midi_to_brseq(midi, timebase, combine_tracks)
        editor = cls(data)
        editor.mark_dirty(DirtyFlags.ALL)
        return editor

    @classmethod
    def from_midi_bytes(
            cls,
            midi_bytes: bytes,
            timebase: int = DEFAULT_TIMEBASE,
            combine_tracks: bool = False,
            annotations: str = "auto",
            merge_policy: str = "safe",
    ) -> Self:
        """Create a BRSEQ from MIDI bytes."""
        midi = MidiFile.from_bytes(midi_bytes)
        if annotations not in {"auto", "ignore"}:
            raise ValueError("annotations must be 'auto' or 'ignore'")
        if annotations == "auto":
            from pysar.core.format.rseq.midi_profile import NintendoMidiProfile
            data = NintendoMidiProfile.import_midi(
                midi,
                timebase=timebase,
                combine_tracks=combine_tracks,
                merge_policy=merge_policy,
            ).data
        else:
            data = midi_to_brseq(midi, timebase, combine_tracks)
        editor = cls(data)
        editor.mark_dirty(DirtyFlags.ALL)
        return editor

    @classmethod
    def from_text(cls, text: str) -> Self:
        """
        Create a BRSEQ from RSEQ text format.

        Args:
            text: RSEQ assembly text

        Returns:
            BrseqEditor instance
        """
        data = from_text(text)
        editor = cls(data)
        editor.mark_dirty(DirtyFlags.ALL)
        return editor

    @classmethod
    def from_text_file(cls, path: str | Path) -> Self:
        """Load from a RSEQ text file."""
        text = Path(path).read_text(encoding="utf-8")
        return cls.from_text(text)

    #
    # Properties
    #

    @property
    def data(self) -> BrseqData:
        return self._data

    @property
    def tracks(self) -> dict[str | int, Track]:
        return self._data.tracks

    @property
    def labels(self) -> list[Label]:
        return self._data.labels

    @property
    def track_names(self) -> list[str]:
        """Get names of all named tracks."""
        return [name for name in self._data.tracks.keys() if isinstance(name, str)]

    @property
    def n_tracks(self) -> int:
        return len(self._data.tracks)

    @property
    def tempo(self) -> int:
        """Get the sequence tempo (BPM)."""
        for track in self._data.tracks.values():
            for cmd in track.commands:
                if cmd.opcode == MML.TEMPO and cmd.args:
                    return cmd.args[0]
        return DEFAULT_TEMPO

    @property
    def timebase(self) -> int:
        """Get the sequence timebase (ticks per beat)."""
        for track in self._data.tracks.values():
            for cmd in track.commands:
                if cmd.opcode == MML.TIMEBASE and cmd.args:
                    return cmd.args[0]
        return DEFAULT_TIMEBASE

    #
    # Track access
    #

    def __len__(self) -> int:
        return len(self._data.tracks)

    def __getitem__(self, key: str | int) -> Track:
        return self._data[key]

    def __contains__(self, key: str | int) -> bool:
        return key in self._data

    def get_track(self, name: str) -> Track | None:
        """Get a track by name."""
        return self._data.tracks.get(name)

    def get_track_by_offset(self, offset: int) -> Track | None:
        """Get a track by its starting offset."""
        for track in self._data.tracks.values():
            if track.start_offset == offset:
                return track
        return None

    #
    # MIDI Conversion
    #

    def to_midi(
            self,
            path: str | Path | None = None,
            ticks_per_beat: int = 480,
            max_loops: int = 1,
            start_label: str | None = None,
            start_offset: int | None = None,
            annotated: bool = True,
    ) -> MidiFile:
        """
        Convert to MIDI.

        Args:
            path: If provided, save to this file
            ticks_per_beat: MIDI resolution
            annotated: Include the semantic PNMP/1 command graph, readable MML
                source, and performance bindings

        Returns:
            MidiFile object
        """
        midi = brseq_to_midi(
            self._data,
            ticks_per_beat,
            max_loops=max_loops,
            start_label=start_label,
            start_offset=start_offset,
        )
        if annotated:
            from pysar.core.format.rseq.midi_profile import annotate_midi
            annotate_midi(
                midi,
                self._data,
                start_label=start_label,
                start_offset=start_offset,
            )
        if path:
            midi.save(path)
        return midi

    def to_midi_bytes(
            self,
            ticks_per_beat: int = 480,
            max_loops: int = 1,
            start_label: str | None = None,
            start_offset: int | None = None,
            annotated: bool = True,
    ) -> bytes:
        """Convert to MIDI bytes."""
        midi = self.to_midi(
            ticks_per_beat=ticks_per_beat,
            max_loops=max_loops,
            start_label=start_label,
            start_offset=start_offset,
            annotated=annotated,
        )
        return midi.to_bytes()

    #
    # Text Conversion
    #

    def to_text(self, include_offsets: bool = False) -> str:
        """
        Convert to RSEQ text format.

        Args:
            include_offsets: Include offset comments

        Returns:
            RSEQ assembly text
        """
        return to_text(self._data, include_offsets)

    def save_text(self, path: str | Path, include_offsets: bool = False) -> None:
        """Save to a RSEQ text file."""
        text = self.to_text(include_offsets)
        Path(path).write_text(text, encoding="utf-8")

    #
    # Playback
    #

    def get_player(self) -> SequencePlayer | None:
        """Get a player for this sequence."""
        if self._player is None:
            self._player = SequencePlayer()
        self._player.load(self._data)
        return self._player

    def play(
            self,
            blocking: bool = False,
            start_label: str | None = None,
    ) -> SequencePlayer | None:
        """
        Play the sequence.

        Args:
            blocking: If True, block until playback finishes
            start_label: Label to start from (default: first track)

        Returns:
            SequencePlayer instance
        """
        player = self.get_player()
        player.load(self._data, start_label)

        if blocking:
            player.play_blocking()
        else:
            player.play_threaded()

        return player

    def play_with_soundfont(
            self,
            sf2_path: str | Path,
            start_label: str | None = None,
            start_offset: int | None = None,
            loop_count: int = 1,
            audio_driver: str | None = None,
            default_program: int | None = None,
            note_override: int | None = None,
    ) -> SequencePlayer | None:
        """
        Play the sequence using a SoundFont via FluidSynth.

        Args:
            sf2_path: Path to the SF2 soundfont file.
            start_label: Optional label name to start from.
            start_offset: Optional byte offset to start from (takes priority).
            loop_count: How many times loops repeat.
            audio_driver: FluidSynth audio driver (None = auto-detect).
            default_program: Program to use when the sequence has variable-based
                PRG (program=-1). If None, defaults to 0.
            note_override: If set, override all note values to select a specific
                key-range variation.

        Returns:
            SequencePlayer instance.
        """
        player = self.get_player()
        player.load(self._data, start_label=start_label, start_offset=start_offset)
        player.play_with_soundfont(
            str(sf2_path),
            loop_count=loop_count,
            audio_driver=audio_driver,
            default_program=default_program,
            note_override=note_override,
        )
        return player

    def stop(self) -> None:
        """Stop playback."""
        if self._player:
            self._player.stop()

    #
    # Modification
    #

    def add_track(self, name: str, track: Track) -> None:
        """Add a track to the sequence."""
        if name in self._data.tracks:
            raise ValueError(f"Track already exists: {name}")

        writer = BrseqWriter()
        if self._data.command_stream:
            last = self._data.command_stream[-1]
            start_offset = last.offset + writer._command_size(last)
        else:
            start_offset = max(
                (existing.end_offset for existing in self._data.tracks.values()),
                default=0,
            )
        track.start_offset = start_offset
        offset = start_offset
        for command in track.commands:
            command.offset = offset
            offset += writer._command_size(command)
        track.end_offset = offset

        if track.label is None:
            track.label = Label(name=name, offset=start_offset)
        else:
            track.label.name = name
            track.label.offset = start_offset
        self._data.tracks[name] = track
        self._data.labels.append(track.label)
        self._data.command_stream.extend(track.commands)
        self.mark_dirty(DirtyFlags.ALL)

    def remove_track(self, name: str) -> Track | None:
        """Remove a track from the sequence."""
        track = self._data.tracks.pop(name, None)
        if track and track.label:
            self._data.labels = [l for l in self._data.labels if l.name != name]
            retained_ids = {
                id(command)
                for other in self._data.tracks.values()
                for command in other.commands
            }
            self._data.command_stream = [
                command for command in self._data.command_stream
                if id(command) in retained_ids
            ]
        self.mark_dirty(DirtyFlags.ALL)
        return track

    def rename_track(self, old_name: str, new_name: str) -> bool:
        """Rename a track."""
        if old_name not in self._data.tracks:
            return False

        track = self._data.tracks.pop(old_name)
        if track.label:
            track.label.name = new_name
        self._data.tracks[new_name] = track

        for label in self._data.labels:
            if label.name == old_name:
                label.name = new_name

        self.mark_dirty(DirtyFlags.ALL)
        return True

    def set_tempo(self, bpm: int) -> None:
        """Set the sequence tempo."""
        # Find and update existing TEMPO command, or add one
        for track in self._data.tracks.values():
            for cmd in track.commands:
                if cmd.opcode == MML.TEMPO:
                    cmd.args = [bpm]
                    self.mark_dirty(DirtyFlags.ALL)
                    return

        # No existing tempo, add to first track
        if self._data.tracks:
            first_track = list(self._data.tracks.values())[0]
            command = Command(
                opcode=MML.TEMPO,
                args=[bpm],
                offset=first_track.start_offset,
            )
            first_track.commands.insert(0, command)
            if self._data.command_stream:
                try:
                    index = self._data.command_stream.index(first_track.commands[1])
                except (IndexError, ValueError):
                    index = 0
                self._data.command_stream.insert(index, command)
            else:
                self._data.command_stream.append(command)
            self.mark_dirty(DirtyFlags.ALL)

    def set_timebase(self, timebase: int) -> None:
        """Set the sequence timebase."""
        for track in self._data.tracks.values():
            for cmd in track.commands:
                if cmd.opcode == MML.TIMEBASE:
                    cmd.args = [timebase]
                    self.mark_dirty(DirtyFlags.ALL)
                    return

        if self._data.tracks:
            first_track = list(self._data.tracks.values())[0]
            command = Command(
                opcode=MML.TIMEBASE,
                args=[timebase],
                offset=first_track.start_offset,
            )
            first_track.commands.insert(0, command)
            if self._data.command_stream:
                try:
                    index = self._data.command_stream.index(first_track.commands[1])
                except (IndexError, ValueError):
                    index = 0
                self._data.command_stream.insert(index, command)
            else:
                self._data.command_stream.append(command)
            self.mark_dirty(DirtyFlags.ALL)

    #
    # Serialization
    #

    def to_bytes(self) -> bytes:
        """Serialize to BRSEQ bytes."""
        writer = BrseqWriter()
        return writer.to_bytes(self._data)

    def save(self, path: str | Path) -> Self:
        """Save to a BRSEQ file."""
        Path(path).write_bytes(self.to_bytes())
        self.clear_dirty()
        return self

    #
    # Analysis
    #

    def get_programs_used(self) -> set[int]:
        """Get all program/instrument numbers used."""
        programs = set()
        for track in self._data.tracks.values():
            for cmd in track.commands:
                if cmd.opcode == MML.PRG and cmd.args:
                    programs.add(cmd.args[0])
        return programs

    def get_notes_used(self) -> set[int]:
        """Get all note numbers used."""
        notes = set()
        for track in self._data.tracks.values():
            for cmd in track.commands:
                if is_note(cmd.opcode):
                    notes.add(cmd.opcode)
        return notes

    def get_duration_ticks(self) -> int:
        """Estimate total duration in ticks."""
        max_ticks = 0
        for track in self._data.tracks.values():
            ticks = 0
            for cmd in track.commands:
                if cmd.opcode == MML.WAIT and cmd.args:
                    ticks += cmd.args[0]
                elif is_note(cmd.opcode) and len(cmd.args) >= 2:
                    ticks += cmd.args[1]  # Note duration
            max_ticks = max(max_ticks, ticks)

        return max_ticks

    def get_duration_seconds(self) -> float:
        """Estimate total duration in seconds."""
        ticks = self.get_duration_ticks()
        ticks_per_second = (self.timebase * self.tempo) / 60.0
        return ticks / ticks_per_second if ticks_per_second > 0 else 0.0

    #
    # Info/Debug
    #

    def summary(self) -> str:
        """Get a detailed summary of the sequence."""
        lines = [
            f"BRSEQ Sequence",
            f"  Version: 0x{self._data.version:04X}",
            f"  Tempo: {self.tempo} BPM",
            f"  Timebase: {self.timebase} ticks/beat",
            f"  Duration: ~{self.get_duration_seconds():.2f}s",
            f"  Tracks: {self.n_tracks}",
            f"  Programs: {sorted(self.get_programs_used())}",
            "",
            "Tracks:",
        ]

        for name, track in self._data.tracks.items():
            n_notes = sum(1 for cmd in track.commands if cmd.opcode < 0x80)
            lines.append(f"  [{name}] {len(track.commands)} commands, {n_notes} notes")

        return "\n".join(lines)

    def __str__(self) -> str:
        return f"Brseq({self.n_tracks} tracks, {self.tempo} BPM)"

    def __repr__(self) -> str:
        return self.__str__()
