import threading
from pathlib import Path

from ..platforms import resolve_platform, run_event_loop
from .options import WindowOptions, WindowPlatform
from .window import Window


class Application:
    """
    Base class for an application (in this case Pysar). The platform stays consistent with every window
    for this application.
    """

    def __init__(self,name: str, *,
                 platform: WindowPlatform = WindowPlatform.AUTO, icon: Path | None = None, debug: bool = False
    ) -> None:
        if not name.strip():
            raise ValueError("An application needs a name.")

        self.name = name.strip()
        self.platform = resolve_platform(platform)
        self.icon = icon
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

    def run(self) -> int:
        run_event_loop(self.platform, self.name, debug=self.debug, icon=self.icon)
        return 0

    def quit(self) -> None:
        for window in self.windows:
            window.close(force=True)

    def _forget(self, window: Window) -> None:
        with self._lock:
            if window in self._windows:
                self._windows.remove(window)
