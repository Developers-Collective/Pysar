from typing import TYPE_CHECKING, Any, Protocol

from .options import Theme, WindowPlatform

if TYPE_CHECKING:
    from .regions import TitleBarRegions


class ContentBackend(Protocol):
    def load_url(self, url: str) -> None: ...

    def load_html(self, html: str) -> None: ...

    def emit(self, event: str, payload: Any) -> None: ...


class WindowBackend(ContentBackend, Protocol):
    platform: WindowPlatform
    # Whether the backend can make the page's title bar the window's
    supports_custom_chrome: bool

    @property
    def native_window(self) -> Any:
        """Either a NSWindow, a WinForms Form or a Gtk.Window"""
        ...

    def open(self) -> None: ...

    def show(self) -> None: ...

    def hide(self) -> None: ...

    def close(self) -> None: ...

    def set_title(self, title: str) -> None: ...

    def minimize(self) -> None: ...

    def toggle_maximize(self) -> None: ...

    def toggle_fullscreen(self) -> None: ...

    # Make the window look like a theme says (it's in the owner's theme already), from any thread
    def set_theme(self, theme: Theme) -> None: ...

    # Where the page drew its title bar (None: it has none), from any thread
    def set_title_bar_regions(self, regions: "TitleBarRegions | None") -> None: ...

    # The page has drawn its first frame and reported its title bar
    def page_ready(self) -> None: ...
