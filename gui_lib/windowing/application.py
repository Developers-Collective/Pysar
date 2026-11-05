import threading
from collections.abc import Sequence
from pathlib import Path

from ..platforms import install_menu, prepare_process, resolve_platform, run_event_loop
from .menus import MenuItem, find
from .options import WindowOptions, WindowPlatform
from .window import Window


class Application:
    """
    Base class for an application (in this case Pysar). The platform stays consistent with every window
    for this application.
    """

    def __init__(self,name: str, *,
                 platform: WindowPlatform = WindowPlatform.AUTO, icon: Path | None = None, debug: bool = False,
                 version: str | None = None
    ) -> None:
        if not name.strip():
            raise ValueError("An application needs a name.")

        self.name = name.strip()
        self.platform = resolve_platform(platform)
        self.icon = icon
        self.version = version
        self._menu: tuple[MenuItem, ...] = ()
        # Whether the system shows the menu itself (macOS's menu bar)
        self._native_menu = False
        prepare_process(self.platform, self.name, icon)

        # Turns on the web inspector
        self.debug = debug
        self._windows: list[Window] = []
        self._lock = threading.Lock()

    @property
    def windows(self) -> list[Window]:
        with self._lock:
            return list(self._windows)

    def create_window(self, options: WindowOptions | None = None) -> Window:
        window = Window(self, options or WindowOptions(title=self.name))

        with self._lock:
            self._windows.append(window)

        window.closed.connect(self._forget)
        return window

    @property
    def menu(self) -> tuple[MenuItem, ...]:
        with self._lock:
            return self._menu

    def set_menu(self, items: Sequence[MenuItem]) -> None:
        with self._lock:
            self._menu = tuple(items)

        self._native_menu = install_menu(self.platform, self._menu, name=self.name, version=self.version)
        if not self._native_menu:
            for window in self.windows:
                window.content.emit("window.menu")

    def page_menu(self) -> tuple[MenuItem, ...]:
        return () if self._native_menu else self.menu

    def run_menu_item(self, item_id: str) -> None:
        item = find(self.page_menu(), item_id)
        if item is not None and item.action is not None and item.is_enabled():
            item.action()

    def run(self) -> int:
        run_event_loop(self.platform, self.name, debug=self.debug, icon=self.icon)
        return 0

    def quit(self, force: bool = False) -> bool:
        windows = self.windows
        if not force and not all(window._may_close(quitting=True) for window in windows):
            return False
        for window in windows:
            window.close(force=True)
        return True

    def _forget(self, window: Window) -> None:
        with self._lock:
            if window in self._windows:
                self._windows.remove(window)
