import logging
import re

logger = logging.getLogger(__name__)


def apply_name(name: str) -> None:
    """Name the app for the desktop"""
    try:
        from gi.repository import GLib

        if GLib.get_prgname() == program_name(name) and GLib.get_application_name() == name:
            return
        GLib.set_prgname(program_name(name))
        GLib.set_application_name(name)
    except Exception:
        logger.warning("couldn't set the app's name", exc_info=True)


def program_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"
