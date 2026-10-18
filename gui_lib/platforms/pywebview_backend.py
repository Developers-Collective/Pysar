# Let's customize pywebview, shall we :)

import functools
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..windowing.content import MissingMethod
from ..windowing.options import ChromeMode, Theme, WindowOptions, WindowPlatform

if TYPE_CHECKING:
    from ..windowing.regions import TitleBarRegions
    from ..windowing.window import Window

logger = logging.getLogger(__name__)

# A page that never finishes loading still gets its window shown after this
SHOW_TIMEOUT = 5.0
# The title bar inside the shell
RUNTIME = Path(__file__).resolve().parents[1] / "web" / "chrome.js"


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
        self._show_lock = threading.Lock()
        self._shown = False
        self._show_timer: threading.Timer | None = None
        self._closed = False
        # Scripts wait for the page. Events are in their true order.
        self._events = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gui-events")

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
            options.title,
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
        events.maximized += lambda: self._owner._handle_state(maximized=True, minimized=False)
        events.minimized += lambda: self._owner._handle_state(minimized=True)
        events.restored += lambda: self._owner._handle_state(maximized=False, minimized=False)
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
        self._pywebview.set_title(title)

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

    def set_title_bar_regions(self, regions: "TitleBarRegions | None") -> None:
        pass

    def page_ready(self) -> None:
        pass

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
        info = json.dumps(self._owner._chrome_info())
        try:
            self._window.run_js(f"window.__pysarChromeInfo = {info};\n{_runtime()}")
        except Exception:  # yeah yeah don't kill me
            logger.exception("couldn't start the page's title bar")
        self._page_loaded()

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
