import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backend import ContentBackend

Method = Callable[..., Any]


class MissingMethod(LookupError):
    """Raised when a page calls a method not exposed to it"""


class Content:
    def __init__(self, backend: "ContentBackend") -> None:
        self._backend = backend
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
        return function(**(params or {}))
