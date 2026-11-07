import ctypes
import logging
import threading
import weakref
from ctypes import wintypes
from typing import TYPE_CHECKING, Any

from ..drag_out import MOVE_INTERVAL, DragOutSession, Point, RenderedFolders, Rendering
from . import native

if TYPE_CHECKING:
    from ...windowing.drag_out import DragOut
    from .backend import Win32WindowBackend

logger = logging.getLogger(__name__)

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
SM_SWAPBUTTON = 23

_rendered = RenderedFolders()
_backends: "weakref.WeakSet[Win32WindowBackend]" = weakref.WeakSet()


def watch(backend: "Win32WindowBackend") -> None:
    _backends.add(backend)


def button_down() -> bool:
    user32 = ctypes.windll.user32
    key = VK_RBUTTON if user32.GetSystemMetrics(SM_SWAPBUTTON) else VK_LBUTTON
    return bool(user32.GetAsyncKeyState(key) & 0x8000)


class WinDragOut(DragOutSession):
    def __init__(self, backend: "Win32WindowBackend", drag: "DragOut") -> None:
        super().__init__(backend._owner, drag)
        self.backend = backend
        self._lock = threading.Lock()

    def start(self) -> bool:
        if self.drag.render is None or not button_down():
            return False
        rendering = Rendering(self.drag.render, _rendered.new()).start()
        threading.Thread(target=self._follow, args=(rendering,), name="gui-drag-out", daemon=True).start()
        return True

    def moved(self, point: Point | None) -> None:
        with self._lock:
            super().moved(point)

    def ended(self, point: Point | None, taken: bool) -> None:
        with self._lock:
            super().ended(point, taken)

    def _follow(self, rendering: Rendering) -> None:
        # The page hears where the pointer is until the files are there
        while rendering.wait(MOVE_INTERVAL) is None and not rendering.finished:
            if not button_down():
                # Dropped before there was anything to drag
                self.ended(self.page_point(), False)
                return
            self.moved(self.page_point())
        paths = rendering.wait(0)
        self.backend._on_gui_thread(lambda: self._drag(paths))

    def _drag(self, paths: Any) -> None:
        if not paths or not button_down() or self.backend._closed:
            self.ended(self.page_point(), False)
            return

        from System.Collections.Specialized import StringCollection
        from System.Windows.Forms import DataObject, DragDropEffects

        files = StringCollection()
        for path in paths:
            files.Add(str(path))
        data = DataObject()
        data.SetFileDropList(files)
        form = self.backend.native_window
        form.GiveFeedback += self._feedback
        refused = refuse_drops()
        effect = 0
        try:
            # Runs the drag's own message loop until the drop
            effect = int(form.DoDragDrop(data, DragDropEffects.Copy))
        except Exception:
            logger.exception("couldn't drag the files")
        finally:
            form.GiveFeedback -= self._feedback
            restore_drops(refused)
        self.ended(self.page_point(), effect != 0)

    def _feedback(self, sender: Any, args: Any) -> None:
        self.moved(self.page_point())

    def page_point(self) -> Point | None:
        hwnd = self.backend._hwnd
        if not hwnd:
            return None
        user32 = ctypes.windll.user32
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)) or not user32.ScreenToClient(hwnd, ctypes.byref(point)):
            return None
        scale = (native.GetDpiForWindow(hwnd) or 96) / 96
        return point.x / scale, (point.y - self.backend.page_top) / scale


def refuse_drops() -> list[tuple[Any, bool]]:
    refused = []
    for backend in list(_backends):
        try:
            webview = backend.native_window.webview
            refused.append((webview, bool(webview.AllowExternalDrop)))
            webview.AllowExternalDrop = False
        except Exception:
            logger.debug("couldn't keep a window from taking the drag", exc_info=True)
    return refused


def restore_drops(refused: list[tuple[Any, bool]]) -> None:
    for webview, allowed in refused:
        try:
            webview.AllowExternalDrop = allowed
        except Exception:
            logger.debug("couldn't let a window take drops again", exc_info=True)
