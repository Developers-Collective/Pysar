import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..platforms import create_backend
from .content import Content
from .dialogs import FileType, MessageBox
from .events import Event
from .menus import page_items
from .options import Backdrop, ChromeMode, Theme, WindowOptions, WindowPlatform
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
        self.backdrop = options.backdrop
        self.state = WindowState()
        self.state_changed = Event("state_changed")
        self.closing = Event("closing")
        self.closed = Event("closed")
        self.files_dropped = Event("files_dropped")
        self.text_focused = False
        self.text_selected = False
        self.edited = False
        self.quitting = False
        self._state_lock = threading.Lock()
        self._close_authorized = threading.Event()
        self._backend = create_backend(application.platform, self, options)
        self.content = Content(self._backend, self)
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
        """What the system calls the window (its taskbar, Mission Control, the Window menu),
        from any thread without waiting, also before it opens or shows"""
        self._backend.set_title(title)

    def frame(self) -> tuple[int, int, int, int]:
        """Where the window is and how big: x and y of its top left from the top left of the
        main screen, width and height, in the system's logical pixels (the page's CSS pixels
        at its zoom of 1). WindowOptions takes the same, to put another window there.
        Not from the GUI thread before the window shows, which it waits for"""
        return self._backend.frame()

    def minimize(self) -> None:
        self._backend.minimize()

    def toggle_maximize(self) -> None:
        self._backend.toggle_maximize()

    def toggle_fullscreen(self) -> None:
        self._backend.toggle_fullscreen()

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self._backend.set_theme(theme)

    def backdrops(self) -> tuple[Backdrop, ...]:
        return self._backend.backdrops()

    def set_backdrop(self, backdrop: Backdrop) -> None:
        if backdrop not in self.backdrops():
            backdrop = Backdrop.NONE
        self.backdrop = backdrop
        self._backend.set_backdrop(backdrop)

    def close(self, force: bool = False) -> None:
        if not force and not self._may_close():
            return
        self._close_authorized.set()
        self._backend.close()

    def open_dialog(self, file_types: Sequence[FileType] = (), *, multiple: bool = False,
                    directory: Path | None = None) -> list[Path] | None:
        return self._backend.file_dialog(file_types, multiple=multiple, directory=directory)

    def save_dialog(self, file_types: Sequence[FileType] = (), *, name: str = "",
                    directory: Path | None = None) -> Path | None:
        chosen = self._backend.file_dialog(file_types, save=True, name=name, directory=directory)
        return chosen[0] if chosen else None

    def folder_dialog(self, *, directory: Path | None = None) -> Path | None:
        chosen = self._backend.file_dialog((), folder=True, directory=directory)
        return chosen[0] if chosen else None

    def message_box(self, message: str, detail: str = "", buttons: Sequence[str] = ("OK",), *,
                    default: int = 0, cancel: int | None = None, warning: bool = False,
                    destructive: int | None = None) -> int:
        box = MessageBox(message, detail, tuple(buttons), default, cancel, warning, destructive)
        return self._backend.message_box(box)

    def set_edited(self, edited: bool) -> None:
        edited = bool(edited)
        if edited != self.edited:
            self.edited = edited
            self._backend.set_edited(edited)

    def show_devtools(self) -> None:
        if self.application.debug:
            self._backend.show_devtools()

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

    def _handle_closing(self, quitting: bool = False) -> bool:
        if self._close_authorized.is_set():
            self._close_authorized.clear()
            return True
        return self._may_close(quitting)

    def _handle_closed(self) -> None:
        self.closed.emit(self)

    def _may_close(self, quitting: bool = False) -> bool:
        # The handlers run right away so they see it while they decide
        self.quitting = quitting
        try:
            return all(result is not False for result in self.closing.emit(self))
        finally:
            self.quitting = False

    def _chrome_info(self) -> dict[str, Any]:
        details = self._backend.chrome_details()
        if self.options.dialog_for is not None:
            # A dialog's page draws only the close button, on the side the desktop puts it
            layout = details.get("captionButtons", {"left": [], "right": ["close"]})
            details = {**details, "captionButtons": {side: [name for name in names if name == "close"]
                                                     for side, names in layout.items()}}
        return {
            "platform": self.platform.value,
            "chrome": self.chrome.value,
            "theme": self.theme.value,
            "backdrop": (self.backdrop if self.backdrop in self.backdrops() else Backdrop.NONE).value,
            "backdrops": [backdrop.value for backdrop in self.backdrops()],
            "state": asdict(self.state),
            **details,
        }

    def _set_title_bar_regions(self, regions: Any = None) -> None:
        self._backend.set_title_bar_regions(TitleBarRegions.from_page(regions))

    def _set_text_focus(self, focused: Any = False, selected: Any = False) -> None:
        self.text_focused = bool(focused)
        self.text_selected = bool(selected) and not self.text_focused

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
        # {backdrop: "none" | "clear"}, one of window.chrome's backdrops
        expose("window.setBackdrop", lambda backdrop: self.set_backdrop(Backdrop(backdrop)))
        expose("window.menu", lambda: page_items(self.application.page_menu()))
        expose("window.menuAction", lambda id: self.application.run_menu_item(id))
        expose("window.textFocus", self._set_text_focus)
