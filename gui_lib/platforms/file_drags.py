import os
import urllib.parse
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any


class DraggedFiles:
    def __init__(self) -> None:
        self._drag: Any = None
        self._paths: tuple[Path, ...] = ()
        self._names: tuple[str, ...] = ()

    def heard(self, drag: Any, paths: Iterable[Path]) -> None:
        """Adds paths the system gave for a drag
        Args:
            drag: what the system knows the drag by
        """
        if drag != self._drag:
            self._drag = drag
            self._paths = ()
        self._paths = tuple(dict.fromkeys((*self._paths, *paths)))
        self._names = tuple(path.name for path in self._paths if path.name)
        use_paths(self._paths)

    def left(self) -> None:
        self._names = ()

    def names(self) -> list[str] | None:
        names = self._names
        return list(names) if names else None


def use_paths(paths: Sequence[Path]) -> None:
    from webview.dom import _dnd_state

    _dnd_state["paths"][:] = [(urllib.parse.quote(path.name), urllib.parse.quote(str(path))) for path in paths]


def uri_paths(values: Iterable[str]) -> list[Path]:
    paths = []
    for value in values:
        value = value.strip()
        if value.startswith("file://"):
            path = urllib.parse.unquote(urllib.parse.urlsplit(value).path)
            if path:
                paths.append(Path(path))
        # Some file managers drag plain paths
        elif value.startswith("/") and os.path.exists(value):
            paths.append(Path(value))
    return paths
