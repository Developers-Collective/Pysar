import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MenuItem:
    title: str = ""
    action: Callable[[], Any] | None = None
    shortcut: str | None = None
    enabled: bool | Callable[[], bool] = True
    items: tuple["MenuItem", ...] | None = None
    role: str | None = None
    separator: bool = False

    def is_enabled(self) -> bool:
        if not callable(self.enabled):
            return self.enabled
        try:
            return bool(self.enabled())
        except Exception:
            logger.exception("couldn't tell whether the menu item %r is enabled", self.title)
            return False


SEPARATOR = MenuItem(separator=True)

EDIT_ROLES = frozenset({"undo", "redo", "cut", "copy", "paste", "selectAll"})


def page_items(items: Sequence[MenuItem], prefix: str = "") -> list[dict[str, Any]]:
    shown: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if item.separator:
            if shown and "separator" not in shown[-1]:
                shown.append({"separator": True})
            continue

        if item.role is not None and item.action is None and item.role not in EDIT_ROLES:
            continue
        entry: dict[str, Any] = {"id": f"{prefix}{index}", "title": item.title,
                                 "enabled": item.is_enabled() or (item.action is None and item.role is not None),
                                 "shortcut": shortcut_label(item.shortcut) if item.shortcut else None}
        if item.role in EDIT_ROLES:
            entry["role"] = item.role
        if item.items is not None:
            entry["items"] = page_items(item.items, f"{prefix}{index}.")
        shown.append(entry)
    if shown and "separator" in shown[-1]:
        shown.pop()
    return shown


def find(items: Sequence[MenuItem], item_id: str) -> MenuItem | None:
    found: MenuItem | None = None
    level: Sequence[MenuItem] | None = items
    for part in str(item_id).split("."):
        if level is None or not part.isdigit() or int(part) >= len(level):
            return None
        found = level[int(part)]
        level = found.items
    return found


def shortcut_label(shortcut: str) -> str:
    names = {"cmdorctrl": "Ctrl", "cmd": "Ctrl", "option": "Alt"}
    return "+".join(names.get(part.lower(), part) for part in shortcut.split("+"))
