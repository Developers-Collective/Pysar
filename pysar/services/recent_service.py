import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RecentArchiveService:
    def __init__(self, config_dir: str | Path | None = None, limit: int = 8) -> None:
        self.limit = limit
        self.config_dir = Path(config_dir) if config_dir is not None else self._default_config_dir()
        self.path = self.config_dir / "recent_archives.json"

    def list_archives(self) -> list[dict[str, Any]]:
        return self._read()

    def remember(self, archive_path: str | Path) -> list[dict[str, Any]]:
        path = Path(archive_path).expanduser().resolve()
        identity = self._identity(path)
        entry = {
            "name": path.name,
            "path": str(path),
            "lastOpened": datetime.now(timezone.utc).isoformat(),
        }
        recent = [
            entry,
            *(item for item in self._read() if self._identity(Path(item["path"])) != identity),
        ]
        self._write(recent[: self.limit])
        return self._read()

    def forget(self, archive_path: str | Path) -> list[dict[str, Any]]:
        identity = self._identity(Path(archive_path).expanduser().resolve())
        recent = [
            item for item in self._read() if self._identity(Path(item["path"])) != identity
        ]
        self._write(recent)
        return self._read()

    @staticmethod
    def _identity(path: Path) -> str:
        """Return a stable identity for a path.

        Comparing path strings lets the same archive appear twice whenever it is
        recorded under a different spelling - a symlink, a '~' form, or a case
        variant on a case-insensitive filesystem.  Ask the filesystem instead,
        and only fall back to a normalised string when the file is gone.
        """
        try:
            info = path.expanduser().resolve().stat()
            return f"node:{info.st_dev}:{info.st_ino}"
        except OSError:
            return "path:" + os.path.normcase(str(path.expanduser()))

    def _read(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        if not isinstance(raw, list):
            return []

        recent = []
        seen = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if not isinstance(path, str) or not path:
                continue
            archive_path = Path(path).expanduser()
            identity = self._identity(archive_path)
            if identity in seen:
                continue
            seen.add(identity)
            recent.append(
                {
                    "name": str(item.get("name") or archive_path.name),
                    "path": path,
                    "lastOpened": str(item.get("lastOpened") or ""),
                    "exists": archive_path.exists(),
                }
            )
        return recent[: self.limit]

    def _write(self, recent: list[dict[str, Any]]) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(recent, indent=2), encoding="utf-8")

    @staticmethod
    def _default_config_dir() -> Path:
        override = os.environ.get("PYSAR_CONFIG_DIR")
        if override:
            return Path(override).expanduser()
        if sys.platform == "win32":
            base = os.environ.get("APPDATA")
            return Path(base).expanduser() / "PYSAR" if base else Path.home() / "AppData" / "Roaming" / "PYSAR"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "PYSAR"
        base = os.environ.get("XDG_CONFIG_HOME")
        return (Path(base).expanduser() if base else Path.home() / ".config") / "pysar"


class SettingsService:
    """Small, durable store for application-wide paths and preferences."""

    def __init__(self, config_dir: str | Path | None = None) -> None:
        self.config_dir = (
            Path(config_dir)
            if config_dir is not None
            else RecentArchiveService._default_config_dir()
        )
        self.path = self.config_dir / "settings.json"
        self._lock = threading.Lock()

    def original_game_path(self) -> Path | None:
        value = self._read().get("originalGamePath")
        if not isinstance(value, str) or not value.strip():
            return None
        return Path(value).expanduser()

    def set_original_game_path(self, path: str | Path | None) -> Path | None:
        with self._lock:
            settings = self._read_unlocked()
            if path is None:
                settings.pop("originalGamePath", None)
                self._write_unlocked(settings)
                return None

            folder = Path(path).expanduser().resolve()
            if not folder.is_dir():
                raise NotADirectoryError(f"Original game folder not found: {folder}")
            settings["originalGamePath"] = str(folder)
            self._write_unlocked(settings)
            return folder

    def _read(self) -> dict[str, Any]:
        with self._lock:
            return self._read_unlocked()

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_unlocked(self, settings: dict[str, Any]) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        temporary.replace(self.path)
