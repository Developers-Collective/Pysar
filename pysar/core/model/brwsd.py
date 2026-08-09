from dataclasses import dataclass, field


@dataclass
class WsdInfo:
    pitch: float = 1.0
    pan: int = 64
    surround_pan: int = 0
    fx_send_a: int = 0
    fx_send_b: int = 0
    fx_send_c: int = 0
    main_send: int = 127


@dataclass
class NoteEvent:
    """A timed note trigger within a track."""

    position: float = 0.0
    length: float = 0.0
    note_index: int = 0


@dataclass
class TrackInfo:
    note_events: list[NoteEvent] = field(default_factory=list)


@dataclass
class NoteInfo:
    """Per-note playback parameters."""

    wave_index: int = 0
    attack: int = 127
    decay: int = 127
    sustain: int = 127
    release: int = 127
    hold: int = 0
    original_key: int = 60
    volume: int = 127
    pan: int = 64
    surround_pan: int = 0
    pitch: float = 1.0


@dataclass
class WaveSound:
    info: WsdInfo = field(default_factory=WsdInfo)
    tracks: list[TrackInfo] = field(default_factory=list)
    notes: list[NoteInfo] = field(default_factory=list)


@dataclass
class BrwsdData:
    version: int = 0x0103
    wave_sounds: list[WaveSound] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.wave_sounds)

    def __getitem__(self, index: int) -> WaveSound:
        return self.wave_sounds[index]

    def __iter__(self):
        return iter(self.wave_sounds)