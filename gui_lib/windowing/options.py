from dataclasses import dataclass
from enum import Enum

# Painted until the page draws, so a window never flashes white
DEFAULT_BACKGROUND_COLOR = 0x121214


class WindowPlatform(Enum):
    AUTO = "auto"
    WIN32 = "win32"
    MACOS = "macos"
    GTK = "gtk"


class ChromeMode(Enum):
    AUTO = "auto"
    CUSTOM = "custom"
    NATIVE = "native"


class Theme(Enum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


class Backdrop(Enum):
    NONE = "none"
    CLEAR = "clear"


@dataclass(frozen=True)
class WindowOptions:
    title: str = ""
    width: int = 1100
    height: int = 720
    min_width: int = 640
    min_height: int = 400
    # None centers the window on its screen
    x: int | None = None
    y: int | None = None
    resizable: bool = True
    # The page to show
    url: str | None = None
    html: str | None = None
    background_color: int = DEFAULT_BACKGROUND_COLOR
    chrome: ChromeMode = ChromeMode.AUTO
    # Can be changed later with Window.set_theme()
    theme: Theme = Theme.SYSTEM
    # Can be changed later with Window.set_backdrop(). Window.backdrops() says which the
    # system has; the window stays opaque with any other
    backdrop: Backdrop = Backdrop.NONE
    # Stay hidden until the page has loaded, instead of showing an empty frame
    show_when_loaded: bool = True
