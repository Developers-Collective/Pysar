"""Events a window raises, for any number of handlers"""

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

Handler = Callable[..., Any]


class Event:
    """Handlers run in the order they connected. A failing handler won't block or stop any others."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._handlers: list[Handler] = []
        self._lock = threading.Lock()

    def connect(self, handler: Handler) -> Callable[[], None]:
        """Adds a handler. The returned function disconnects it. """
        with self._lock:
            self._handlers.append(handler)
        return lambda: self.disconnect(handler)

    def disconnect(self, handler: Handler) -> None:
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def emit(self, *args: Any) -> list[Any]:
        """Call every handler and return what each returned. None for one that raised an Error."""
        with self._lock:
            handlers = list(self._handlers)
        results = []
        for handler in handlers:
            try:
                results.append(handler(*args))
            except Exception:  # I know this is bad but idc
                logger.exception("a %s handler failed", self._name)
                results.append(None)
        return results
