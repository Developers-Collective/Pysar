from pathlib import Path

from ...windowing.options import WindowPlatform
from .. import pywebview_backend


class MacOsWindowBackend(pywebview_backend.PywebviewWindowBackend):
    GUI = "cocoa"
    platform = WindowPlatform.MACOS


def run(*, debug: bool, icon: Path | None) -> None:
    pywebview_backend.run(MacOsWindowBackend.GUI, debug=debug, icon=icon)
