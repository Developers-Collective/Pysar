import ctypes
import logging
from ctypes import wintypes
from typing import TYPE_CHECKING

from . import native as n
from .title_bar import top_edge_hit_test

if TYPE_CHECKING:
    from .caption import CaptionButtons
    from .backend import Win32WindowBackend

logger = logging.getLogger(__name__)

SUBCLASS_ID = 0x50797361 # "Pysa" in ASCII

# If we don't remove this flag, we would be missing things like snap layouts
SYSTEM_BUTTONS = n.WS_SYSMENU


class Frame:
    def __init__(self, hwnd: int, backend: "Win32WindowBackend", caption: "CaptionButtons", background: int) -> None:
        self.hwnd = hwnd
        self._backend = backend
        self._caption = caption
        self._background = background    # the color painted behind the page (a COLORREF)

        self.menu = n.GetSystemMenu(hwnd, False)

        # Kept here, or ctypes frees the callback while Windows still calls it
        self._procedure = n.SUBCLASSPROC(self._window_procedure)
        if not n.SetWindowSubclass(hwnd, self._procedure, SUBCLASS_ID, 0):
            raise ctypes.WinError(ctypes.get_last_error())

        # The original style is kept for remove()
        self._original_style = n.GetWindowLongPtrW(hwnd, n.GWL_STYLE)
        n.SetWindowLongPtrW(hwnd, n.GWL_STYLE, self._original_style & ~SYSTEM_BUTTONS)
        self._frame_changed()

    def remove(self) -> None:
        """Undo what __init__ did if the custom title bar can't be installed"""
        n.RemoveWindowSubclass(self.hwnd, self._procedure, SUBCLASS_ID)
        n.SetWindowLongPtrW(self.hwnd, n.GWL_STYLE, self._original_style)
        self._frame_changed()

    def _frame_changed(self) -> None:
        """Make Windows work out the window's frame after its style changed"""
        n.SetWindowPos(self.hwnd, None, 0, 0, 0, 0,
                       n.SWP_FRAMECHANGED | n.SWP_NOMOVE | n.SWP_NOSIZE | n.SWP_NOZORDER | n.SWP_NOACTIVATE)

    def _window_procedure(self, hwnd, message, wparam, lparam, subclass, data):
        try:
            result = self._handle(message, wparam, lparam)
            if result is not None:
                return result
        except Exception:
            logger.exception("window procedure failed")
        return n.DefSubclassProc(hwnd, message, wparam, lparam)

    def _handle(self, message: int, wparam: int, lparam: int) -> int | None:
        # wndProc
        match message:
            # Where the client area is
            case n.WM_NCCALCSIZE if wparam and n.has_frame(self.hwnd):
                return self._calc_size(wparam, lparam)

            # What part of the window is under the pointer
            case n.WM_NCHITTEST if n.has_frame(self.hwnd):
                return self._hit_test(wparam, lparam)

            # WinForms sets its own styles again
            case n.WM_STYLECHANGING if wparam & 0xFFFFFFFF == n.GWL_STYLE & 0xFFFFFFFF:
                styles = n.STYLESTRUCT.from_address(lparam)

                if styles.styleNew & n.WS_THICKFRAME:
                    styles.styleNew &= ~SYSTEM_BUTTONS
                return None

            # The pointer over the caption buttons
            case _ if message in _CAPTION_MESSAGES:
                return self._caption.handle(message, wparam)

            # Paint right away while the window is being shown.
            # Otherwise, DWM's first frame of the window would be white
            case n.WM_ERASEBKGND:
                client = wintypes.RECT()
                n.GetClientRect(self.hwnd, ctypes.byref(client))
                self._draw(wparam, client)    # wparam is the device context to paint
                return 1                      # erased

            case n.WM_PAINT:
                return self._paint()

            # The window turning active or inactive (-1 = let Windows still handle that)
            case n.WM_NCACTIVATE:
                return n.DefSubclassProc(self.hwnd, message, wparam, -1)

            case n.WM_ACTIVATE:
                active = (wparam & 0xFFFF) != n.WA_INACTIVE
                self._caption.set_active(active)
                self._backend.activated(active)

            # The window size changed
            case n.WM_SIZE:
                self._backend.layout()

            # The final message before its good bye...
            case n.WM_NCDESTROY:
                n.RemoveWindowSubclass(self.hwnd, self._procedure, SUBCLASS_ID)

        return None

    def _calc_size(self, wparam: int, lparam: int) -> int:
        params = n.NCCALCSIZE_PARAMS.from_address(lparam)
        top = params.rgrc[0].top
        result = n.DefSubclassProc(self.hwnd, n.WM_NCCALCSIZE, wparam, lparam)

        # Anything but 0 asks Windows for something special
        if result != 0:
            return result

        params.rgrc[0].top = top
        if n.IsZoomed(self.hwnd):
            # A maximized window reaches past its monitor by the resize border,
            # which would cut off the top of the page
            params.rgrc[0].top += n.resize_border(self.hwnd)
        return 0

    def _hit_test(self, wparam: int, lparam: int) -> int:
        """Tell Windows what part of the window is under the pointer"""
        result = n.DefSubclassProc(self.hwnd, n.WM_NCHITTEST, wparam, lparam)
        if result != n.HTCLIENT:
            return result

        point = n.screen_point(lparam)
        n.ScreenToClient(self.hwnd, ctypes.byref(point))

        # The top edge resizes the window, also above the caption buttons
        edge = top_edge_hit_test(self._backend.title_bar_layout, point.x, point.y,
                                 n.resize_border(self.hwnd), bool(n.IsZoomed(self.hwnd)))
        if edge is not None:
            return edge

        button = self._caption.hit_test(point.x, point.y)
        return button if button is not None else result

    def _paint(self) -> int:
        paint = n.PAINTSTRUCT()
        hdc = n.BeginPaint(self.hwnd, ctypes.byref(paint))
        try:
            # rcPaint is the part that needs painting
            self._draw(hdc, paint.rcPaint)
        finally:
            n.EndPaint(self.hwnd, ctypes.byref(paint))
        return 0

    def _draw(self, hdc: int, area: wintypes.RECT) -> None:
        brush = n.CreateSolidBrush(self._background)
        n.FillRect(hdc, ctypes.byref(area), brush)
        n.DeleteObject(brush)

        splash = self._backend.splash
        if splash is not None:
            self._draw_splash(hdc, splash)

        self._caption.paint(hdc)

    def _draw_splash(self, hdc: int, splash: tuple[int, int, int]) -> None:
        hbitmap, width, height = splash

        # The page fills the client area below page_top
        client = wintypes.RECT()
        n.GetClientRect(self.hwnd, ctypes.byref(client))
        top = self._backend.page_top
        page_width, page_height = client.right, client.bottom - top

        memory = n.CreateCompatibleDC(hdc)
        old = n.SelectObject(memory, hbitmap)

        if (width, height) == (page_width, page_height):
            n.BitBlt(hdc, 0, top, width, height, memory, 0, 0, n.SRCCOPY)
        else:
            # The page size has changed since it was captured, so the capture is
            # stretched to fit (HALFTONE stretches smoothly)
            n.SetStretchBltMode(hdc, n.HALFTONE)
            n.StretchBlt(hdc, 0, top, page_width, page_height, memory, 0, 0, width, height, n.SRCCOPY)

        n.SelectObject(memory, old)
        n.DeleteDC(memory)


# The messages about the pointer over the non-client area, which the caption
# buttons handle
_CAPTION_MESSAGES = frozenset((
    n.WM_NCMOUSEMOVE, n.WM_NCMOUSELEAVE, n.WM_NCLBUTTONDOWN, n.WM_NCLBUTTONDBLCLK,
    n.WM_NCLBUTTONUP, n.WM_NCRBUTTONDOWN, n.WM_NCRBUTTONUP,
))


def show_system_menu(hwnd: int, menu: int, x: int, y: int) -> None:
    """Open the window menu (Restore, Move, Size, ...) at a point on the screen"""

    if not menu:
        return

    # Rebuild Windows' menu
    # Anything not being able to be used will be grayed out
    maximized = bool(n.IsZoomed(hwnd))
    usable = {
        n.SC_RESTORE:  maximized,
        n.SC_MOVE:     not maximized,
        n.SC_SIZE:     not maximized,
        n.SC_MINIMIZE: True,
        n.SC_MAXIMIZE: not maximized,
        n.SC_CLOSE:    True,
    }

    for command, enabled in usable.items():
        state = n.MF_ENABLED if enabled else n.MF_GRAYED
        n.EnableMenuItem(menu, command, n.MF_BYCOMMAND | state)

    # Close is the default item, shown in bold
    n.SetMenuDefaultItem(menu, n.SC_CLOSE, False)

    # TPM_RETURNCMD returns the chosen command instead of sending it.
    # TPM_RIGHTBUTTON enables also right button clicking
    command = n.TrackPopupMenu(menu, n.TPM_RETURNCMD | n.TPM_RIGHTBUTTON, x, y, 0, hwnd, None)

    if command:
        n.PostMessageW(hwnd, n.WM_SYSCOMMAND, command, 0)
