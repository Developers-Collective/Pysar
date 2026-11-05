import threading
from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backend import ContentBackend
    from .window import Window

Method = Callable[..., Any]

_caller: ContextVar["Window | None"] = ContextVar("gui_lib_caller", default=None)


class MissingMethod(LookupError):
    """Raised when a page calls a method not exposed to it"""


def calling_window() -> "Window | None":
    return _caller.get()


class Content:
    def __init__(self, backend: "ContentBackend", window: "Window | None" = None) -> None:
        self._backend = backend
        self._window = window
        self._methods: dict[str, Method] = {}
        self._lock = threading.Lock()

    def load_url(self, url: str) -> None:
        self._backend.load_url(url)

    def load_html(self, html: str) -> None:
        self._backend.load_html(html)

    def expose(self, name: str, method: Method | None = None) -> Any:
        """Let the page call a method by name.

        Methods take the page's named parameters and run outside the GUI
        thread, so they may block.
        """
        def register(function: Method) -> Method:
            with self._lock:
                self._methods[name] = function
            return function

        return register(method) if method is not None else register

    def emit(self, event: str, payload: Any = None) -> None:
        """Send an event to the page, from any thread, without waiting for it"""
        self._backend.emit(event, payload)

    def dispatch(self, method: str, params: dict[str, Any] | None) -> Any:
        """Run a method the page called"""
        with self._lock:
            function = self._methods.get(method)
        if function is None:
            raise MissingMethod(f"No method {method!r} is exposed to the page.")
        token = _caller.set(self._window)
        try:
            return function(**(params or {}))
        finally:
            _caller.reset(token)
