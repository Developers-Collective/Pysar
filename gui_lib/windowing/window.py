import threading
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any

from ..platforms import create_backend
from .content import Content
from .events import Event
from .options import ChromeMode, Theme, WindowOptions, WindowPlatform
from .regions import TitleBarRegions

if TYPE_CHECKING:
    from .application import Application


@dataclass(frozen=True)
class WindowState:
    maximized: bool = False
    minimized: bool = False
    fullscreen: bool = False
    focused: bool = True
    dark: bool = False  # dark mode


class Window:
    def __init__(self, application: "Application", options: WindowOptions) -> None:
        self.application = application
        self.options = options
        self.theme = options.theme
        self.state = WindowState()
        self.state_changed = Event("state_changed")
        self.closing = Event("closing")
        self.closed = Event("closed")
        self._state_lock = threading.Lock()
        self._close_authorized = threading.Event()
        self._backend = create_backend(application.platform, self, options)
        self.content = Content(self._backend)
        self._expose_window_methods()

    @property
    def platform(self) -> WindowPlatform:
        return self._backend.platform

    @property
    def native_window(self) -> Any:
        return self._backend.native_window

    @property
    def chrome(self) -> ChromeMode:
        """The title bar the window gets"""
        if self.options.chrome is not ChromeMode.NATIVE and self._backend.supports_custom_chrome:
            return ChromeMode.CUSTOM
        return ChromeMode.NATIVE

    def open(self) -> None:
        self._backend.open()

    def show(self) -> None:
        self._backend.show()

    def hide(self) -> None:
        self._backend.hide()

    def set_title(self, title: str) -> None:
        self._backend.set_title(title)

    def minimize(self) -> None:
        self._backend.minimize()

    def toggle_maximize(self) -> None:
        self._backend.toggle_maximize()

    def toggle_fullscreen(self) -> None:
        self._backend.toggle_fullscreen()

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self._backend.set_theme(theme)

    def close(self, force: bool = False) -> None:
        if not force and not self._may_close():
            return
        self._close_authorized.set()
        self._backend.close()

    def _handle_state(self, **changes: bool) -> None:
        with self._state_lock:
            state = replace(self.state, **changes)
            if state == self.state:
                return
            self.state = state

        self.state_changed.emit(self, state)
        self.content.emit("window.state", asdict(state))

    def _handle_caption(self, hover: str | None, pressed: str | None) -> None:
        self.content.emit("window.caption", {"hover": hover, "pressed": pressed})

    def _handle_closing(self) -> bool:
        if self._close_authorized.is_set():
            self._close_authorized.clear()
            return True
        return self._may_close()

    def _handle_closed(self) -> None:
        self.closed.emit(self)

    def _may_close(self) -> bool:
        return all(result is not False for result in self.closing.emit(self))

    def _chrome_info(self) -> dict[str, Any]:
        return {
            "platform": self.platform.value,
            "chrome": self.chrome.value,
            "theme": self.theme.value,
            "state": asdict(self.state)
        }

    def _set_title_bar_regions(self, regions: Any = None) -> None:
        self._backend.set_title_bar_regions(TitleBarRegions.from_page(regions))

    def _expose_window_methods(self) -> None:
        expose = self.content.expose
        expose("window.chrome", self._chrome_info)
        expose("window.titleBarRegions", self._set_title_bar_regions)
        expose("window.ready", self._backend.page_ready)
        expose("window.state", lambda: asdict(self.state))
        expose("window.minimize", self.minimize)
        expose("window.toggleMaximize", self.toggle_maximize)
        expose("window.toggleFullscreen", self.toggle_fullscreen)
        expose("window.close", self.close)
        # {theme: "system" | "light" | "dark"}, for the page's settings
        expose("window.setTheme", lambda theme: self.set_theme(Theme(theme)))
