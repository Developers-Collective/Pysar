# Let's customize pywebview, shall we :)

import functools
import json
import logging
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..windowing.content import MissingMethod
from ..windowing.options import Backdrop, ChromeMode, Theme, WindowOptions, WindowPlatform
from .file_drags import DraggedFiles

if TYPE_CHECKING:
    from ..windowing.dialogs import FileType, MessageBox
    from ..windowing.regions import TitleBarRegions
    from ..windowing.window import Window

logger = logging.getLogger(__name__)

# A page that never finishes loading still gets its window shown after this
SHOW_TIMEOUT = 5.0
# The title bar inside the shell
RUNTIME = Path(__file__).resolve().parents[1] / "web" / "chrome.js"
DROP_TARGET = "guiDropTarget"


@functools.cache
def _runtime() -> str:
    return RUNTIME.read_text(encoding="utf-8")


class _PageApi:
    def __init__(self, owner: "Window") -> None:
        self._owner = owner
        self._missing: set[str] = set()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return {"value": self._owner.content.dispatch(method, params)}
        except MissingMethod as error:
            if method not in self._missing:
                self._missing.add(method)
                logger.warning("the page called %s, which isn't exposed", method)
            return _error(error)
        except Exception as error:
            logger.exception("%s failed", method)
            return _error(error)


def _error(error: Exception) -> dict[str, Any]:
    return {"error": {"name": type(error).__name__, "message": str(error)}}


class PywebviewWindowBackend:
    # Each platform names the GUI pywebview uses, so pywebview never falls back
    # to another toolkit by itself (see Pysar issue #2 were it ended up in Qt instead of GTK)
    GUI = ""
    platform = WindowPlatform.AUTO
    supports_custom_chrome = False

    def __init__(self, owner: "Window", options: WindowOptions) -> None:
        self._owner = owner
        self._options = options
        self._window: Any = None
        # The title as it was set last
        self._title = options.title
        self._show_lock = threading.Lock()
        self._shown = False
        self._show_timer: threading.Timer | None = None
        self._closed = False
        # Scripts wait for the page. Events are in their true order.
        self._events = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gui-events")
        # pywebview keeps one answer per window, so two dialogs at once would mix theirs up
        self._dialog_lock = threading.Lock()
        self._dragged = DraggedFiles()

    @property
    def native_window(self) -> Any:
        return getattr(self._window, "native", None)

    @property
    def _pywebview(self) -> Any:
        if self._window is None:
            raise RuntimeError("The window isn't open yet.")
        return self._window

    def open(self) -> None:
        import webview

        options = self._options
        self._window = window = webview.create_window(
            self._title,
            url=options.url,
            html=options.html,
            js_api=_PageApi(self._owner),
            width=options.width,
            height=options.height,
            x=options.x,
            y=options.y,
            min_size=(options.min_width, options.min_height),
            resizable=options.resizable,
            background_color=f"#{options.background_color:06x}",
            frameless=self._frameless(),
            easy_drag=False,
            hidden=self._starts_hidden(),
        )
        events = window.events
        events.before_show += self._before_show
        events.loaded += self._loaded
        events.closing += self._closing
        events.closed += self._closed_event
        self._track_window_state(events)
        if options.show_when_loaded:
            self._show_timer = threading.Timer(SHOW_TIMEOUT, self._show_once)
            self._show_timer.daemon = True
            self._show_timer.start()

    def show(self) -> None:
        self._pywebview.show()

    def hide(self) -> None:
        self._pywebview.hide()

    def close(self) -> None:
        self._pywebview.destroy()

    def set_title(self, title: str) -> None:
        self._title = title
        window = self._window
        gui = getattr(window, "gui", None)
        if gui is not None:
            gui.set_title(title, window.uid)

    def frame(self) -> tuple[int, int, int, int]:
        window = self._pywebview
        return window.x, window.y, window.width, window.height

    def minimize(self) -> None:
        self._pywebview.minimize()

    def toggle_maximize(self) -> None:
        if self._owner.state.maximized:
            self._pywebview.restore()
        else:
            self._pywebview.maximize()

    def toggle_fullscreen(self) -> None:
        self._pywebview.toggle_fullscreen()

    def set_theme(self, theme: Theme) -> None:
        self._apply_theme()

    def set_backdrop(self, backdrop: Backdrop) -> None:
        pass

    def backdrops(self) -> tuple[Backdrop, ...]:
        return (Backdrop.NONE,)

    def set_title_bar_regions(self, regions: "TitleBarRegions | None") -> None:
        pass

    def page_ready(self) -> None:
        pass

    def chrome_details(self) -> dict[str, Any]:
        return {}

    def file_dialog(self, file_types: Sequence["FileType"], *, save: bool = False, multiple: bool = False,
                    name: str = "", directory: Path | None = None, folder: bool = False) -> list[Path] | None:
        import webview

        if threading.current_thread() is threading.main_thread():
            raise RuntimeError("A file dialog waits for the user, so it can't be opened from the GUI thread.")
        kind = webview.FileDialog.FOLDER if folder else webview.FileDialog.SAVE if save else webview.FileDialog.OPEN
        with self._dialog_lock:
            chosen = self._pywebview.create_file_dialog(
                kind,
                directory=str(directory) if directory is not None else "",
                allow_multiple=multiple,
                save_filename=name,
                # A folder dialog shows folders only
                file_types=() if folder else tuple(self._file_filter(file_type) for file_type in file_types),
            )
        if not chosen:
            return None
        # A save dialog answers with the path itself on macOS, and a tuple of one elsewhere
        return [Path(path) for path in ((chosen,) if isinstance(chosen, str) else chosen)]

    def message_box(self, box: "MessageBox") -> int:
        raise NotImplementedError(f"There's no message box on {self.platform.value}.")

    def set_edited(self, edited: bool) -> None:
        # Only macOS shows it in the window's frame
        pass

    def show_devtools(self) -> None:
        pass

    def dragged_names(self) -> list[str] | None:
        return self._dragged.names()

    def _wait_for_answer(self, done: threading.Event) -> None:
        while not done.wait(0.25):
            if self._closed:
                return

    def _file_filter(self, file_type: "FileType") -> str:
        patterns = ";".join(pattern for extension in file_type.extensions for pattern in self._patterns(extension))
        return f"{file_type.name} ({patterns or '*.*'})"

    def _patterns(self, extension: str) -> list[str]:
        return [f"*.{extension}"]

    def _track_window_state(self, events: Any) -> None:
        events.maximized += lambda: self._owner._handle_state(maximized=True, minimized=False)
        events.minimized += lambda: self._owner._handle_state(minimized=True)
        events.restored += lambda: self._owner._handle_state(maximized=False, minimized=False)

    def load_url(self, url: str) -> None:
        self._pywebview.load_url(url)

    def load_html(self, html: str) -> None:
        self._pywebview.load_html(html)

    def emit(self, event: str, payload: Any) -> None:
        if self._window is None or self._closed:
            return

        detail = json.dumps({"name": event, "payload": payload}, ensure_ascii=False)
        self._send_script(f"window.dispatchEvent(new CustomEvent('gui:event', {{detail: {detail}}}))")

    def _send_script(self, script: str) -> None:
        try:
            self._events.submit(self._run_script, script)
        except RuntimeError:
            # Closed
            pass

    def _frameless(self) -> bool:
        """Whether pywebview creates the window without the system frame."""
        return False

    def _starts_hidden(self) -> bool:
        """Whether pywebview creates the window hidden"""
        return self._options.show_when_loaded

    def _page_loaded(self) -> None:
        if self._options.show_when_loaded:
            self._show_once()

    def _present(self) -> None:
        """Show the window for the first time"""
        self._window.show()

    def _install_chrome(self) -> None:
        """Install the custom title bar before the window shows up"""

    def _apply_theme(self) -> None:
        theme = self._owner.theme
        if theme is not Theme.SYSTEM:
            self._owner._handle_state(dark=theme is Theme.DARK)

    def _before_show(self) -> None:
        self._apply_theme()
        if self._owner.chrome is ChromeMode.CUSTOM:
            self._install_chrome()

    def _loaded(self) -> None:
        if self._title != self._options.title:
            self.set_title(self._title)
        info = json.dumps(self._owner._chrome_info())
        try:
            self._window.run_js(f"window.__pysarChromeInfo = {info};\n{_runtime()}")
        except Exception:  # yeah yeah don't kill me
            logger.exception("couldn't start the page's title bar")
        self._watch_drops()
        self._page_loaded()

    def _watch_drops(self) -> None:
        from webview.dom import DOMEventHandler

        try:
            self._window.dom.document.on("drop", DOMEventHandler(self._dropped, prevent_default=True))
        except Exception:
            logger.exception("couldn't watch for dropped files")

    def _dropped(self, event: dict[str, Any]) -> None:
        files = (event.get("dataTransfer") or {}).get("files") or []
        paths = [Path(file["pywebviewFullPath"]) for file in files if isinstance(file, dict) and file.get("pywebviewFullPath")]
        target = event.get(DROP_TARGET)
        if isinstance(target, str):
            self._owner.content.emit("window.filesDropped", {"target": target, "paths": [str(path) for path in paths]})
        elif paths:
            self._owner.files_dropped.emit(self._owner, paths)

    def _show_once(self) -> None:
        with self._show_lock:
            if self._shown or self._closed:
                return
            self._shown = True

        if self._show_timer is not None:
            self._show_timer.cancel()

        self._present()

    def _closing(self) -> bool | None:
        # pywebview keeps the window open when a closing handler returns False
        return None if self._owner._handle_closing() else False

    def _closed_event(self) -> None:
        self._closed = True
        if self._show_timer is not None:
            self._show_timer.cancel()

        self._events.shutdown(wait=False, cancel_futures=True)
        self._owner._handle_closed()

    def _run_script(self, script: str) -> None:
        try:
            self._window.run_js(script)
        except Exception:
            # A closing or reloading page drops its events
            logger.debug("couldn't deliver an event to the page", exc_info=True)


def run(gui: str, *, debug: bool, icon: Path | None) -> None:
    # Imported here so that importing gui_lib doesn't load pywebview
    import mimetypes
    import webview

    # pywebview uses the mimetypes table from Python, but we might lack several types.
    # To stop the browser from bitching about the wrong type, we add them here before
    # we start pywebview

    for extension, kind in ((".js", "text/javascript"), (".mjs", "text/javascript"), (".css", "text/css"),
                            (".svg", "image/svg+xml"), (".json", "application/json"), (".wasm", "application/wasm")):
        mimetypes.add_type(kind, extension)
    webview.start(gui=gui, debug=debug, icon=str(icon) if icon is not None else None)
