import importlib
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from ..windowing.options import WindowOptions, WindowPlatform

if TYPE_CHECKING:
    from ..windowing.backend import WindowBackend
    from ..windowing.menus import MenuItem
    from ..windowing.window import Window

_PACKAGES = {
    WindowPlatform.WIN32: "win32",
    WindowPlatform.MACOS: "macos",
    WindowPlatform.GTK: "gtk",
}


def resolve_platform(requested: WindowPlatform) -> WindowPlatform:
    if requested is not WindowPlatform.AUTO:
        return requested

    if sys.platform == "win32":
        return WindowPlatform.WIN32

    if sys.platform == "darwin":
        return WindowPlatform.MACOS

    if sys.platform.startswith("linux"):
        return WindowPlatform.GTK

    raise NotImplementedError(f"There's no window backend for {sys.platform}.")


def _package(platform: WindowPlatform) -> ModuleType:
    return importlib.import_module(f"{__name__}.{_PACKAGES[platform]}")


def prepare_process(platform: WindowPlatform, name: str, icon: Path | None) -> None:
    prepare = getattr(_package(platform), "prepare_process", None)
    if prepare is not None:
        prepare(name, icon)


def install_menu(platform: WindowPlatform, items: Sequence["MenuItem"], *, name: str, version: str | None) -> bool:
    install = getattr(_package(platform), "install_menu", None)
    if install is None:
        return False
    install(items, name=name, version=version)
    return True


def create_backend(platform: WindowPlatform, owner: "Window", options: WindowOptions) -> "WindowBackend":
    return _package(platform).WindowBackend(owner, options)


def run_event_loop(platform: WindowPlatform, name: str, *, debug: bool, icon: Path | None) -> None:
    package = _package(platform)
    package.apply_name(name)
    package.run(debug=debug, icon=icon)
