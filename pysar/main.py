import os
import sys
import tempfile
import threading
from pathlib import Path

import webview

from pysar import __display_name__
from pysar.api import PysarApi


PACKAGE_ROOT = Path(__file__).resolve().parent
GUI_DIR = PACKAGE_ROOT / "gui"
SHELL_HTML = GUI_DIR / "shell.html"


APP_NAME = __display_name__
APP_ID = "pysar"
DEFAULT_WINDOW_WIDTH = 960
DEFAULT_WINDOW_HEIGHT = 640
MIN_WINDOW_SIZE = (DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
MACOS_ICON_CONTENT_SCALE = 0.82


def _resource_root() -> Path:
    roots: list[Path] = []
    if hasattr(sys, "_MEIPASS"):
        roots.append(Path(sys._MEIPASS))  # type: ignore[attr-defined]
    roots.append(PACKAGE_ROOT.parent)

    for root in roots:
        for dirname in ("resources", "ressources"):
            candidate = root / dirname
            if candidate.exists():
                return candidate

    return PACKAGE_ROOT.parent / "resources"


RESOURCE_DIR = _resource_root()
APP_ICON_PNG_PATH = RESOURCE_DIR / "logo" / "pysar.png"
APP_ICON_ICO_PATH = RESOURCE_DIR / "logo" / "pysar.ico"


def _macos_padded_icon_path(icon_path: Path) -> Path | None:
    """Return a transparently inset PNG for Cocoa's native app icon slot."""
    if not icon_path.exists():
        return None

    cache_path = Path(tempfile.gettempdir()) / f"{APP_ID}-icon-{int(MACOS_ICON_CONTENT_SCALE * 100)}-padded.png"
    try:
        if cache_path.exists() and cache_path.stat().st_mtime_ns >= icon_path.stat().st_mtime_ns:
            return cache_path

        from AppKit import NSBitmapImageRep, NSBitmapImageFileTypePNG, NSImage  # type: ignore

        image = NSImage.alloc().initByReferencingFile_(str(icon_path))
        if image is None:
            return icon_path
        source_bitmap = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
        size = image.size()
        width = float(source_bitmap.pixelsWide()) if source_bitmap is not None else float(size.width)
        height = float(source_bitmap.pixelsHigh()) if source_bitmap is not None else float(size.height)
        if width <= 0 or height <= 0:
            return icon_path

        padded = NSImage.alloc().initWithSize_((width, height))
        padded.lockFocus()
        try:
            draw_width = width * MACOS_ICON_CONTENT_SCALE
            draw_height = height * MACOS_ICON_CONTENT_SCALE
            image.drawInRect_((
                ((width - draw_width) / 2.0, (height - draw_height) / 2.0),
                (draw_width, draw_height),
            ))
        finally:
            padded.unlockFocus()

        bitmap = NSBitmapImageRep.imageRepWithData_(padded.TIFFRepresentation())
        png = bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}) if bitmap is not None else None
        if png is not None and png.writeToFile_atomically_(str(cache_path), True):
            return cache_path
    except Exception:
        pass
    return icon_path


def _apply_macos_app_metadata() -> None:
    try:
        from Foundation import NSBundle  # type: ignore
    except Exception:
        return
    try:
        bundle = NSBundle.mainBundle()
        if bundle is not None:
            info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
            if info is not None:
                info["CFBundleName"] = APP_NAME
                info["CFBundleDisplayName"] = APP_NAME
    except Exception:
        pass


def _apply_windows_app_metadata() -> None:
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def _apply_linux_app_metadata() -> None:
    try:
        os.environ.setdefault("RESOURCE_NAME", APP_NAME.lower())
    except Exception:
        pass
    try:
        from gi.repository import GLib  # type: ignore
        GLib.set_application_name(APP_NAME)
        GLib.set_prgname(APP_NAME.lower())
    except Exception:
        pass


def _apply_app_metadata() -> None:
    if sys.platform == "darwin":
        _apply_macos_app_metadata()
    elif sys.platform == "win32":
        _apply_windows_app_metadata()
    elif sys.platform.startswith("linux"):
        _apply_linux_app_metadata()


def _webview_icon_path() -> str | None:
    if sys.platform == "darwin":
        # Apply the padded asset through pywebview after Cocoa has initialized
        # its NSApplication. The same icon argument is used by the Windows and
        # Linux backends below.
        icon_path = _macos_padded_icon_path(APP_ICON_PNG_PATH)
        return str(icon_path) if icon_path is not None else None
    if sys.platform == "win32":
        return str(APP_ICON_ICO_PATH) if APP_ICON_ICO_PATH.exists() else None
    return str(APP_ICON_PNG_PATH) if APP_ICON_PNG_PATH.exists() else None


def _initial_window_options() -> tuple[int, int, int | None, int | None]:
    width = DEFAULT_WINDOW_WIDTH
    height = DEFAULT_WINDOW_HEIGHT
    x = None
    y = None

    try:
        screens = list(getattr(webview, "screens", []))
        if not screens:
            return width, height, x, y

        screen = screens[0]
        width = min(width, screen.width)
        height = min(height, screen.height)
        x = screen.x + max((screen.width - width) // 2, 0)
        y = screen.y + max((screen.height - height) // 2, 0)
    except Exception:
        pass

    return width, height, x, y


def main() -> int:
    _apply_app_metadata()

    api = PysarApi()
    width, height, x, y = _initial_window_options()
    window = webview.create_window(
        title=APP_NAME,
        url=SHELL_HTML.as_uri(),
        js_api=api,
        width=width,
        height=height,
        x=x,
        y=y,
        min_size=MIN_WINDOW_SIZE,
        background_color="#0e1013",
        frameless=False,
        easy_drag=False,
        hidden=True,
    )
    api.bind(window)

    def on_window_closing() -> bool | None:
        if api.dump_in_progress:
            return False
        if api.consume_window_close_authorization():
            return None
        if not api.has_dirty_documents():
            return None
        # pywebview cancels a close when a handler returns False. The prompt is
        # dispatched just after this native callback unwinds so evaluating JS
        # cannot deadlock the GUI thread.
        api.request_window_close_prompt()
        return False

    window.events.closing += on_window_closing
    window.events.loaded += lambda: threading.Timer(2.0, window.show).start()

    # Let pywebview select the native backend: Cocoa on macOS, WinForms on
    # Windows, and GTK on Linux. ``PYSAR_GUI`` remains an explicit override.
    gui = os.environ.get("PYSAR_GUI") or None
    webview.start(
        gui=gui,
        debug=bool(os.environ.get("PYSAR_DEBUG")),
        icon=_webview_icon_path(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
