import atexit
import io
import json
import shutil
import struct
import threading
import time
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
from webview import FileDialog

from pysar.core.exceptions import ArchiveDumpCancelled
from pysar.core.model.brsar import FileEntry, SoundDataEntry, SoundType, StreamSoundInfo
from pysar.discord_presence import DiscordPresence
from pysar import __display_name__, __display_version__, __release_phase__, __version__
from pysar.services import (
    ArchiveService,
    AudioService,
    PreviewOptions,
    ProjectService,
    RecentArchiveService,
    SettingsService,
)
from pysar.seq.archive import make_playback_context
from pysar.seq.renderer import SequenceRenderer
from pysar.seq.resolver import clear_wave_payload_cache, midi_ratio
from pysar.seq.types import PlaybackContext


def _empty_ui_data() -> dict[str, Any]:
    return {
        "archive": None,
        "sounds": [],
        "banks": [],
        "groups": [],
        "players": [],
        "waveArchives": [],
        "files": [],
    }


def _sequence_related_track_order(
        tracks: list[dict[str, Any]],
        root_index: int,
) -> list[dict[str, Any]]:
    if not tracks or not 0 <= int(root_index) < len(tracks):
        return []

    def resolve_target(offset: int) -> int | None:
        offset = int(offset)
        for candidate in tracks:
            if int(candidate["startOffset"]) == offset:
                return int(candidate["index"])
        for candidate in tracks:
            if int(candidate["startOffset"]) <= offset < int(candidate["endOffset"]):
                return int(candidate["index"])
        return None

    root_index = int(root_index)
    ordered = []
    seen: set[int] = set()
    pending = [{"trackIndex": root_index, "kind": "root", "sourceTrackIndex": None}]
    while pending:
        relation = pending.pop()
        source_index = int(relation["trackIndex"])
        if source_index in seen:
            continue
        seen.add(source_index)
        ordered.append(relation)
        source = tracks[source_index]
        references = sorted(
            source.get("references", []),
            key=lambda reference: 0 if reference.get("kind") == "fallthrough" else 1,
        )
        children = []
        for reference in references:
            target_index = resolve_target(int(reference["targetOffset"]))
            if target_index is None or target_index == source_index or target_index in seen:
                continue
            children.append({
                "trackIndex": target_index,
                "kind": str(reference["kind"]),
                "sourceTrackIndex": source_index,
                "trackNo": reference.get("trackNo"),
            })
        pending.extend(reversed(children))
    return ordered


class PysarApi:
    _MAX_AUDIO_CACHE_BYTES = 128 * 1024 * 1024
    _MAX_SEQUENCE_SOURCE_CACHE_BYTES = 32 * 1024 * 1024
    _MAX_SEQUENCE_SOURCE_CACHE_ITEMS = 4
    _MAX_SEQUENCE_SOURCE_WARMERS = 2
    _TRUNCATED_SEQUENCE_PREVIEW_MS = 30_000
    _dump_state_init_lock = threading.Lock()
    _bank_edit_lock_init_lock = threading.Lock()

    def __init__(self) -> None:
        self._window = None
        self._window_close_lock = threading.Lock()
        self._window_close_authorized = False
        self._window_close_prompt_pending = False
        self._dump_lock = threading.Lock()
        self._dump_in_progress = False
        self._dump_commit_complete = False
        self._dump_cancel_event = threading.Event()
        self._bank_edit_lock = threading.Lock()
        self.project_service = ProjectService()
        self.archive_service = ArchiveService()
        self.audio_service = AudioService()
        self.recent_service = RecentArchiveService()
        self.settings_service = SettingsService()
        self.session = self.project_service.new_session()
        self.discord_presence = DiscordPresence()
        self.discord_presence.start()
        atexit.register(self.discord_presence.close)
        self._stream_specs: dict[str, dict[str, Any]] = {}
        self._stream_lock = threading.Lock()
        self._context_cache: dict[str, PlaybackContext] = {}
        self._context_cache_lock = threading.Lock()
        self._strm_source_revision = 0
        self._duration_cache: dict[tuple, int] = {}
        self._sequence_playback_cache: dict[tuple, dict[str, Any]] = {}
        self._duration_pending: set[tuple] = set()
        self._duration_lock = threading.Lock()
        self._warm_lock = threading.Lock()
        self._warm_keys: set[tuple] = set()
        self._audio_cache_lock = threading.Lock()
        self._audio_cache: dict[tuple, bytes] = {}
        self._prerendering: set[tuple] = set()
        self._sequence_source_cache_lock = threading.Lock()
        self._sequence_source_cache: dict[tuple[str, int, int], tuple[bytes, str]] = {}
        self._sequence_source_pending: dict[tuple[str, int, int], dict[str, Any]] = {}
        self._sequence_source_failures: dict[tuple[str, int, int], str] = {}
        self._sequence_source_warmers = threading.BoundedSemaphore(
            self._MAX_SEQUENCE_SOURCE_WARMERS,
        )
        self._stream_server = self._start_stream_server()
        atexit.register(self._stream_server.shutdown)
        threading.Thread(target=self._warm_audio_codecs, daemon=True).start()

    def bind(self, window) -> None:
        self._window = window

    @property
    def dump_in_progress(self) -> bool:
        """Whether a staged whole-archive dump is currently being written."""
        self._ensure_dump_cancellation_state()
        with self._dump_lock:
            return self._dump_in_progress

    def _ensure_dump_cancellation_state(self) -> threading.Event:
        """Lazily support lightweight/headless instances made with ``__new__``."""
        with self._dump_state_init_lock:
            if not hasattr(self, "_dump_lock"):
                self._dump_lock = threading.Lock()
            if not hasattr(self, "_dump_in_progress"):
                self._dump_in_progress = False
            if not hasattr(self, "_dump_commit_complete"):
                self._dump_commit_complete = False
            event = getattr(self, "_dump_cancel_event", None)
            if event is None:
                event = threading.Event()
                self._dump_cancel_event = event
            return event

    def abort_dump(self) -> dict:
        """Request cancellation without waiting behind the long-running dump."""
        cancel_event = self._ensure_dump_cancellation_state()
        with self._dump_lock:
            active = bool(self._dump_in_progress and not self._dump_commit_complete)
            if active:
                cancel_event.set()
        return {"ok": True, "abortRequested": active}

    def request_window_close_prompt(self) -> None:
        """Ask the frontend to confirm a native window close once."""
        with self._window_close_lock:
            if self._window_close_prompt_pending:
                return
            self._window_close_prompt_pending = True

        def dispatch() -> None:
            try:
                self.push_event("window_close_requested", None)
            except Exception:
                # Let a later close attempt retry if the frontend was still
                # loading or was temporarily unavailable.
                with self._window_close_lock:
                    self._window_close_prompt_pending = False

        threading.Timer(0.01, dispatch).start()

    def consume_window_close_authorization(self) -> bool:
        """Consume the one-shot bypass used by a confirmed native close."""
        with self._window_close_lock:
            authorized = self._window_close_authorized
            self._window_close_authorized = False
            if authorized:
                self._window_close_prompt_pending = False
            return authorized

    def cancel_window_close(self) -> dict:
        with self._window_close_lock:
            self._window_close_prompt_pending = False
        return {"ok": True}

    def confirm_window_close(self) -> dict:
        """Close the native window after Save or Discard was chosen in JS."""
        window = self._window
        if window is None:
            return {"ok": False, "error": "Application window is unavailable"}
        with self._window_close_lock:
            self._window_close_authorized = True
            self._window_close_prompt_pending = False

        def destroy() -> None:
            try:
                window.destroy()
            except Exception:
                with self._window_close_lock:
                    self._window_close_authorized = False

        # Returning through the JS bridge before destroying the webview avoids
        # tearing down an in-flight Save/Discard call.
        threading.Timer(0.01, destroy).start()
        return {"ok": True}

    def app_ready(self) -> dict:
        if self._window is not None:
            threading.Timer(0.01, self._window.show).start()
        return {"ok": True}

    def set_discord_presence_icon(self, icon: str | None = None) -> dict:
        """Update Discord Rich Presence for the currently active application tab."""
        self.discord_presence.set_small_image(icon)
        return {"ok": True}

    def get_app_metadata(self) -> dict:
        return {
            "ok": True,
            "name": __display_name__,
            "version": __version__,
            "displayVersion": __display_version__,
            "phase": __release_phase__,
        }

    def _require_safe_mutation(
            self,
            action: str,
            kind: str,
            *identity: int,
    ) -> None:
        """Backend guard shared by API mutations not implemented on Brsar."""
        archive = self.project_service.require_archive(self.session)
        archive.set_safe_mode(self.session.safe_mode)
        archive.require_safe_mutation(action, kind, *identity)

    def set_safe_mode(self, enabled: bool, confirmed: bool = False) -> dict:
        """Set the runtime-only safety lock; unlocking requires confirmation."""
        try:
            archive = self.project_service.require_archive(self.session)
            enabled = bool(enabled)
            if not enabled and self.session.safe_mode and not bool(confirmed):
                return {
                    "ok": False,
                    "requiresConfirmation": True,
                    "error": (
                        "Unsafe mode permits renaming, deleting and reindexing "
                        "original game resources."
                    ),
                }
            self.session.safe_mode = enabled
            archive.set_safe_mode(enabled)
            return {"ok": True, "safeMode": enabled, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_archive_dialog(self) -> Optional[dict]:
        if self._window is None:
            return None
        result = self._window.create_file_dialog(
            dialog_type=FileDialog.OPEN,
            allow_multiple=False,
            file_types=("BRSAR archive (*.brsar)", "All files (*.*)"),
        )
        if not result:
            return None
        return self.load_archive(result[0])

    def load_archive(self, path: str) -> dict:
        try:
            self._clear_audio_streams()
            clear_wave_payload_cache()
            self.session = self.project_service.open_archive(path)
            try:
                recent = self.recent_service.remember(path)
            except Exception:
                recent = self.recent_service.list_archives()
            return {"ok": True, "data": self._ui_data(), "recentArchives": recent}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_recent_archives(self) -> dict:
        return {"ok": True, "recentArchives": self.recent_service.list_archives()}

    def forget_recent_archive(self, path: str) -> dict:
        return {"ok": True, "recentArchives": self.recent_service.forget(path)}

    def save_archive(self, path: Optional[str] = None) -> dict:
        try:
            saved = self.project_service.save_archive(self.session, path)
            return {"ok": True, "path": str(saved)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _next_dump_destination(parent: Path, stem: str) -> Path:
        """Return a non-existing archive dump directory below *parent*."""
        candidate = parent / f"{stem}_dump"
        suffix = 2
        while candidate.exists():
            candidate = parent / f"{stem}_dump_{suffix}"
            suffix += 1
        return candidate

    def _converted_dump_work_units(
        self,
        archive,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> int:
        """Count the individual conversion outputs used for dump progress."""
        units = (
            len(archive.data.bank_entries)
            + sum(1 for item in archive.data.embedded_files.values() if item.magic == "RWAR")
            + len(archive.data.sound_entries)  # sound metadata
        )
        for sound_id, entry in enumerate(archive.data.sound_entries):
            if cancel_callback is not None and cancel_callback():
                raise ArchiveDumpCancelled("Archive dump cancelled")
            if entry.sound_type in {SoundType.STRM, SoundType.WAVE}:
                units += 1  # one WAV
            elif entry.sound_type == SoundType.SEQ:
                variation_count = 0
                try:
                    variations = self._sequence_variations(sound_id).get("variations", [])
                    variation_count = len(variations) if isinstance(variations, list) else 0
                except Exception:
                    # The later export records a useful manifest error. Keep a
                    # conservative unit here so the progress UI remains usable.
                    pass
                units += 2 + variation_count  # default WAV, variation WAVs, MIDI
        return units

    def _dump_converted_sound_assets(
        self,
        archive,
        root: Path,
        manifest: dict[str, Any],
        progress_callback: Callable[[str, bool], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> int:
        """Add the same per-sound exports available from the All Sounds view."""
        records = {
            int(record["sound_index"]): record
            for record in manifest.get("sounds", [])
            if isinstance(record, dict) and record.get("sound_index") is not None
        }
        manifest_errors = manifest.setdefault("errors", [])
        converted_count = 0

        def check_cancelled() -> None:
            if cancel_callback is not None and cancel_callback():
                raise ArchiveDumpCancelled("Archive dump cancelled")

        def report_progress(message: str, completed: bool = False) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(message, completed)
            except Exception:
                pass

        for sound_id, entry in enumerate(archive.data.sound_entries):
            check_cancelled()
            record = records.get(sound_id)
            if record is None:
                continue
            sound_name = archive._resolve_sound_name(sound_id, entry)
            safe_name = archive._sanitize_name(sound_name, fallback=f"SOUND_{sound_id:05d}")
            sound_dir = root / "sounds" / f"{safe_name}__{sound_id:05d}"
            audio_dir = sound_dir / "audio"
            audio_dir.mkdir(parents=True, exist_ok=True)
            metadata_path = sound_dir / "sound.json"

            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}

            outputs: list[str] = []
            format_outputs: dict[str, list[str]] = {}
            errors = list(metadata.get("errors") or [])

            def record_export(label: str, path: Path) -> None:
                nonlocal converted_count
                def on_file_written(saved_path: Path) -> None:
                    report_progress(
                        f"Exported {label.upper()}: {saved_path.name}",
                        completed=True,
                    )

                result = self._export_sound_to_path(
                    sound_id,
                    path,
                    on_file_written=on_file_written,
                    cancel_callback=cancel_callback,
                )
                check_cancelled()
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or f"{label} export failed")
                exported = [Path(str(item)) for item in result.get("paths", []) if item]
                if not exported:
                    raise RuntimeError(f"{label} export produced no files")
                relative = [str(item.relative_to(root)) for item in exported]
                format_outputs[label] = relative
                outputs.extend(relative)
                converted_count += len(relative)

            if entry.sound_type not in {SoundType.SEQ, SoundType.STRM, SoundType.WAVE}:
                message = f"Unsupported sound type: {entry.sound_type}"
                errors.append(message)
                manifest_errors.append({
                    "scope": f"sound:{sound_id}:{sound_name}",
                    "message": message,
                })
            else:
                # Keep formats independent. For example, a malformed sequence
                # can still retain its MIDI source even if it cannot be rendered
                # to WAV on this machine.
                export_jobs = [("wav", audio_dir / f"{safe_name}.wav")]
                if entry.sound_type == SoundType.SEQ:
                    # The WAV export renders the default playback plus every
                    # selectable variation; MIDI preserves the BRSEQ source.
                    export_jobs.append(("midi", audio_dir / f"{safe_name}.midi"))
                for label, path in export_jobs:
                    report_progress(f"Exporting {label.upper()}: {sound_name}")
                    try:
                        record_export(label, path)
                    except ArchiveDumpCancelled:
                        raise
                    except Exception as exc:
                        message = f"{label.upper()}: {exc}"
                        errors.append(message)
                        manifest_errors.append({
                            "scope": f"sound:{sound_id}:{sound_name}",
                            "message": message,
                        })

            metadata["outputs"] = outputs
            metadata["converted_outputs"] = format_outputs
            metadata["errors"] = errors
            check_cancelled()
            archive._write_json(metadata_path, metadata)
            record["outputs"] = outputs
            record["n_outputs"] = len(outputs)
            record["n_errors"] = len(errors)

        return converted_count

    @staticmethod
    def _dump_original_external_files(
        archive,
        root: Path,
        manifest: dict[str, Any],
        external_root: Path | None,
        external_resolver: Callable[[str | None], Path | None] | None = None,
        progress_callback: Callable[[str, bool], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> None:
        """Copy externally stored subfiles beside the embedded raw dump."""
        raw_dir = root / "raw_external"
        raw_files = manifest.setdefault("raw_files", [])
        manifest_errors = manifest.setdefault("errors", [])
        copied_any = False

        def check_cancelled() -> None:
            if cancel_callback is not None and cancel_callback():
                raise ArchiveDumpCancelled("Archive dump cancelled")

        def copy_file(source: Path, destination: Path) -> None:
            with source.open("rb") as source_file, destination.open("wb") as output:
                while True:
                    check_cancelled()
                    chunk = source_file.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
            check_cancelled()
            shutil.copystat(source, destination)

        def report_progress(message: str, completed: bool = False) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(message, completed)
            except Exception:
                pass

        for file_index, file_entry in enumerate(archive.data.file_entries):
            check_cancelled()
            source_text = str(file_entry.external_file_path or "").strip()
            if not source_text:
                continue
            report_progress(f"Copying external file {file_index}: {source_text}")
            source = external_resolver(source_text) if external_resolver is not None else None
            if source is None:
                source = Path(source_text.replace("\\", "/")).expanduser()
                if not source.is_absolute():
                    if external_root is None:
                        manifest_errors.append({
                            "scope": f"external_file:{file_index}",
                            "message": f"Cannot resolve external file: {source_text}",
                        })
                        report_progress(f"Skipped external file {file_index}: {source_text}", completed=True)
                        continue
                    source = external_root / source
            if not source.is_file():
                manifest_errors.append({
                    "scope": f"external_file:{file_index}",
                    "message": f"External file not found: {source_text}",
                })
                report_progress(f"Missing external file {file_index}: {source_text}", completed=True)
                continue

            raw_dir.mkdir(parents=True, exist_ok=True)
            safe_stem = archive._sanitize_name(source.stem, fallback=f"FILE_{file_index:06d}")
            suffix = source.suffix or ".bin"
            destination = raw_dir / f"file_{file_index:06d}_{safe_stem}{suffix}"
            copy_file(source, destination)
            raw_files.append(str(destination.relative_to(root)))
            copied_any = True
            report_progress(f"Copied external file {file_index}: {destination.name}", completed=True)

        if copied_any:
            manifest["raw_external_files"] = [
                path for path in raw_files if str(path).startswith("raw_external/")
            ]

    def dump_archive_to_path(self, output_dir: str, mode: str = "converted") -> dict:
        """Dump the complete open BRSAR as originals or converted assets.

        The dump is staged beside the destination and renamed only after its
        manifest has been written.  Existing destinations are never mixed with
        a new dump, which prevents stale files and makes the operation safe to
        retry after a failed conversion.
        """
        cancel_event = self._ensure_dump_cancellation_state()
        with self._dump_lock:
            if self._dump_in_progress:
                return {"ok": False, "error": "An archive dump is already in progress"}
            # Clear only while claiming a new dump. Abort requests for an
            # active operation can then safely set the event without racing a
            # later initialization step.
            cancel_event.clear()
            self._dump_in_progress = True
            self._dump_commit_complete = False

        staging: Path | None = None
        try:
            def check_cancelled() -> None:
                if cancel_event.is_set():
                    raise ArchiveDumpCancelled("Archive dump cancelled")

            check_cancelled()
            archive = self.project_service.require_archive(self.session)
            # Keep the dump entry point usable in headless/library contexts
            # that construct the API around an existing session without
            # running the desktop initializer.
            if not hasattr(self, "archive_service"):
                self.archive_service = ArchiveService()
            if not hasattr(self, "audio_service"):
                self.audio_service = AudioService()
            dump_mode = str(mode or "converted").strip().lower()
            if dump_mode == "raw":
                dump_mode = "original"
            if dump_mode not in {"original", "converted"}:
                raise ValueError("Dump mode must be 'original' or 'converted'")
            destination = Path(str(output_dir)).expanduser()
            if not destination.name or destination.name in {".", ".."}:
                raise ValueError("Choose a folder name for the archive dump")
            if destination.exists():
                kind = "file" if destination.is_file() else "folder"
                raise FileExistsError(
                    f"The destination {kind} already exists: {destination}"
                )
            check_cancelled()

            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = destination.parent / (
                f".{destination.name}.pysar-dump-{uuid.uuid4().hex}"
            )
            external_root = (
                self.session.archive_path.parent
                if self.session.archive_path is not None
                else None
            )
            external_file_count = sum(
                1 for entry in archive.data.file_entries
                if str(entry.external_file_path or "").strip()
            )
            if dump_mode == "original":
                work_units = len(archive.data.embedded_files) + external_file_count
            else:
                # Calculating sequence variations lets each generated WAV move
                # the bar, rather than leaving it frozen for a whole sequence.
                try:
                    self.push_event("dump_progress", {
                        "mode": dump_mode,
                        "completed": 0,
                        "total": 0,
                        "percent": 0,
                        "detail": "Inspecting sequence variations…",
                    })
                except Exception:
                    pass
                work_units = self._converted_dump_work_units(
                    archive,
                    cancel_callback=cancel_event.is_set,
                )
            progress_total = max(1, work_units + 1)  # finalise and publish
            progress_completed = 0

            def report_dump_progress(detail: str, completed: bool = False) -> None:
                nonlocal progress_completed
                if completed:
                    progress_completed = min(progress_total, progress_completed + 1)
                try:
                    self.push_event("dump_progress", {
                        "mode": dump_mode,
                        "completed": progress_completed,
                        "total": progress_total,
                        "percent": int(round(progress_completed * 100 / progress_total)),
                        "detail": detail,
                    })
                except Exception:
                    # UI updates must never make a dump fail.
                    pass

            report_dump_progress(
                "Preparing original subfiles…" if dump_mode == "original" else "Preparing converted assets…"
            )

            if dump_mode == "original":
                # Keep this dump lossless and uncluttered: only the original
                # embedded Nintendo subfiles plus their manifest.
                archive.dump_archive(
                    staging,
                    include_raw=True,
                    decode_assets=False,
                    decode_sounds=False,
                    include_sound_metadata=False,
                    decode_wave_archives=False,
                    include_streams=False,
                    overwrite=False,
                    progress=False,
                    external_root=external_root,
                    external_resolver=self._find_external_brstm_path,
                    progress_callback=report_dump_progress,
                    cancel_callback=cancel_event.is_set,
                )
            else:
                # Let the archive dumper create the shared SF2 and wave-archive
                # WAV assets while retaining lossless embedded originals for
                # recovery. Per-sound assets are added below through the same
                # exporters used by the All Sounds view.
                archive.dump_archive(
                    staging,
                    include_raw=True,
                    decode_assets=True,
                    decode_sounds=False,
                    include_sound_metadata=True,
                    decode_wave_archives=True,
                    include_streams=True,
                    loop_count=1,
                    seq_max_ticks=12_000,
                    overwrite=False,
                    progress=False,
                    external_root=external_root,
                    external_resolver=self._find_external_brstm_path,
                    progress_callback=report_dump_progress,
                    cancel_callback=cancel_event.is_set,
                )
            check_cancelled()
            manifest_path = staging / "manifest.json"
            if not manifest_path.is_file():
                raise RuntimeError("Archive dump completed without a manifest")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise RuntimeError("Archive dump produced an invalid manifest")

            manifest["dump_mode"] = dump_mode
            if dump_mode == "original":
                self._dump_original_external_files(
                    archive,
                    staging,
                    manifest,
                    external_root,
                    external_resolver=self._find_external_brstm_path,
                    progress_callback=report_dump_progress,
                    cancel_callback=cancel_event.is_set,
                )
            else:
                self._dump_converted_sound_assets(
                    archive,
                    staging,
                    manifest,
                    progress_callback=report_dump_progress,
                    cancel_callback=cancel_event.is_set,
                )
            check_cancelled()
            report_dump_progress("Finalising archive dump…")
            archive._write_json(manifest_path, manifest)

            # Make Abort and the publication commit mutually exclusive. If an
            # abort call reports ``abortRequested``, it won the lock first and
            # the destination is guaranteed to remain unpublished.
            with self._dump_lock:
                check_cancelled()
                staging.rename(destination)
                staging = None
                self._dump_commit_complete = True
            # A failed conversion can intentionally skip one or more output
            # units while still yielding a useful partial dump. Completion of
            # the staged operation should nevertheless always read 100%.
            progress_completed = progress_total
            report_dump_progress("Archive dump complete")
            errors = manifest.get("errors")
            if not isinstance(errors, list):
                errors = []
            decoded_paths: set[str] = set()
            for bank in manifest.get("banks", []):
                if isinstance(bank, dict):
                    decoded_paths.update(str(path) for path in bank.get("outputs", []) if path)
            for wave_archive in manifest.get("wave_archives", []):
                if isinstance(wave_archive, dict):
                    decoded_paths.update(str(path) for path in wave_archive.get("outputs", []) if path)
            sound_output_count = sum(
                max(0, int(sound.get("n_outputs", 0)))
                for sound in manifest.get("sounds", [])
                if isinstance(sound, dict)
            )
            raw_count = len(manifest.get("raw_files", []))
            converted_count = len(decoded_paths) + sound_output_count
            label = "Original subfiles" if dump_mode == "original" else "Converted assets"
            item_count = raw_count if dump_mode == "original" else converted_count
            summary = f"{label} · {item_count} file{'s' if item_count != 1 else ''}"
            if errors:
                summary += f" · {len(errors)} issue{'s' if len(errors) != 1 else ''}"
            return {
                "ok": True,
                "path": str(destination),
                "mode": dump_mode,
                "counts": manifest.get("counts", {}),
                "rawCount": raw_count,
                "decodedCount": converted_count,
                "errorCount": len(errors),
                "partial": bool(errors),
                "errors": errors,
                "summary": summary,
            }
        except ArchiveDumpCancelled:
            return {
                "ok": False,
                "cancelled": True,
                "error": "Archive dump cancelled",
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            with self._dump_lock:
                self._dump_in_progress = False
                self._dump_commit_complete = False
                cancel_event.clear()

    def dump_archive_dialog(self, mode: str = "converted") -> dict:
        """Choose a parent folder and create one kind of archive dump inside it."""
        if self._window is None:
            return {"ok": False, "error": "No window"}
        try:
            archive = self.project_service.require_archive(self.session)
            result = self._window.create_file_dialog(
                dialog_type=FileDialog.FOLDER,
                allow_multiple=False,
            )
            if not result:
                return {"ok": False, "error": "Cancelled"}
            selected = result[0] if isinstance(result, (list, tuple)) else result
            parent = Path(str(selected)).expanduser()
            if not parent.is_dir():
                return {"ok": False, "error": f"Folder not found: {parent}"}

            source_stem = (
                self.session.archive_path.stem
                if self.session.archive_path is not None
                else "archive"
            )
            safe_stem = archive._sanitize_name(source_stem, fallback="archive")
            destination = self._next_dump_destination(parent, safe_stem)
            return self.dump_archive_to_path(str(destination), mode)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def close_archive(self) -> dict:
        self._clear_audio_streams()
        self.project_service.close_session(self.session)
        return {"ok": True, "data": _empty_ui_data()}

    def get_state(self) -> dict:
        return {"ok": True, "data": self._ui_data()}

    def list_sounds(self) -> list:
        return self._ui_data()["sounds"]

    def list_banks(self) -> list:
        return self._ui_data()["banks"]

    def list_players(self) -> list:
        return self._ui_data()["players"]

    def list_groups(self) -> list:
        return self._ui_data()["groups"]

    def list_archives(self) -> list:
        return self._ui_data()["waveArchives"]

    def get_wave_archive_details(self, file_id: int) -> dict:
        try:
            details = self.archive_service.get_wave_archive_details(self.session, int(file_id))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "data": self._wave_archive_details_payload(details)}

    @staticmethod
    def _wave_archive_details_payload(details) -> dict[str, Any]:
        """Serialize one wave archive's live sample metadata for the UI."""
        return {
            "fileId": details.file_id,
            "name": details.name,
            "size": details.size,
            "waveCount": details.wave_count,
            "waves": [
                {
                    "index": w.index,
                    "encoding": w.encoding,
                    "sampleRate": w.sample_rate,
                    "channels": w.n_channels,
                    "samples": w.n_samples,
                    "loopStart": w.loop_start,
                    "looped": w.is_looped,
                    "sizeBytes": w.size_bytes,
                    "durationMs": w.duration_ms,
                }
                for w in details.waves
            ],
        }

    def get_bank_details(self, bank_id: int) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            bank_id = int(bank_id)
            details = self.archive_service.get_bank_details(self.session, int(bank_id))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        instruments = [
            {
                "program": ins.program,
                "name": ins.name,
                "zoneCount": ins.zone_count,
                "waveIndices": ins.wave_indices,
                "keyLow": ins.key_low,
                "keyHigh": ins.key_high,
                "isEmpty": ins.is_empty,
                "isNew": archive.is_new("bank_instrument", bank_id, ins.program),
                "protected": archive.is_protected("bank_instrument", bank_id, ins.program),
                "zones": [
                    {
                        "waveIndex": z.wave_index,
                        "keyLow": z.key_low,
                        "keyHigh": z.key_high,
                        "velocityLow": z.velocity_low,
                        "velocityHigh": z.velocity_high,
                        "originalKey": z.original_key,
                        "volume": z.volume,
                        "pan": z.pan,
                        "pitch": z.pitch,
                        "attack": z.attack,
                        "decay": z.decay,
                        "sustain": z.sustain,
                        "release": z.release,
                        "hold": z.hold,
                        "noteOffType": z.note_off_type,
                        "alternateAssign": z.alternate_assign,
                        "isNew": archive.is_new("bank_zone", bank_id, ins.program, zone_index),
                        "protected": archive.is_protected("bank_zone", bank_id, ins.program, zone_index),
                    }
                    for zone_index, z in enumerate(ins.zones)
                ],
            }
            for ins in details.instruments
        ]
        return {
            "ok": True,
            "data": {
                "bankIndex": details.bank_index,
                "name": details.name,
                "instrumentCount": details.instrument_count,
                "activeInstrumentCount": details.active_instrument_count,
                "waveCount": details.wave_count,
                "audioFileId": details.audio_file_id,
                "instruments": instruments,
            },
        }

    def get_sequence_details(self, sound_id: int) -> dict:
        try:
            data = self._sequence_details(int(sound_id))
            return {"ok": True, "data": data}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def lint_sequence_text(self, source_text: str) -> dict:
        """Validate MML through the same parse/write/read path used by Compile."""
        import re

        try:
            from pysar.core.format.rseq import Brseq

            compiled = Brseq.from_text(str(source_text))
            raw = compiled.to_bytes()
            Brseq.from_bytes(raw)
            return {"ok": True, "valid": True}
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            line_match = re.search(r"\bLine\s+(\d+)\b", message, flags=re.IGNORECASE)
            return {
                "ok": True,
                "valid": False,
                "error": message,
                "line": int(line_match.group(1)) if line_match else None,
            }

    def get_sequence_variations(self, sound_id: int) -> dict:
        try:
            data = self._sequence_variations(int(sound_id))
            return {"ok": True, "data": data}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def references(self, kind: str, item_id: int, depth: int = 2) -> dict:
        return {"nodes": [], "edges": []}

    def render_preview(self, sound_id: int) -> Optional[str]:
        return None

    def get_sound_preview(
        self,
        sound_id: int,
        seq_note_override: int | None = None,
        seq_program_override: int | None = None,
        seq_random_overrides: Any = None,
    ) -> dict:
        result = self.get_sound_stream_url(
            sound_id,
            seq_note_override,
            seq_program_override,
            seq_random_overrides,
        )
        if result.get("ok"):
            result["dataUrl"] = result["url"]
        return result

    def get_sound_duration(
        self,
        sound_id: int,
        seq_note_override: int | None = None,
        seq_program_override: int | None = None,
        seq_random_overrides: Any = None,
    ) -> dict:
        try:
            spec = {
                "seq_note_override": self._valid_midi_note(seq_note_override),
                "seq_program_override": self._valid_program(seq_program_override),
                "seq_random_overrides": self._valid_random_overrides(seq_random_overrides),
            }
            key = self._duration_cache_key(int(sound_id), spec)
            with self._duration_lock:
                cached = self._duration_cache.get(key)
                seq_playback = self._sequence_playback_cache.get(key)
            if cached is not None:
                self._warm_sound_preview_async(int(sound_id), spec)
                effective_duration = (
                    int(seq_playback.get("loopEndMs") or cached)
                    if seq_playback and seq_playback.get("looped")
                    else cached
                )
                result = {"ok": True, "durationMs": effective_duration}
                if seq_playback is not None:
                    result["seqPlayback"] = dict(seq_playback)
                return result
            archive = self.project_service.require_archive(self.session)
            entry = self.archive_service._sound_entry(archive, int(sound_id))
            if entry.sound_type == SoundType.SEQ:
                # Walking a long BRSEQ can monopolize the GIL for hundreds of
                # milliseconds. Keep it off the click-to-first-audio path and
                # publish the duration shortly after playback has started.
                self._schedule_sequence_duration(int(sound_id), spec, key)
                self._warm_sound_preview_async(int(sound_id), spec)
                return {"ok": True, "durationMs": 0, "pending": True}
            duration_ms = self._estimate_sound_duration_ms(int(sound_id), spec)
            with self._duration_lock:
                self._duration_cache[key] = duration_ms
            self._warm_sound_preview_async(int(sound_id), spec)
            return {
                "ok": True,
                "durationMs": duration_ms,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _schedule_sequence_duration(self, sound_id: int, spec: dict[str, Any], key: tuple) -> None:
        with self._duration_lock:
            if key in self._duration_cache or key in self._duration_pending:
                return
            self._duration_pending.add(key)

        def worker() -> None:
            try:
                current_path = str(self.session.archive_path) if self.session.archive_path is not None else None
                if current_path != key[0]:
                    return
                duration_ms, seq_playback = self._inspect_sound_timing(sound_id, spec)
                current_path = str(self.session.archive_path) if self.session.archive_path is not None else None
                if current_path != key[0]:
                    return
                with self._duration_lock:
                    self._duration_cache[key] = duration_ms
                    if seq_playback is not None:
                        self._sequence_playback_cache[key] = seq_playback
                effective_duration = (
                    int(seq_playback.get("loopEndMs") or duration_ms)
                    if seq_playback and seq_playback.get("looped")
                    else duration_ms
                )
                self.push_event("duration_update", {
                    "soundId": sound_id,
                    "durationMs": effective_duration,
                    "seqNoteOverride": spec.get("seq_note_override"),
                    "seqProgramOverride": spec.get("seq_program_override"),
                    "seqRandomOverrides": list(spec.get("seq_random_overrides") or ()),
                    "seqPlayback": seq_playback,
                })
            finally:
                with self._duration_lock:
                    self._duration_pending.discard(key)

        timer = threading.Timer(0.35, worker)
        timer.daemon = True
        timer.start()

    def get_sound_stream_url(
        self,
        sound_id: int,
        seq_note_override: int | None = None,
        seq_program_override: int | None = None,
        seq_random_overrides: Any = None,
        offset_ms: int = 0,
        strm_track_indices: Any = None,
    ) -> dict:
        try:
            token = uuid.uuid4().hex
            requested_offset_ms = max(0, int(offset_ms))
            spec = {
                "kind": "sound",
                "sound_id": int(sound_id),
                "seq_note_override": self._valid_midi_note(seq_note_override),
                "seq_program_override": self._valid_program(seq_program_override),
                "seq_random_overrides": self._valid_random_overrides(seq_random_overrides),
                "offset_ms": requested_offset_ms,
                "strm_track_indices": self._valid_strm_track_indices(strm_track_indices),
            }
            archive = self.project_service.require_archive(self.session)
            entry = self.archive_service._sound_entry(archive, int(sound_id))
            spec["sound_type"] = entry.sound_type.name
            strm_context = None
            strm_playback = None
            strm_progressive = None
            if entry.sound_type == SoundType.STRM:
                spec["strm_source_revision"] = int(getattr(self, "_strm_source_revision", 0))
                sound_name = self.archive_service._sound_name(archive, int(sound_id), entry)
                strm_context = self._get_or_create_context(archive, sound_name)
                if strm_context.brstm is not None:
                    brstm = strm_context.brstm
                    # A decoded Web Audio buffer needs full-file coordinates:
                    # loopStart and UI seek offsets are absolute. Track mixes
                    # therefore share a full-stream cache regardless of where
                    # playback is resumed; the frontend applies offset_ms.
                    spec["offset_ms"] = 0
                    spec["sample_rate"] = max(1, int(brstm.sample_rate))
                    spec["total_frames"] = max(0, int(brstm.n_samples))
                    strm_playback = self._strm_playback_metadata(brstm)
            cache_key = self._duration_cache_key(int(sound_id), spec)
            if entry.sound_type == SoundType.SEQ:
                with self._duration_lock:
                    duration_ms = self._duration_cache.get(cache_key)
                    seq_playback = self._sequence_playback_cache.get(cache_key)
                if duration_ms is None:
                    # Never make Play wait for a full sequence walk merely to
                    # learn its final length. The stream starts immediately;
                    # the deferred duration scan or stream completion fills
                    # the exact duration/cache in the background.
                    self._schedule_sequence_duration(int(sound_id), spec, cache_key)
                    duration_ms = 0
                    spec["finite_stream"] = False
                else:
                    spec["finite_stream"] = duration_ms > 0
                    if seq_playback and seq_playback.get("looped"):
                        duration_ms = max(0, int(seq_playback.get("loopEndMs") or duration_ms))
                        spec["seq_loop_end_ms"] = duration_ms
                        spec["total_frames"] = max(0, int(seq_playback.get("loopEndFrame") or 0))
            else:
                spec["finite_stream"] = True
                duration_ms = self._estimate_sound_duration_ms(int(sound_id), spec)
            spec["duration_ms"] = duration_ms
            if entry.sound_type == SoundType.STRM:
                if strm_context is not None and strm_context.brstm is not None:
                    brstm = strm_context.brstm
                    selected = spec.get("strm_track_indices")
                    all_tracks = [track["index"] for track in strm_playback["tracks"]]
                    if selected == all_tracks:
                        # All tracks is the default mix and shares the preview
                        # produced when the sound was selected.
                        spec["strm_track_indices"] = None
                    if brstm.is_looped:
                        sample_rate = max(1, int(brstm.sample_rate))
                        total_frames = max(0, int(brstm.n_samples))
                        start_frame = max(
                            0,
                            min(total_frames, round(requested_offset_ms * sample_rate / 1000)),
                        )
                        loop_start_frame = max(0, min(total_frames, int(brstm.loop_start)))
                        start_token = uuid.uuid4().hex
                        start_spec = dict(spec)
                        start_spec["start_frame"] = start_frame
                        start_spec["segment_role"] = "strm_initial"
                        loop_token = None
                        loop_spec = None
                        # If the initial suffix contains loopStart→end, the
                        # browser derives its loop buffer from those same PCM
                        # bytes. A second decode is only needed for a cold seek
                        # that starts inside the loop range.
                        if start_frame > loop_start_frame:
                            loop_token = uuid.uuid4().hex
                            loop_spec = dict(spec)
                            loop_spec["start_frame"] = loop_start_frame
                            loop_spec["segment_role"] = "strm_loop"
                        strm_progressive = {
                            "startToken": start_token,
                            "startSpec": start_spec,
                            "loopToken": loop_token,
                            "loopSpec": loop_spec,
                            "startFrame": start_frame,
                            "loopStartFrame": loop_start_frame,
                            "totalFrames": total_frames,
                            "sampleRate": sample_rate,
                        }
            with self._stream_lock:
                self._stream_specs[token] = spec
                if strm_progressive is not None:
                    self._stream_specs[strm_progressive["startToken"]] = strm_progressive["startSpec"]
                    if strm_progressive["loopToken"] is not None:
                        self._stream_specs[strm_progressive["loopToken"]] = strm_progressive["loopSpec"]
            result = {
                "ok": True,
                "url": self._stream_url(token),
                "durationMs": duration_ms,
            }
            if entry.sound_type == SoundType.SEQ and seq_playback is not None:
                result["seqPlayback"] = dict(seq_playback)
            if strm_playback is not None:
                result["strmPlayback"] = strm_playback
            if strm_progressive is not None:
                result["progressiveStrm"] = {
                    "startUrl": self._stream_url(strm_progressive["startToken"]),
                    "loopUrl": (
                        self._stream_url(strm_progressive["loopToken"])
                        if strm_progressive["loopToken"] is not None
                        else None
                    ),
                    "startFrame": strm_progressive["startFrame"],
                    "loopStartFrame": strm_progressive["loopStartFrame"],
                    "totalFrames": strm_progressive["totalFrames"],
                    "sampleRate": strm_progressive["sampleRate"],
                }
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_wave_sample_stream_url(self, archive_file_id: int, wave_index: int, offset_ms: int = 0) -> dict:
        try:
            token = uuid.uuid4().hex
            spec = {
                "kind": "wave_sample",
                "archive_file_id": int(archive_file_id),
                "wave_index": int(wave_index),
                "offset_ms": max(0, int(offset_ms)),
            }
            duration_ms = self._estimate_wave_sample_duration_ms(int(archive_file_id), int(wave_index))
            spec["duration_ms"] = duration_ms
            with self._stream_lock:
                self._stream_specs[token] = spec
            return {
                "ok": True,
                "url": self._stream_url(token),
                "durationMs": duration_ms,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_bank_note_stream_url(
        self,
        bank_id: int,
        program: int,
        key: int,
        velocity: int = 127,
        offset_ms: int = 0,
    ) -> dict:
        """Return a shared-player stream for the sample mapped to a bank note.

        Bank keyboard previews used to be sent directly to ``sounddevice``.  That
        bypassed the browser player, so it could not show the active preview or
        keep its transport controls in sync.  Resolve the mapped wave here and
        expose it through the same local WAV stream used by the rest of the UI.
        """
        try:
            bank_id = int(bank_id)
            program = int(program)
            key = max(0, min(127, int(key)))
            velocity = max(0, min(127, int(velocity)))
            brbnk, _data_fid, archive = self._get_brbnk_for_bank(bank_id)
            param = brbnk.get_inst_param(program, key, velocity)
            if param is None:
                return {"ok": False, "error": "No sample mapped to this key/velocity"}

            bank_entry = archive.data.bank_entries[bank_id]
            _file_index, audio_fid = self.archive_service._resolve_file_index(
                archive, bank_entry.file_index
            )
            if audio_fid is None:
                return {"ok": False, "error": "Bank has no audio file"}

            wave_index = int(param.wave_index)
            playback_rate = midi_ratio(key, int(param.original_key), float(param.pitch))
            sample_rate, source_frames = self._wave_sample_info(int(audio_fid), wave_index)
            total_frames = max(1, int(np.ceil(source_frames / playback_rate))) if source_frames else 0
            duration_ms = int(round(total_frames * 1000 / max(1, sample_rate)))
            token = uuid.uuid4().hex
            spec = {
                "kind": "bank_note",
                "archive_file_id": int(audio_fid),
                "wave_index": wave_index,
                "volume": max(0.0, min(1.0, float(param.volume) / 127.0)),
                "playback_rate": playback_rate,
                "total_frames": total_frames,
                "offset_ms": max(0, int(offset_ms)),
                "duration_ms": duration_ms,
            }
            with self._stream_lock:
                self._stream_specs[token] = spec
            return {
                "ok": True,
                "url": self._stream_url(token),
                "durationMs": duration_ms,
                "waveIndex": wave_index,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _clear_audio_streams(self) -> None:
        with self._stream_lock:
            self._stream_specs.clear()
        with self._duration_lock:
            self._duration_cache.clear()
            self._sequence_playback_cache.clear()
            self._duration_pending.clear()
        with self._warm_lock:
            self._warm_keys.clear()
        with self._audio_cache_lock:
            self._audio_cache.clear()
            self._prerendering.clear()
        with self._context_cache_lock:
            self._context_cache.clear()
        clear_wave_payload_cache()

    def _clear_strm_sources(self) -> None:
        """Invalidate external-stream state without disturbing other playback."""
        archive = getattr(self.session, "archive", None)
        sound_ids: set[int] = set()
        sound_names: set[str] = set()
        if archive is not None:
            for sound_id, entry in enumerate(archive.data.sound_entries):
                if entry.sound_type != SoundType.STRM:
                    continue
                sound_ids.add(sound_id)
                sound_names.add(self.archive_service._sound_name(archive, sound_id, entry))

        self._strm_source_revision = int(getattr(self, "_strm_source_revision", 0)) + 1
        with self._stream_lock:
            self._stream_specs = {
                token: spec for token, spec in self._stream_specs.items()
                if spec.get("sound_type") != SoundType.STRM.name
                and int(spec.get("sound_id", -1)) not in sound_ids
            }
        with self._duration_lock:
            self._duration_cache = {
                key: value for key, value in self._duration_cache.items()
                if len(key) < 2 or int(key[1]) not in sound_ids
            }
            self._duration_pending = {
                key for key in self._duration_pending
                if len(key) < 2 or int(key[1]) not in sound_ids
            }
        with self._warm_lock:
            self._warm_keys = {
                key for key in self._warm_keys
                if len(key) < 2 or int(key[1]) not in sound_ids
            }
        with self._audio_cache_lock:
            self._audio_cache = {
                key: value for key, value in self._audio_cache.items()
                if not (len(key) >= 3 and key[1] == "sound" and int(key[2]) in sound_ids)
            }
            self._prerendering = {
                key for key in self._prerendering
                if not (len(key) >= 3 and key[1] == "sound" and int(key[2]) in sound_ids)
            }
        with self._context_cache_lock:
            self._context_cache = {
                name: context for name, context in self._context_cache.items()
                if name not in sound_names
            }

    def _get_or_create_context(self, archive, sound_name: str) -> PlaybackContext:
        """Return a cached PlaybackContext, creating one if needed."""
        if not hasattr(self, "_context_cache_lock"):
            self._context_cache_lock = threading.Lock()
        if not hasattr(self, "_context_cache"):
            self._context_cache = {}
        with self._context_cache_lock:
            cached = self._context_cache.get(sound_name)
        if cached is not None:
            return cached

        context = None
        strm_source_revision = None
        lookup = archive.lookup_sound(sound_name)
        if lookup is not None:
            entry = archive.data.sound_entries[lookup[2]]
            if entry.sound_type == SoundType.STRM:
                from pysar.core.format.rstm import Brstm

                strm_source_revision = int(getattr(self, "_strm_source_revision", 0))
                file_entry = archive.data.file_entries[entry.file_index]
                if file_entry.external_file_path:
                    resolution = self._resolve_external_brstm(file_entry.external_file_path)
                    brstm_path = resolution["resolved"]
                    if brstm_path is None:
                        if resolution["expected"] is None:
                            raise FileNotFoundError(
                                f'Cannot resolve the external BRSTM for "{sound_name}" without '
                                "a saved BRSAR path or an original-game fallback"
                            )
                        fallback = resolution["fallbackRoot"]
                        fallback_hint = (
                            f" or the configured original-game folder ({fallback})"
                            if fallback is not None else ""
                        )
                        raise FileNotFoundError(
                            f'External BRSTM for "{sound_name}" was not found at the expected '
                            f'BRSAR path ({resolution["expected"]}){fallback_hint}'
                        )
                    brstm = Brstm.open(brstm_path)
                else:
                    raw = archive._resolve_file_raw(entry.file_index)
                    brstm = Brstm.from_bytes(raw) if raw is not None else None
                if brstm is not None:
                    context = PlaybackContext(
                        name=sound_name,
                        sound_type=entry.sound_type,
                        entry=entry,
                        archive_volume=max(0.0, min(1.0, entry.volume / 127.0)),
                        brstm=brstm,
                    )
        if context is None:
            context = make_playback_context(archive, sound_name)
        if (
            strm_source_revision is not None
            and strm_source_revision != int(getattr(self, "_strm_source_revision", 0))
        ):
            return self._get_or_create_context(archive, sound_name)
        with self._context_cache_lock:
            self._context_cache[sound_name] = context
        return context

    @staticmethod
    def _valid_strm_track_indices(value: Any) -> list[int] | None:
        if not isinstance(value, (list, tuple, set)):
            return None
        indices: set[int] = set()
        for item in value:
            try:
                index = int(item)
            except (TypeError, ValueError):
                continue
            if index >= 0:
                indices.add(index)
        return sorted(indices)

    @staticmethod
    def _strm_playback_metadata(brstm) -> dict[str, Any]:
        """Describe the BRSTM's authored track/channel mappings."""
        channel_count = max(1, int(brstm.n_channels))
        tracks = []
        authored = list(getattr(brstm.data, "tracks", ()) or ())
        for index, track in enumerate(authored):
            channels = (
                track.resolved_channel_indices()
                if hasattr(track, "resolved_channel_indices")
                else [track.left_channel_id, track.right_channel_id][:max(1, int(track.channel_count))]
            )
            channels = [int(channel) for channel in channels if 0 <= int(channel) < channel_count]
            if not channels:
                continue
            tracks.append({
                "index": index,
                "channels": channels,
                "volume": max(0, min(255, int(getattr(track, "volume", 127)))),
                "pan": max(0, min(255, int(getattr(track, "pan", 64)))),
            })
        if not tracks:
            for index, start_channel in enumerate(range(0, channel_count, 2)):
                tracks.append({
                    "index": index,
                    "channels": list(range(start_channel, min(start_channel + 2, channel_count))),
                    "volume": 127,
                    "pan": 64,
                })
        sample_rate = max(1, int(brstm.sample_rate))
        return {
            "looped": bool(brstm.is_looped),
            "loopStartMs": int(round(int(brstm.loop_start) * 1000 / sample_rate)),
            "tracks": tracks,
        }

    def get_strm_playback_metadata(self, sound_id: int) -> dict:
        """Return the loop and track controls for a selected external BRSTM."""
        try:
            archive = self.project_service.require_archive(self.session)
            entry = self.archive_service._sound_entry(archive, int(sound_id))
            if entry.sound_type != SoundType.STRM:
                return {"ok": False, "error": "Not a STRM sound"}
            sound_name = self.archive_service._sound_name(archive, int(sound_id), entry)
            context = self._get_or_create_context(archive, sound_name)
            if context.brstm is None:
                return {"ok": False, "error": "The BRSTM file could not be loaded"}
            return {"ok": True, "strmPlayback": self._strm_playback_metadata(context.brstm)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def warm_sound_preview(
        self,
        sound_id: int,
        seq_note_override: int | None = None,
        seq_program_override: int | None = None,
        seq_random_overrides: Any = None,
    ) -> dict:
        spec = {
            "seq_note_override": self._valid_midi_note(seq_note_override),
            "seq_program_override": self._valid_program(seq_program_override),
            "seq_random_overrides": self._valid_random_overrides(seq_random_overrides),
        }
        self._warm_sound_preview_async(int(sound_id), spec)
        return {"ok": True}

    def prerender_sound_preview(
        self,
        sound_id: int,
        seq_note_override: int | None = None,
        seq_program_override: int | None = None,
        seq_random_overrides: Any = None,
    ) -> dict:
        """Build the exact browser WAV in the background after selection."""
        spec = {
            "kind": "sound",
            "sound_id": int(sound_id),
            "seq_note_override": self._valid_midi_note(seq_note_override),
            "seq_program_override": self._valid_program(seq_program_override),
            "seq_random_overrides": self._valid_random_overrides(seq_random_overrides),
            "offset_ms": 0,
            "strm_track_indices": None,
        }
        try:
            archive = self.project_service.require_archive(self.session)
            entry = self.archive_service._sound_entry(archive, int(sound_id))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if entry.sound_type == SoundType.STRM:
            spec["strm_source_revision"] = int(getattr(self, "_strm_source_revision", 0))
        key = self._audio_cache_key(spec)
        with self._audio_cache_lock:
            if key in self._audio_cache or key in self._prerendering:
                return {"ok": True}
            self._prerendering.add(key)

        def worker() -> None:
            try:
                spec["sound_type"] = entry.sound_type.name
                duration_ms = self._estimate_sound_duration_ms(int(sound_id), spec)
                spec["duration_ms"] = duration_ms
                spec["finite_stream"] = duration_ms > 0
                sample_rate = self._stream_sample_rate(spec)
                payload = bytearray()
                for chunk in self._stream_audio_chunks(spec):
                    if not chunk.size:
                        continue
                    payload.extend(self._pcm16_bytes(chunk))
                    if len(payload) > self._MAX_AUDIO_CACHE_BYTES - 44:
                        payload.clear()
                        break
                revision = spec.get("strm_source_revision")
                if payload and (
                    revision is None
                    or revision == int(getattr(self, "_strm_source_revision", 0))
                ):
                    wav = self._streaming_wav_header(sample_rate, data_size=len(payload)) + payload
                    self._put_cached_audio(key, bytes(wav))
            except Exception:
                pass
            finally:
                with self._audio_cache_lock:
                    self._prerendering.discard(key)

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    def _warm_sound_preview_async(self, sound_id: int, spec: dict[str, Any]) -> None:
        key = self._duration_cache_key(sound_id, spec)
        with self._warm_lock:
            if key in self._warm_keys:
                return
            self._warm_keys.add(key)

        def worker() -> None:
            try:
                self._warm_sound_preview(sound_id, spec)
            except Exception:
                with self._warm_lock:
                    self._warm_keys.discard(key)

        threading.Thread(target=worker, daemon=True).start()

    def _warm_sound_preview(self, sound_id: int, spec: dict[str, Any]) -> None:
        archive = self.project_service.require_archive(self.session)
        entry = self.archive_service._sound_entry(archive, int(sound_id))
        sound_name = self.archive_service._sound_name(archive, sound_id, entry)
        # Parsing/cross-file resolution is useful to warm. Decoding whole
        # RWAR banks or rendering whole clips is not: it competes with Play
        # and makes cold startup scale with the length/size of unrelated data.
        self._get_or_create_context(archive, sound_name)

    def get_wave_sample_preview(self, archive_file_id: int, wave_index: int) -> dict:
        result = self.get_wave_sample_stream_url(archive_file_id, wave_index)
        if result.get("ok"):
            result["dataUrl"] = result["url"]
        return result

    def get_sound_sample_stream_url(self, sound_id: int, sample_no: int = 0, offset_ms: int = 0) -> dict:
        """Return a stream URL for one replace-dialog sample on a WAVE/SEQ sound."""
        try:
            archive = self.project_service.require_archive(self.session)
            entry = self.archive_service._sound_entry(archive, int(sound_id))
            sample_no = int(sample_no)

            info = self.get_sound_samples(int(sound_id))
            if not info.get("ok"):
                return info
            samples = info.get("samples") or []
            if entry.sound_type == SoundType.SEQ:
                sample = next((s for s in samples if int(s.get("wavNo", -1)) == sample_no), None)
                seq_info = entry.sound_info
                bank_entry = archive.data.bank_entries[int(seq_info.bank_index)]
                _, audio_file_id = self.archive_service._resolve_file_index(archive, bank_entry.file_index)
            elif entry.sound_type == SoundType.WAVE:
                sample = next((s for s in samples if int(s.get("noteIndex", -1)) == sample_no), None)
                _, audio_file_id = self.archive_service._resolve_file_index(archive, entry.file_index)
            else:
                return {"ok": False, "error": "Only WAVE and SEQ samples can be previewed"}

            if sample is None:
                return {"ok": False, "error": f"Sample {sample_no} not found"}
            if audio_file_id is None:
                return {"ok": False, "error": "Could not resolve sample wave archive"}
            return self.get_wave_sample_stream_url(
                int(audio_file_id),
                int(sample["waveIndex"]),
                int(offset_ms),
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def play_sound(self, sound_id: int, offset_ms: int = 0, volume: float = 1.0) -> dict:
        try:
            audio = self.audio_service.play_sound(
                self.session,
                int(sound_id),
                offset_ms=int(offset_ms),
                volume=float(volume),
            )
            return {"ok": True, "durationMs": int(round(audio.duration * 1000))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def play_wave_sample(self, archive_file_id: int, wave_index: int, volume: float = 1.0, offset_ms: int = 0) -> dict:
        try:
            info = self.audio_service.play_wave_sample(
                self.session,
                int(archive_file_id),
                int(wave_index),
                volume=float(volume),
                offset_ms=int(offset_ms),
            )
            return {"ok": True, **info}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def play_sequence(self, sequence_id: int, variables: Optional[dict] = None) -> dict:
        return {"ok": True, "playing": sequence_id}

    def stop(self) -> dict:
        self.audio_service.stop()
        return {"ok": True}

    def _start_stream_server(self) -> ThreadingHTTPServer:
        api = self

        class StreamHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                token = self.path.rsplit("/", 1)[-1].removesuffix(".wav")
                api._serve_stream(token, self)

            def do_HEAD(self) -> None:
                token = self.path.rsplit("/", 1)[-1].removesuffix(".wav")
                api._serve_stream(token, self, head_only=True)

            def log_message(self, format: str, *args: Any) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), StreamHandler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def _stream_url(self, token: str) -> str:
        host, port = self._stream_server.server_address
        return f"http://{host}:{port}/audio/{token}.wav"

    def _stream_sample_rate(self, spec: dict[str, Any]) -> int:
        if spec.get("sample_rate") is not None:
            return max(1, int(spec["sample_rate"]))
        if spec.get("kind") not in {"wave_sample", "bank_note"}:
            return 32000
        try:
            archive = self.project_service.require_archive(self.session)
            embedded = archive.data.embedded_files.get(int(spec["archive_file_id"]))
            if embedded is None or embedded.magic != "RWAR":
                return 32000
            from pysar.core.format.rwar import Brwar
            brwar = Brwar.from_bytes(embedded.raw_data)
            wave_index = int(spec["wave_index"])
            if wave_index < 0 or wave_index >= len(brwar):
                return 32000
            return max(1, int(brwar[wave_index].sample_rate))
        except Exception:
            return 32000

    def _serve_stream(self, token: str, handler: BaseHTTPRequestHandler, *, head_only: bool = False) -> None:
        with self._stream_lock:
            spec = self._stream_specs.get(token)
        if spec is None:
            handler.send_error(404, "Unknown audio stream")
            return

        cache_key = self._audio_cache_key(spec)
        with self._audio_cache_lock:
            cached_wav = self._audio_cache.get(cache_key)
            start_frame = max(0, int(spec.get("start_frame") or 0))
            if cached_wav is None and (start_frame > 0 or int(spec.get("offset_ms") or 0) > 0):
                base_spec = dict(spec)
                base_spec["offset_ms"] = 0
                base_spec["start_frame"] = 0
                base_spec.pop("segment_role", None)
                base_wav = self._audio_cache.get(self._audio_cache_key(base_spec))
                if base_wav is not None:
                    cached_wav = (
                        self._slice_cached_wav_frames(base_wav, start_frame)
                        if start_frame > 0
                        else self._slice_cached_wav(base_wav, int(spec["offset_ms"]))
                    )
        if cached_wav is not None:
            self._serve_cached_wav(cached_wav, handler, head_only=head_only)
            return

        try:
            sample_rate = self._stream_sample_rate(spec)
            total_frames = self._stream_total_frames(spec, sample_rate)
            data_size = None if total_frames is None else max(0, total_frames) * 2 * 2
            handler.send_response(200)
            handler.send_header("Content-Type", "audio/wav")
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.send_header("Cache-Control", "private, max-age=3600")
            if data_size is not None:
                handler.send_header("Content-Length", str(44 + data_size))
            handler.end_headers()
            if head_only:
                return
            header = self._streaming_wav_header(sample_rate, channels=2, data_size=data_size)
            handler.wfile.write(header)
            handler.wfile.flush()
            emitted_frames = 0
            cached_parts = [header]
            cache_size = len(header)
            stream_started = time.monotonic()
            pace_sequence = (
                spec.get("kind") == "sound"
                and spec.get("sound_type") == SoundType.SEQ.name
            )

            def yield_playback_budget() -> None:
                if not pace_sequence:
                    return
                # Render an initial cushion immediately, then produce at about
                # playback speed. Otherwise a long BRSEQ consumes a full CPU
                # core until its entire WAV is cached, starving the webview.
                rendered_seconds = emitted_frames / max(1, sample_rate)
                delay = rendered_seconds - 2.0 - (time.monotonic() - stream_started)
                if delay > 0:
                    time.sleep(delay)

            for chunk in self._stream_audio_chunks(spec):
                if chunk.size == 0:
                    continue
                raw = self._pcm16_bytes(chunk)
                handler.wfile.write(raw)
                handler.wfile.flush()
                emitted_frames += len(chunk)
                if cache_size <= self._MAX_AUDIO_CACHE_BYTES:
                    cached_parts.append(raw)
                    cache_size += len(raw)
                yield_playback_budget()
            if total_frames is not None and emitted_frames < total_frames:
                remaining = total_frames - emitted_frames
                silence_frames = max(1, sample_rate)
                while remaining > 0:
                    frames = min(remaining, silence_frames)
                    raw = b"\x00" * frames * 2 * 2
                    handler.wfile.write(raw)
                    if cache_size <= self._MAX_AUDIO_CACHE_BYTES:
                        cached_parts.append(raw)
                        cache_size += len(raw)
                    remaining -= frames
                    emitted_frames += frames
                    yield_playback_budget()
                handler.wfile.flush()
            if cache_size <= self._MAX_AUDIO_CACHE_BYTES:
                # Unknown-length streams get a truthful header in the cached
                # copy, making every later play and byte-range seek exact.
                payload = b"".join(cached_parts[1:])
                wav = self._streaming_wav_header(sample_rate, data_size=len(payload)) + payload
                self._put_cached_audio(cache_key, wav)
                if spec.get("kind") == "sound" and int(spec.get("duration_ms") or 0) <= 0:
                    exact_ms = int(round((len(payload) // 4) * 1000 / max(1, sample_rate)))
                    duration_key = self._duration_cache_key(int(spec["sound_id"]), spec)
                    with self._duration_lock:
                        self._duration_cache[duration_key] = exact_ms
                        seq_playback = self._sequence_playback_cache.get(duration_key)
                    duration_payload = {
                        "soundId": int(spec["sound_id"]),
                        "durationMs": exact_ms,
                        "seqNoteOverride": spec.get("seq_note_override"),
                        "seqProgramOverride": spec.get("seq_program_override"),
                        "seqRandomOverrides": list(spec.get("seq_random_overrides") or ()),
                    }
                    if seq_playback is not None:
                        duration_payload["seqPlayback"] = dict(seq_playback)
                    self.push_event("duration_update", duration_payload)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            try:
                handler.wfile.write(b"")
            except Exception:
                pass

    def _serve_cached_wav(self, wav: bytes, handler: BaseHTTPRequestHandler, *, head_only: bool = False) -> None:
        start = 0
        end = len(wav) - 1
        status = 200
        value = handler.headers.get("Range", "")
        if value.startswith("bytes="):
            try:
                first, last = value[6:].split(",", 1)[0].split("-", 1)
                if first:
                    start = int(first)
                    end = min(end, int(last)) if last else end
                elif last:
                    count = int(last)
                    start = max(0, len(wav) - count)
                if start < 0 or start > end or start >= len(wav):
                    raise ValueError
                status = 206
            except (TypeError, ValueError):
                handler.send_response(416)
                handler.send_header("Content-Range", f"bytes */{len(wav)}")
                handler.end_headers()
                return
        body = memoryview(wav)[start:end + 1]
        handler.send_response(status)
        handler.send_header("Content-Type", "audio/wav")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Cache-Control", "private, max-age=3600")
        handler.send_header("Content-Length", str(len(body)))
        if status == 206:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{len(wav)}")
        handler.end_headers()
        if not head_only:
            handler.wfile.write(body)

    def _slice_cached_wav(self, wav: bytes, offset_ms: int) -> bytes:
        if len(wav) < 44:
            return wav
        sample_rate = max(1, struct.unpack_from("<I", wav, 24)[0])
        block_align = max(1, struct.unpack_from("<H", wav, 32)[0])
        frame = max(0, round(offset_ms * sample_rate / 1000))
        payload = wav[min(len(wav), 44 + frame * block_align):]
        return self._streaming_wav_header(sample_rate, channels=2, data_size=len(payload)) + payload

    def _slice_cached_wav_frames(self, wav: bytes, start_frame: int) -> bytes:
        if len(wav) < 44:
            return wav
        sample_rate = max(1, struct.unpack_from("<I", wav, 24)[0])
        block_align = max(1, struct.unpack_from("<H", wav, 32)[0])
        frame = max(0, int(start_frame))
        payload = wav[min(len(wav), 44 + frame * block_align):]
        return self._streaming_wav_header(sample_rate, channels=2, data_size=len(payload)) + payload

    def _stream_audio_chunks(self, spec: dict[str, Any]):
        sample_rate = self._stream_sample_rate(spec)
        explicit_start_frame = spec.get("start_frame")
        skip_frames = (
            max(0, int(explicit_start_frame))
            if explicit_start_frame is not None
            else max(0, round(int(spec.get("offset_ms") or 0) * sample_rate / 1000))
        )
        remaining_skip = skip_frames
        max_frames = self._stream_total_frames(spec, sample_rate)
        emitted_frames = 0

        def current_max_frames() -> int | None:
            nonlocal max_frames
            if (
                max_frames is None
                and spec.get("kind") == "sound"
                and spec.get("sound_type") == SoundType.SEQ.name
            ):
                key = self._duration_cache_key(int(spec["sound_id"]), spec)
                with self._duration_lock:
                    duration_ms = self._duration_cache.get(key)
                    seq_playback = self._sequence_playback_cache.get(key)
                if duration_ms is not None and duration_ms > 0:
                    if seq_playback and seq_playback.get("looped") and seq_playback.get("loopEndFrame") is not None:
                        metadata_rate = max(1, int(seq_playback.get("sampleRate") or sample_rate))
                        loop_end_frame = round(int(seq_playback["loopEndFrame"]) * sample_rate / metadata_rate)
                        max_frames = max(0, loop_end_frame - skip_frames)
                    else:
                        effective_duration_ms = (
                            int(seq_playback.get("loopEndMs") or duration_ms)
                            if seq_playback and seq_playback.get("looped")
                            else int(duration_ms)
                        )
                        remaining_ms = max(0, effective_duration_ms - int(spec.get("offset_ms") or 0))
                        max_frames = round(remaining_ms * sample_rate / 1000)
            return max_frames

        def trim(chunks, *, apply_skip: bool = True):
            nonlocal remaining_skip, emitted_frames
            if not apply_skip:
                remaining_skip = 0
            limit = current_max_frames()
            if limit is not None and limit <= 0:
                return
            for chunk in chunks:
                if chunk.size == 0:
                    continue
                if remaining_skip >= len(chunk):
                    remaining_skip -= len(chunk)
                    continue
                if remaining_skip:
                    chunk = chunk[remaining_skip:]
                    remaining_skip = 0
                limit = current_max_frames()
                if limit is not None:
                    remaining = limit - emitted_frames
                    if remaining <= 0:
                        break
                    chunk = chunk[:remaining]
                emitted_frames += len(chunk)
                yield chunk

        if spec["kind"] == "wave_sample":
            yield from trim(
                self._stream_wave_sample_chunks(
                    int(spec["archive_file_id"]),
                    int(spec["wave_index"]),
                    start_frame=skip_frames,
                ),
                apply_skip=False,
            )
            return

        if spec["kind"] == "bank_note":
            yield from trim(
                self._stream_wave_sample_chunks(
                    int(spec["archive_file_id"]),
                    int(spec["wave_index"]),
                    start_frame=skip_frames,
                    gain=float(spec.get("volume", 1.0)),
                    playback_rate=float(spec.get("playback_rate", 1.0)),
                ),
                apply_skip=False,
            )
            return

        archive = self.project_service.require_archive(self.session)
        sound_id = int(spec["sound_id"])
        sound_name = self.archive_service._sound_name(archive, sound_id, self.archive_service._sound_entry(archive, sound_id))
        options = None
        if spec.get("seq_note_override") is not None or spec.get("seq_program_override") is not None or spec.get("seq_random_overrides"):
            options = PreviewOptions(
                seq_note_override=spec.get("seq_note_override"),
                seq_program_override=spec.get("seq_program_override"),
                seq_random_overrides=tuple(spec.get("seq_random_overrides") or ()),
            )
        settings = options.to_render_options() if options is not None else PreviewOptions().to_render_options()
        spec["sample_rate"] = settings.sample_rate
        context = self._get_or_create_context(archive, sound_name)
        if context.sound_type == SoundType.STRM and context.brstm is not None:
            settings.sample_rate = max(1, int(context.brstm.sample_rate))
            spec["sample_rate"] = settings.sample_rate
        renderer = SequenceRenderer()
        if context.sound_type == SoundType.SEQ:
            yield from trim(renderer.stream(context, settings, start_frame=skip_frames), apply_skip=False)
        elif context.sound_type == SoundType.WAVE:
            yield from trim(
                renderer.stream_wave_sound(context, settings, start_frame=skip_frames),
                apply_skip=False,
            )
        elif context.sound_type == SoundType.STRM:
            yield from trim(
                renderer.stream_stream_sound(
                    context,
                    settings,
                    track_indices=spec.get("strm_track_indices"),
                    start_frame=skip_frames,
                ),
                apply_skip=False,
            )

    def _duration_cache_key(self, sound_id: int, spec: dict[str, Any]) -> tuple:
        path = self.session.archive_path
        return (
            str(path) if path is not None else None,
            int(sound_id),
            spec.get("seq_note_override"),
            spec.get("seq_program_override"),
            tuple(spec.get("seq_random_overrides") or ()),
        )

    def _audio_cache_key(self, spec: dict[str, Any]) -> tuple:
        path = self.session.archive_path
        kind = spec.get("kind")
        if kind == "sound":
            return (
                str(path) if path is not None else None,
                kind,
                int(spec["sound_id"]),
                spec.get("strm_source_revision"),
                spec.get("seq_note_override"),
                spec.get("seq_program_override"),
                tuple(spec.get("seq_random_overrides") or ()),
                max(0, int(spec.get("seq_loop_end_ms") or 0)),
                tuple(spec.get("strm_track_indices") or ()),
                max(0, int(spec.get("offset_ms") or 0)),
                max(0, int(spec.get("start_frame") or 0)),
            )
        return (
            str(path) if path is not None else None,
            kind,
            int(spec.get("archive_file_id", -1)),
            int(spec.get("wave_index", -1)),
            float(spec.get("volume", 1.0)),
            float(spec.get("playback_rate", 1.0)),
            max(0, int(spec.get("offset_ms") or 0)),
        )

    def _put_cached_audio(self, key: tuple, wav: bytes) -> None:
        if not wav or len(wav) > self._MAX_AUDIO_CACHE_BYTES:
            return
        with self._audio_cache_lock:
            self._audio_cache.pop(key, None)
            while self._audio_cache and sum(map(len, self._audio_cache.values())) + len(wav) > self._MAX_AUDIO_CACHE_BYTES:
                self._audio_cache.pop(next(iter(self._audio_cache)))
            self._audio_cache[key] = wav

    def _stream_wave_sample_chunks(
        self,
        archive_file_id: int,
        wave_index: int,
        *,
        start_frame: int = 0,
        gain: float = 1.0,
        playback_rate: float = 1.0,
    ):
        archive = self.project_service.require_archive(self.session)
        embedded = archive.data.embedded_files.get(int(archive_file_id))
        if embedded is None or embedded.magic != "RWAR":
            raise ValueError(f"file_id {archive_file_id} is not an RWAR")

        from pysar.core.format.rwar import Brwar

        brwar = Brwar.from_bytes(embedded.raw_data)
        if wave_index < 0 or wave_index >= len(brwar):
            raise ValueError(f"wave index {wave_index} is out of range")
        brwav = brwar[int(wave_index)]
        pcm = np.frombuffer(brwav.decode(), dtype="<i2").astype(np.float32) / 32768.0
        channels = max(1, int(brwav.n_channels))
        pcm = pcm.reshape(-1, channels)
        if channels == 1:
            pcm = np.repeat(pcm, 2, axis=1)
        elif channels > 2:
            pcm = pcm[:, :2]
        gain = max(0.0, min(1.0, float(gain)))
        if gain != 1.0:
            pcm = np.clip(pcm * gain, -1.0, 1.0)

        # A bank zone maps its sample across a MIDI key range.  Resample by
        # the pressed-key/original-key ratio, matching NW4R bank playback.
        rate = max(1.0e-6, float(playback_rate))
        output_frames = max(1, int(np.ceil(len(pcm) / rate))) if len(pcm) else 0
        chunk_frames = 32768
        cursor = max(0, min(output_frames, int(start_frame)))
        if abs(rate - 1.0) < 1.0e-9:
            for cursor in range(cursor, output_frames, chunk_frames):
                yield pcm[cursor:cursor + chunk_frames]
            return

        while cursor < output_frames:
            end = min(output_frames, cursor + chunk_frames)
            positions = np.arange(cursor, end, dtype=np.float64) * rate
            left = np.floor(positions).astype(np.int64)
            right = np.minimum(left + 1, len(pcm) - 1)
            fraction = (positions - left).astype(np.float32)[:, None]
            yield (pcm[left] * (1.0 - fraction) + pcm[right] * fraction).astype(np.float32, copy=False)
            cursor = end

    def _stream_total_frames(self, spec: dict[str, Any], sample_rate: int) -> int | None:
        if spec.get("finite_stream") is False:
            return None
        if spec.get("total_frames") is not None:
            offset_frames = (
                max(0, int(spec["start_frame"]))
                if spec.get("start_frame") is not None
                else max(0, round(int(spec.get("offset_ms") or 0) * sample_rate / 1000))
            )
            return max(0, int(spec["total_frames"]) - offset_frames)
        duration_ms = int(spec.get("duration_ms") or 0)
        if duration_ms <= 0:
            return None
        offset_ms = max(0, int(spec.get("offset_ms") or 0))
        remaining_ms = max(0, duration_ms - offset_ms)
        return max(0, round(remaining_ms * sample_rate / 1000))

    @staticmethod
    def _streaming_wav_header(sample_rate: int, channels: int = 2, data_size: int | None = None) -> bytes:
        if data_size is None:
            data_size = 0xFFFFFFFF
            riff_size = 0xFFFFFFFF
        else:
            data_size = max(0, min(int(data_size), 0xFFFFFFFF - 36))
            riff_size = 36 + data_size
        sample_width = 2
        byte_rate = sample_rate * channels * sample_width
        block_align = channels * sample_width
        return struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            riff_size,
            b"WAVE",
            b"fmt ",
            16,
            1,
            channels,
            sample_rate,
            byte_rate,
            block_align,
            sample_width * 8,
            b"data",
            data_size,
        )

    @staticmethod
    def _pcm16_bytes(samples: np.ndarray) -> bytes:
        pcm = np.asarray(samples, dtype=np.float32)
        if pcm.ndim == 1:
            pcm = pcm.reshape(-1, 1)
        if pcm.shape[1] == 1:
            pcm = np.repeat(pcm, 2, axis=1)
        elif pcm.shape[1] > 2:
            pcm = pcm[:, :2]
        return np.round(np.clip(pcm, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()

    def _estimate_sound_duration_ms(self, sound_id: int, spec: dict[str, Any]) -> int:
        return self._inspect_sound_timing(sound_id, spec)[0]

    def _inspect_sound_timing(
        self,
        sound_id: int,
        spec: dict[str, Any],
    ) -> tuple[int, dict[str, Any] | None]:
        try:
            archive = self.project_service.require_archive(self.session)
            entry = self.archive_service._sound_entry(archive, sound_id)
            name = self.archive_service._sound_name(archive, sound_id, entry)
            context = self._get_or_create_context(archive, name)
            if context.sound_type == SoundType.STRM and context.brstm is not None:
                return int(round(context.brstm.duration * 1000)), None
            if context.sound_type == SoundType.WAVE and context.brwsd is not None and context.brwar is not None:
                wave_index = int(context.extras.get("wave_sound_index", -1))
                if 0 <= wave_index < len(context.brwsd):
                    notes = context.brwsd[wave_index].notes
                    if notes and 0 <= int(notes[0].wave_index) < len(context.brwar):
                        return int(round(float(context.brwar[int(notes[0].wave_index)].duration) * 1000)), None
            if context.sound_type == SoundType.SEQ and context.brseq is not None:
                options = None
                if spec.get("seq_note_override") is not None or spec.get("seq_program_override") is not None or spec.get("seq_random_overrides"):
                    options = PreviewOptions(
                        seq_note_override=spec.get("seq_note_override"),
                        seq_program_override=spec.get("seq_program_override"),
                        seq_random_overrides=tuple(spec.get("seq_random_overrides") or ()),
                    )
                return self._inspect_sequence_timing(context, options)
        except Exception:
            return 0, None
        return 0, None

    @staticmethod
    def _estimate_sequence_duration_ms(context, options: PreviewOptions | None = None) -> int:
        return PysarApi._inspect_sequence_timing(context, options)[0]

    @staticmethod
    def _inspect_sequence_timing(
        context,
        options: PreviewOptions | None = None,
    ) -> tuple[int, dict[str, Any]]:
        from pysar.core.format.rseq.mml import MML

        settings = options.to_render_options() if options is not None else PreviewOptions().to_render_options()
        try:
            player = SequenceRenderer().make_sequence_player(context, settings)
        except (AttributeError, ValueError):
            # Timing does not require a bank. Keep duration inspection useful
            # for incomplete/editing contexts where samples are unavailable.
            from pysar.seq.player import SequencePlayer
            player = SequencePlayer()
            player.load(
                context.brseq.data,
                start_label=getattr(context, "start_label", None),
                start_offset=getattr(context, "start_offset", None),
                default_programs=getattr(context, "default_programs", None),
            )
        # A duration scan only needs event ticks and tempo changes. Building
        # every mixer-control event and retaining the complete event list can
        # monopolize the Python/UI thread for long sequences while playback
        # is filling its buffer.
        loop_starts: dict[int, tuple[int, int]] = {}
        first_execution: dict[tuple[int, int], int] = {}
        loop_candidates: list[tuple[int, int, int]] = []

        def on_command(track_no: int, tick: int, command) -> None:
            track = int(track_no)
            command_tick = int(tick)
            offset = int(command.offset or 0)
            first_execution.setdefault((track, offset), command_tick)
            mml = command.get_mml()
            if mml == MML.LOOP_START:
                count = int(command.args[0]) if command.args else 0
                loop_starts[track] = (command_tick, count)
            elif mml == MML.LOOP_END:
                start = loop_starts.get(track)
                if start is not None and start[1] == 0:
                    loop_candidates.append((track, start[0], command_tick))
            elif mml == MML.JUMP and command.args:
                target = int(command.args[0])
                start_tick = first_execution.get((track, target))
                if start_tick is not None and target <= offset:
                    loop_candidates.append((track, start_tick, command_tick))

        player.set_command_callback(on_command)
        tempo = 120
        last_event_tick = 0
        timing_segments: list[tuple[int, int]] = []
        tempo_changes: list[tuple[int, int]] = [(0, tempo)]
        for tick, events in player.iter_event_ticks(
            max_ticks=settings.max_ticks,
            loop_count=settings.loop_count,
            one_shot=settings.one_shot,
            suppress_track_state=lambda: True,
            track_state_markers=True,
        ):
            if events:
                tick = int(tick)
                timing_segments.append((max(0, tick - last_event_tick), tempo))
                last_event_tick = tick
            for event in events:
                if event.get("type") == "tempo":
                    tempo = int(event.get("tempo", tempo) or tempo)
                    tempo_changes.append((int(tick), tempo))
        if not timing_segments:
            return 0, {
                "looped": False,
                "loopStartMs": 0,
                "loopEndMs": 0,
                "loopStartFrame": 0,
                "loopEndFrame": 0,
                "sampleRate": settings.sample_rate,
            }
        # Reproduce _seq_event_times_ms' arithmetic exactly so the optimized
        # scan cannot shift an existing duration by even one millisecond.
        timebase = max(1, player.timebase)
        current_time = 0.0
        for delta_ticks, segment_tempo in timing_segments:
            current_time += delta_ticks * 60.0 / (max(1, segment_tempo) * timebase)
        last_ms = int(round(current_time * 1000))
        duration_ms = int(round(last_ms + settings.tail_seconds * 1000))
        if player.truncated:
            duration_ms = min(duration_ms, PysarApi._TRUNCATED_SEQUENCE_PREVIEW_MS)

        def tick_to_seconds(target_tick: int) -> float:
            target = max(0, int(target_tick))
            elapsed = 0.0
            cursor = 0
            active_tempo = 120
            for change_tick, next_tempo in sorted(tempo_changes, key=lambda item: item[0]):
                if change_tick > target:
                    break
                elapsed += max(0, change_tick - cursor) * 60.0 / (max(1, active_tempo) * timebase)
                cursor = max(cursor, change_tick)
                active_tempo = max(1, int(next_tempo))
            elapsed += max(0, target - cursor) * 60.0 / (max(1, active_tempo) * timebase)
            return elapsed

        valid_candidates = [item for item in loop_candidates if item[2] > item[1]]
        selected = next((item for item in valid_candidates if item[0] == 0), None)
        if selected is None and valid_candidates:
            selected = valid_candidates[0]
        playback = {
            "looped": False,
            "loopStartMs": 0,
            "loopEndMs": 0,
            "loopStartFrame": 0,
            "loopEndFrame": 0,
            "sampleRate": settings.sample_rate,
        }
        if selected is not None:
            loop_start_seconds = tick_to_seconds(selected[1])
            loop_end_seconds = tick_to_seconds(selected[2])
            loop_start_ms = int(round(loop_start_seconds * 1000))
            loop_end_ms = int(round(loop_end_seconds * 1000))
            if loop_end_ms > loop_start_ms:
                playback = {
                    "looped": True,
                    "loopStartMs": loop_start_ms,
                    "loopEndMs": loop_end_ms,
                    "loopStartFrame": round(loop_start_seconds * settings.sample_rate),
                    "loopEndFrame": round(loop_end_seconds * settings.sample_rate),
                    "sampleRate": settings.sample_rate,
                }
        return duration_ms, playback

    def _estimate_wave_sample_duration_ms(self, archive_file_id: int, wave_index: int) -> int:
        try:
            sample_rate, frames = self._wave_sample_info(archive_file_id, wave_index)
            return int(round(frames * 1000 / max(1, sample_rate)))
        except Exception:
            return 0

    def _wave_sample_info(self, archive_file_id: int, wave_index: int) -> tuple[int, int]:
        archive = self.project_service.require_archive(self.session)
        embedded = archive.data.embedded_files.get(int(archive_file_id))
        if embedded is None or embedded.magic != "RWAR":
            return 32000, 0
        from pysar.core.format.rwar import Brwar
        brwar = Brwar.from_bytes(embedded.raw_data)
        if wave_index < 0 or wave_index >= len(brwar):
            return 32000, 0
        brwav = brwar[int(wave_index)]
        return max(1, int(brwav.sample_rate)), max(0, int(brwav.n_samples))

    @staticmethod
    def _warm_audio_codecs() -> None:
        try:
            from pysar.core.codec.decode_adpcm import warm_adpcm_decoder
            warm_adpcm_decoder()
        except Exception:
            pass
        # BRSTM uses a separate block decoder/JIT signature from BRWAV.
        try:
            from pysar.core.codec.brstm_adpcm import decode_adpcm_channel_chunk
            decode_adpcm_channel_chunk(
                bytes(8),
                tuple((0, 0) for _ in range(8)),
                0,
                0,
                1,
            )
        except Exception:
            pass
        # Pre-compile the numba render kernel so the first playback is instant.
        try:
            from pysar.seq.renderer import (
                _AX_SRC_COEFFICIENTS_NB,
                _render_voice_loop,
                _NOTE_TABLE_NB,
                _PITCH_TABLE_NB,
                _LFO_SIN_TABLE_NB,
                _VS_SIZE,
            )
            _dummy = np.zeros((1, 2), dtype=np.float32)
            _vs = np.zeros(_VS_SIZE, dtype=np.float64)
            _vs[14] = 3.0  # _ENV_ST_SUSTAIN
            _z2 = np.zeros(2, dtype=np.float32)
            _render_voice_loop(
                _dummy, _dummy.copy(), _dummy.copy(), _dummy.copy(),
                np.zeros(4, dtype=np.float32), _vs,
                _z2.copy(), _z2.copy(), _z2.copy(), _z2.copy(), _z2.copy(),
                1.0, 1.0, 32000.0, 32000.0,
                0.0, 0.0, 0,
                1.0, 0.0, 0.0, 0.0,
                0, 64,
                0.0, False,
                0.0, 0.0, 0.0, 0.0, 0.0, False,
                False, 0, 4,
                _AX_SRC_COEFFICIENTS_NB,
                _NOTE_TABLE_NB, _PITCH_TABLE_NB, _LFO_SIN_TABLE_NB,
            )
        except Exception:
            pass

    def _sequence_shared_sounds(self, archive, brseq, file_index: int) -> list[dict[str, Any]]:
        from pysar.core.model.brsar import SeqSoundInfo, SoundType

        shared_sounds = []
        for candidate_id, candidate in enumerate(archive.data.sound_entries):
            if (
                    candidate.sound_type != SoundType.SEQ
                    or not isinstance(candidate.sound_info, SeqSoundInfo)
                    or int(candidate.file_index) != int(file_index)
            ):
                continue
            candidate_name = self.archive_service._sound_name(
                archive,
                candidate_id,
                candidate,
            )
            candidate_start_label, candidate_start_offset = archive._resolve_seq_start(
                brseq,
                candidate_name,
                int(candidate.sound_info.seq_label_offset),
            )
            shared_sounds.append(
                {
                    "id": candidate_id,
                    "name": candidate_name,
                    "startLabel": candidate_start_label,
                    "startOffset": candidate_start_offset,
                    "seqLabelOffset": int(candidate.sound_info.seq_label_offset),
                }
            )
        return shared_sounds

    def _sequence_details(self, sound_id: int) -> dict[str, Any]:
        from pysar.core.format.rseq.mml import DEFAULT_TEMPO, DEFAULT_TIMEBASE, MML, is_note
        from pysar.core.format.rseq.text import _format_command
        from pysar.core.model.brsar import SeqSoundInfo, SoundType
        from pysar.seq.archive import make_playback_context

        archive = self.project_service.require_archive(self.session)
        entry = self.archive_service._sound_entry(archive, sound_id)
        if entry.sound_type != SoundType.SEQ or not isinstance(entry.sound_info, SeqSoundInfo):
            raise ValueError(f"sound {sound_id} is not a SEQ sound")

        name = self.archive_service._sound_name(archive, sound_id, entry)
        context = self._get_or_create_context(archive, name)
        if context.brseq is None:
            raise ValueError(f"sound {sound_id} has no BRSEQ data")
        seq = context.brseq.data
        start_label = context.start_label
        start_offset = context.start_offset

        shared_sounds = self._sequence_shared_sounds(
            archive,
            context.brseq,
            int(entry.file_index),
        )

        offset_to_label: dict[int, str] = {}
        for label in seq.labels:
            offset_to_label[label.offset] = label.name
        for key, track in seq.tracks.items():
            if isinstance(key, int):
                offset_to_label[key] = f"_anon_{key:04X}"
            offset_to_label.setdefault(track.start_offset, track.name)

        def command_kind(cmd) -> str:
            if is_note(cmd.opcode):
                return "note"
            try:
                mml = cmd.get_mml()
            except Exception:
                return "raw"
            if mml in (MML.WAIT, MML.TEMPO, MML.TIMEBASE):
                return "time"
            if mml in (MML.JUMP, MML.CALL, MML.RET, MML.FIN, MML.OPEN_TRACK, MML.ALLOC_TRACK):
                return "flow"
            if mml in (MML.PRG, MML.VOLUME, MML.PAN, MML.PITCH_BEND, MML.BEND_RANGE):
                return "control"
            return "cmd"

        def command_delta_ticks(cmd, note_wait: bool) -> int:
            args = [a for a in getattr(cmd, "args", []) if isinstance(a, int)]
            if is_note(cmd.opcode):
                return max(0, int(args[1])) if note_wait and len(args) >= 2 else 0
            try:
                mml = cmd.get_mml()
            except Exception:
                return 0
            if mml == MML.WAIT and args:
                return max(0, int(args[0]))
            return 0

        aliases_by_start: dict[int, list[tuple[Any, Any]]] = {}
        for key, track in seq.tracks.items():
            aliases_by_start.setdefault(int(track.start_offset), []).append((key, track))

        def alias_priority(item: tuple[Any, Any]) -> tuple[int, int, int, str]:
            key, track = item
            key_name = str(key) if isinstance(key, str) else ""
            return (
                0 if key_name == name else 1,
                0 if isinstance(key, str) else 1,
                0 if key_name and not key_name.startswith("_anon_") else 1,
                key_name or track.name,
            )

        canonical_sources = [
            min(items, key=alias_priority)[1]
            for _start, items in sorted(aliases_by_start.items())
        ]

        tracks = []
        flat_lines = []
        line_no = 1
        for index, track in enumerate(canonical_sources):
            tempo = DEFAULT_TEMPO
            timebase = DEFAULT_TIMEBASE
            note_wait = True
            current_ms = 0.0
            references = []
            lines = [
                {
                    "line": line_no,
                    "offset": track.start_offset,
                    "offsetHex": f"0x{track.start_offset:04X}",
                    "label": f"{track.name}:",
                    "op": "",
                    "arg": "",
                    "text": f"{track.name}:",
                    "kind": "label",
                    "startMs": 0,
                }
            ]
            line_no += 1

            for cmd in track.commands:
                text = _format_command(cmd, offset_to_label)
                op, _, arg = text.partition(" ")
                line = {
                    "line": line_no,
                    "offset": int(cmd.offset or 0),
                    "offsetHex": f"0x{int(cmd.offset or 0):04X}",
                    "label": "",
                    "op": op,
                    "arg": arg,
                    "text": text,
                    "kind": command_kind(cmd),
                    "startMs": int(round(current_ms)),
                }
                lines.append(line)
                flat_lines.append({**line, "trackIndex": index, "trackName": track.name})
                line_no += 1

                delta = command_delta_ticks(cmd, note_wait)
                current_ms += delta * 60000.0 / max(1, tempo * timebase)

                try:
                    mml = cmd.get_mml()
                except Exception:
                    mml = None
                if mml == MML.TEMPO and cmd.args and isinstance(cmd.args[0], int):
                    tempo = max(1, int(cmd.args[0]))
                elif mml == MML.TIMEBASE and cmd.args and isinstance(cmd.args[0], int):
                    timebase = max(1, int(cmd.args[0]))
                elif mml == MML.NOTE_WAIT and cmd.args:
                    note_wait = bool(cmd.args[0])
                if mml == MML.CALL and cmd.args:
                    references.append({"kind": "call", "targetOffset": int(cmd.args[0])})
                elif mml == MML.JUMP and cmd.args:
                    references.append({"kind": "jump", "targetOffset": int(cmd.args[0])})
                elif mml == MML.OPEN_TRACK and len(cmd.args) >= 2:
                    references.append({
                        "kind": "open",
                        "trackNo": int(cmd.args[0]),
                        "targetOffset": int(cmd.args[1]),
                    })

            last_command = track.commands[-1] if track.commands else None
            try:
                last_mml = last_command.get_mml() if last_command is not None else None
            except Exception:
                last_mml = None
            ends_flow = (
                last_mml in (MML.FIN, MML.RET)
                or (
                    last_mml == MML.JUMP
                    and not bool(getattr(last_command, "has_if", False))
                )
            )
            if last_command is not None and not ends_flow:
                references.append({
                    "kind": "fallthrough",
                    "targetOffset": int(track.end_offset),
                })

            tracks.append(
                {
                    "index": index,
                    "name": track.name,
                    "startOffset": track.start_offset,
                    "endOffset": track.end_offset,
                    "lineCount": len(lines),
                    "durationMs": int(round(current_ms)),
                    "lines": lines,
                    "references": references,
                }
            )

        start_track_index = 0
        selected_label = next(
            (label for label in seq.labels if label.name == name),
            None,
        )
        root_offset = (
            int(start_offset)
            if start_offset is not None
            else int(selected_label.offset) if selected_label is not None
            else int(entry.sound_info.seq_label_offset)
        )
        for track in tracks:
            if track["startOffset"] <= root_offset < track["endOffset"]:
                start_track_index = track["index"]
                break
            if start_label is not None and track["name"] == start_label:
                start_track_index = track["index"]
                break

        related_order = _sequence_related_track_order(tracks, start_track_index)
        related_tracks = []
        for relation in related_order:
            related_track = dict(tracks[int(relation["trackIndex"])])
            related_track["relation"] = relation
            if relation["kind"] == "root":
                related_track["displayName"] = name
            related_tracks.append(related_track)

        line_by_offset: dict[int, dict[str, Any]] = {}
        for track in related_tracks:
            for line in track["lines"]:
                if not line["op"]:
                    continue
                line_by_offset.setdefault(
                    int(line["offset"]),
                    {**line, "trackIndex": track["index"], "trackName": track["name"]},
                )

        settings = PreviewOptions().to_render_options()
        player = SequenceRenderer().make_sequence_player(context, settings)
        command_events: list[dict[str, Any]] = []

        def on_command(track_no: int, tick: int, command) -> None:
            command_events.append(
                {
                    "type": "command",
                    "tick": int(tick),
                    "track": int(track_no),
                    "offset": int(command.offset or 0),
                }
            )

        player.set_command_callback(on_command)
        events = player.render_events(
            max_ticks=settings.max_ticks,
            loop_count=settings.loop_count,
            one_shot=settings.one_shot,
        )
        tempo_events = [ev for ev in events if ev.get("type") == "tempo"]
        trace_events = sorted(
            [*tempo_events, *command_events],
            key=lambda ev: (int(ev.get("tick", 0)), 0 if ev.get("type") == "tempo" else 1),
        )
        # Walk the merged event stream once, tracking the live tempo, so we can
        # also resolve note commands' end times in milliseconds (length is in
        # ticks in the BRSEQ but the UI playhead is in ms).
        trace = []
        timebase = max(1, int(player.timebase))
        current_tempo = 120
        last_tick = 0
        current_time = 0.0
        for ev in trace_events:
            tick = int(ev.get("tick", last_tick))
            delta_ticks = max(0, tick - last_tick)
            current_time += delta_ticks * 60.0 / (max(1, current_tempo) * timebase)
            last_tick = tick
            ms = int(round(current_time * 1000))
            if ev.get("type") == "tempo":
                current_tempo = int(ev.get("tempo", current_tempo) or current_tempo)
                continue
            offset = int(ev.get("offset") or 0)
            line = line_by_offset.get(offset)
            op = line["op"] if line else ""
            note = self._parse_note_op(op)
            length_ms = 0
            if note is not None and line:
                length_ticks = self._parse_note_length(line.get("arg") or "")
                if length_ticks > 0:
                    length_ms = int(round(length_ticks * 60_000.0 / (max(1, current_tempo) * timebase)))
            trace.append(
                {
                    "ms": ms,
                    "tick": tick,
                    "trackNo": int(ev.get("track", 0)),
                    "trackIndex": line["trackIndex"] if line else None,
                    "trackName": line["trackName"] if line else None,
                    "offset": offset,
                    "offsetHex": f"0x{offset:04X}",
                    "line": line["line"] if line else None,
                    "op": op,
                    "note": note,
                    "lengthMs": length_ms,
                }
            )

        return {
            "soundId": sound_id,
            "name": name,
            "fileIndex": entry.file_index,
            "version": seq.version,
            "sourceText": context.brseq.to_text(),
            "labels": [
                {
                    "name": label.name,
                    "offset": int(label.offset),
                    "startOffset": int(archive._seq_effective_label_offset(context.brseq, label.name)),
                }
                for label in seq.labels
            ],
            "sharedSounds": shared_sounds,
            "sharedReferenceCount": len(shared_sounds),
            "startLabel": start_label,
            "startOffset": start_offset,
            "seqLabelOffset": entry.sound_info.seq_label_offset,
            "startTrackIndex": start_track_index,
            "trackCount": len(tracks),
            "relatedTrackCount": len(related_tracks),
            "lineCount": sum(t["lineCount"] for t in tracks),
            "tracks": tracks,
            "relatedTracks": related_tracks,
            "flatLines": flat_lines,
            "trace": trace,
        }

    def _sequence_variations(self, sound_id: int) -> dict[str, Any]:
        from pysar.core.model.brbnk import WaveDataLocationType
        from pysar.core.format.rseq.mml import MML
        from pysar.core.model.brsar import SeqSoundInfo, SoundType

        archive = self.project_service.require_archive(self.session)
        entry = self.archive_service._sound_entry(archive, sound_id)
        if entry.sound_type != SoundType.SEQ or not isinstance(entry.sound_info, SeqSoundInfo):
            raise ValueError(f"sound {sound_id} is not a SEQ sound")

        name = self.archive_service._sound_name(archive, sound_id, entry)
        context = self._get_or_create_context(archive, name)
        if context.brseq is None or context.brbnk is None or context.brwar is None:
            return {"variations": [], "programs": []}

        settings = PreviewOptions().to_render_options()
        player = SequenceRenderer().make_sequence_player(context, settings)
        player.trace_random_calls()
        events = player.render_events(
            max_ticks=settings.max_ticks,
            loop_count=settings.loop_count,
            one_shot=settings.one_shot,
        )
        note_events = [ev for ev in events if ev.get("type") in ("note_on", "note_change")]
        programs = sorted({int(ev.get("program", 0)) for ev in note_events})
        if not programs:
            programs = sorted({int(program) for program in context.default_programs.values()}) or [0]

        def wave_signature(rendered_events: list[dict]) -> frozenset[int]:
            waves: set[int] = set()
            for event in rendered_events:
                if event.get("type") not in ("note_on", "note_change"):
                    continue
                param = context.brbnk.get_inst_param(
                    int(event.get("program", 0)),
                    int(event.get("note", 60)),
                    int(event.get("velocity", 127)),
                )
                if param is not None and param.wave_data_location_type == WaveDataLocationType.INDEX:
                    waves.add(int(param.wave_index))
            return frozenset(waves)

        # Only random operations capable of changing program/key/velocity or
        # conditional flow can select another sample. Validate their actual
        # rendered wave outcomes so random timing/pan/etc. never becomes a UI
        # "variation" and identical range values collapse to one choice.
        candidate_mml = {MML.PRG, MML.TRANSPOSE, MML.VELOCITY_RANGE, MML.OPEN_TRACK, MML.JUMP, MML.CALL}
        random_sources = []
        seen_calls: set[int] = set()
        for call in player.random_calls:
            index = int(call["index"])
            if index in seen_calls:
                continue
            seen_calls.add(index)
            minimum = int(call["minimum"])
            maximum = int(call["maximum"])
            if maximum - minimum > 63:
                continue
            if not bool(call["extended"]):
                try:
                    if MML(int(call["opcode"])) not in candidate_mml:
                        continue
                except ValueError:
                    continue
            random_sources.append(dict(call))

        variations: list[dict[str, Any]] = []
        true_sources: list[dict[str, Any]] = []
        for source in random_sources:
            outcomes: dict[frozenset[int], int] = {}
            for value in range(int(source["minimum"]), int(source["maximum"]) + 1):
                settings.seq_random_overrides = ((int(source["index"]), value),)
                variant_player = SequenceRenderer().make_sequence_player(context, settings)
                variant_events = variant_player.render_events(
                    max_ticks=settings.max_ticks,
                    loop_count=settings.loop_count,
                    one_shot=settings.one_shot,
                )
                signature = wave_signature(variant_events)
                if signature:
                    outcomes.setdefault(signature, value)
            if len(outcomes) <= 1:
                continue
            true_sources.append(source)
            for position, value in enumerate(outcomes.values(), start=1):
                variations.append({
                    "id": f"r{source['index']}:v{value}",
                    "label": f"Variation {position}",
                    "randomOverrides": [[int(source["index"]), value]],
                    "randomSource": source,
                })
            break

        return {
            "variations": variations,
            "programs": programs,
            "noteCount": len(note_events),
            "randomSources": true_sources,
        }

    @staticmethod
    def _parse_note_op(op: str) -> Optional[int]:
        """Parse a BRSEQ disassembly note op like ``c4`` / ``cs5`` / ``n60``
        into a MIDI note number. Returns None for non-note ops."""
        if not op:
            return None
        # Strip any conditional/random/etc. prefix suffix added by the formatter.
        head = op.split("_", 1)[0].strip().lower()
        if not head:
            return None
        if head.startswith("n"):
            rest = head[1:]
            if rest.isdigit():
                return int(rest)
            return None
        from pysar.core.format.rseq.mml import name_to_note
        try:
            return int(name_to_note(head))
        except Exception:
            return None

    @staticmethod
    def _parse_note_length(arg: str) -> int:
        """Note args look like ``velocity, length`` - pull the length in ticks."""
        if not arg:
            return 0
        parts = [p.strip() for p in arg.split(",") if p.strip()]
        if len(parts) >= 2:
            try:
                return int(parts[1])
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _valid_midi_note(value: int | None) -> int | None:
        if value is None:
            return None
        try:
            note = int(value)
        except (TypeError, ValueError):
            return None
        if 0 <= note <= 127:
            return note
        return None

    @staticmethod
    def _valid_program(value: int | None) -> int | None:
        if value is None:
            return None
        try:
            program = int(value)
        except (TypeError, ValueError):
            return None
        if program >= 0:
            return program
        return None

    @staticmethod
    def _valid_random_overrides(value: Any) -> tuple[tuple[int, int], ...]:
        if value is None:
            return ()
        if isinstance(value, dict):
            raw_pairs = value.items()
        elif isinstance(value, (list, tuple)):
            raw_pairs = value
        else:
            return ()
        out: list[tuple[int, int]] = []
        for item in raw_pairs:
            try:
                index, random_value = item
                out.append((max(0, int(index)), int(random_value)))
            except (TypeError, ValueError):
                continue
        return tuple(sorted(out[:16]))

    @staticmethod
    def _seq_event_times_ms(events: list[dict[str, Any]], timebase: int) -> list[tuple[int, dict[str, Any]]]:
        if timebase <= 0:
            timebase = 48
        current_tempo = 120
        last_tick = 0
        current_time = 0.0
        timed: list[tuple[int, dict[str, Any]]] = []
        for event in events:
            tick = int(event.get("tick", last_tick))
            delta_ticks = max(0, tick - last_tick)
            current_time += delta_ticks * 60.0 / (max(1, current_tempo) * timebase)
            last_tick = tick
            timed.append((int(round(current_time * 1000)), event))
            if event.get("type") == "tempo":
                current_tempo = int(event.get("tempo", current_tempo) or current_tempo)
        return timed

    def update_sound(self, sound_id: int, patch: dict) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            if sound_id < 0 or sound_id >= len(archive.data.sound_entries):
                return {"ok": False, "error": f"Invalid sound id {sound_id}"}
            entry = archive.data.sound_entries[sound_id]

            if "name" in patch:
                archive.rename_sound(int(sound_id), patch["name"])
            if "volume" in patch:
                entry.volume = max(0, min(127, int(patch["volume"])))
            if "player" in patch:
                pi = int(patch["player"])
                if 0 <= pi < len(archive.data.player_entries):
                    entry.player_index = pi
            if "priority" in patch:
                entry.player_priority = max(0, min(127, int(patch["priority"])))
            if "pan" in patch:
                entry.pan_mode = max(0, min(3, int(patch.get("panMode", entry.pan_mode))))
                entry.pan_curve = max(0, min(3, int(patch.get("panCurve", entry.pan_curve))))
            if "actorPlayerId" in patch:
                entry.actor_player_id = max(0, int(patch["actorPlayerId"]))

            self.project_service.mark_dirty(self.session)
            return {"ok": True, "data": self._ui_data(), "dirty": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def update_bank(self, bank_id: int, patch: dict) -> dict:
        if "name" not in patch:
            return {"ok": True, "dirty": False, "data": self._ui_data()}
        return self.rename_bank(bank_id, patch["name"])

    def rename_bank(self, bank_id: int, name: str) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            archive.rename_bank(int(bank_id), name)
            self.project_service.mark_dirty(self.session)
            return {
                "ok": True,
                "dirty": True,
                "bankId": int(bank_id),
                "data": self._ui_data(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_bank(
            self,
            bank_id: int,
            replacement_bank_id: int | None = None,
    ) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            bank_id = int(bank_id)
            if not 0 <= bank_id < len(archive.data.bank_entries):
                raise ValueError(f"Invalid bank id {bank_id}")
            references = [
                {
                    "kind": "sound",
                    "id": index,
                    "name": self.archive_service._sound_name(archive, index, sound),
                }
                for index, sound in enumerate(archive.data.sound_entries)
                if (
                    sound.sound_type == SoundType.SEQ
                    and getattr(sound.sound_info, "bank_index", None) == bank_id
                )
            ]
            if references and replacement_bank_id is None:
                candidates = [
                    index for index in range(len(archive.data.bank_entries))
                    if index != bank_id
                ]
                if not candidates:
                    return {
                        "ok": False,
                        "error": "Cannot delete the only bank while sequence sounds still reference it",
                    }
                target_file = int(archive.data.bank_entries[bank_id].file_index)
                suggested = next(
                    (
                        index for index in candidates
                        if int(archive.data.bank_entries[index].file_index) == target_file
                    ),
                    candidates[0],
                )
                suggested_entry = archive.data.bank_entries[suggested]
                suggested_name = (
                    archive.data.names[suggested_entry.file_name_index]
                    if 0 <= suggested_entry.file_name_index < len(archive.data.names)
                    else f"BANK_{suggested:04d}"
                )
                return {
                    "ok": False,
                    "requiresReplacement": True,
                    "error": "Bank is still referenced",
                    "references": references,
                    "suggestedReplacement": suggested,
                    "replacementName": suggested_name,
                }

            replacement_new = archive.delete_bank(bank_id, replacement_bank_id)
            self._clear_audio_streams()
            clear_wave_payload_cache()
            self.project_service.mark_dirty(self.session)
            return {
                "ok": True,
                "dirty": True,
                "deletedBankId": bank_id,
                "replacementBankId": replacement_new,
                "data": self._ui_data(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _get_brbnk_for_bank(self, bank_id: int):
        """Load Brbnk editor and file_id for a bank entry."""
        from pysar.core.format.rbnk import Brbnk
        archive = self.project_service.require_archive(self.session)
        if bank_id < 0 or bank_id >= len(archive.data.bank_entries):
            raise ValueError(f"Invalid bank id {bank_id}")
        bank_entry = archive.data.bank_entries[bank_id]
        data_fid, _ = self.archive_service._resolve_file_index(archive, bank_entry.file_index)
        if data_fid is None:
            raise ValueError("Bank has no embedded data file")
        embedded = archive.data.embedded_files.get(data_fid)
        if embedded is None or embedded.magic != "RBNK":
            raise ValueError("Bank file is not an RBNK")
        brbnk = Brbnk.from_bytes(embedded.raw_data)
        return brbnk, data_fid, archive

    def _ensure_bank_edit_lock(self) -> threading.Lock:
        """Return the bank mutation lock, including for lightweight test APIs."""
        with self._bank_edit_lock_init_lock:
            lock = getattr(self, "_bank_edit_lock", None)
            if lock is None:
                lock = threading.Lock()
                self._bank_edit_lock = lock
            return lock

    def _save_brbnk(
            self,
            brbnk,
            bank_id: int,
            archive,
            *,
            bootstrap_empty_wave: bool = False,
    ) -> None:
        """Serialize an edit into every physical copy of a logical bank."""
        archive.replace_bank_file(
            int(bank_id),
            brbnk.to_bytes(),
            preserve_child_provenance=True,
            bootstrap_empty_wave=bool(bootstrap_empty_wave),
        )
        self.project_service.mark_dirty(self.session)
        clear_wave_payload_cache()
        self._clear_audio_streams()

    def _bank_edit_response(self, bank_id: int, *, dirty: bool = True) -> dict:
        """Return local bank details plus refreshed archive-wide metadata."""
        return {
            "ok": True,
            "dirty": bool(dirty),
            "data": self.get_bank_details(int(bank_id)).get("data"),
            "archiveData": self._ui_data(),
        }

    def _bank_resources(self, bank_id: int):
        """Resolve a bank's RBNK and companion RWAR without using stale caches."""
        from pysar.core.format.rbnk import Brbnk
        from pysar.core.format.rwar import Brwar

        archive = self.project_service.require_archive(self.session)
        bank_id = int(bank_id)
        if bank_id < 0 or bank_id >= len(archive.data.bank_entries):
            raise ValueError(f"Invalid bank id {bank_id}")
        entry = archive.data.bank_entries[bank_id]
        data_ids: set[int] = set()
        audio_ids: set[int] = set()
        for group in archive.data.group_entries:
            for sub in group.group_table:
                if int(sub.group_index) != int(entry.file_index):
                    continue
                if sub.file_id is not None:
                    data_ids.add(int(sub.file_id))
                if sub.audio_file_id is not None:
                    audio_ids.add(int(sub.audio_file_id))
        data_fid = min(data_ids) if data_ids else None
        audio_fid = min(audio_ids) if audio_ids else None
        if data_fid is None:
            raise ValueError("Bank does not have an embedded RBNK file")
        data_file = archive.data.embedded_files.get(data_fid)
        audio_file = archive.data.embedded_files.get(audio_fid) if audio_fid is not None else None
        if data_file is None or data_file.magic != "RBNK":
            raise ValueError("Bank data file is not an RBNK")
        if audio_file is not None and audio_file.magic != "RWAR":
            raise ValueError("Bank audio file is not an RWAR")
        name = (
            archive.data.names[entry.file_name_index]
            if 0 <= entry.file_name_index < len(archive.data.names)
            else f"BANK_{bank_id:04d}"
        )
        return (
            archive,
            entry,
            int(data_fid),
            None if audio_fid is None else int(audio_fid),
            Brbnk.from_bytes(data_file.raw_data),
            None if audio_file is None else Brwar.from_bytes(audio_file.raw_data),
            name,
        )

    @staticmethod
    def _validate_bank_wave_references(brbnk, brwar) -> None:
        if brwar is None:
            if brbnk.get_wave_indices():
                raise ValueError("Bank references external waves but has no companion RWAR")
            return
        invalid = sorted(index for index in brbnk.get_wave_indices() if not 0 <= index < len(brwar))
        if invalid:
            preview = ", ".join(str(index) for index in invalid[:8])
            if len(invalid) > 8:
                preview += ", ..."
            raise ValueError(
                f"Bank references waves not present in its RWAR: {preview} "
                f"(archive has {len(brwar)} waves)"
            )

    def _shared_bank_names(self, bank_id: int) -> list[str]:
        archive = self.project_service.require_archive(self.session)
        target = archive.data.bank_entries[int(bank_id)]
        names = []
        for index, entry in enumerate(archive.data.bank_entries):
            if entry.file_index != target.file_index:
                continue
            names.append(
                archive.data.names[entry.file_name_index]
                if 0 <= entry.file_name_index < len(archive.data.names)
                else f"BANK_{index:04d}"
            )
        return names

    def _replace_bank_payloads(
            self,
            bank_id: int,
            bank_raw: bytes,
            wave_archive_raw: bytes | None = None,
    ) -> dict:
        from pysar.core.format.rbnk import Brbnk
        from pysar.core.format.rwar import Brwar

        archive, _entry, _data_fid, audio_fid, _old_bank, old_war, _name = self._bank_resources(bank_id)
        replacement_bank = Brbnk.from_bytes(bytes(bank_raw))
        replacement_war = old_war if wave_archive_raw is None else Brwar.from_bytes(bytes(wave_archive_raw))
        if wave_archive_raw is not None and audio_fid is None:
            raise ValueError("This bank has no companion RWAR slot to replace with SF2 samples")
        if not (replacement_war is None and replacement_bank.data.has_embedded_waves):
            self._validate_bank_wave_references(replacement_bank, replacement_war)

        # Everything above is validation. Mutate only after both payloads can
        # be parsed and serialized, so a failed import cannot half-replace a bank.
        final_bank_raw = bytes(bank_raw)
        final_war_raw = None if wave_archive_raw is None else bytes(wave_archive_raw)
        archive.replace_bank_file(int(bank_id), final_bank_raw, final_war_raw)
        self.project_service.mark_dirty(self.session)
        clear_wave_payload_cache()
        self._clear_audio_streams()
        details = self.get_bank_details(int(bank_id)).get("data")
        return {
            "ok": True,
            "dirty": True,
            "data": details,
            "archiveData": self._ui_data(),
            "sharedBanks": self._shared_bank_names(int(bank_id)),
        }

    @staticmethod
    def _file_matches_bytes(path: Path, payload: bytes, chunk_size: int = 1024 * 1024) -> bool:
        if not path.is_file() or path.stat().st_size != len(payload):
            return False
        view = memoryview(payload)
        with path.open("rb") as stream:
            offset = 0
            while offset < len(view):
                chunk = stream.read(min(chunk_size, len(view) - offset))
                if chunk != view[offset:offset + len(chunk)]:
                    return False
                if not chunk:
                    return False
                offset += len(chunk)
        return True

    @staticmethod
    def _write_export_files_atomically(outputs: list[tuple[Path, bytes]]) -> None:
        """Stage a related export and roll every destination back on failure."""
        if not outputs:
            return
        destinations = [path for path, _payload in outputs]
        if len(set(destinations)) != len(destinations):
            raise ValueError("Export destinations must be distinct")
        for path in destinations:
            if path.exists() and not path.is_file():
                raise ValueError(f"Export destination is not a file: {path}")
            if not path.parent.is_dir():
                raise ValueError(f"Export folder does not exist: {path.parent}")

        staged: list[tuple[Path, Path]] = []
        try:
            for path, payload in outputs:
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                with temporary.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                staged.append((path, temporary))
        except Exception:
            for _path, temporary in staged:
                temporary.unlink(missing_ok=True)
            raise

        backups: list[tuple[Path, Path]] = []
        installed: list[Path] = []
        try:
            for path, _temporary in staged:
                if path.exists():
                    backup = path.with_name(f".{path.name}.{uuid.uuid4().hex}.bak")
                    path.replace(backup)
                    backups.append((path, backup))
            for path, temporary in staged:
                temporary.replace(path)
                installed.append(path)
        except Exception as export_error:
            rollback_errors: list[str] = []
            for path in reversed(installed):
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    rollback_errors.append(f"remove {path}: {exc}")
            for path, backup in reversed(backups):
                try:
                    if backup.exists():
                        backup.replace(path)
                except OSError as exc:
                    rollback_errors.append(f"restore {path} from {backup}: {exc}")
            if rollback_errors:
                raise RuntimeError(
                    "Export failed and could not be fully rolled back: " + "; ".join(rollback_errors)
                ) from export_error
            raise
        else:
            for _path, backup in backups:
                backup.unlink(missing_ok=True)
        finally:
            for _path, temporary in staged:
                temporary.unlink(missing_ok=True)

    def export_bank_to_path(
        self,
        bank_id: int,
        output_path: str,
        export_format: str | None = None,
        overwrite_companion: bool = False,
    ) -> dict:
        """Testable non-dialog bank export entry point."""
        try:
            archive, _entry, data_fid, audio_fid, brbnk, brwar, name = self._bank_resources(bank_id)
            path = Path(str(output_path)).expanduser()
            kind = str(export_format or path.suffix.lstrip(".") or "brbnk").lower()
            warnings: list[str] = []
            companion_path: Path | None = None
            if kind in {"sf2", "soundfont"}:
                if brwar is None:
                    raise ValueError("SF2 export requires a companion RWAR wave archive")
                if path.suffix.lower() != ".sf2":
                    path = path.with_suffix(".sf2")
                brbnk.export_sf2(path, brwar=brwar, bank_name=name)
                stereo = [index for index in range(len(brwar)) if brwar[index].n_channels > 1]
                if stereo:
                    preview = ", ".join(str(index) for index in stereo[:12])
                    if len(stereo) > 12:
                        preview += ", ..."
                    warnings.append(
                        f"Downmixed {len(stereo)} stereo BRWAV sample(s) to mono for SF2 export "
                        f"(wave indexes: {preview})"
                    )
            elif kind in {"brbnk", "rbnk"}:
                if path.suffix.lower() != ".brbnk":
                    path = path.with_suffix(".brbnk")
                # Raw export is byte-identical and retains fields a future
                # reader may understand even if the editor does not touch them.
                bank_payload = archive.data.embedded_files[data_fid].raw_data
                outputs = [(path, bank_payload)]
                if audio_fid is not None:
                    companion_path = path.with_suffix(".brwar")
                    wave_payload = archive.data.embedded_files[audio_fid].raw_data
                    if companion_path.exists() and not companion_path.is_file():
                        raise ValueError(f"Companion BRWAR destination is not a file: {companion_path}")
                    if (
                        companion_path.exists()
                        and not self._file_matches_bytes(companion_path, wave_payload)
                        and not bool(overwrite_companion)
                    ):
                        return {
                            "ok": False,
                            "requiresCompanionOverwrite": True,
                            "error": "A different companion BRWAR already exists",
                            "companionPath": str(companion_path),
                        }
                    outputs.append((companion_path, wave_payload))
                self._write_export_files_atomically(outputs)
            else:
                raise ValueError(f"Unsupported bank export format: {kind}")
            return {
                "ok": True,
                "path": str(path),
                "companionPath": None if companion_path is None else str(companion_path),
                "format": kind,
                "warnings": warnings,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def export_bank_dialog(self, bank_id: int, export_format: str | None = None) -> dict:
        if self._window is None:
            return {"ok": False, "error": "No file dialog is available"}
        try:
            _archive, _entry, _data_fid, _audio_fid, _brbnk, _brwar, name = self._bank_resources(bank_id)
            stem = self._export_filename_stem(name, int(bank_id))
            all_formats = (
                ("Nintendo BRBNK (*.brbnk)", ".brbnk"),
                ("SoundFont 2 (*.sf2)", ".sf2"),
            )
            requested = (
                None
                if export_format is None
                else str(export_format).strip().lower().lstrip(".")
            )
            if requested in {None, ""}:
                formats = all_formats
                selection = self._choose_export_save_path(
                    f"{stem}.brbnk", formats, title="Export Bank",
                )
                if selection is None:
                    return {"ok": False, "error": "Cancelled", "cancelled": True}
                chosen, selected_filter = selection
                suffixes = dict(formats)
                selected_suffix = suffixes.get(selected_filter, formats[0][1])
            elif requested in {"brbnk", "rbnk"}:
                selected_suffix = ".brbnk"
                file_type = all_formats[0][0]
            elif requested in {"sf2", "soundfont"}:
                selected_suffix = ".sf2"
                file_type = all_formats[1][0]
            else:
                return {"ok": False, "error": f"Unsupported bank export format: {export_format}"}
            if requested not in {None, ""}:
                result = self._window.create_file_dialog(
                    dialog_type=FileDialog.SAVE,
                    save_filename=f"{stem}{selected_suffix}",
                    file_types=(file_type, "All files (*.*)"),
                )
                if not result:
                    return {"ok": False, "error": "Cancelled", "cancelled": True}
                chosen = result if isinstance(result, str) else result[0]
            output_path = self._normalise_export_path(chosen, selected_suffix)
            if output_path.suffix.lower() != selected_suffix:
                output_path = output_path.with_suffix(selected_suffix)
            exported = self.export_bank_to_path(
                int(bank_id), str(output_path), selected_suffix.lstrip("."),
            )
            if exported.get("requiresCompanionOverwrite"):
                # The web UI owns confirmation prompts so every platform gets
                # the same styled dialog. Return enough context to retry the
                # atomic paired export without opening the file picker again.
                exported.update({
                    "bankId": int(bank_id),
                    "path": str(output_path),
                    "format": selected_suffix.lstrip("."),
                })
            return exported
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _bank_import_kind(path: Path) -> str:
        with path.open("rb") as stream:
            header = stream.read(12)
        if header[:4] == b"RBNK":
            return "brbnk"
        if header[:4] == b"RIFF" and header[8:12] == b"sfbk":
            return "sf2"
        raise ValueError("Selected file is neither a BRBNK nor a SoundFont 2 file")

    def replace_bank_from_path(self, bank_id: int, source_path: str) -> dict:
        try:
            from pysar.core.format.rbnk.sf2_import import load_sf2

            path = Path(str(source_path)).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"Bank file not found: {path}")
            kind = self._bank_import_kind(path)
            if kind == "sf2":
                imported = load_sf2(path)
                from pysar.core.format.rbnk import Brbnk
                result = self._replace_bank_payloads(
                    int(bank_id),
                    Brbnk(imported.bank).to_bytes(),
                    imported.wave_archive.to_bytes(),
                )
                result["warnings"] = imported.warnings
                result["format"] = "sf2"
                return result
            sidecar = next(
                (
                    candidate
                    for candidate in (path.with_suffix(".brwar"), path.with_suffix(".rwar"))
                    if candidate.is_file()
                ),
                None,
            )
            result = self._replace_bank_payloads(
                int(bank_id),
                path.read_bytes(),
                None if sidecar is None else sidecar.read_bytes(),
            )
            result["warnings"] = []
            result["format"] = "brbnk"
            result["companionPath"] = None if sidecar is None else str(sidecar)
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def replace_bank_dialog(
            self,
            bank_id: int,
            confirm_shared: bool = False,
            import_format: str | None = None,
    ) -> dict:
        if self._window is None:
            return {"ok": False, "error": "No file dialog is available"}
        try:
            requested = None if import_format is None else str(import_format).strip().lower().lstrip(".")
            if requested in {"rbnk"}:
                requested = "brbnk"
            elif requested in {"soundfont"}:
                requested = "sf2"
            if requested not in {None, "", "brbnk", "sf2"}:
                raise ValueError(f"Unsupported bank import format: {import_format}")

            shared = self._shared_bank_names(int(bank_id))
            if len(shared) > 1 and not bool(confirm_shared):
                return {
                    "ok": False,
                    "requiresConfirmation": True,
                    "sharedBanks": shared,
                    "error": "This bank file is shared by multiple bank entries",
                }
            file_types = {
                "brbnk": ("Nintendo BRBNK (*.brbnk)",),
                "sf2": ("SoundFont 2 (*.sf2)",),
            }.get(requested, ("Bank files (*.brbnk;*.sf2)", "All files (*.*)"))
            result = self._window.create_file_dialog(
                dialog_type=FileDialog.OPEN,
                allow_multiple=False,
                file_types=file_types,
            )
            if not result:
                return {"ok": False, "error": "Cancelled", "cancelled": True}
            chosen = result if isinstance(result, str) else result[0]
            chosen_path = Path(str(chosen))
            if requested:
                expected_suffix = ".brbnk" if requested == "brbnk" else ".sf2"
                if chosen_path.suffix.lower() != expected_suffix:
                    raise ValueError(f"Please choose a {expected_suffix.upper()} file")
                if self._bank_import_kind(chosen_path) != requested:
                    raise ValueError(
                        f"The selected {expected_suffix.upper()} file does not contain valid "
                        f"{'BRBNK' if requested == 'brbnk' else 'SoundFont 2'} data"
                    )
            return self.replace_bank_from_path(int(bank_id), str(chosen))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def import_bank_from_path(self, source_path: str, name: str | None = None) -> dict:
        """Import a new bank. SF2 is self-contained; BRBNK needs a sidecar BRWAR."""
        try:
            from pysar.core.format.rbnk import Brbnk
            from pysar.core.format.rbnk.sf2_import import load_sf2
            from pysar.core.format.rwar import Brwar

            path = Path(str(source_path)).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"Bank file not found: {path}")
            kind = self._bank_import_kind(path)
            warnings: list[str] = []
            if kind == "sf2":
                imported = load_sf2(path)
                bank_raw = Brbnk(imported.bank).to_bytes()
                war_raw = imported.wave_archive.to_bytes()
                base_name = imported.name or path.stem
                warnings = imported.warnings
            else:
                bank = Brbnk.from_bytes(path.read_bytes())
                sidecar = next(
                    (candidate for candidate in (path.with_suffix(".brwar"), path.with_suffix(".rwar")) if candidate.is_file()),
                    None,
                )
                if sidecar is None:
                    if bank.data.has_embedded_waves:
                        warnings.append(
                            "Imported the legacy embedded-wave BRBNK unchanged; editing its embedded "
                            "wave block is not supported"
                        )
                    elif bank.get_wave_indices():
                        raise ValueError(
                            "A standalone BRBNK contains mappings but no samples. Place a matching "
                            f"{path.stem}.brwar beside it, import an SF2, or replace an existing bank."
                        )
                    war = Brwar.new()
                    war_raw = war.to_bytes()
                else:
                    war = Brwar.open(sidecar)
                    self._validate_bank_wave_references(bank, war)
                    war_raw = sidecar.read_bytes()
                bank_raw = path.read_bytes()
                base_name = path.stem

            archive = self.project_service.require_archive(self.session)
            if name is None:
                cleaned = "".join(
                    char if char.isascii() and (char.isalnum() or char == "_") else "_"
                    for char in str(base_name).strip()
                ).strip("_")
                if cleaned and cleaned[0].isdigit():
                    cleaned = "BANK_" + cleaned
                desired = cleaned or f"BANK_{len(archive.data.bank_entries):04d}"
            else:
                desired = str(name).strip()
            existing = {
                archive.data.names[entry.file_name_index]
                for entry in archive.data.bank_entries
                if 0 <= entry.file_name_index < len(archive.data.names)
            }
            unique = desired
            suffix = 1
            while unique in existing:
                unique = f"{desired}_{suffix:02d}"
                suffix += 1
            bank_index = archive.create_bank(unique, bank_raw, war_raw)
            self.project_service.mark_dirty(self.session)
            clear_wave_payload_cache()
            self._clear_audio_streams()
            return {
                "ok": True,
                "dirty": True,
                "bankId": bank_index,
                "name": unique,
                "warnings": warnings,
                "data": self._ui_data(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def create_bank(self, name: str | None = None) -> dict:
        """Create an empty editable RBNK with an empty companion RWAR."""
        try:
            from pysar.core.format.rbnk import Brbnk
            from pysar.core.format.rwar import Brwar

            archive = self.project_service.require_archive(self.session)
            desired = str(name or "").strip() or f"BANK_{len(archive.data.bank_entries):04d}"
            bank_index = archive.create_bank(
                desired,
                Brbnk.new().to_bytes(),
                Brwar.new().to_bytes(),
            )
            self.project_service.mark_dirty(self.session)
            return {
                "ok": True,
                "dirty": True,
                "bankId": bank_index,
                "name": desired,
                "data": self._ui_data(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def import_bank_dialog(self, import_format: str | None = None) -> dict:
        if self._window is None:
            return {"ok": False, "error": "No file dialog is available"}
        try:
            requested = None if import_format is None else str(import_format).strip().lower().lstrip(".")
            if requested in {"rbnk"}:
                requested = "brbnk"
            elif requested in {"soundfont"}:
                requested = "sf2"
            if requested not in {None, "", "brbnk", "sf2"}:
                raise ValueError(f"Unsupported bank import format: {import_format}")
            file_types = {
                "brbnk": ("Nintendo BRBNK (*.brbnk)",),
                "sf2": ("SoundFont 2 (*.sf2)",),
            }.get(requested, ("Bank files (*.brbnk;*.sf2)", "All files (*.*)"))
            result = self._window.create_file_dialog(
                dialog_type=FileDialog.OPEN,
                allow_multiple=False,
                file_types=file_types,
            )
            if not result:
                return {"ok": False, "error": "Cancelled", "cancelled": True}
            chosen = result if isinstance(result, str) else result[0]
            chosen_path = Path(str(chosen))
            if requested:
                expected_suffix = ".brbnk" if requested == "brbnk" else ".sf2"
                if chosen_path.suffix.lower() != expected_suffix:
                    raise ValueError(f"Please choose a {expected_suffix.upper()} file")
                if self._bank_import_kind(chosen_path) != requested:
                    raise ValueError(
                        f"The selected {expected_suffix.upper()} file does not contain valid "
                        f"{'BRBNK' if requested == 'brbnk' else 'SoundFont 2'} data"
                    )
            return self.import_bank_from_path(str(chosen))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def update_bank_zone(self, bank_id: int, program: int, zone_index: int, patch: dict) -> dict:
        try:
            brbnk, _data_fid, archive = self._get_brbnk_for_bank(bank_id)
            brbnk.update_zone(int(program), int(zone_index), patch)
            self._save_brbnk(brbnk, int(bank_id), archive)
            return self._bank_edit_response(bank_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def add_bank_instrument(self, bank_id: int, program: int) -> dict:
        try:
            bank_id = int(bank_id)
            program = int(program)
            if program < 0:
                raise ValueError("Instrument program cannot be negative")
            with self._ensure_bank_edit_lock():
                brbnk, _data_fid, archive = self._get_brbnk_for_bank(bank_id)
                if (
                    program < len(brbnk.instruments)
                    and not brbnk.instruments[program].is_empty()
                ):
                    response = self._bank_edit_response(bank_id, dirty=False)
                    response["alreadyExists"] = True
                    return response

                from pysar.core.model.brbnk import InstParam
                brbnk.add_instrument(program, InstParam())
                self._save_brbnk(
                    brbnk,
                    bank_id,
                    archive,
                    bootstrap_empty_wave=True,
                )
                archive.register_new("bank_instrument", bank_id, program)
                archive.register_new("bank_zone", bank_id, program, 0)
                return self._bank_edit_response(bank_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def remove_bank_instrument(self, bank_id: int, program: int) -> dict:
        try:
            brbnk, _data_fid, archive = self._get_brbnk_for_bank(bank_id)
            bank_id = int(bank_id)
            program = int(program)
            archive.require_safe_mutation(
                "deleting it", "bank_instrument", bank_id, program,
            )
            brbnk.remove_instrument(program)
            self._save_brbnk(brbnk, bank_id, archive)
            archive.unregister_new(
                "bank_instrument", bank_id, program, recursive=True,
            )
            return self._bank_edit_response(bank_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def set_bank_instrument_program(self, bank_id: int, old_program: int, new_program: int) -> dict:
        try:
            brbnk, _data_fid, archive = self._get_brbnk_for_bank(bank_id)
            bank_id = int(bank_id)
            old_program = int(old_program)
            new_program = int(new_program)
            archive.require_safe_mutation(
                "reindexing it", "bank_instrument", bank_id, old_program,
            )
            brbnk.set_instrument_program(old_program, new_program)
            self._save_brbnk(brbnk, bank_id, archive)
            archive.move_bank_instrument_provenance(
                bank_id, old_program, new_program,
            )
            return self._bank_edit_response(bank_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def split_bank_key_region(self, bank_id: int, program: int, at_key: int) -> dict:
        try:
            brbnk, _data_fid, archive = self._get_brbnk_for_bank(bank_id)
            bank_id = int(bank_id)
            program = int(program)
            at_key = int(at_key)
            if not 0 <= program < len(brbnk.instruments):
                raise ValueError(f"Invalid program {program}")
            zones = brbnk.instruments[program].get_all_inst_params()
            target_zone = next(
                (
                    index for index, (_param, key_range, _velocity_range) in enumerate(zones)
                    if key_range is None or int(key_range[0]) <= at_key <= int(key_range[1])
                ),
                0,
            )
            brbnk.split_key_region(program, at_key)
            self._save_brbnk(brbnk, bank_id, archive)
            inserted_zone = target_zone + 1
            archive.remap_child_provenance_after_insert(
                "bank_zone", (bank_id, program), inserted_zone,
            )
            archive.register_new("bank_zone", bank_id, program, inserted_zone)
            return self._bank_edit_response(bank_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_bank_zone(self, bank_id: int, program: int, zone_index: int) -> dict:
        try:
            brbnk, _data_fid, archive = self._get_brbnk_for_bank(bank_id)
            bank_id = int(bank_id)
            program = int(program)
            zone_index = int(zone_index)
            archive.require_safe_mutation(
                "deleting it", "bank_zone", bank_id, program, zone_index,
            )
            brbnk.delete_zone(program, zone_index)
            became_empty = (
                0 <= program < len(brbnk.instruments)
                and brbnk.instruments[program].is_empty()
            )
            self._save_brbnk(brbnk, bank_id, archive)
            archive.remap_child_provenance_after_delete(
                "bank_zone", (bank_id, program), zone_index,
            )
            if became_empty:
                archive.unregister_new(
                    "bank_instrument", bank_id, program, recursive=True,
                )
            return self._bank_edit_response(bank_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def resize_bank_key_region(self, bank_id: int, program: int, zone_index: int, new_high: int) -> dict:
        try:
            brbnk, _data_fid, archive = self._get_brbnk_for_bank(bank_id)
            brbnk.resize_key_region(int(program), int(zone_index), int(new_high))
            self._save_brbnk(brbnk, int(bank_id), archive)
            return self._bank_edit_response(bank_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def play_bank_note(self, bank_id: int, program: int, key: int, velocity: int = 127) -> dict:
        """Play a single note from a bank instrument."""
        try:
            brbnk, data_fid, archive = self._get_brbnk_for_bank(bank_id)
            param = brbnk.get_inst_param(int(program), int(key), int(velocity))
            if param is None:
                return {"ok": False, "error": "No sample mapped to this key/velocity"}
            bank_entry = archive.data.bank_entries[int(bank_id)]
            _, audio_fid = self.archive_service._resolve_file_index(archive, bank_entry.file_index)
            if audio_fid is None:
                return {"ok": False, "error": "Bank has no audio file"}
            self.audio_service.play_wave_sample(
                self.session, int(audio_fid), int(param.wave_index),
                volume=param.volume / 127.0,
                playback_rate=midi_ratio(int(key), int(param.original_key), float(param.pitch)),
            )
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def create_group(self, name: str | None = None) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            archive.create_group(name)
            self.project_service.mark_dirty(self.session)
            return {"ok": True, "dirty": True, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def rename_group(self, group_id: int, name: str) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            archive.rename_group(int(group_id), str(name))
            self.project_service.mark_dirty(self.session)
            return {"ok": True, "dirty": True, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_group(self, group_id: int) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            archive.delete_group(int(group_id))
            self.project_service.mark_dirty(self.session)
            return {"ok": True, "dirty": True, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def reorder_groups(self, order: list[int]) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            archive.reorder_groups([int(i) for i in order])
            self.project_service.mark_dirty(self.session)
            return {"ok": True, "dirty": True, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def move_files_to_group(self, file_indices: list[int], target_group_id: int) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            archive.move_files_to_group([int(i) for i in file_indices], int(target_group_id))
            self.project_service.mark_dirty(self.session)
            return {"ok": True, "dirty": True, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _choose_import_path(self, file_types: tuple[str, ...]) -> Path | None:
        if self._window is None:
            raise RuntimeError("No window")
        result = self._window.create_file_dialog(
            dialog_type=FileDialog.OPEN,
            allow_multiple=False,
            file_types=file_types,
        )
        if not result:
            return None
        chosen = result if isinstance(result, str) else result[0]
        return Path(str(chosen)).expanduser()

    @staticmethod
    def _read_import_file(path: str | Path, *, max_bytes: int = 512 * 1024 * 1024) -> bytes:
        source = Path(path).expanduser()
        if not source.is_file():
            raise ValueError(f"File does not exist: {source}")
        size = source.stat().st_size
        if size <= 0:
            raise ValueError("Import file is empty")
        if size > max_bytes:
            raise ValueError(f"Import file is too large ({size} bytes)")
        return source.read_bytes()

    def export_wave_archive_to_path(self, file_id: int, path: str) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            embedded = archive.data.embedded_files.get(int(file_id))
            if embedded is None or embedded.magic != "RWAR":
                raise ValueError(f"Embedded file {file_id} is not an RWAR")
            # Parse before export so corrupt embedded data is never presented as
            # a successful standalone archive.
            from pysar.core.format.rwar import Brwar
            Brwar.from_bytes(embedded.raw_data)
            destination = self._normalise_export_path(path, ".brwar").with_suffix(".brwar")
            destination.write_bytes(embedded.raw_data)
            return {"ok": True, "path": str(destination), "fileId": int(file_id)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def export_wave_archive_dialog(self, file_id: int) -> dict:
        try:
            selection = self._choose_export_save_path(
                f"WAR_{int(file_id):04d}.brwar",
                (("Nintendo BRWAR (*.brwar)", ".brwar"),),
            )
            if selection is None:
                return {"ok": False, "error": "Cancelled"}
            return self.export_wave_archive_to_path(int(file_id), str(selection[0]))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _wave_archive_sample(self, file_id: int, wave_index: int):
        """Return a validated BRWAR sample together with its owning archive."""
        from pysar.core.format.rwar import Brwar

        archive = self.project_service.require_archive(self.session)
        file_id = int(file_id)
        wave_index = int(wave_index)
        embedded = archive.data.embedded_files.get(file_id)
        if embedded is None or embedded.magic != "RWAR":
            raise ValueError(f"Embedded file {file_id} is not an RWAR")
        brwar = Brwar.from_bytes(embedded.raw_data)
        if not 0 <= wave_index < len(brwar):
            raise IndexError(f"BRWAV index {wave_index} is out of range for WAR_{file_id:04d}")
        return archive, brwar, brwar[wave_index]

    def export_wave_archive_sample_to_path(self, file_id: int, wave_index: int, path: str) -> dict:
        """Export one BRWAR entry as its raw BRWAV or decoded WAV equivalent."""
        try:
            _archive, _brwar, brwav = self._wave_archive_sample(file_id, wave_index)
            destination = self._normalise_export_path(path, ".wav")
            suffix = destination.suffix.lower()
            if suffix == ".brwav":
                destination.write_bytes(self._brwav_export_bytes(brwav))
                file_format = "BRWAV"
            elif suffix == ".wav":
                brwav.decode_to_wav(destination)
                file_format = "WAV"
            else:
                raise ValueError("BRWAR samples can only be exported as .brwav or .wav")
            return {
                "ok": True,
                "fileId": int(file_id),
                "waveIndex": int(wave_index),
                "format": file_format,
                "path": str(destination),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def export_wave_archive_sample_dialog(self, file_id: int, wave_index: int) -> dict:
        """Prompt for a BRWAV/WAV destination with format choice in the save dialog."""
        try:
            file_id = int(file_id)
            wave_index = int(wave_index)
            self._wave_archive_sample(file_id, wave_index)  # validate before showing UI
            formats = (
                ("WAV audio (*.wav)", ".wav"),
                ("Nintendo BRWAV (*.brwav)", ".brwav"),
            )
            selection = self._choose_export_save_path(
                f"WAR_{file_id:04d}_wave_{wave_index:04d}.wav",
                formats,
                title="Export BRWAV",
            )
            if selection is None:
                return {"ok": False, "error": "Cancelled"}
            chosen, selected_filter = selection
            selected_suffix = dict(formats).get(selected_filter, formats[0][1])
            destination = self._normalise_export_path(chosen, selected_suffix)
            if destination.suffix.lower() != selected_suffix:
                destination = destination.with_suffix(selected_suffix)
            return self.export_wave_archive_sample_to_path(file_id, wave_index, str(destination))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _replacement_brwav_from_path(path: str, encoding: str | None = None):
        """Load raw BRWAV input or encode WAV input using the requested codec."""
        from pysar.core.format.rwav import Brwav
        from pysar.core.types import AudioCodec

        source = Path(str(path)).expanduser()
        if not source.is_file():
            raise ValueError(f"Replacement file does not exist: {source}")
        suffix = source.suffix.lower()
        if suffix == ".brwav":
            return Brwav.from_bytes(source.read_bytes())
        if suffix == ".wav":
            try:
                target_codec = AudioCodec[str(encoding or "").strip().upper()]
            except KeyError as exc:
                raise ValueError(
                    "Choose an RWAV encoding for the WAV import: ADPCM, PCM16, or PCM8"
                ) from exc
            return Brwav.from_wav(source, encoding=target_codec)
        raise ValueError("Choose a .brwav or uncompressed mono .wav file")

    def _choose_brwav_replacement_source(self) -> tuple[Path, str] | None:
        source = self._choose_import_path((
            "Audio samples (*.wav;*.brwav)",
            "WAV audio (*.wav)",
            "Nintendo BRWAV (*.brwav)",
        ))
        if source is None:
            return None
        suffix = source.suffix.lower()
        if suffix not in {".wav", ".brwav"}:
            raise ValueError("Choose a .brwav or uncompressed mono .wav file")
        return source, "WAV" if suffix == ".wav" else "BRWAV"

    def replace_wave_archive_sample_from_path(
        self,
        file_id: int,
        wave_index: int,
        path: str,
        encoding: str | None = None,
    ) -> dict:
        """Replace one BRWAR entry while retaining all BRSAR copy metadata.

        Raw BRWAV input is copied with its embedded encoding.  WAV input is
        encoded using the explicitly selected target codec.
        """
        try:
            replacement = self._replacement_brwav_from_path(path, encoding)

            archive, brwar, _old_wave = self._wave_archive_sample(file_id, wave_index)
            file_id = int(file_id)
            wave_index = int(wave_index)
            brwar.replace(wave_index, replacement)
            # Reuse the archive-level replacement path: it updates every
            # physical RWAR copy and every dependent FILE/GROUP size field.
            archive.replace_wave_archive(file_id, brwar.to_bytes())
            self._clear_audio_streams()
            self.project_service.mark_dirty(self.session)

            details = self._wave_archive_details_payload(
                self.archive_service.get_wave_archive_details(self.session, file_id)
            )
            return {
                "ok": True,
                "dirty": True,
                "fileId": file_id,
                "waveIndex": wave_index,
                "wave": details["waves"][wave_index],
                "data": self._ui_data(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_wave_archive_sample_replacement_source_dialog(self, file_id: int, wave_index: int) -> dict:
        """Choose a replacement source before asking the UI for WAV encoding."""
        try:
            self._wave_archive_sample(file_id, wave_index)  # validate selection first
            selection = self._choose_brwav_replacement_source()
            if selection is None:
                return {"ok": False, "error": "Cancelled"}
            source, source_format = selection
            return {
                "ok": True,
                "fileId": int(file_id),
                "waveIndex": int(wave_index),
                "path": str(source),
                "sourceFormat": source_format,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def import_wave_archive_from_path(self, path: str, group_id: int | None = None) -> dict:
        try:
            raw = self._read_import_file(path)
            archive = self.project_service.require_archive(self.session)
            file_id = archive.import_wave_archive(
                raw,
                group_index=None if group_id is None else int(group_id),
            )
            self._clear_audio_streams()
            clear_wave_payload_cache()
            self.project_service.mark_dirty(self.session)
            return {"ok": True, "dirty": True, "fileId": file_id, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def import_wave_archive_dialog(self) -> dict:
        try:
            source = self._choose_import_path(("Nintendo BRWAR (*.brwar *.rwar)", "All files (*.*)"))
            if source is None:
                return {"ok": False, "error": "Cancelled"}
            return self.import_wave_archive_from_path(str(source))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def replace_wave_archive_from_path(self, file_id: int, path: str) -> dict:
        try:
            raw = self._read_import_file(path)
            archive = self.project_service.require_archive(self.session)
            wave_count = archive.replace_wave_archive(int(file_id), raw)
            self._clear_audio_streams()
            clear_wave_payload_cache()
            self.project_service.mark_dirty(self.session)
            return {
                "ok": True,
                "dirty": True,
                "fileId": int(file_id),
                "waveCount": wave_count,
                "data": self._ui_data(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def replace_wave_archive_dialog(self, file_id: int) -> dict:
        try:
            source = self._choose_import_path(("Nintendo BRWAR (*.brwar *.rwar)", "All files (*.*)"))
            if source is None:
                return {"ok": False, "error": "Cancelled"}
            return self.replace_wave_archive_from_path(int(file_id), str(source))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_wave_archive(self, file_id: int, confirmed: bool = False) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            references = archive.get_wave_archive_references(int(file_id))
            live = [ref for ref in references if ref["kind"] in {"bank", "sound", "file"}]
            if live and not bool(confirmed):
                return {
                    "ok": False,
                    "requiresConfirmation": True,
                    "error": "Wave archive is still referenced",
                    "references": live,
                }
            archive.delete_wave_archive(int(file_id), detach_references=bool(confirmed))
            self._clear_audio_streams()
            clear_wave_payload_cache()
            self.project_service.mark_dirty(self.session)
            return {"ok": True, "dirty": True, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _player_json_payload(archive, player_id: int) -> dict[str, Any]:
        player_id = int(player_id)
        if not 0 <= player_id < len(archive.data.player_entries):
            raise ValueError(f"Invalid player id {player_id}")
        entry = archive.data.player_entries[player_id]
        name = (
            archive.data.names[entry.file_name_index]
            if 0 <= entry.file_name_index < len(archive.data.names)
            else f"PLAYER_{player_id:04d}"
        )
        return {
            "format": "pysar-player",
            "version": 1,
            "name": name,
            "playableSounds": int(entry.n_playable_sounds),
            "heapSize": int(entry.heap_size),
        }

    @staticmethod
    def _parse_player_json(raw: bytes) -> dict[str, Any]:
        if len(raw) > 1024 * 1024:
            raise ValueError("Player metadata file is too large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Player metadata is not valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Player metadata must be a JSON object")
        if payload.get("format", "pysar-player") != "pysar-player":
            raise ValueError("Unsupported player metadata format")

        def bounded_integer(value: object, label: str, minimum: int, maximum: int) -> int:
            # JSON booleans subclass int in Python, and int(1.9) silently
            # truncates. Neither is faithful player metadata.
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{label} must be an integer")
            if not minimum <= value <= maximum:
                raise ValueError(f"{label} must be between {minimum} and {maximum}")
            return value

        if bounded_integer(payload.get("version", 1), "Player metadata version", 1, 1) != 1:
            raise ValueError("Unsupported player metadata version")
        raw_name = payload.get("name", "")
        if not isinstance(raw_name, str):
            raise ValueError("Player metadata name must be a string")
        name = raw_name.strip()
        if not name:
            raise ValueError("Player metadata has no name")
        playable_sounds = payload.get("playableSounds", payload.get("nPlayableSounds", 0))
        heap_size = payload.get("heapSize", payload.get("heap", 0))
        return {
            "name": name,
            "playableSounds": bounded_integer(playable_sounds, "Playable sounds", 0, 0xFF),
            "heapSize": bounded_integer(heap_size, "Heap size", 0, 0xFFFFFFFF),
        }

    def create_player(self, name: str | None = None) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            player_id = archive.create_player(name)
            self.project_service.mark_dirty(self.session)
            return {"ok": True, "dirty": True, "playerId": player_id, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def update_player(self, player_id: int, patch: dict) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            current = self._player_json_payload(archive, int(player_id))
            archive.update_player(
                int(player_id),
                name=str(patch.get("name", current["name"])),
                playable_sounds=patch.get("playableSounds", current["playableSounds"]),
                heap_size=patch.get("heapSize", current["heapSize"]),
            )
            self.project_service.mark_dirty(self.session)
            return {"ok": True, "dirty": True, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def export_player_to_path(self, player_id: int, path: str) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            payload = self._player_json_payload(archive, int(player_id))
            destination = self._normalise_export_path(path, ".json").with_suffix(".json")
            destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            return {"ok": True, "path": str(destination), "playerId": int(player_id)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def export_player_dialog(self, player_id: int) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            payload = self._player_json_payload(archive, int(player_id))
            stem = self._export_filename_stem(payload["name"], int(player_id))
            selection = self._choose_export_save_path(
                f"{stem}.json", (("Pysar player metadata (*.json)", ".json"),),
            )
            if selection is None:
                return {"ok": False, "error": "Cancelled"}
            return self.export_player_to_path(int(player_id), str(selection[0]))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def import_player_from_path(self, path: str) -> dict:
        try:
            payload = self._parse_player_json(self._read_import_file(path, max_bytes=1024 * 1024))
            archive = self.project_service.require_archive(self.session)
            player_id = archive.create_player(
                payload["name"],
                playable_sounds=payload["playableSounds"],
                heap_size=payload["heapSize"],
            )
            self.project_service.mark_dirty(self.session)
            return {"ok": True, "dirty": True, "playerId": player_id, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def import_player_dialog(self) -> dict:
        try:
            source = self._choose_import_path(("Pysar player metadata (*.json)", "JSON files (*.json)"))
            if source is None:
                return {"ok": False, "error": "Cancelled"}
            return self.import_player_from_path(str(source))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def replace_player_from_path(self, player_id: int, path: str) -> dict:
        try:
            payload = self._parse_player_json(self._read_import_file(path, max_bytes=1024 * 1024))
            archive = self.project_service.require_archive(self.session)
            archive.update_player(
                int(player_id),
                name=payload["name"],
                playable_sounds=payload["playableSounds"],
                heap_size=payload["heapSize"],
            )
            self.project_service.mark_dirty(self.session)
            return {"ok": True, "dirty": True, "playerId": int(player_id), "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def replace_player_dialog(self, player_id: int) -> dict:
        try:
            source = self._choose_import_path(("Pysar player metadata (*.json)", "JSON files (*.json)"))
            if source is None:
                return {"ok": False, "error": "Cancelled"}
            return self.replace_player_from_path(int(player_id), str(source))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_player(self, player_id: int, replacement_player_id: int | None = None) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            player_id = int(player_id)
            if not 0 <= player_id < len(archive.data.player_entries):
                raise ValueError(f"Invalid player id {player_id}")
            references = [
                {"kind": "sound", "id": index, "name": self.archive_service._sound_name(archive, index, sound)}
                for index, sound in enumerate(archive.data.sound_entries)
                if int(sound.player_index) == player_id
            ]
            if references and replacement_player_id is None:
                candidates = [index for index in range(len(archive.data.player_entries)) if index != player_id]
                if not candidates:
                    return {
                        "ok": False,
                        "error": "Cannot delete the only player while sounds still reference it",
                    }
                suggested = candidates[0]
                replacement_name = self._player_json_payload(archive, suggested)["name"]
                return {
                    "ok": False,
                    "requiresReplacement": True,
                    "error": "Player is still referenced",
                    "references": references,
                    "suggestedReplacement": suggested,
                    "replacementName": replacement_name,
                }
            archive.delete_player(player_id, replacement_player_id)
            self.project_service.mark_dirty(self.session)
            return {"ok": True, "dirty": True, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_dirty_state(self) -> dict:
        return {"ok": True, "dirty": self.session.dirty}

    def save_archive_as(self) -> dict:
        if self._window is None:
            return {"ok": False, "error": "No window"}
        try:
            result = self._window.create_file_dialog(
                dialog_type=FileDialog.SAVE,
                save_filename="archive.brsar",
                file_types=("BRSAR archive (*.brsar)", "All files (*.*)"),
            )
            if not result:
                return {"ok": False, "error": "Cancelled"}
            path = result if isinstance(result, str) else result[0]
            saved = self.project_service.save_archive(self.session, path)
            try:
                self.recent_service.remember(str(saved))
            except Exception:
                pass
            return {"ok": True, "path": str(saved), "dirty": False, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _choose_export_save_path(
        self,
        default_filename: str,
        formats: tuple[tuple[str, str], ...],
        *,
        title: str = "Export",
    ) -> tuple[Path, str] | None:
        """Open the current pywebview backend's native save panel.

        Cocoa needs one small adapter because pywebview exposes its file-type
        chooser for open panels but not save panels.  The other backends expose
        their native file-type picker through ``create_file_dialog`` directly.
        """
        from pysar.native_save_dialog import UNAVAILABLE, choose_macos_export_path

        native_selection = choose_macos_export_path(
            self._window,
            default_filename,
            formats,
            title=title,
        )
        if native_selection is not UNAVAILABLE:
            return native_selection

        if self._window is None:
            raise RuntimeError("No file dialog is available")
        result = self._window.create_file_dialog(
            dialog_type=FileDialog.SAVE,
            save_filename=default_filename,
            file_types=tuple(label for label, _suffix in formats),
        )
        if not result:
            return None
        chosen = Path(str(result if isinstance(result, str) else result[0])).expanduser()
        selected_filter = next(
            (label for label, suffix in formats if chosen.suffix.lower() == suffix.lower()),
            formats[0][0],
        )
        return chosen, selected_filter

    def export_sound_dialog(self, sound_id: int) -> dict:
        """Choose a format and destination appropriate for one sound, then export it.

        The native save panel is deliberately configured from the selected sound
        type: streams are rendered to WAV, wave sounds additionally expose their
        source BRWAV data, and sequences may be rendered, dumped as BRSEQ, or
        converted to MIDI.
        """
        if self._window is None:
            return {"ok": False, "error": "No window"}
        try:
            archive = self.project_service.require_archive(self.session)
            entry = self.archive_service._sound_entry(archive, int(sound_id))
            sound_name = self.archive_service._sound_name(archive, int(sound_id), entry)
            stem = self._export_filename_stem(sound_name, int(sound_id))

            if entry.sound_type == SoundType.STRM:
                formats = (("WAV audio (*.wav)", ".wav"),)
            elif entry.sound_type == SoundType.WAVE:
                formats = (
                    ("WAV audio (*.wav)", ".wav"),
                    ("Nintendo BRWAV (*.brwav)", ".brwav"),
                )
            elif entry.sound_type == SoundType.SEQ:
                formats = (
                    ("WAV audio (*.wav)", ".wav"),
                    ("Nintendo BRSEQ (*.brseq)", ".brseq"),
                    ("Standard MIDI (*.midi;*.mid)", ".midi"),
                )
            else:
                return {"ok": False, "error": "Unsupported sound type"}

            selection = self._choose_export_save_path(f"{stem}.wav", formats)
            if selection is None:
                return {"ok": False, "error": "Cancelled"}
            chosen, selected_filter = selection
            suffixes = dict(formats)
            selected_suffix = suffixes.get(selected_filter, formats[0][1])
            output_path = self._normalise_export_path(chosen, selected_suffix)
            if output_path.suffix.lower() != selected_suffix:
                output_path = output_path.with_suffix(selected_suffix)
            return self._export_sound_to_path(int(sound_id), output_path)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _export_filename_stem(name: str, sound_id: int) -> str:
        """Return a portable default filename without allowing path components."""
        stem = str(name or "").replace("/", "_").replace("\\", "_").replace("\x00", "").strip()
        return stem or f"sound_{sound_id}"

    @staticmethod
    def _normalise_export_path(chosen: Any, default_suffix: str) -> Path:
        path = Path(str(chosen)).expanduser()
        if not path.name or path.name in {".", ".."}:
            raise ValueError("Choose a filename for the export")
        if not path.suffix:
            path = path.with_suffix(default_suffix)
        return path

    def _export_sound_to_path(
        self,
        sound_id: int,
        output_path: Path,
        on_file_written: Callable[[Path], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> dict:
        def check_cancelled() -> None:
            if cancel_callback is not None and cancel_callback():
                raise ArchiveDumpCancelled("Archive dump cancelled")

        def report_written(path: Path) -> None:
            if on_file_written is None:
                return
            try:
                on_file_written(path)
            except Exception:
                pass

        check_cancelled()
        archive = self.project_service.require_archive(self.session)
        entry = self.archive_service._sound_entry(archive, int(sound_id))
        sound_name = self.archive_service._sound_name(archive, int(sound_id), entry)
        suffix = output_path.suffix.lower()

        if entry.sound_type == SoundType.STRM:
            if suffix != ".wav":
                raise ValueError("STRM sounds can only be exported as .wav")
            saved = self._save_export_wav(
                archive, int(sound_id), sound_name, entry, output_path,
                cancel_callback=cancel_callback,
            )
            check_cancelled()
            report_written(saved)
            return {"ok": True, "format": "WAV", "path": str(saved), "paths": [str(saved)]}

        if entry.sound_type == SoundType.WAVE:
            if suffix == ".wav":
                saved = self._save_export_wav(
                    archive, int(sound_id), sound_name, entry, output_path,
                    cancel_callback=cancel_callback,
                )
                check_cancelled()
                report_written(saved)
                return {"ok": True, "format": "WAV", "path": str(saved), "paths": [str(saved)]}
            if suffix == ".brwav":
                paths = self._export_wave_brwavs(archive, entry, output_path)
                check_cancelled()
                for path in paths:
                    report_written(path)
                return {
                    "ok": True,
                    "format": "BRWAV",
                    "path": str(paths[0]),
                    "paths": [str(path) for path in paths],
                }
            raise ValueError("WAVE sounds can only be exported as .wav or .brwav")

        if entry.sound_type == SoundType.SEQ:
            if suffix == ".wav":
                paths = self._export_sequence_wavs(
                    archive,
                    int(sound_id),
                    sound_name,
                    entry,
                    output_path,
                    on_file_written=report_written,
                    cancel_callback=cancel_callback,
                )
                return {
                    "ok": True,
                    "format": "WAV",
                    "path": str(paths[0]),
                    "paths": [str(path) for path in paths],
                    "variationCount": len(paths),
                }
            if suffix == ".brseq":
                brseq = archive.get_seq(entry.file_index)
                raw = archive._resolve_file_raw(entry.file_index)
                output_path.write_bytes(raw if raw is not None and not brseq.is_dirty else brseq.to_bytes())
                check_cancelled()
                report_written(output_path)
                return {"ok": True, "format": "BRSEQ", "path": str(output_path), "paths": [str(output_path)]}
            if suffix in {".mid", ".midi"}:
                brseq = archive.get_seq(entry.file_index)
                start_label, start_offset = archive._resolve_seq_start(
                    brseq, sound_name, entry.sound_info.seq_label_offset,
                )
                brseq.to_midi(
                    output_path,
                    start_label=start_label,
                    start_offset=start_offset,
                )
                check_cancelled()
                report_written(output_path)
                return {"ok": True, "format": "MIDI", "path": str(output_path), "paths": [str(output_path)]}
            raise ValueError("SEQ sounds can only be exported as .wav, .brseq, or .midi")

        raise ValueError("Unsupported sound type")

    def _save_export_wav(
        self,
        archive,
        sound_id: int,
        sound_name: str,
        entry,
        output_path: Path,
        options: PreviewOptions | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> Path:
        """Save the same render used for playback, including BRSAR-relative STRMs."""
        if cancel_callback is not None:
            return self._save_export_wav_cancellable(
                archive,
                sound_name,
                entry,
                output_path,
                options,
                cancel_callback,
            )
        if entry.sound_type == SoundType.STRM:
            from pysar.seq.engine import SoundArchiveEngine

            context = self._get_or_create_context(archive, sound_name)
            return SoundArchiveEngine().save_wav(context, output_path, options)
        return self.audio_service.save_preview_wav(self.session, sound_id, output_path, options)

    def _save_export_wav_cancellable(
        self,
        archive,
        sound_name: str,
        entry,
        output_path: Path,
        options: PreviewOptions | None,
        cancel_callback: Callable[[], bool],
    ) -> Path:
        """Stream a dump WAV and poll Abort between bounded render blocks."""
        import wave
        from pysar.seq.types import RenderOptions

        def check_cancelled() -> None:
            if cancel_callback():
                raise ArchiveDumpCancelled("Archive dump cancelled")

        check_cancelled()
        context = self._get_or_create_context(archive, sound_name)
        settings = options.to_render_options() if options is not None else RenderOptions()
        renderer = SequenceRenderer()
        if entry.sound_type == SoundType.SEQ:
            blocks = renderer.stream(context, settings)
        elif entry.sound_type == SoundType.WAVE:
            blocks = renderer.stream_wave_sound(context, settings)
        elif entry.sound_type == SoundType.STRM:
            # Native-rate streaming avoids the full-buffer resampling fallback
            # and lets long streams observe Abort between BRSTM blocks.
            settings.sample_rate = max(1, int(context.brstm.sample_rate))
            blocks = renderer.stream_stream_sound(context, settings)
        else:
            raise ValueError("Unsupported sound type")

        partial_path = output_path.with_name(f".{output_path.name}.partial")
        try:
            with wave.open(str(partial_path), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(settings.sample_rate)
                for block in blocks:
                    check_cancelled()
                    pcm = np.round(np.clip(block, -1.0, 1.0) * 32767.0).astype("<i2")
                    output.writeframesraw(pcm.tobytes(order="C"))
                check_cancelled()
            check_cancelled()
            partial_path.replace(output_path)
        except Exception:
            partial_path.unlink(missing_ok=True)
            raise
        return output_path

    @staticmethod
    def _brwav_export_bytes(brwav) -> bytes:
        raw = brwav.data.raw_bytes
        if raw is not None and not brwav.is_dirty:
            return bytes(raw)
        return brwav.to_bytes()

    def _export_wave_brwavs(self, archive, entry, output_path: Path) -> list[Path]:
        """Dump each BRWAV referenced by a WAVE sound without losing samples."""
        wave_info = entry.sound_info
        brwsd = archive.get_wsd(entry.file_index)
        if not 0 <= int(wave_info.wave_index) < len(brwsd):
            raise ValueError("The WAVE sound does not reference a valid BRWSD entry")

        notes = brwsd[int(wave_info.wave_index)].notes
        if not notes:
            raise ValueError("The WAVE sound has no BRWAV samples to export")
        wave_index = int(notes[0].wave_index)
        brwar = archive.get_wave_war(entry.file_index)
        if not 0 <= wave_index < len(brwar):
            raise ValueError(f"WAVE sound references missing BRWAV #{wave_index}")
        output_path.write_bytes(self._brwav_export_bytes(brwar[wave_index]))
        return [output_path]

    @staticmethod
    def _export_variation_path(output_path: Path, label: Any, position: int) -> Path:
        text = str(label or f"variation {position}").strip()
        safe_label = "".join(char if char.isalnum() or char in " -_." else "_" for char in text).strip(" .")
        return output_path.with_name(f"{output_path.stem} - {safe_label or f'variation {position}'}{output_path.suffix}")

    def _export_sequence_wavs(
        self,
        archive,
        sound_id: int,
        sound_name: str,
        entry,
        output_path: Path,
        on_file_written: Callable[[Path], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> list[Path]:
        """Render the default sequence plus every variation exposed by the UI."""
        def check_cancelled() -> None:
            if cancel_callback is not None and cancel_callback():
                raise ArchiveDumpCancelled("Archive dump cancelled")

        def report_written(path: Path) -> None:
            if on_file_written is None:
                return
            try:
                on_file_written(path)
            except Exception:
                pass

        check_cancelled()
        default_path = self._save_export_wav(
            archive,
            sound_id,
            sound_name,
            entry,
            output_path,
            cancel_callback=cancel_callback,
        )
        paths = [default_path]
        report_written(default_path)
        variations = self._sequence_variations(sound_id).get("variations", [])
        check_cancelled()
        used_paths = {output_path}

        for position, variation in enumerate(variations, start=1):
            check_cancelled()
            if not isinstance(variation, dict):
                continue
            candidate_position = position
            path = self._export_variation_path(output_path, variation.get("label"), candidate_position)
            while path in used_paths:
                candidate_position += 1
                path = self._export_variation_path(output_path, variation.get("label"), candidate_position)
            used_paths.add(path)

            random_overrides = tuple(
                (int(pair[0]), int(pair[1]))
                for pair in (variation.get("randomOverrides") or [])
                if isinstance(pair, (list, tuple)) and len(pair) >= 2
            )
            options = PreviewOptions(
                seq_program_override=self._valid_program(variation.get("program")),
                seq_note_override=self._valid_midi_note(variation.get("note")),
                seq_random_overrides=random_overrides,
            )
            saved_path = self._save_export_wav(
                archive,
                sound_id,
                sound_name,
                entry,
                path,
                options,
                cancel_callback=cancel_callback,
            )
            paths.append(saved_path)
            report_written(saved_path)
        return paths

    def get_sound_samples(self, sound_id: int) -> dict:
        """Get the list of samples/variations for a sound (for Replace Sound UI)."""
        try:
            archive = self.project_service.require_archive(self.session)
            entry = self.archive_service._sound_entry(archive, int(sound_id))
            sound_name = self.archive_service._sound_name(archive, int(sound_id), entry)

            def sample_info(brwav, **extra) -> dict:
                sample_rate = max(1, int(brwav.sample_rate))
                sample_count = int(brwav.n_samples)
                encoding = brwav.encoding.name if hasattr(brwav.encoding, "name") else str(brwav.encoding)
                return {
                    **extra,
                    "encoding": encoding,
                    "sampleRate": sample_rate,
                    "channels": int(brwav.n_channels),
                    "samples": sample_count,
                    "durationMs": int(round(sample_count * 1000 / sample_rate)),
                    "looped": bool(brwav.is_looped),
                }

            if entry.sound_type == SoundType.STRM:
                from pysar.core.format.rstm import Brstm

                file_entry = archive.data.file_entries[entry.file_index]
                declared_file_size = int(file_entry.file_size or 0)
                declared_channels = int(entry.sound_info.n_alloc_channels or 0)
                declared_track_flags = int(entry.sound_info.alloc_track_flag or 0)
                declared_track_count = declared_track_flags.bit_length()
                result = {
                    "ok": True,
                    "soundType": "STRM",
                    "soundName": sound_name,
                    "soundId": int(sound_id),
                    "fileIndex": int(entry.file_index),
                    "playerIndex": int(entry.player_index),
                    "volume": int(entry.volume),
                    "priority": int(entry.player_priority),
                    "externalPath": file_entry.external_file_path,
                    "fileSize": declared_file_size,
                    "channels": declared_channels,
                    "trackFlags": declared_track_flags,
                    "tracks": declared_track_count,
                    "codec": None,
                    "sampleRate": 0,
                    "totalSamples": 0,
                    "durationMs": 0,
                    "looped": None,
                    "loopStart": 0,
                    "loopEnd": 0,
                    "resolvedPath": None,
                    "resolvedSource": None,
                    "metadataMismatches": [],
                    "metadataMismatch": False,
                    "metadataAvailable": False,
                    "samples": [],
                }
                resolution = self._resolve_external_brstm(file_entry.external_file_path)
                expected_path = resolution["expected"]
                result["expectedPath"] = str(expected_path) if expected_path is not None else None
                result["originalGamePath"] = (
                    str(resolution["fallbackRoot"])
                    if resolution["fallbackRoot"] is not None else None
                )
                result["fallbackPaths"] = [
                    str(path) for path in resolution["fallbackCandidates"]
                ]
                result["fallbackPath"] = (
                    str(resolution["fallbackExpected"])
                    if resolution["fallbackExpected"] is not None else None
                )
                brstm_path = resolution["resolved"]
                if brstm_path is None:
                    if expected_path is None:
                        result["metadataError"] = "The external BRSTM path cannot be resolved without a saved BRSAR"
                    elif resolution["fallbackRoot"] is not None:
                        result["metadataError"] = (
                            "The external BRSTM was not found in the patch or the configured "
                            f"original-game folder. Patch path: {expected_path}"
                        )
                    else:
                        result["metadataError"] = (
                            f"The external BRSTM was not found at the patch path: {expected_path}. "
                            "Choose the original game's Sound/rsar folder to enable fallback."
                        )
                    return result
                result["resolvedPath"] = str(brstm_path)
                result["resolvedSource"] = resolution["source"]
                try:
                    result["actualFileSize"] = int(brstm_path.stat().st_size)
                except OSError:
                    pass
                try:
                    brstm = Brstm.open(brstm_path)
                    sample_rate = max(1, int(brstm.sample_rate))
                    sample_count = int(brstm.n_samples)
                    actual_file_size = int(brstm_path.stat().st_size)
                    actual_channels = max(1, int(brstm.n_channels))
                    actual_tracks = max(1, int(brstm.data.n_tracks))
                    mismatches = []
                    for field, expected_value, actual_value in (
                        ("File size", declared_file_size, actual_file_size),
                        ("Channels", declared_channels, actual_channels),
                    ):
                        if expected_value > 0 and expected_value != actual_value:
                            mismatches.append({
                                "field": field,
                                "expected": expected_value,
                                "actual": actual_value,
                            })
                    if declared_track_count != actual_tracks:
                        mismatches.append({
                            "field": "Track allocation",
                            "expected": declared_track_count,
                            "actual": actual_tracks,
                        })
                    result.update({
                        "codec": brstm.codec.name,
                        "sampleRate": sample_rate,
                        "channels": actual_channels,
                        "totalSamples": sample_count,
                        "durationMs": int(round(sample_count * 1000 / sample_rate)),
                        "looped": bool(brstm.is_looped),
                        "loopStart": int(brstm.loop_start),
                        "loopEnd": sample_count,
                        "metadataAvailable": True,
                        "actualFileSize": actual_file_size,
                        "tracks": actual_tracks,
                        "metadataMismatches": mismatches,
                        "metadataMismatch": bool(mismatches),
                    })
                except Exception as exc:
                    result["metadataError"] = str(exc)
                return result

            samples = []
            if entry.sound_type == SoundType.WAVE:
                wave_info = entry.sound_info
                brwsd = archive.get_wsd(entry.file_index)
                brwar = archive.get_wave_war(entry.file_index)
                if 0 <= wave_info.wave_index < len(brwsd):
                    wsd_entry = brwsd[wave_info.wave_index]
                    # WsdPlayer starts one channel and always requests note 0;
                    # later note-table entries are not selectable variations.
                    for note_idx, note in enumerate(wsd_entry.notes[:1]):
                        wave_index = int(note.wave_index)
                        if 0 <= wave_index < len(brwar):
                            samples.append(sample_info(
                                brwar[wave_index],
                                noteIndex=note_idx,
                                waveIndex=wave_index,
                            ))
                return {
                    "ok": True,
                    "soundType": "WAVE",
                    "soundName": sound_name,
                    "samples": samples,
                }

            elif entry.sound_type == SoundType.SEQ:
                seq_info = entry.sound_info
                brbnk = archive.get_bank(seq_info.bank_index)
                brwar = archive.get_bank_war(seq_info.bank_index)
                brseq = archive.get_seq(entry.file_index)
                start_label, start_offset = archive._resolve_seq_start(
                    brseq, sound_name, seq_info.seq_label_offset
                )
                default_program = archive._resolve_default_program(
                    sound_name, entry, brseq, start_label, start_offset
                )
                if isinstance(default_program, dict):
                    programs = []
                    for value in default_program.values():
                        if value is None:
                            continue
                        program_value = int(value)
                        if program_value not in programs:
                            programs.append(program_value)
                elif default_program is None:
                    programs = [0]
                else:
                    programs = [int(default_program)]
                if not programs:
                    programs = [0]

                from pysar.core.model.brbnk import WaveDataLocationType
                seen = set()
                for program in programs:
                    try:
                        instrument = brbnk[program]
                    except Exception:
                        continue
                    for param, key_range, vel_range in instrument.get_all_inst_params():
                        if param.wave_data_location_type != WaveDataLocationType.INDEX:
                            continue
                        wave_index = int(param.wave_index)
                        if wave_index in seen or wave_index < 0 or wave_index >= len(brwar):
                            continue
                        seen.add(wave_index)
                        key_low, key_high = key_range or (0, 127)
                        samples.append(sample_info(
                            brwar[wave_index],
                            wavNo=len(samples),
                            waveIndex=wave_index,
                            program=program,
                            keyLow=int(key_low),
                            keyHigh=int(key_high),
                            originalKey=int(param.original_key),
                        ))
                return {
                    "ok": True,
                    "soundType": "SEQ",
                    "soundName": sound_name,
                    "samples": samples,
                    "program": programs[0] if programs else 0,
                }
            return {"ok": False, "error": "Unsupported sound type"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _waveform_peaks(pcm: np.ndarray, width: int = 220) -> list[float]:
        samples = np.asarray(pcm)
        if samples.ndim == 2:
            samples = np.max(np.abs(samples.astype(np.float32)), axis=1)
        else:
            samples = np.abs(samples.astype(np.float32).reshape(-1))
        if samples.size == 0:
            return []

        width = max(1, min(int(width), int(samples.size)))
        edges = np.linspace(0, samples.size, width + 1, dtype=np.int64)
        scale = max(1.0, float(np.max(samples)))
        return [
            round(float(np.max(samples[edges[i]:edges[i + 1]])) / scale, 4)
            for i in range(width)
        ]

    def get_strm_sound_details(self, sound_id: int) -> dict:
        """Return live BRSAR and BRSTM data for a stream detail tab."""
        result = self.get_sound_samples(int(sound_id))
        if not result.get("ok"):
            return result
        if result.get("soundType") != "STRM":
            return {"ok": False, "error": "Not a STRM sound"}

        result["waveform"] = []
        resolved_path = result.get("resolvedPath")
        if not resolved_path:
            return result
        try:
            from pysar.core.format.rstm import Brstm

            brstm = Brstm.open(resolved_path)
            result["waveform"] = self._waveform_peaks(brstm.decode())
        except Exception as exc:
            result["waveformError"] = str(exc)
        return result

    @staticmethod
    def _wav_file_info(path: str) -> dict:
        from pysar.core.format.rwav import Brwav

        wav_path = Path(str(path)).expanduser()
        if not wav_path.is_file():
            raise FileNotFoundError(f"WAV file not found: {path}")
        brwav = Brwav.from_wav(str(wav_path))
        encoding = brwav.encoding.name if hasattr(brwav.encoding, "name") else str(brwav.encoding)
        sample_rate = max(1, int(brwav.sample_rate))
        sample_count = int(brwav.n_samples)
        return {
            "path": str(wav_path),
            "name": wav_path.name,
            "encoding": encoding,
            "sampleRate": sample_rate,
            "channels": int(brwav.n_channels),
            "samples": sample_count,
            "durationMs": int(round(sample_count * 1000 / sample_rate)),
            "looped": bool(brwav.is_looped),
        }

    def get_wav_file_info(self, wav_path: str) -> dict:
        try:
            return {"ok": True, **self._wav_file_info(str(wav_path))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _brstm_source_wav_info(path: str) -> dict:
        import wave

        wav_path = Path(str(path)).expanduser()
        if not wav_path.is_file():
            raise FileNotFoundError(f"WAV file not found: {path}")
        with wave.open(str(wav_path), "rb") as wav:
            if wav.getcomptype() != "NONE":
                raise ValueError(f"Unsupported compressed WAV format: {wav.getcomptype()}")
            sample_rate = max(1, int(wav.getframerate()))
            sample_count = int(wav.getnframes())
            channels = int(wav.getnchannels())
            sample_width = int(wav.getsampwidth())
        return {
            "path": str(wav_path),
            "name": wav_path.name,
            "encoding": f"PCM{sample_width * 8}",
            "sampleRate": sample_rate,
            "channels": channels,
            "samples": sample_count,
            "durationMs": int(round(sample_count * 1000 / sample_rate)),
            "looped": False,
        }

    def replace_sound_sample_from_wav_path(self, sound_id: int, wav_no: int, wav_path: str) -> dict:
        try:
            from pysar.core.format.rwav import Brwav

            wav_file = Path(str(wav_path)).expanduser()
            if not wav_file.is_file():
                return {"ok": False, "error": f"WAV file not found: {wav_path}"}
            brwav = Brwav.from_wav(str(wav_file))

            archive = self.project_service.require_archive(self.session)
            entry = archive.data.sound_entries[int(sound_id)]
            sound_name = self.archive_service._sound_name(archive, int(sound_id), entry)

            if entry.sound_type == SoundType.WAVE:
                archive.replace_wav_sound(sound_name, brwav, note_index=int(wav_no))
            elif entry.sound_type == SoundType.SEQ:
                archive.replace_seq_sound(sound_name, brwav, wav_no=int(wav_no))
            else:
                return {"ok": False, "error": "Cannot replace samples for this sound type"}

            self.project_service.mark_dirty(self.session)
            # Clear caches so subsequent previews re-parse from updated embedded data
            archive.clear_subfile_caches()
            self._clear_audio_streams()
            return {"ok": True, "dirty": True, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def replace_wave_sound_from_path(
        self,
        sound_id: int,
        path: str,
        encoding: str | None = None,
    ) -> dict:
        """Replace the one playable WAVE sound sample from WAV or raw BRWAV."""
        try:
            replacement = self._replacement_brwav_from_path(path, encoding)
            archive = self.project_service.require_archive(self.session)
            entry = self.archive_service._sound_entry(archive, int(sound_id))
            if entry.sound_type != SoundType.WAVE:
                return {"ok": False, "error": "Not a WAVE sound"}

            sound_name = self.archive_service._sound_name(archive, int(sound_id), entry)
            archive.replace_wav_sound(sound_name, replacement, note_index=0)
            self.project_service.mark_dirty(self.session)
            archive.clear_subfile_caches()
            self._clear_audio_streams()
            return {"ok": True, "dirty": True, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_wave_sound_replacement_source_dialog(self, sound_id: int) -> dict:
        """Open the native WAV/BRWAV picker for a WAVE sound replacement."""
        try:
            archive = self.project_service.require_archive(self.session)
            entry = self.archive_service._sound_entry(archive, int(sound_id))
            if entry.sound_type != SoundType.WAVE:
                return {"ok": False, "error": "Not a WAVE sound"}
            selection = self._choose_brwav_replacement_source()
            if selection is None:
                return {"ok": False, "error": "Cancelled"}
            source, source_format = selection
            return {
                "ok": True,
                "soundId": int(sound_id),
                "path": str(source),
                "sourceFormat": source_format,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def replace_sound_sample(self, sound_id: int, wav_no: int = 0) -> dict:
        if self._window is None:
            return {"ok": False, "error": "No window"}
        try:
            result = self._window.create_file_dialog(
                dialog_type=FileDialog.OPEN,
                allow_multiple=False,
                file_types=("WAV files (*.wav)", "All files (*.*)"),
            )
            if not result:
                return {"ok": False, "error": "Cancelled"}
            wav_path = result[0] if isinstance(result, (list, tuple)) else result
            return self.replace_sound_sample_from_wav_path(sound_id, wav_no, str(wav_path))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _strm_context(self, sound_id: int):
        archive = self.project_service.require_archive(self.session)
        entry = self.archive_service._sound_entry(archive, int(sound_id))
        if entry.sound_type != SoundType.STRM:
            raise ValueError("Not a STRM sound")
        if entry.file_index < 0 or entry.file_index >= len(archive.data.file_entries):
            raise ValueError(f"Invalid STRM file index {entry.file_index}")
        sound_name = self.archive_service._sound_name(archive, int(sound_id), entry)
        return archive, entry, sound_name, archive.data.file_entries[entry.file_index]

    def _expected_external_brstm_path(self, external_path: str | None) -> Path | None:
        """Return the patch-local location described by the BRSAR."""
        raw_path = str(external_path or "").strip()
        if not raw_path:
            return None

        path = Path(raw_path.replace("\\", "/")).expanduser()
        if path.is_absolute():
            return path
        if self.session.archive_path is None:
            return None
        return self.session.archive_path.parent / path

    @staticmethod
    def _fallback_relative_brstm_path(external_path: str | None) -> Path | None:
        """Return a safe relative BRSAR path suitable for a fallback root."""
        raw_path = str(external_path or "").strip().replace("\\", "/")
        if not raw_path or raw_path.startswith("/") or raw_path.startswith("//"):
            return None
        # A drive-qualified path is absolute on Windows even when this helper
        # is exercised by tests on another platform.
        if len(raw_path) >= 2 and raw_path[1] == ":":
            return None
        parts = [part for part in raw_path.split("/") if part not in {"", "."}]
        if not parts or any(part == ".." for part in parts):
            return None
        return Path(*parts)

    def _original_game_root(self) -> Path | None:
        service = getattr(self, "settings_service", None)
        if service is None:
            return None
        try:
            root = service.original_game_path()
            return root.resolve() if root is not None else None
        except (OSError, RuntimeError):
            return None

    def _original_game_brstm_candidates(self, external_path: str | None) -> list[Path]:
        """Build supported layouts without allowing the BRSAR path to escape."""
        root = self._original_game_root()
        relative = self._fallback_relative_brstm_path(external_path)
        if root is None or relative is None:
            return []

        candidates: list[Path] = []
        paths = [root / relative, root / "rsar" / relative, root / "Sound" / "rsar" / relative]
        if relative.parts and root.name.casefold() == relative.parts[0].casefold():
            paths.insert(0, root.joinpath(*relative.parts[1:]))
        for raw_candidate in paths:
            try:
                candidate = raw_candidate.resolve()
                candidate.relative_to(root)
            except (OSError, RuntimeError, ValueError):
                continue
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def _resolve_external_brstm(self, external_path: str | None) -> dict[str, Any]:
        """Resolve a stream locally first and then against the original game."""
        expected = self._expected_external_brstm_path(external_path)
        root = self._original_game_root()
        fallback_candidates = self._original_game_brstm_candidates(external_path)

        resolved = None
        source = None
        if expected is not None and expected.is_file():
            resolved = expected
            source = "patch"
        else:
            for candidate in fallback_candidates:
                if candidate.is_file():
                    resolved = candidate
                    source = "original-game"
                    break

        fallback_expected = (
            resolved if source == "original-game"
            else fallback_candidates[0] if fallback_candidates else None
        )

        return {
            "expected": expected,
            "fallbackRoot": root,
            "fallbackCandidates": fallback_candidates,
            "fallbackExpected": fallback_expected,
            "resolved": resolved,
            "source": source,
        }

    def _find_external_brstm_path(self, external_path: str | None) -> Path | None:
        return self._resolve_external_brstm(external_path)["resolved"]

    def get_original_game_path(self) -> dict:
        root = self._original_game_root()
        return {
            "ok": True,
            "path": str(root) if root is not None else None,
            "exists": bool(root is not None and root.is_dir()),
        }

    def choose_original_game_path(self) -> dict:
        if self._window is None:
            return {"ok": False, "error": "No folder dialog is available"}
        try:
            result = self._window.create_file_dialog(
                dialog_type=FileDialog.FOLDER,
                allow_multiple=False,
            )
            if not result:
                return {"ok": False, "cancelled": True, "error": "Cancelled"}
            chosen = result[0] if isinstance(result, (list, tuple)) else result
            root = Path(str(chosen)).expanduser().resolve()
            if not root.is_dir():
                return {"ok": False, "error": f"Original game folder not found: {root}"}
            # Choosing and applying are deliberately separate. If the detail
            # tab is closed while the native picker is open, its request token
            # can discard the selection without mutating global settings.
            return {"ok": True, "path": str(root)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def set_original_game_path(self, path: str) -> dict:
        try:
            root = self.settings_service.set_original_game_path(path)
            self._clear_strm_sources()
            return {"ok": True, "path": str(root)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def clear_original_game_path(self) -> dict:
        try:
            self.settings_service.set_original_game_path(None)
            self._clear_strm_sources()
            return {"ok": True, "path": None}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _choose_brstm_file_path(self) -> Path | None:
        if self._window is None:
            raise FileNotFoundError(
                "The referenced BRSTM could not be found and no file dialog is available"
            )
        result = self._window.create_file_dialog(
            dialog_type=FileDialog.OPEN,
            allow_multiple=False,
            file_types=("BRSTM files (*.brstm)", "All files (*.*)"),
        )
        if not result:
            return None
        chosen = result[0] if isinstance(result, (list, tuple)) else result
        return Path(str(chosen)).expanduser()

    def _choose_brstm_save_path(self, default_filename: str) -> Path | None:
        if self._window is None:
            raise RuntimeError("No file dialog is available")
        result = self._window.create_file_dialog(
            dialog_type=FileDialog.SAVE,
            save_filename=default_filename,
            file_types=("BRSTM files (*.brstm)", "All files (*.*)"),
        )
        if not result:
            return None
        chosen = result[0] if isinstance(result, (list, tuple)) else result
        path = Path(str(chosen)).expanduser()
        if path.is_dir():
            path = path / default_filename
        if not path.suffix:
            path = path.with_suffix(".brstm")
        return path

    def _relative_brstm_output_path(self, external_path: str) -> Path:
        raw_path = str(external_path or "").strip()
        if not raw_path:
            raise ValueError("External BRSTM path is required")

        path = Path(raw_path.replace("\\", "/")).expanduser()
        if path.is_absolute():
            return path
        if self.session.archive_path is None:
            raise ValueError("Save the BRSAR before using its relative BRSTM folder")
        return self.session.archive_path.parent / path

    def _brstm_output_path(
            self,
            external_path: str,
            save_to_relative_path: bool,
            default_filename: str,
    ) -> Path | None:
        if bool(save_to_relative_path):
            return self._relative_brstm_output_path(external_path)
        return self._choose_brstm_save_path(default_filename)

    @staticmethod
    def _create_brstm_from_wav(
            wav_path: str | Path,
            codec: str = "ADPCM",
            loop_enabled: bool = False,
            loop_start: int = 0,
            loop_end: int | None = None,
    ):
        from pysar.core.format.rstm import Brstm
        from pysar.core.types import AudioCodec

        wav_file = Path(str(wav_path)).expanduser()
        if not wav_file.is_file():
            raise FileNotFoundError(f"WAV file not found: {wav_path}")

        codec_name = str(codec or "ADPCM").strip().upper()
        try:
            audio_codec = AudioCodec[codec_name]
        except KeyError as exc:
            raise ValueError(f"Unsupported BRSTM codec: {codec}") from exc

        if bool(loop_enabled):
            start = int(loop_start)
            end = None if loop_end is None else int(loop_end)
            if start < 0:
                raise ValueError("Loop start cannot be negative")
            if end is None or end <= start:
                raise ValueError("Loop end must be greater than loop start")
        else:
            start = 0
            end = None

        return Brstm.from_wav(
            wav_file,
            codec=audio_codec,
            loop_start=start,
            loop_end=end,
        )

    def _required_external_brstm_path(self, external_path: str | None) -> tuple[Path, bool] | None:
        resolved = self._find_external_brstm_path(external_path)
        if resolved is not None:
            return resolved, False

        # Metadata patching is mandatory. If the game-relative path cannot be
        # resolved on this machine, ask the user for the corresponding file.
        chosen = self._choose_brstm_file_path()
        if chosen is None:
            return None
        if not chosen.is_file():
            raise FileNotFoundError(f"BRSTM file not found: {chosen}")
        return chosen, True

    def update_strm_path(self, sound_id: int, new_path: str) -> dict:
        try:
            from pysar.core.format.rstm import Brstm

            external_path = str(new_path).strip()
            if not external_path:
                return {"ok": False, "error": "External BRSTM path is required"}

            archive, entry, sound_name, _ = self._strm_context(int(sound_id))
            resolved = self._required_external_brstm_path(external_path)
            if resolved is None:
                return {"ok": False, "error": "Cancelled"}
            brstm_path, used_picker = resolved
            brstm = Brstm.open(brstm_path)

            archive.patch_brstm(sound_name, brstm, new_path=external_path)
            self.project_service.mark_dirty(self.session)
            self._clear_audio_streams()
            return {
                "ok": True,
                "dirty": True,
                "data": self._ui_data(),
                "metadataSource": str(brstm_path),
                "usedPicker": used_picker,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def replace_strm_file_from_wav_path(
            self,
            sound_id: int,
            wav_path: str,
            codec: str = "ADPCM",
            loop_enabled: bool = False,
            loop_start: int = 0,
            loop_end: int | None = None,
            save_to_relative_path: bool = False,
    ) -> dict:
        try:
            wav_file = Path(str(wav_path)).expanduser()
            if not wav_file.is_file():
                return {"ok": False, "error": f"WAV file not found: {wav_path}"}

            archive, entry, sound_name, file_entry = self._strm_context(int(sound_id))
            external_path = str(file_entry.external_file_path or "").strip()
            if not external_path:
                return {"ok": False, "error": "The STRM sound has no external BRSTM path"}

            default_filename = Path(external_path.replace("\\", "/")).name or f"{sound_name}.brstm"
            target_path = self._brstm_output_path(
                external_path,
                bool(save_to_relative_path),
                default_filename,
            )
            if target_path is None:
                return {"ok": False, "error": "Cancelled"}

            replacement = self._create_brstm_from_wav(
                wav_file,
                codec,
                bool(loop_enabled),
                int(loop_start),
                loop_end,
            )
            replacement_bytes = replacement.to_bytes()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(replacement_bytes)

            # Keep the existing BRSAR path and patch all metadata from the BRSTM
            # that was actually written. A Save As location is never used as a
            # playback fallback; the file must be placed at the BRSAR path.
            archive.patch_brstm(sound_name, replacement)
            self.project_service.mark_dirty(self.session)
            self._clear_audio_streams()
            return {
                "ok": True,
                "dirty": True,
                "data": self._ui_data(),
                "externalPath": file_entry.external_file_path,
                "writtenPath": str(target_path),
                "usedSaveDialog": not bool(save_to_relative_path),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def replace_strm_file_from_wav(self, sound_id: int) -> dict:
        if self._window is None:
            return {"ok": False, "error": "No window"}
        try:
            result = self._window.create_file_dialog(
                dialog_type=FileDialog.OPEN,
                allow_multiple=False,
                file_types=("WAV files (*.wav)", "All files (*.*)"),
            )
            if not result:
                return {"ok": False, "error": "Cancelled"}
            wav_path = result[0] if isinstance(result, (list, tuple)) else result
            return self.replace_strm_file_from_wav_path(int(sound_id), str(wav_path))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_brwsd_list(self) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            indices = archive._find_brwsd_file_indices()
            items = []
            for fi in indices:
                # Get some label
                fentry = archive.data.file_entries[fi]
                pos = fentry.file_positions[0] if fentry.file_positions else None
                group_name = None
                if pos and 0 <= pos.group_index < len(archive.data.group_entries):
                    g = archive.data.group_entries[pos.group_index]
                    if 0 <= g.file_name_index < len(archive.data.names):
                        group_name = archive.data.names[g.file_name_index]
                items.append({
                    "fileIndex": fi,
                    "label": f"BRWSD #{fi}" + (f" ({group_name})" if group_name else ""),
                })
            return {"ok": True, "items": items}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _sequence_template_bytes(
            tempo: int = 120,
            program: int = 0,
            note: int = 60,
            velocity: int = 100,
            duration: int = 48,
    ) -> bytes:
        """Build a small, editable one-track BRSEQ starter sequence."""
        from pysar.core.format.rseq import Brseq
        from pysar.core.format.rseq.mml import DEFAULT_TIMEBASE, note_to_name

        tempo = max(1, min(0xFFFF, int(tempo)))
        program = max(0, min(0xFFFF, int(program)))
        note = max(0, min(127, int(note)))
        velocity = max(1, min(127, int(velocity)))
        duration = max(1, min(0x0FFFFFFF, int(duration)))
        # The text compiler assigns real command offsets before serialization.
        # Hand-built Commands would all default to offset zero, making older
        # writers remap ``main`` to the last command instead of the first.
        source = "\n".join((
            "main:",
            f"    timebase {DEFAULT_TIMEBASE}",
            f"    tempo {tempo}",
            f"    prg {program}",
            f"    {note_to_name(note)} {velocity}, {duration}",
            "    fin",
            "",
        ))
        return Brseq.from_text(source).to_bytes()

    @staticmethod
    def _sequence_source_from_path(path: str) -> tuple[bytes, str]:
        """Read BRSEQ or MIDI input and return validated BRSEQ bytes."""
        from pysar.core.format.rseq import Brseq

        source = Path(str(path)).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"Sequence file not found: {path}")
        suffix = source.suffix.lower()
        if suffix == ".brseq":
            raw = source.read_bytes()
            Brseq.from_bytes(raw)
            return raw, "BRSEQ"
        if suffix in {".mid", ".midi"}:
            return Brseq.from_midi(source).to_bytes(), "MIDI"
        raise ValueError("Choose a .brseq, .mid, or .midi file")

    @staticmethod
    def _sequence_source_cache_key(path: str) -> tuple[str, int, int]:
        """Identify one exact on-disk revision without hashing a large MIDI."""
        source = Path(str(path)).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Sequence file not found: {path}")
        stat = source.stat()
        return str(source), int(stat.st_mtime_ns), int(stat.st_size)

    def _ensure_sequence_source_cache(self) -> None:
        """Lazily initialize import state for lightweight/headless API tests."""
        if not hasattr(self, "_sequence_source_cache_lock"):
            self._sequence_source_cache_lock = threading.Lock()
            self._sequence_source_cache = {}
            self._sequence_source_pending = {}
            self._sequence_source_failures = {}
            self._sequence_source_warmers = threading.BoundedSemaphore(
                self._MAX_SEQUENCE_SOURCE_WARMERS,
            )

    def _load_sequence_source(self, path: str) -> tuple[bytes, str]:
        """Convert once per file revision and coalesce concurrent import calls."""
        self._ensure_sequence_source_cache()
        key = self._sequence_source_cache_key(path)

        while True:
            with self._sequence_source_cache_lock:
                cached = self._sequence_source_cache.pop(key, None)
                if cached is not None:
                    # Reinsertion makes the small regular dict an LRU cache.
                    self._sequence_source_cache[key] = cached
                    return cached
                failure = self._sequence_source_failures.get(key)
                if failure is not None:
                    raise ValueError(failure)
                pending = self._sequence_source_pending.get(key)
                if pending is None:
                    pending = {"event": threading.Event()}
                    self._sequence_source_pending[key] = pending
                    owner = True
                else:
                    owner = False
            if owner:
                break
            pending["event"].wait()
            if "result" in pending:
                return pending["result"]
            if "error" in pending:
                raise ValueError(pending["error"])

        try:
            converted = self._sequence_source_from_path(key[0])
        except Exception as exc:
            with self._sequence_source_cache_lock:
                self._sequence_source_failures[key] = str(exc)
                while len(self._sequence_source_failures) > self._MAX_SEQUENCE_SOURCE_CACHE_ITEMS:
                    self._sequence_source_failures.pop(next(iter(self._sequence_source_failures)))
                pending["error"] = str(exc)
                self._sequence_source_pending.pop(key, None)
                pending["event"].set()
            raise

        with self._sequence_source_cache_lock:
            # A changed file gets a different key. Discard older revisions of
            # the same path so edits do not consume the whole cache.
            for old_key in tuple(self._sequence_source_cache):
                if old_key[0] == key[0] and old_key != key:
                    self._sequence_source_cache.pop(old_key, None)
            for old_key in tuple(self._sequence_source_failures):
                if old_key[0] == key[0]:
                    self._sequence_source_failures.pop(old_key, None)
            self._sequence_source_cache[key] = converted
            while (
                len(self._sequence_source_cache) > self._MAX_SEQUENCE_SOURCE_CACHE_ITEMS
                or (
                    len(self._sequence_source_cache) > 1
                    and sum(len(item[0]) for item in self._sequence_source_cache.values())
                    > self._MAX_SEQUENCE_SOURCE_CACHE_BYTES
                )
            ):
                self._sequence_source_cache.pop(next(iter(self._sequence_source_cache)))
            pending["result"] = converted
            self._sequence_source_pending.pop(key, None)
            pending["event"].set()
        return converted

    def _warm_sequence_source(self, path: str) -> None:
        """Prepare MIDI conversion off the UI bridge, with bounded workers."""
        self._ensure_sequence_source_cache()
        if not self._sequence_source_warmers.acquire(blocking=False):
            return
        try:
            self._load_sequence_source(path)
        except Exception:
            # The foreground import returns the cached conversion error.
            pass
        finally:
            self._sequence_source_warmers.release()

    @staticmethod
    def _midi_source_info(path: str) -> dict:
        """Inspect MIDI metadata without running the expensive BRSEQ compiler."""
        from pysar.core.format.rseq import Brseq, NintendoMidiProfile
        from pysar.core.format.rseq.midi import (
            MidiEventType,
            MidiFile,
            MidiMetaType,
        )
        from pysar.core.format.rseq.mml import DEFAULT_TEMPO, DEFAULT_TIMEBASE
        from pysar.core.format.rsar import Brsar

        source = Path(str(path)).expanduser()
        midi = MidiFile.from_file(source)
        profile = NintendoMidiProfile.from_midi(midi)
        if profile is not None:
            imported = NintendoMidiProfile.import_midi(midi, profile=profile)
            brseq = imported.brseq
            return {
                "path": str(source),
                "name": source.name,
                "format": "MIDI",
                "annotated": True,
                "profile": "pysar.nw4r-midi/1",
                "entryLabel": imported.entry_label,
                "entryOffset": imported.entry_offset,
                "tracks": int(brseq.n_tracks),
                "midiTracks": len(midi.tracks),
                "tempo": int(brseq.tempo),
                "timebase": int(brseq.timebase),
                "labels": [
                    {
                        "name": label.name,
                        "offset": int(label.offset),
                        "startOffset": int(Brsar._seq_effective_label_offset(brseq, label.name)),
                    }
                    for label in brseq.labels
                ],
            }

        channels: set[int] = set()
        tempo_at_zero = DEFAULT_TEMPO
        channel_events = {
            MidiEventType.NOTE_OFF,
            MidiEventType.NOTE_ON,
            MidiEventType.CONTROL_CHANGE,
            MidiEventType.PROGRAM_CHANGE,
            MidiEventType.PITCH_BEND,
        }
        for track in midi.tracks:
            absolute_tick = 0
            for event in track.events:
                absolute_tick += int(event.delta_time)
                if event.event_type in channel_events:
                    channels.add(int(event.channel))
                if (
                    absolute_tick == 0
                    and event.status == MidiEventType.META
                    and len(event.data) >= 4
                    and event.data[0] == MidiMetaType.SET_TEMPO
                ):
                    micros = (
                        (int(event.data[1]) << 16)
                        | (int(event.data[2]) << 8)
                        | int(event.data[3])
                    )
                    if micros > 0:
                        tempo_at_zero = int(round(60_000_000 / micros))
        return {
            "path": str(source),
            "name": source.name,
            "format": "MIDI",
            "annotated": False,
            "tracks": max(1, len(channels)),
            "midiTracks": len(midi.tracks),
            "tempo": int(tempo_at_zero),
            "timebase": int(DEFAULT_TIMEBASE),
            "labels": [{"name": "main", "offset": 0, "startOffset": 0}],
        }

    @staticmethod
    def _sequence_source_info(
            raw: bytes,
            *,
            path: str | None = None,
            source_format: str = "BRSEQ",
    ) -> dict:
        from pysar.core.format.rseq import Brseq
        from pysar.core.format.rsar import Brsar

        brseq = Brseq.from_bytes(raw)
        return {
            "path": path,
            "name": Path(path).name if path else None,
            "format": source_format,
            "tracks": int(brseq.n_tracks),
            "tempo": int(brseq.tempo),
            "timebase": int(brseq.timebase),
            "labels": [
                {
                    "name": label.name,
                    "offset": int(label.offset),
                    "startOffset": int(Brsar._seq_effective_label_offset(brseq, label.name)),
                }
                for label in brseq.labels
            ],
        }

    def get_sequence_sources(self) -> dict:
        """List embedded BRSEQ files available to a new SEQ sound."""
        try:
            archive = self.project_service.require_archive(self.session)
            items = []
            for file_index in archive._find_seq_file_indices():
                brseq = archive.get_seq(file_index)
                positions = archive.data.file_entries[file_index].file_positions
                group_index = int(positions[0].group_index) if positions else None
                references = [
                    sound_id for sound_id, entry in enumerate(archive.data.sound_entries)
                    if entry.sound_type == SoundType.SEQ
                    and int(entry.file_index) == int(file_index)
                ]
                items.append({
                    "fileIndex": int(file_index),
                    "label": f"BRSEQ #{file_index} · {len(references)} sound(s)",
                    "groupIndex": group_index,
                    "soundIds": references,
                    "labels": [
                        {"name": label.name, "offset": int(label.offset)}
                        for label in brseq.labels
                    ],
                })
            return {"ok": True, "items": items}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def inspect_sequence_source(self, path: str) -> dict:
        try:
            source = Path(str(path)).expanduser()
            if source.suffix.lower() in {".mid", ".midi"}:
                info = self._midi_source_info(str(source))
                threading.Thread(
                    target=self._warm_sequence_source,
                    args=(str(source),),
                    daemon=True,
                    name="pysar-midi-import",
                ).start()
                return {"ok": True, **info, "preparing": True}
            raw, source_format = self._load_sequence_source(str(source))
            return {
                "ok": True,
                **self._sequence_source_info(
                    raw,
                    path=str(source),
                    source_format=source_format,
                ),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_sequence_source(self) -> dict:
        if self._window is None:
            return {"ok": False, "error": "No window"}
        try:
            result = self._window.create_file_dialog(
                dialog_type=FileDialog.OPEN,
                allow_multiple=False,
                file_types=(
                    "Nintendo BRSEQ or MIDI (*.brseq;*.mid;*.midi)",
                    "Nintendo BRSEQ (*.brseq)",
                    "Standard MIDI (*.mid;*.midi)",
                    "All files (*.*)",
                ),
            )
            if not result:
                return {"ok": False, "error": "Cancelled"}
            path = result[0] if isinstance(result, (list, tuple)) else result
            return self.inspect_sequence_source(str(path))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def add_seq_sound(
            self,
            name: str,
            source_kind: str,
            bank_index: int,
            player_index: int = 0,
            volume: int = 90,
            group_index: int | None = None,
            source_path: str | None = None,
            existing_file_index: int | None = None,
            start_label: str | None = None,
            template_tempo: int = 120,
            template_program: int = 0,
            template_note: int = 60,
            template_velocity: int = 100,
            template_duration: int = 48,
    ) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            kind = str(source_kind or "").strip().lower()
            kwargs: dict[str, Any] = {}
            if kind == "existing":
                if existing_file_index is None:
                    raise ValueError("Choose an existing BRSEQ")
                kwargs["seq_file_index"] = int(existing_file_index)
            elif kind in {"brseq", "midi", "file"}:
                if not source_path:
                    raise ValueError("Choose a BRSEQ or MIDI file")
                raw, detected = self._load_sequence_source(str(source_path))
                if kind in {"brseq", "midi"} and detected.lower() != kind:
                    raise ValueError(f"Expected {kind.upper()} input, got {detected}")
                kwargs["brseq_raw"] = raw
                kwargs["group_index"] = None if group_index is None else int(group_index)
            elif kind == "new":
                kwargs["brseq_raw"] = self._sequence_template_bytes(
                    template_tempo,
                    template_program,
                    template_note,
                    template_velocity,
                    template_duration,
                )
                kwargs["group_index"] = None if group_index is None else int(group_index)
            else:
                raise ValueError("Unknown sequence source")

            sound_index = archive.add_seq_sound(
                str(name),
                bank_index=int(bank_index),
                player_index=int(player_index),
                volume=int(volume),
                start_label=str(start_label) if start_label else None,
                **kwargs,
            )
            self.project_service.mark_dirty(self.session)
            self._clear_audio_streams()
            return {
                "ok": True,
                "dirty": True,
                "soundId": int(sound_index),
                "fileIndex": int(archive.data.sound_entries[sound_index].file_index),
                "data": self._ui_data(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def replace_sequence_from_path(
            self,
            sound_id: int,
            source_path: str,
            start_label: str | None = None,
            start_offset: int | None = None,
    ) -> dict:
        try:
            raw, source_format = self._load_sequence_source(str(source_path))
            if (
                    start_label is None
                    and start_offset is None
                    and Path(str(source_path)).suffix.lower() in {".mid", ".midi"}
            ):
                from pysar.core.format.rseq import NintendoMidiAnnotations
                annotated = NintendoMidiAnnotations.import_file(source_path)
                start_label = annotated.entry_label
                start_offset = annotated.entry_offset
            return self._replace_sequence_bytes(
                int(sound_id),
                raw,
                start_label=start_label,
                start_offset=start_offset,
                source_format=source_format,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def replace_sequence_dialog(self, sound_id: int) -> dict:
        chosen = self.choose_sequence_source()
        if not chosen.get("ok"):
            return chosen
        return self.replace_sequence_from_path(
            int(sound_id),
            str(chosen["path"]),
            chosen.get("entryLabel"),
            chosen.get("entryOffset"),
        )

    def _replace_sequence_bytes(
            self,
            sound_id: int,
            raw: bytes,
            *,
            start_label: str | None = None,
            start_offset: int | None = None,
            source_format: str = "BRSEQ",
    ) -> dict:
        archive = self.project_service.require_archive(self.session)
        old_file_index = int(
            self.archive_service._sound_entry(archive, sound_id).file_index
        )
        references = sum(
            1 for entry in archive.data.sound_entries
            if entry.sound_type == SoundType.SEQ
            and int(entry.file_index) == old_file_index
        )
        file_index = archive.replace_seq_sound_data(
            int(sound_id),
            bytes(raw),
            start_label=start_label,
            start_offset=start_offset,
            copy_on_write=True,
        )
        self.project_service.mark_dirty(self.session)
        self._clear_audio_streams()
        return {
            "ok": True,
            "dirty": True,
            "format": source_format,
            "fileIndex": int(file_index),
            "copyOnWrite": references > 1,
            "sharedReferenceCount": references,
            "data": self._ui_data(),
        }

    def compile_sequence_text(
            self,
            sound_id: int,
            source_text: str,
            start_label: str | None = None,
    ) -> dict:
        """Compile normalized RSEQ text and apply it to one sound atomically."""
        try:
            from pysar.core.format.rseq import Brseq

            compiled = Brseq.from_text(str(source_text))
            raw = compiled.to_bytes()
            # Parse the serialized result before mutating the archive; this
            # catches writer/reference errors and canonicalizes label offsets.
            Brseq.from_bytes(raw)
            result = self._replace_sequence_bytes(
                int(sound_id),
                raw,
                start_label=start_label,
                source_format="MML",
            )
            result["sourceText"] = Brseq.from_bytes(raw).to_text()
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def update_sequence_start_label(self, sound_id: int, label_name: str) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            offset = archive.set_seq_sound_start_label(
                int(sound_id), str(label_name),
            )
            self.project_service.mark_dirty(self.session)
            self._clear_audio_streams()
            return {
                "ok": True,
                "dirty": True,
                "offset": int(offset),
                "data": self._ui_data(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_seq_sound(self, sound_id: int) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            sound_id = int(sound_id)
            if hasattr(archive, "require_safe_mutation"):
                archive.require_safe_mutation("delete", "sound", sound_id)
            archive.delete_seq_sound_entry(sound_id)
            self.project_service.mark_dirty(self.session)
            self._clear_audio_streams()
            return {"ok": True, "dirty": True, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def export_sequence_dialog(self, sound_id: int, export_format: str) -> dict:
        """Export a SEQ explicitly as BRSEQ or MIDI from its editor toolbar."""
        if self._window is None:
            return {"ok": False, "error": "No window"}
        try:
            archive = self.project_service.require_archive(self.session)
            entry = self.archive_service._sound_entry(archive, int(sound_id))
            if entry.sound_type != SoundType.SEQ:
                raise ValueError("Selected sound is not a SEQ sound")
            name = self.archive_service._sound_name(
                archive, int(sound_id), entry,
            )
            kind = str(export_format).strip().lower()
            if kind == "brseq":
                formats = (("Nintendo BRSEQ (*.brseq)", ".brseq"),)
            elif kind in {"mid", "midi"}:
                formats = (("Standard MIDI (*.midi;*.mid)", ".midi"),)
            else:
                raise ValueError("Export format must be BRSEQ or MIDI")
            suffix = formats[0][1]
            selection = self._choose_export_save_path(
                f"{self._export_filename_stem(name, int(sound_id))}{suffix}",
                formats,
            )
            if selection is None:
                return {"ok": False, "error": "Cancelled"}
            path, _ = selection
            return self._export_sound_to_path(
                int(sound_id),
                self._normalise_export_path(path, suffix),
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_wav_file(self, inspect: bool = True) -> dict:
        if self._window is None:
            return {"ok": False, "error": "No window"}
        try:
            result = self._window.create_file_dialog(
                dialog_type=FileDialog.OPEN,
                allow_multiple=False,
                file_types=("WAV files (*.wav)", "All files (*.*)"),
            )
            if not result:
                return {"ok": False, "error": "Cancelled"}
            wav_path = result[0] if isinstance(result, (list, tuple)) else result
            if not bool(inspect):
                source = Path(str(wav_path)).expanduser()
                return {"ok": True, "path": str(source), "name": source.name}
            # Reading the RIFF header is enough for chooser metadata. Encoding
            # the entire source as DSP-ADPCM here made a successful file pick
            # look stuck; the real conversion belongs to the apply operation.
            return {"ok": True, **self._brstm_source_wav_info(str(wav_path))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_brstm_wav_file(self) -> dict:
        if self._window is None:
            return {"ok": False, "error": "No window"}
        try:
            result = self._window.create_file_dialog(
                dialog_type=FileDialog.OPEN,
                allow_multiple=False,
                file_types=("WAV files (*.wav)", "All files (*.*)"),
            )
            if not result:
                return {"ok": False, "error": "Cancelled"}
            wav_path = result[0] if isinstance(result, (list, tuple)) else result
            return {"ok": True, **self._brstm_source_wav_info(str(wav_path))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def add_wave_sound_from_wav_path(self, name: str, wav_path: str, player_index: int = 0, volume: int = 90, brwsd_file_index: int | None = None) -> dict:
        try:
            wav_file = Path(str(wav_path)).expanduser()
            if not wav_file.is_file():
                return {"ok": False, "error": f"WAV file not found: {wav_path}"}

            from pysar.core.format.rwav import Brwav
            brwav = Brwav.from_wav(str(wav_file))

            archive = self.project_service.require_archive(self.session)
            archive.add_wav_sound(
                name,
                brwav,
                volume=max(0, min(127, int(volume))),
                player_index=max(0, min(len(archive.data.player_entries) - 1, int(player_index))),
                brwsd_file_index=int(brwsd_file_index) if brwsd_file_index is not None else None,
            )
            self.project_service.mark_dirty(self.session)
            self._clear_audio_streams()
            return {"ok": True, "dirty": True, "data": self._ui_data()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def add_wave_sound_from_wav(self, name: str, player_index: int = 0, volume: int = 90, brwsd_file_index: int | None = None) -> dict:
        if self._window is None:
            return {"ok": False, "error": "No window"}
        try:
            result = self._window.create_file_dialog(
                dialog_type=FileDialog.OPEN,
                allow_multiple=False,
                file_types=("WAV files (*.wav)", "All files (*.*)"),
            )
            if not result:
                return {"ok": False, "error": "Cancelled"}
            wav_path = result[0] if isinstance(result, (list, tuple)) else result
            return self.add_wave_sound_from_wav_path(name, str(wav_path), player_index, volume, brwsd_file_index)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _validate_new_strm_sound(archive, name: str, player_index: int) -> str:
        name = archive._validate_sound_name(name)
        if not 0 <= int(player_index) < len(archive.data.player_entries):
            raise ValueError(f"Invalid player index {player_index}")
        return name

    def _insert_strm_sound(
            self,
            archive,
            name: str,
            external_path: str,
            brstm,
            player_index: int,
            volume: int,
    ) -> int:
        from pysar.core.model.brsar import StreamSoundInfo

        name = self._validate_new_strm_sound(archive, name, player_index)
        channel_count = int(brstm.n_channels)
        track_count = int(brstm.data.n_tracks)
        if channel_count <= 0:
            raise ValueError("BRSTM must contain at least one channel")
        if track_count <= 0:
            raise ValueError("BRSTM must contain at least one track")
        if track_count > 16:
            raise ValueError("BRSTM uses more than the 16 allocatable stream tracks")
        declared_size = 0
        if not brstm.is_dirty:
            declared_size = max(0, int(getattr(brstm.data, "file_size", 0) or 0))
        stream_size = declared_size or len(brstm.to_bytes())

        # These fields are runtime capacities, not the number of INFO table
        # entries. A non-zero authored pool is immutable here; fail before
        # appending anything when this stream cannot fit in it.
        common = archive.data.arc_common_info
        if common.n_stream_tracks and track_count > common.n_stream_tracks:
            raise ValueError(
                f"BRSTM needs {track_count} tracks, but this archive reserves only "
                f"{common.n_stream_tracks}"
            )
        if common.n_stream_channels and channel_count > common.n_stream_channels:
            raise ValueError(
                f"BRSTM needs {channel_count} channels, but this archive reserves only "
                f"{common.n_stream_channels}"
            )
        if common.n_stream_sounds == 0:
            common.n_stream_sounds = 1
        if common.n_stream_tracks == 0:
            common.n_stream_tracks = track_count
        if common.n_stream_channels == 0:
            common.n_stream_channels = channel_count

        new_file_index = len(archive.data.file_entries)
        archive.data.file_entries.append(FileEntry(
            file_size=stream_size,
            wave_file_size=0,
            entry_num=-1,
            external_file_path=str(external_path),
            file_positions=[],
        ))
        archive.register_new("file", new_file_index)

        name_idx = archive._get_or_add_name(name)
        archive.data.sound_entries.append(SoundDataEntry(
            file_name_index=name_idx,
            file_index=new_file_index,
            player_index=int(player_index),
            volume=max(0, min(127, int(volume))),
            player_priority=64,
            actor_player_id=0,
            sound_type=SoundType.STRM,
            sound_info=StreamSoundInfo(
                start_position=0,
                n_alloc_channels=channel_count,
                alloc_track_flag=(1 << track_count) - 1,
            ),
        ))
        archive.register_new("sound", len(archive.data.sound_entries) - 1)

        if archive.data.snd_trie is not None:
            archive.data.snd_trie.insert(name, name_idx, len(archive.data.sound_entries) - 1)
            archive.data.snd_trie_raw = None
        else:
            archive._rebuild_sound_trie()

        from pysar.core.base import DirtyFlags
        archive.mark_dirty(DirtyFlags.DATA)
        return new_file_index

    def add_strm_sound(self, name: str, external_path: str, player_index: int = 0, volume: int = 90) -> dict:
        try:
            from pysar.core.format.rstm import Brstm

            archive = self.project_service.require_archive(self.session)
            self._validate_new_strm_sound(archive, name, int(player_index))
            if not str(external_path or "").strip():
                return {"ok": False, "error": "External BRSTM path is required"}

            resolved = self._required_external_brstm_path(external_path)
            if resolved is None:
                return {"ok": False, "error": "Cancelled"}
            brstm_path, used_picker = resolved
            brstm = Brstm.open(brstm_path)

            new_file_index = self._insert_strm_sound(
                archive,
                name,
                external_path,
                brstm,
                int(player_index),
                int(volume),
            )
            self.project_service.mark_dirty(self.session)
            self._clear_audio_streams()
            return {
                "ok": True,
                "dirty": True,
                "data": self._ui_data(),
                "metadataSource": str(brstm_path),
                "usedPicker": used_picker,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def add_strm_sound_from_wav_path(
            self,
            name: str,
            external_path: str,
            wav_path: str,
            player_index: int = 0,
            volume: int = 90,
            codec: str = "ADPCM",
            loop_enabled: bool = False,
            loop_start: int = 0,
            loop_end: int | None = None,
            save_to_relative_path: bool = False,
    ) -> dict:
        try:
            archive = self.project_service.require_archive(self.session)
            self._validate_new_strm_sound(archive, name, int(player_index))
            if not str(external_path or "").strip():
                return {"ok": False, "error": "External BRSTM path is required"}

            default_filename = Path(
                str(external_path or "").replace("\\", "/")
            ).name or f"{name}.brstm"
            target_path = self._brstm_output_path(
                external_path,
                bool(save_to_relative_path),
                default_filename,
            )
            if target_path is None:
                return {"ok": False, "error": "Cancelled"}

            brstm = self._create_brstm_from_wav(
                wav_path,
                codec,
                bool(loop_enabled),
                int(loop_start),
                loop_end,
            )
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(brstm.to_bytes())

            new_file_index = self._insert_strm_sound(
                archive,
                name,
                external_path,
                brstm,
                int(player_index),
                int(volume),
            )
            self.project_service.mark_dirty(self.session)
            self._clear_audio_streams()
            return {
                "ok": True,
                "dirty": True,
                "data": self._ui_data(),
                "writtenPath": str(target_path),
                "usedSaveDialog": not bool(save_to_relative_path),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def push_event(self, event_type: str, payload: Any) -> None:
        if self._window is None:
            return
        js = (
            "window.dispatchEvent(new CustomEvent('pysar-event', {detail: "
            + json.dumps({"type": event_type, "payload": payload})
            + "}))"
        )
        self._window.evaluate_js(js)

    def _ui_data(self) -> dict[str, Any]:
        if self.session.archive is None:
            return _empty_ui_data()

        archive_path = self.session.archive_path
        archive = self.session.archive
        archive.set_safe_mode(self.session.safe_mode)
        summary = self.archive_service.get_summary(self.session)
        groups = []
        for item in self.archive_service.list_groups(self.session):
            group_entry = archive.data.group_entries[item.group_index]
            entries = []
            for slot, sub in enumerate(group_entry.group_table):
                fid = sub.file_id
                embedded = archive.data.embedded_files.get(fid) if fid is not None else None
                kind = (embedded.magic or "").strip() or "BIN" if embedded else "?"
                size = len(embedded.raw_data) if embedded else 0
                label = f"{kind}_{fid}" if fid is not None else f"slot_{slot}"
                entries.append({
                    "slot": slot,
                    "fileIndex": fid,
                    "logicalFileIndex": int(sub.group_index),
                    "isNew": archive.is_new("file", int(sub.group_index)),
                    "protected": archive.is_protected("file", int(sub.group_index)),
                    "label": label,
                    "kind": kind,
                    "size": size,
                    "linkedText": None,
                })
            groups.append({
                "id": item.group_index,
                "name": item.name,
                "isNew": archive.is_new("group", item.group_index),
                "protected": archive.is_protected("group", item.group_index),
                "items": item.item_count,
                "size": item.file_size + item.audio_size,
                "fileSize": item.file_size,
                "audioSize": item.audio_size,
                "fileId": item.file_id,
                "audioFileId": item.audio_file_id,
                "entries": entries,
            })
        group_ids = {group["name"]: group["id"] for group in groups}
        banks = [
            {
                "id": item.bank_index,
                "name": item.name,
                "instruments": item.instrument_count,
                "waves": item.wave_count,
                "file": item.file_index,
                "dataFileId": item.data_file_id,
                "audioFileId": item.audio_file_id,
                "isNew": archive.is_new("bank", item.bank_index),
                "protected": archive.is_protected("bank", item.bank_index),
            }
            for item in self.archive_service.list_banks(self.session)
        ]
        players = [
            {
                "id": item.player_index,
                "name": item.name,
                "playableSounds": item.playable_sound_count,
                "heap": item.heap_size,
                "isNew": archive.is_new("player", item.player_index),
                "protected": archive.is_protected("player", item.player_index),
            }
            for item in self.archive_service.list_players(self.session)
        ]
        sounds = []
        for item in self.archive_service.list_sounds(self.session):
            entry = archive.data.sound_entries[item.sound_id] if item.sound_id < len(archive.data.sound_entries) else None
            sound_data = {
                "id": item.sound_id,
                "name": item.name,
                "type": item.sound_type,
                "bank": item.bank_index,
                "group": group_ids.get(item.group_name),
                "player": item.player_index,
                "file": item.file_index,
                "volume": item.volume,
                "priority": entry.player_priority if entry else 0,
                "pan": 0,
                "pitch": 1.0,
                "dataFileId": item.data_file_id,
                "audioFileId": item.audio_file_id,
                "isNew": archive.is_new("sound", item.sound_id),
                "protected": archive.is_protected("sound", item.sound_id),
            }
            if entry is not None and entry.sound_type == SoundType.STRM:
                file_entry = archive.data.file_entries[entry.file_index]
                sound_data.update({
                    "externalPath": file_entry.external_file_path,
                    "fileSize": int(file_entry.file_size or 0),
                    "channels": int(entry.sound_info.n_alloc_channels or 0),
                    "trackFlags": int(entry.sound_info.alloc_track_flag or 0),
                })
            sounds.append(sound_data)
        wave_archives = [
            {
                "id": item.file_id,
                "name": item.name,
                "size": item.size,
                "waves": item.wave_count,
                "linkedBanks": item.linked_banks,
                "isNew": archive.is_wave_archive_new(item.file_id),
                "protected": self.session.safe_mode and not archive.is_wave_archive_new(item.file_id),
            }
            for item in self.archive_service.list_wave_archives(self.session)
        ]

        def listed_file_is_new(item) -> bool:
            if item.file_index is not None:
                return archive.is_new("file", item.file_index)
            logical = archive.logical_file_indices_for_embedded(item.file_id)
            return bool(logical) and all(
                archive.is_new("file", file_index) for file_index in logical
            )

        files = [
            {
                "id": item.file_id,
                "kind": item.kind,
                "size": item.size,
                "label": item.label,
                "linked": [{"kind": r.kind, "id": r.id, "name": r.name} for r in item.linked],
                "external": item.is_external,
                "fileIndex": item.file_index,
                "externalPath": item.external_path,
                "isNew": listed_file_is_new(item),
                "protected": self.session.safe_mode and not listed_file_is_new(item),
            }
            for item in self.archive_service.list_files(self.session)
        ]

        path = Path(archive_path) if archive_path is not None else None
        return {
            "archive": {
                **asdict(summary),
                "name": path.name if path is not None else "Untitled archive",
                "path": str(path) if path is not None else None,
                "size": path.stat().st_size if path is not None and path.exists() else 0,
                "sounds": summary.sound_count,
                "banks": summary.bank_count,
                "groups": summary.group_count,
                "players": summary.player_count,
                "version": summary.version,
                "safeMode": bool(self.session.safe_mode),
                "provenanceStatus": archive.data.provenance.status,
                "newEntityCount": sum(
                    len(entries) for entries in archive.data.provenance.entities.values()
                ),
            },
            "sounds": sounds,
            "banks": banks,
            "groups": groups,
            "players": players,
            "waveArchives": wave_archives,
            "files": files,
        }
