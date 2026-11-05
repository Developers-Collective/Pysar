from typing import Any

from .app_config import apply_name
from .runtime import prepare_process

__all__ = ["WindowBackend", "apply_name", "prepare_process", "run"]


def __getattr__(name: str) -> Any:
    if name in ("WindowBackend", "run"):
        from . import backend

        return {"WindowBackend": backend.GtkWindowBackend, "run": backend.run}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
