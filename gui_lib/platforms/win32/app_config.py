import ctypes
import logging

logger = logging.getLogger(__name__)

# The longest AppUserModelID Windows accepts
MAX_ID_LENGTH = 128


def apply_name(name: str) -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_user_model_id(name))
    except (AttributeError, OSError):
        logger.warning("couldn't set the app's taskbar ID", exc_info=True)


def app_user_model_id(name: str) -> str:
    # make everything besides letters and digits a dot
    return "".join(c if c.isalnum() else "." for c in name)[:MAX_ID_LENGTH].strip(".")
