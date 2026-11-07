import logging
import os
import re
import shutil
import socket
import stat
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..windowing.drag_out import DragOut
    from ..windowing.window import Window

logger = logging.getLogger(__name__)

DRAG_MOVED = "window.dragOutMoved"
DRAG_ENDED = "window.dragOutEnded"

MOVE_INTERVAL = 1 / 30
KEEP_SECONDS = 600.0
RENDER_WAIT = 10.0
TRASH_NAMES = frozenset({".Trash", ".Trashes"})
HOME_TRASH = (".local", "share", "Trash")
_UNSAFE_NAME = re.compile(r'[\x00-\x1f/\\:*?"<>|]+')

Point = tuple[float, float]


class DropFolderError(ValueError):
    """A drop names a place that isn't a folder the app may write into."""


def drop_folder(path: str | os.PathLike[str]) -> Path:
    text = os.fspath(path)
    if "\0" in text:
        raise DropFolderError(f"drop folder {text!r} has a NUL byte")
    folder = Path(text)
    if not folder.is_absolute() or ".." in folder.parts:
        raise DropFolderError(f"drop folder {text!r} isn't an absolute path")
    try:
        info = os.lstat(folder)
    except OSError as error:
        raise DropFolderError(f"drop folder {text!r} doesn't exist") from error
    if stat.S_ISLNK(info.st_mode):
        raise DropFolderError(f"drop folder {text!r} is a link")
    if not stat.S_ISDIR(info.st_mode):
        raise DropFolderError(f"drop folder {text!r} isn't a folder")
    real = Path(os.path.realpath(folder))
    if _in_trash(real.parts):
        raise DropFolderError(f"drop folder {text!r} is in the trash")
    if not os.access(real, os.W_OK | os.X_OK):
        raise DropFolderError(f"drop folder {text!r} isn't writable")
    return real


def _in_trash(parts: tuple[str, ...]) -> bool:
    if any(part in TRASH_NAMES or part.startswith(".Trash-") for part in parts):
        return True
    return any(parts[index:index + len(HOME_TRASH)] == HOME_TRASH for index in range(len(parts)))


def xds_folder(uri: str | bytes, hostname: str | None = None) -> Path:
    if isinstance(uri, bytes):
        try:
            uri = uri.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DropFolderError(f"drop URI {uri!r} isn't UTF-8") from error
    uri = uri.strip().rstrip("\0")
    parts = urllib.parse.urlsplit(uri)
    if parts.scheme != "file" or parts.query or parts.fragment:
        raise DropFolderError(f"drop URI {uri!r} isn't a file URI")
    host = parts.netloc.lower()
    if host not in ("", "localhost", (hostname or socket.gethostname()).lower()):
        raise DropFolderError(f"drop URI {uri!r} is on another machine")
    path = urllib.parse.unquote(parts.path)
    if not path.startswith("/") or path.endswith("/"):
        raise DropFolderError(f"drop URI {uri!r} doesn't name a file")
    return drop_folder(os.path.dirname(path))


def linux_protocol(x11: bool, desktop: str, can_render: bool) -> str | None:
    # Nautilus 43 and later don't take XDS
    gnome = "GNOME" in desktop.upper().split(":")
    if x11 and not (gnome and can_render):
        return "xds"
    return "uris" if can_render else None


def file_name(label: str) -> str:
    name = _UNSAFE_NAME.sub(" ", label).strip(" .")
    return name[:120] or "Export"


class PendingDrop:
    def __init__(self, folder: Path, answer: Callable[[bool], None],
                 on_gui: Callable[[Callable[[], None]], None]) -> None:
        self.folder = folder
        self._answer = answer
        self._on_gui = on_gui
        self._lock = threading.Lock()
        self._done = False

    @property
    def done(self) -> bool:
        return self._done

    def finish(self, ok: bool) -> bool:
        with self._lock:
            if self._done:
                return False
            self._done = True

        def answer() -> None:
            try:
                self._answer(bool(ok))
            except Exception:
                logger.exception("couldn't answer a drop")

        self._on_gui(answer)
        return True


class PendingDrops:
    def __init__(self) -> None:
        self._drops: list[PendingDrop] = []
        self._lock = threading.Lock()

    def add(self, drop: PendingDrop) -> None:
        with self._lock:
            self._drops = [each for each in self._drops if not each.done]
            self._drops.append(drop)

    def cancel_all(self) -> None:
        with self._lock:
            drops, self._drops = self._drops, []
        for drop in drops:
            drop.finish(False)


class DragOutSession:
    def __init__(self, owner: "Window", drag: "DragOut", clock: Callable[[], float] = time.monotonic) -> None:
        self.owner = owner
        self.drag = drag
        self.name = file_name(drag.label)
        self._clock = clock
        self._sent_at = float("-inf")
        self._unknown = False
        self._ended = False

    def moved(self, point: Point | None) -> None:
        if self._ended:
            return
        if point is None:
            if not self._unknown:
                self._unknown = True
                self.owner.content.emit(DRAG_MOVED, None)
            return
        now = self._clock()
        if not self._unknown and now - self._sent_at < MOVE_INTERVAL:
            return
        self._unknown = False
        self._sent_at = now
        self.owner.content.emit(DRAG_MOVED, _point(point))

    def ended(self, point: Point | None, taken: bool) -> None:
        if self._ended:
            return
        self._ended = True
        self.owner.content.emit(DRAG_ENDED, {"point": _point(point) if point is not None else None,
                                             "taken": bool(taken)})

    def folder_dropped(self, folder: Path, drop: PendingDrop) -> None:
        try:
            self.drag.folder_dropped(folder, drop.finish)
        except Exception:
            logger.exception("the app couldn't take a drop on %s", folder)
            drop.finish(False)


def _point(point: Point) -> dict[str, float]:
    return {"x": float(point[0]), "y": float(point[1])}


class RenderedFolders:
    def __init__(self, root: Path | None = None, keep: float = KEEP_SECONDS) -> None:
        self._root = root or Path(tempfile.gettempdir()) / f"gui-lib-drags-{_user()}"
        self._keep = keep
        self._lock = threading.Lock()

    def new(self) -> Path:
        with self._lock:
            self.sweep()
            self._root.mkdir(mode=0o700, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="drag-", dir=self._root))

    def sweep(self, now: float | None = None, keep: float | None = None) -> None:
        now = time.time() if now is None else now
        keep = self._keep if keep is None else keep
        try:
            folders = list(self._root.iterdir())
        except OSError:
            return
        for folder in folders:
            try:
                if folder.is_dir() and not folder.is_symlink() and now - folder.stat().st_mtime >= keep:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                logger.debug("couldn't remove %s", folder, exc_info=True)


class Rendering:
    def __init__(self, render: Callable[[Path], Sequence[Path]], folder: Path,
                 done: Callable[["Rendering"], None] | None = None) -> None:
        self.folder = folder
        self._render = render
        self._done = done
        self._finished = threading.Event()
        self._paths: list[Path] | None = None

    def start(self) -> "Rendering":
        threading.Thread(target=self._run, name="gui-drag-render", daemon=True).start()
        return self

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    def wait(self, timeout: float | None = RENDER_WAIT) -> list[Path] | None:
        self._finished.wait(timeout)
        return self._paths

    def _run(self) -> None:
        try:
            self._paths = rendered_paths(self._render(self.folder), self.folder)
        except Exception:
            logger.exception("couldn't write the dragged files")
        finally:
            self._finished.set()
            if self._done is not None:
                self._done(self)


def _user() -> str:
    return str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")


def rendered_paths(paths: Sequence[Path], folder: Path) -> list[Path] | None:
    real = Path(os.path.realpath(folder))
    found = []
    for path in paths:
        resolved = Path(os.path.realpath(path))
        if resolved.parent != real or not resolved.exists():
            logger.warning("a render returned %s, which isn't in %s", path, folder)
            return None
        found.append(resolved)
    return found or None


def call_on(on_gui: Callable[[Callable[[], None]], None], function: Callable[[], Any],
            fallback: Any = None) -> Any:
    if threading.current_thread() is threading.main_thread():
        on_gui = _now
    done = threading.Event()
    result = [fallback]

    def run() -> None:
        try:
            result[0] = function()
        except Exception:
            logger.exception("couldn't start the drag")
        finally:
            done.set()

    on_gui(run)
    done.wait()
    return result[0]


def _now(function: Callable[[], None]) -> None:
    function()
