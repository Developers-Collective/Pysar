# Inspired by Pysar issue #2:
#
# GTK or WebKitGTK couldn't be loaded because pywebview quietly tried Qt instead.
# So to circumvent this, we check that the system's GTK
# and WebKitGTK load, and if they don't, say so and how to install them.

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

NO_WORKAROUNDS = "GUI_LIB_NO_WORKAROUNDS"

# What to install by package manager
PACKAGES = {
    "apt": "sudo apt install gir1.2-gtk-3.0 gir1.2-webkit2-4.1 python3-gi",

    # WebKitGTK's typelib needs xlib's from gobject-introspection, which a minimal Fedora lacks
    "dnf": "sudo dnf install gtk3 webkit2gtk4.1 python3-gobject gobject-introspection",

    "pacman": "sudo pacman -S gtk3 webkit2gtk-4.1 python-gobject",

    "zypper": "sudo zypper install typelib-1_0-Gtk-3_0 typelib-1_0-WebKit2-4_1 python3-gobject",
}


def prepare_process(name: str, icon: Path | None = None) -> None:
    _graphics_workarounds()
    error = _web_engine_error()
    if error is not None:
        message = _missing_message(error)
        _show_error(name, message)
        # SystemExit prints the message to stderr too when it was started from a terminal
        raise SystemExit(message)

    from .app_config import apply_name

    apply_name(name)


def _graphics_workarounds() -> None:
    if os.environ.get(NO_WORKAROUNDS):
        return

    # WebKitGTK's DMA-BUF renderer shows blank windows with NVIDIA's own driver
    if Path("/proc/driver/nvidia/version").exists():
        os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")


def _web_engine_error() -> str | None:
    """Why GTK 3 and WebKitGTK can't be loaded, or None if they can"""
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        # pywebview takes the 4.1 API and falls back to 4.0, and so does this check
        try:
            gi.require_version("WebKit2", "4.1")
        except ValueError:
            gi.require_version("WebKit2", "4.0")

        from gi.repository import Gtk, WebKit2  # noqa: F401
    except Exception as error:
        return f"{type(error).__name__}: {error}"
    return None


def _missing_message(error: str) -> str:
    manager = next((name for name in PACKAGES if shutil.which(name)), None)
    install = PACKAGES[manager] if manager else "Install GTK 3, WebKitGTK 4.1 and PyGObject with your package manager."
    return (
        "The app needs GTK 3 and WebKitGTK from your system, and they couldn't be loaded:\n"
        f"    {error}\n\n"
        "To install them:\n"
        f"    {install}"
    )


def _show_error(title: str, message: str) -> None:
    """Show the error in a dialog, without GTK (which is what failed)"""
    for command in (["zenity", "--error", "--no-markup", f"--title={title}", f"--text={message}"],
                    ["kdialog", "--title", title, "--error", message],
                    ["xmessage", "-center", message]):
        if shutil.which(command[0]):
            try:
                subprocess.run(command, check=False, timeout=600)
            except (OSError, subprocess.TimeoutExpired):
                continue
            return
