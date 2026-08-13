from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Callable, Generic, TypeVar

from pysar.core.base import DirtyFlags
from pysar.core.exceptions import BrsarError
from pysar.core.model.brsar import (
    SeqSoundInfo,
    SoundType,
    StreamSoundInfo,
    WaveSoundInfo,
)

if TYPE_CHECKING:
    from pysar.core.format.rsar.brsar import Brsar


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ArchiveValidationIssue:
    """One actionable archive validation failure."""

    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


@dataclass(slots=True)
class ArchiveValidationReport:
    """Result of validating an in-memory BRSAR model."""

    issues: list[ArchiveValidationIssue] = field(default_factory=list)
    serialized_size: int | None = None
    serialized_sha256: str | None = None
    round_trip_checked: bool = False

    @property
    def ok(self) -> bool:
        return not self.issues

    def add(self, code: str, path: str, message: str) -> None:
        self.issues.append(ArchiveValidationIssue(code, path, message))

    def raise_if_invalid(self) -> None:
        if self.ok:
            return
        first = self.issues[0]
        extra = len(self.issues) - 1
        suffix = f" ({extra} more issue{'s' if extra != 1 else ''})" if extra else ""
        raise ArchiveValidationError(
            f"Archive validation failed at {first.path}: {first.message}{suffix}",
            self,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [issue.to_dict() for issue in self.issues],
            "serializedSize": self.serialized_size,
            "serializedSha256": self.serialized_sha256,
            "roundTripChecked": self.round_trip_checked,
        }


class ArchiveValidationError(BrsarError):
    """Raised when a transaction would leave an invalid archive."""

    def __init__(self, message: str, report: ArchiveValidationReport):
        super().__init__(message)
        self.report = report


def _valid_index(value: object, count: int) -> bool:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return False
    return 0 <= number < count


def _check_name_reference(
        report: ArchiveValidationReport,
        names: list[str],
        value: object,
        path: str,
        *,
        allow_anonymous: bool = False,
) -> None:
    if allow_anonymous:
        try:
            if int(value) == -1:
                return
        except (TypeError, ValueError):
            pass
    if not _valid_index(value, len(names)):
        report.add(
            "invalid_name_reference",
            path,
            f"name index {value!r} is outside 0..{max(-1, len(names) - 1)}",
        )


def _validate_structure(archive: "Brsar", report: ArchiveValidationReport) -> None:
    data = archive.data
    names = data.names
    sound_count = len(data.sound_entries)
    bank_count = len(data.bank_entries)
    player_count = len(data.player_entries)
    file_count = len(data.file_entries)
    group_count = len(data.group_entries)

    for index, sound in enumerate(data.sound_entries):
        base = f"sounds[{index}]"
        _check_name_reference(report, names, sound.file_name_index, f"{base}.name")
        if not _valid_index(sound.file_index, file_count):
            report.add(
                "invalid_file_reference",
                f"{base}.fileIndex",
                f"file index {sound.file_index!r} is outside 0..{max(-1, file_count - 1)}",
            )
        if not _valid_index(sound.player_index, player_count):
            report.add(
                "invalid_player_reference",
                f"{base}.playerIndex",
                f"player index {sound.player_index!r} is outside 0..{max(-1, player_count - 1)}",
            )

        if sound.sound_type == SoundType.SEQ:
            if not isinstance(sound.sound_info, SeqSoundInfo):
                report.add("invalid_sound_info", f"{base}.soundInfo", "SEQ sound has non-SEQ metadata")
            elif not _valid_index(sound.sound_info.bank_index, bank_count):
                report.add(
                    "invalid_bank_reference",
                    f"{base}.soundInfo.bankIndex",
                    f"bank index {sound.sound_info.bank_index!r} is outside 0..{max(-1, bank_count - 1)}",
                )
        elif sound.sound_type == SoundType.STRM:
            if not isinstance(sound.sound_info, StreamSoundInfo):
                report.add("invalid_sound_info", f"{base}.soundInfo", "STRM sound has non-STRM metadata")
        elif sound.sound_type == SoundType.WAVE:
            if not isinstance(sound.sound_info, WaveSoundInfo):
                report.add("invalid_sound_info", f"{base}.soundInfo", "WAVE sound has non-WAVE metadata")
            elif int(sound.sound_info.wave_index) < 0:
                report.add(
                    "invalid_wave_reference",
                    f"{base}.soundInfo.waveIndex",
                    "wave index cannot be negative",
                )
        else:
            report.add(
                "invalid_sound_type",
                f"{base}.type",
                f"unsupported sound type {sound.sound_type!r}",
            )

    for index, bank in enumerate(data.bank_entries):
        base = f"banks[{index}]"
        _check_name_reference(report, names, bank.file_name_index, f"{base}.name")
        if not _valid_index(bank.file_index, file_count):
            report.add(
                "invalid_file_reference",
                f"{base}.fileIndex",
                f"file index {bank.file_index!r} is outside 0..{max(-1, file_count - 1)}",
            )

    for index, player in enumerate(data.player_entries):
        _check_name_reference(report, names, player.file_name_index, f"players[{index}].name")

    for group_index, group in enumerate(data.group_entries):
        base = f"groups[{group_index}]"
        # Nintendo-generated archives legitimately use -1 for anonymous
        # groups (including the sole group in an otherwise empty archive).
        _check_name_reference(
            report,
            names,
            group.file_name_index,
            f"{base}.name",
            allow_anonymous=True,
        )
        for attribute in ("file_id", "audio_file_id"):
            file_id = getattr(group, attribute)
            if file_id is not None and int(file_id) not in data.embedded_files:
                report.add(
                    "missing_embedded_file",
                    f"{base}.{attribute}",
                    f"embedded file {file_id} does not exist",
                )
        for slot, sub in enumerate(group.group_table):
            sub_path = f"{base}.items[{slot}]"
            logical_file = int(sub.group_index)
            if not _valid_index(logical_file, file_count):
                report.add(
                    "invalid_file_reference",
                    f"{sub_path}.fileIndex",
                    f"logical file index {logical_file} is outside 0..{max(-1, file_count - 1)}",
                )
            else:
                reverse = data.file_entries[logical_file].file_positions
                if not any(
                    int(position.group_index) == group_index and int(position.index) == slot
                    for position in reverse
                ):
                    report.add(
                        "missing_reverse_position",
                        sub_path,
                        "group item is missing the matching FILE position",
                    )
            for attribute in ("file_id", "audio_file_id"):
                file_id = getattr(sub, attribute)
                if file_id is not None and int(file_id) not in data.embedded_files:
                    report.add(
                        "missing_embedded_file",
                        f"{sub_path}.{attribute}",
                        f"embedded file {file_id} does not exist",
                    )

    for file_index, entry in enumerate(data.file_entries):
        for position_index, position in enumerate(entry.file_positions):
            path = f"files[{file_index}].positions[{position_index}]"
            if not _valid_index(position.group_index, group_count):
                report.add(
                    "invalid_group_reference",
                    f"{path}.groupIndex",
                    f"group index {position.group_index!r} is outside 0..{max(-1, group_count - 1)}",
                )
                continue
            group = data.group_entries[int(position.group_index)]
            if not _valid_index(position.index, len(group.group_table)):
                report.add(
                    "invalid_group_item_reference",
                    f"{path}.itemIndex",
                    f"group item index {position.index!r} is outside 0..{max(-1, len(group.group_table) - 1)}",
                )
                continue
            actual = int(group.group_table[int(position.index)].group_index)
            if actual != file_index:
                report.add(
                    "mismatched_file_position",
                    path,
                    f"position points to logical file {actual}, expected {file_index}",
                )

    for file_id, embedded in data.embedded_files.items():
        path = f"embeddedFiles[{file_id}]"
        if int(embedded.file_id) != int(file_id):
            report.add(
                "mismatched_embedded_file_id",
                f"{path}.fileId",
                f"embedded record identifies itself as {embedded.file_id}",
            )
        raw_magic = bytes(embedded.raw_data[:4]).decode("ascii", errors="replace")
        declared_magic = str(embedded.magic or "")
        if declared_magic and raw_magic != declared_magic:
            report.add(
                "mismatched_embedded_magic",
                f"{path}.magic",
                f"declared {declared_magic!r}, payload starts with {raw_magic!r}",
            )


def validate_archive(
        archive: "Brsar",
        *,
        round_trip: bool = False,
) -> ArchiveValidationReport:
    report = ArchiveValidationReport()
    _validate_structure(archive, report)
    if not round_trip or not report.ok:
        return report

    report.round_trip_checked = True
    try:
        raw = archive.to_bytes()
        report.serialized_size = len(raw)
        report.serialized_sha256 = sha256(raw).hexdigest()

        from pysar.core.format.rsar.brsar import Brsar

        reparsed = Brsar.from_bytes(raw)
        parsed_report = validate_archive(reparsed, round_trip=False)
        for issue in parsed_report.issues:
            report.add(
                f"round_trip_{issue.code}",
                issue.path,
                issue.message,
            )
        if report.ok:
            second = reparsed.to_bytes()
            if second != raw:
                report.add(
                    "round_trip_unstable",
                    "archive",
                    "serializing the reparsed archive changed its bytes",
                )
    except Exception as exc:
        report.add(
            "serialization_failed",
            "archive",
            f"archive could not be serialized and reparsed: {exc}",
        )
    return report


@dataclass(frozen=True, slots=True)
class ArchiveSnapshot:
    """Serializable archive state used for rollback and one-step undo."""

    raw: bytes
    safe_mode: bool
    dirty_flags: DirtyFlags

    @classmethod
    def capture(cls, archive: "Brsar") -> "ArchiveSnapshot":
        dirty_flags = archive._dirty
        raw = archive.to_bytes()
        return cls(raw=raw, safe_mode=archive.safe_mode, dirty_flags=dirty_flags)

    def clone(self) -> "Brsar":
        from pysar.core.format.rsar.brsar import Brsar

        clone = Brsar.from_bytes(self.raw)
        clone.set_safe_mode(self.safe_mode)
        clone._dirty = self.dirty_flags
        return clone

    def restore(self, archive: "Brsar") -> None:
        """Restore state without replacing the live editor object."""
        restored = self.clone()
        archive._data = restored._data
        archive.clear_subfile_caches()
        archive._safe_mode = self.safe_mode
        archive._dirty = self.dirty_flags


class ArchiveTransaction(AbstractContextManager["Brsar"]):
    def __init__(
            self,
            archive: "Brsar",
            *,
            label: str = "Archive mutation",
            round_trip: bool = True,
    ):
        self.archive = archive
        self.label = str(label)
        self.round_trip = bool(round_trip)
        self.snapshot: ArchiveSnapshot | None = None
        self.validation: ArchiveValidationReport | None = None
        self.after_raw: bytes | None = None
        self.changed = False

    def __enter__(self) -> "Brsar":
        self.snapshot = ArchiveSnapshot.capture(self.archive)
        return self.archive

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self.snapshot is None:
            raise RuntimeError("Archive transaction was not entered")
        if exc_type is not None:
            self.snapshot.restore(self.archive)
            return False

        self.validation = validate_archive(self.archive, round_trip=self.round_trip)
        if not self.validation.ok:
            self.snapshot.restore(self.archive)
            self.validation.raise_if_invalid()

        self.after_raw = self.archive.to_bytes()
        self.changed = self.after_raw != self.snapshot.raw
        return False


@dataclass(slots=True)
class ArchiveMutationPreview(Generic[T]):
    """Result of running a mutation against an isolated archive clone."""

    value: T
    archive: "Brsar"
    changed: bool
    validation: ArchiveValidationReport


def preview_archive_mutation(
        archive: "Brsar",
        operation: Callable[["Brsar"], T],
        *,
        label: str = "Archive mutation preview",
        round_trip: bool = True,
) -> ArchiveMutationPreview[T]:
    """Run an operation on a serialized clone, leaving the source untouched."""
    preview = ArchiveSnapshot.capture(archive).clone()
    transaction = ArchiveTransaction(preview, label=label, round_trip=round_trip)
    with transaction as candidate:
        value = operation(candidate)
    assert transaction.validation is not None
    return ArchiveMutationPreview(
        value=value,
        archive=preview,
        changed=transaction.changed,
        validation=transaction.validation,
    )
