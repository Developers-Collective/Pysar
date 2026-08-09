from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pysar.core.format.rsar import Brsar
    from pysar.seq.types import RenderOptions


@dataclass(slots=True)
class ProjectSession:
    archive_path: Path | None = None
    archive: "Brsar | None" = None
    dirty: bool = False
    safe_mode: bool = True
    destructive_operations_executed: bool = False
    undo_snapshot: bytes | None = None
    undo_label: str | None = None
    selected_sound_id: int | None = None
    selected_bank_index: int | None = None
    selected_group_index: int | None = None


@dataclass(slots=True)
class ArchiveSummary:
    version: str
    sound_count: int
    seq_count: int
    wave_count: int
    stream_count: int
    bank_count: int
    player_count: int
    group_count: int
    embedded_file_count: int


@dataclass(slots=True)
class SoundListItem:
    sound_id: int
    name: str
    sound_type: str
    file_index: int
    player_index: int
    volume: int
    group_name: str | None = None
    bank_index: int | None = None
    # Resolved file_ids for the sound's file_index. data_file_id is the
    # RBNK / RWSD / RSEQ / RSTM, audio_file_id is the companion RWAR (if any).
    data_file_id: int | None = None
    audio_file_id: int | None = None


@dataclass(slots=True)
class SoundDetails:
    sound_id: int
    name: str
    sound_type: str
    file_index: int
    player_index: int
    volume: int
    player_priority: int
    actor_player_id: int
    pan_mode: int
    pan_curve: int
    group_name: str | None = None
    bank_index: int | None = None
    seq_label_offset: int | None = None
    wave_index: int | None = None
    alloc_track: int | None = None
    start_position: int | None = None
    n_alloc_channels: int | None = None
    alloc_track_flag: int | None = None


@dataclass(slots=True)
class BankListItem:
    bank_index: int
    name: str
    file_index: int
    data_file_id: int | None = None
    audio_file_id: int | None = None
    instrument_count: int = 0
    wave_count: int = 0


@dataclass(slots=True)
class GroupListItem:
    group_index: int
    name: str
    item_count: int
    file_id: int | None = None
    audio_file_id: int | None = None
    file_size: int = 0
    audio_size: int = 0


@dataclass(slots=True)
class PlayerListItem:
    player_index: int
    name: str
    playable_sound_count: int
    heap_size: int = 0


@dataclass(slots=True)
class WaveArchiveListItem:
    file_id: int
    name: str
    size: int
    wave_count: int
    linked_banks: list[str]


@dataclass(slots=True)
class WaveListItem:
    """One BRWAV inside a wave archive, surfaced for inspection."""
    index: int
    encoding: str            # "ADPCM" | "PCM8" | "PCM16"
    sample_rate: int
    n_channels: int
    n_samples: int           # number of decoded samples
    loop_start: int          # in samples
    is_looped: bool
    size_bytes: int
    duration_ms: int


@dataclass(slots=True)
class WaveArchiveDetails:
    file_id: int
    name: str
    size: int
    wave_count: int
    waves: list[WaveListItem]


@dataclass(slots=True)
class BankZone:
    """One playable zone inside a bank instrument: a single InstParam plus
    the key/velocity range it covers."""
    wave_index: int
    key_low: int
    key_high: int
    velocity_low: int
    velocity_high: int
    original_key: int
    volume: int
    pan: int
    pitch: float
    attack: int
    decay: int
    sustain: int
    release: int
    hold: int = 0
    note_off_type: int = 0
    alternate_assign: int = 0


@dataclass(slots=True)
class BankInstrument:
    program: int
    name: str
    zone_count: int
    wave_indices: list[int]
    key_low: int
    key_high: int
    is_empty: bool
    zones: list[BankZone]


@dataclass(slots=True)
class BankDetails:
    bank_index: int
    name: str
    instrument_count: int
    active_instrument_count: int
    wave_count: int
    audio_file_id: int | None
    instruments: list[BankInstrument]


@dataclass(slots=True)
class FileReferrer:
    """One thing in the BRSAR that points at a file_id (a sound or a bank)."""
    kind: str  # "sound" | "bank"
    id: int
    name: str


@dataclass(slots=True)
class FileListItem:
    file_id: int
    kind: str
    size: int
    label: str
    linked: list[FileReferrer]
    is_external: bool = False
    file_index: int | None = None
    external_path: str | None = None


@dataclass(slots=True)
class PreviewOptions:
    sample_rate: int = 32000
    loop_count: int = 1
    max_ticks: int = 300_000
    tail_seconds: float = 0.45
    block_frames: int = 512
    master_gain: float = 0.85
    max_physical_voices: int = 96
    one_shot: bool = True
    seq_program_override: int | None = None
    seq_note_override: int | None = None
    seq_random_overrides: tuple[tuple[int, int], ...] = ()

    def to_render_options(self) -> "RenderOptions":
        from pysar.seq.types import RenderOptions

        return RenderOptions(
            sample_rate=self.sample_rate,
            loop_count=self.loop_count,
            max_ticks=self.max_ticks,
            tail_seconds=self.tail_seconds,
            block_frames=self.block_frames,
            master_gain=self.master_gain,
            max_physical_voices=self.max_physical_voices,
            one_shot=self.one_shot,
            seq_program_override=self.seq_program_override,
            seq_note_override=self.seq_note_override,
            seq_random_overrides=tuple((int(index), int(value)) for index, value in self.seq_random_overrides),
        )
