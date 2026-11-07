import ctypes
import ctypes.util
import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

try:
    gi.require_version("GdkX11", "3.0")
    from gi.repository import GdkX11
except (ImportError, ValueError):
    GdkX11 = None

from ..drag_out import (
    DragOutSession,
    DropFolderError,
    PendingDrop,
    Point,
    RenderedFolders,
    Rendering,
    linux_protocol,
    xds_folder,
)

if TYPE_CHECKING:
    from ...windowing.drag_out import DragOut
    from .backend import GtkWindowBackend

logger = logging.getLogger(__name__)

XDS = "XdndDirectSave0"
XDS_TYPE = "text/plain"
URI_LIST = "text/uri-list"
INFO_XDS = 1
INFO_URIS = 2
# The XDS answers
SAVED = b"S"
FAILED = b"E"
# How often the pointer is read on X11. Wayland doesn't say where it is outside the app's windows
POLL_MS = 33
# XDS URIs are a path, never this long
MAX_URI = 16384

_active: "GtkDragOut | None" = None
_rendered = RenderedFolders()


def on_gui(function: Callable[[], None]) -> None:
    def run() -> bool:
        function()
        return False

    GLib.idle_add(run)


def is_x11() -> bool:
    return GdkX11 is not None and isinstance(Gdk.Display.get_default(), GdkX11.X11Display)


class GtkDragOut(DragOutSession):
    def __init__(self, backend: "GtkWindowBackend", drag: "DragOut", protocol: str,
                 properties: "X11Properties | None" = None) -> None:
        super().__init__(backend._owner, drag)
        self.backend = backend
        self.protocol = protocol
        self.widget = backend._gtk_window
        self._properties = properties
        self._xid: int | None = None
        self._handlers: list[int] = []
        self._poll = 0
        self._polling = False
        self._icon: Any = None
        self._saved = False
        self._delivered = False
        # Dropped on one of the app's windows, at this point on the source's page if known
        self._inside = False
        self._inside_at: Point | None = None
        self._where: Point | None = None
        self._rendering: Rendering | None = None
        if protocol == "uris" and drag.render is not None:
            self._rendering = Rendering(drag.render, _rendered.new()).start()

    def begin(self, event: Any) -> bool:
        targets = Gtk.TargetList.new([])
        if self.protocol == "xds":
            targets.add(Gdk.Atom.intern(XDS, False), 0, INFO_XDS)
        else:
            targets.add(Gdk.Atom.intern(URI_LIST, False), 0, INFO_URIS)
        for signal, handler in (("drag-begin", self._began), ("drag-data-get", self._data_get),
                                ("drag-failed", self._failed), ("drag-end", self._end)):
            self._handlers.append(self.widget.connect(signal, handler))
        context = self.widget.drag_begin_with_coordinates(targets, Gdk.DragAction.COPY, 1, event, -1, -1)
        if context is None:
            self._disconnect()
            return False
        if is_x11():
            self._polling = True
            self._poll = GLib.timeout_add(POLL_MS, self._read_pointer)
        return True

    #
    # The drag's own signals, on the window it started from
    #

    def _began(self, widget: Any, context: Any) -> None:
        self._icon = label_icon(self.drag.label)
        Gtk.drag_set_icon_widget(context, self._icon, -8, -8)
        if self.protocol == "xds" and self._properties is not None:
            try:
                self._xid = context.get_source_window().get_xid()
                self._properties.set(self._xid, XDS, XDS_TYPE, self.name.encode())
            except Exception:
                logger.exception("couldn't offer the drop's name")

    def _data_get(self, widget: Any, context: Any, data: Any, info: int, time: int) -> None:
        if info == INFO_XDS:
            data.set(data.get_target(), 8, self.xds_answer())
        elif info == INFO_URIS:
            paths = self._rendering.wait() if self._rendering is not None else None
            if self._rendering is not None and not self._rendering.finished:
                logger.warning("the dragged files took too long to write")
            if paths:
                self._delivered = bool(data.set_uris([path.as_uri() for path in paths]))

    def xds_answer(self) -> bytes:
        uri = self._properties.get(self._xid, XDS) if self._properties is not None and self._xid else None
        try:
            if not uri:
                raise DropFolderError("the file manager set no XDS URI")
            folder = xds_folder(uri)
        except DropFolderError as error:
            logger.warning("refused a drop: %s", error)
            return FAILED
        self._saved = True
        # XDS has heard back already
        drop = PendingDrop(folder, lambda ok: None, on_gui)
        self.backend._drops.add(drop)
        self.backend._come_forward()
        self.folder_dropped(folder, drop)
        return SAVED

    def _failed(self, widget: Any, context: Any, result: Any) -> bool:
        # The page flies its own ghost back instead of GTK's
        return True

    def _end(self, widget: Any, context: Any) -> None:
        global _active
        if _active is self:
            _active = None
        self._disconnect()
        if self._xid and self._properties is not None:
            try:
                self._properties.delete(self._xid, XDS)
            except Exception:
                logger.debug("couldn't remove the XDS property", exc_info=True)
        if self._icon is not None:
            self._icon.destroy()
            self._icon = None
        if self._inside:
            self.ended(self._inside_at, False)
        else:
            self.ended(self._where, self._saved or self._delivered)

    def _disconnect(self) -> None:
        for handler in self._handlers:
            self.widget.disconnect(handler)
        self._handlers.clear()
        if self._poll:
            GLib.source_remove(self._poll)
            self._poll = 0

    #
    # Where it is
    #

    def _read_pointer(self) -> bool:
        display = Gdk.Display.get_default()
        seat = display.get_default_seat() if display is not None else None
        if seat is None:
            return True
        _, x, y = seat.get_pointer().get_position()
        origin = page_origin(self.backend)
        if origin is not None:
            self._report((x - origin[0], y - origin[1]))
        return True

    def over(self, backend: "GtkWindowBackend", x: float, y: float) -> None:
        """Over one of the app's windows, at x, y on its page"""
        if self._polling:
            return
        self._report(self._in_source(backend, x, y))

    def left(self) -> None:
        if not self._polling:
            self._where = None
            self.moved(None)

    def dropped_on(self, backend: "GtkWindowBackend", x: float, y: float) -> None:
        self._inside = True
        self._inside_at = self._in_source(backend, x, y)

    def _in_source(self, backend: "GtkWindowBackend", x: float, y: float) -> Point | None:
        if backend is self.backend:
            return x, y
        there, here = page_origin(backend), page_origin(self.backend)
        if there is None or here is None:
            return None
        return there[0] + x - here[0], there[1] + y - here[1]

    def _report(self, point: Point | None) -> None:
        self._where = point
        self.moved(point)


def page_origin(backend: "GtkWindowBackend") -> Point | None:
    webview = backend._webview
    window = webview.get_window() if webview is not None else None
    if window is None:
        return None
    *_, x, y = window.get_origin()
    return x, y


def label_icon(label: str) -> Any:
    icon = Gtk.Window(type=Gtk.WindowType.POPUP)
    icon.get_style_context().add_class("osd")
    text = Gtk.Label(label=label)
    text.set_margin_start(10)
    text.set_margin_end(10)
    text.set_margin_top(5)
    text.set_margin_bottom(5)
    icon.add(text)
    icon.show_all()
    return icon


def begin(backend: "GtkWindowBackend", drag: "DragOut", desktop: str | None = None) -> bool:
    global _active
    event, webview = backend._press_event, backend._webview
    if _active is not None or event is None or webview is None or not button_down(webview):
        return False
    x11 = is_x11()
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "") if desktop is None else desktop
    protocol = linux_protocol(x11, desktop, drag.render is not None)
    if protocol is None:
        return False
    properties = None
    if protocol == "xds":
        properties = X11Properties.shared()
        if properties is None:
            return False
    session = GtkDragOut(backend, drag, protocol, properties)
    if not session.begin(event):
        return False
    _active = session
    return True


def button_down(webview: Any) -> bool:
    window = webview.get_window()
    display = Gdk.Display.get_default()
    seat = display.get_default_seat() if display is not None else None
    if window is None or seat is None:
        return False
    *_, mask = window.get_device_position(seat.get_pointer())
    return bool(mask & Gdk.ModifierType.BUTTON1_MASK)


def own(context: Any) -> "GtkDragOut | None":
    session = _active
    if session is None or Gtk.drag_get_source_widget(context) != session.widget:
        return None
    return session


#
# The app's windows, as drop targets. Connected before WebKit's handlers, which they keep out
#

def motion(backend: "GtkWindowBackend", context: Any, x: int, y: int, time: int) -> bool:
    session = own(context)
    if session is None:
        return False
    Gdk.drag_status(context, 0, time)
    session.over(backend, x, y)
    return True


def drop(backend: "GtkWindowBackend", context: Any, x: int, y: int, time: int) -> bool:
    session = own(context)
    if session is None:
        return False
    session.dropped_on(backend, x, y)
    Gtk.drag_finish(context, False, False, time)
    return True


def leave(context: Any) -> None:
    session = own(context)
    if session is not None:
        session.left()


class X11Properties:
    _shared: "X11Properties | None" = None
    _tried = False

    def __init__(self, library: Any, display: int) -> None:
        self._x = library
        self._display = display
        self._atoms: dict[str, int] = {}

    @classmethod
    def shared(cls) -> "X11Properties | None":
        if not cls._tried:
            cls._tried = True
            try:
                cls._shared = cls._open()
            except OSError:
                logger.warning("couldn't open Xlib, so drags out can't name their folder", exc_info=True)
        return cls._shared

    @classmethod
    def _open(cls) -> "X11Properties | None":
        x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XInternAtom.restype = ctypes.c_ulong
        x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        x.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long,
                                         ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                                         ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
                                         ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p)]
        x.XDeleteProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        x.XFree.argtypes = [ctypes.c_void_p]
        x.XFlush.argtypes = [ctypes.c_void_p]
        display = x.XOpenDisplay(None)
        return cls(x, display) if display else None

    def _atom(self, name: str) -> int:
        if name not in self._atoms:
            self._atoms[name] = self._x.XInternAtom(self._display, name.encode(), False)
        return self._atoms[name]

    def set(self, xid: int, name: str, kind: str, value: bytes) -> None:
        # 8-bit items, PropModeReplace
        self._x.XChangeProperty(self._display, xid, self._atom(name), self._atom(kind), 8, 0, value, len(value))
        self._x.XFlush(self._display)

    def get(self, xid: int, name: str) -> bytes | None:
        kind, size, count, rest = ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong()
        data = ctypes.c_void_p()
        # Any type. The length counts 32-bit units
        status = self._x.XGetWindowProperty(self._display, xid, self._atom(name), 0, MAX_URI // 4, False, 0,
                                            ctypes.byref(kind), ctypes.byref(size), ctypes.byref(count),
                                            ctypes.byref(rest), ctypes.byref(data))
        if status != 0 or not data.value:
            return None
        try:
            return ctypes.string_at(data.value, count.value) if size.value == 8 else None
        finally:
            self._x.XFree(data)

    def delete(self, xid: int, name: str) -> None:
        self._x.XDeleteProperty(self._display, xid, self._atom(name))
        self._x.XFlush(self._display)
