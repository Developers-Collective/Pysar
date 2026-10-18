import ctypes
import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import native as n

if TYPE_CHECKING:
    from ...windowing.regions import Rect, TitleBarRegions
    from .backend import Win32WindowBackend

logger = logging.getLogger(__name__)

CLASS_NAME = "PysarTitleBar"
WM_LAYOUT = n.WM_APP + 1
HIT_TESTS = {"minimize": n.HTMINBUTTON, "maximize": n.HTMAXBUTTON, "close": n.HTCLOSE}

# left, top, right, bottom in the frame's client area (in physical pixels)
Box = tuple[int, int, int, int]


@dataclass(frozen=True)
class TitleBarLayout:
    top: int
    bottom: int
    width: int
    maximized: bool
    # The strip along the window's top edge that resizes it (0 = maximized)
    resize_strip: int
    corner: int
    controls: tuple[Box, ...]
    buttons: tuple[tuple[int, Box], ...]
    scale: float
    page_top: int
    background: tuple[str, str]
    background_box: "Rect"

    @property
    def caption(self) -> Box | None:
        if not self.buttons:
            return None
        boxes = [box for _, box in self.buttons]
        return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def lay_out(regions: "TitleBarRegions | None", page_top: int, width: int, border: int, maximized: bool) -> TitleBarLayout | None:
    """Convert the title bar that the page reported into pixels in the frame's client area.
       The page measures its title bar in CSS pixels, but Windows needs physical pixels.
    """

    # a window without width (i.e., while minimized), or a bar without height
    if regions is None or width <= 0 or regions.bar[3] <= 0:
        return None

    scale = regions.scale
    bar_x, bar_y, bar_width, bar_height = regions.bar

    # How much wider the window is now than when the page measured the bar, in CSS
    # pixels. It is negative when the window got narrower, and 0 once the page has
    # caught up. The bar always spans the whole window, so its width is the window's.
    growth = width / scale - bar_width

    # How far an element moves when the window grows, as a share of the growth
    LEFT, CENTER, RIGHT = 0.0, 0.5, 1.0

    def side_of(rect: "Rect") -> float:
        """Guess which side of the bar an element belongs to, by which third its center is in"""
        x, _, rect_width, _ = rect
        center = x + rect_width / 2 - bar_x

        if center < bar_width / 3:
            return LEFT

        if center > bar_width * 2 / 3:
            return RIGHT

        return CENTER

    def to_pixels(rect: "Rect", side: float) -> Box:
        """Move a rectangle along with the window's growth and convert it into physical pixels"""
        x, y, rect_width, rect_height = rect
        x += growth * side

        left = math.floor(x * scale)
        right = math.ceil((x + rect_width) * scale)
        top = page_top + math.floor(y * scale)
        bottom = page_top + math.ceil((y + rect_height) * scale)

        return left, top, right, bottom

    # Only the bar's top and bottom are needed
    # since it always spans the whole width
    _, top, _, bottom = to_pixels(regions.bar, LEFT)

    controls = tuple(to_pixels(rect, side_of(rect)) for rect in regions.controls)

    # The caption buttons always sit at the right edge, so they always move with it
    buttons = tuple((HIT_TESTS[name], to_pixels(rect, RIGHT)) for name, rect in regions.caption.items())

    # The strip along the top edge that resizes the window is half as thick as the
    # resize border. A maximized window can't be resized, so it has no strip.
    resize_strip = 0 if maximized else border // 2

    # Near the left and right ends, the top edge resizes the window diagonally
    corner = border * 2

    # The element painted behind the caption buttons (or the bar, if the page didn't
    # say which one) spans the window too, so it grows along with it
    background_box = _widened(regions.background_box or regions.bar, growth)

    return TitleBarLayout(
        top=top,
        bottom=bottom,
        width=width,
        maximized=maximized,
        resize_strip=resize_strip,
        corner=corner,
        controls=controls,
        buttons=buttons,
        scale=scale,
        page_top=page_top,
        background=regions.background,
        background_box=background_box,
    )


def _widened(box: "Rect", shift: float) -> "Rect":
    x, y, width, height = box
    return x, y, width + shift, height


def top_edge_hit_test(layout: TitleBarLayout | None, x: int, y: int, border: int, maximized: bool) -> int | None:
    strip = layout.resize_strip if layout is not None else (0 if maximized else border // 2)
    if y >= strip:
        return None

    width = layout.width if layout is not None else 0
    corner = border * 2
    if x < corner:
        return n.HTTOPLEFT

    if width and x >= width - corner:
        return n.HTTOPRIGHT

    return n.HTTOP


_windows: dict[int, "TitleBarWindow"] = {}
_procedure = None


def _register_class() -> None:
    global _procedure
    if _procedure is not None:
        return
    procedure = n.WNDPROC(_window_procedure)
    window_class = n.WNDCLASSEXW()
    window_class.cbSize = ctypes.sizeof(n.WNDCLASSEXW)
    window_class.lpfnWndProc = procedure
    window_class.hInstance = n.GetModuleHandleW(None)
    window_class.hCursor = n.LoadCursorW(None, n.IDC_ARROW)
    window_class.lpszClassName = CLASS_NAME

    if not n.RegisterClassExW(ctypes.byref(window_class)):
        raise ctypes.WinError(ctypes.get_last_error())

    _procedure = procedure

# wndProc
def _window_procedure(hwnd, message, wparam, lparam):
    window = _windows.get(hwnd)
    if window is not None:
        try:
            result = window.handle(message, wparam, lparam)
            if result is not None:
                return result
        except Exception:  # don't skin me alive
            logger.exception("title bar window procedure failed")
    return n.DefWindowProcW(hwnd, message, wparam, lparam)


class TitleBarWindow:
    def __init__(self, parent: int, backend: "Win32WindowBackend") -> None:
        _register_class()

        self.parent = parent
        self._backend = backend
        self._layout: TitleBarLayout | None = None
        hwnd = n.CreateWindowExW(
            n.WS_EX_LAYERED | n.WS_EX_NOREDIRECTIONBITMAP, CLASS_NAME, None, n.WS_CHILD,
            0, 0, 0, 0, parent, None, n.GetModuleHandleW(None), None,
        )

        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        self.hwnd = hwnd
        _windows[hwnd] = self
        n.SetLayeredWindowAttributes(hwnd, 0, 255, n.LWA_ALPHA)

    def request_layout(self) -> None:
        n.PostMessageW(self.hwnd, WM_LAYOUT, 0, 0)

    def place(self, layout: TitleBarLayout | None) -> None:
        """Lays this window over the page's title bar so it only catches what the bar should"""
        self._layout = layout

        if layout is None:
            n.ShowWindow(self.hwnd, n.SW_HIDE)
            return

        height = layout.bottom - layout.top
        region = n.CreateRectRgn(0, 0, layout.width, height)
        caption = layout.caption

        cutouts = list(layout.controls)
        if caption is not None:
            cutouts.append(caption)

        for left, top, right, bottom in cutouts:
            # The boxes are measured from the top of the client area, but this window
            # starts layout.top pixels lower
            cut = n.CreateRectRgn(left, top - layout.top, right, bottom - layout.top)
            n.CombineRgn(region, region, cut, n.RGN_DIFF)
            # Windows copies the cut into the region, so it can be deleted right away
            n.DeleteObject(cut)

        # Add the top edge back across the WHOLE width including the cuts.
        #
        # resize_strip is measured from the client area's top, so only the part of it below
        # layout.top falls in this window
        strip = max(0, layout.resize_strip - layout.top)
        if strip:
            edge = n.CreateRectRgn(0, 0, layout.width, strip)
            n.CombineRgn(region, region, edge, n.RGN_OR)
            n.DeleteObject(edge)

        n.SetWindowPos(self.hwnd, n.HWND_TOP, 0, layout.top, layout.width, height, n.SWP_NOACTIVATE | n.SWP_SHOWWINDOW)

        # Once set, the region belongs to the system, which frees it when it's no longer necessary
        if not n.SetWindowRgn(self.hwnd, region, True):
            n.DeleteObject(region)

    def handle(self, message: int, wparam: int, lparam: int) -> int | None:
        # How I hate you windows
        match message:
            case n.WM_NCHITTEST:
                # layout not ready, so skip
                layout = self._layout
                if layout is None:
                    return n.HTTRANSPARENT

                point = n.screen_point(lparam)
                n.ScreenToClient(self.hwnd, ctypes.byref(point))

                edge = top_edge_hit_test(
                    layout,
                    point.x,
                    point.y + layout.top,
                    layout.corner // 2,
                    layout.maximized
                )

                return edge if edge is not None else n.HTCAPTION

            case n.WM_NCLBUTTONDOWN | n.WM_NCLBUTTONDBLCLK:
                return n.SendMessageW(self.parent, message, wparam, lparam)

            case n.WM_NCRBUTTONDOWN if wparam == n.HTCAPTION:
                return 0

            case n.WM_NCRBUTTONUP if wparam == n.HTCAPTION:
                point = n.screen_point(lparam)
                self._backend.show_system_menu(point.x, point.y)
                return 0

            case _ if message == WM_LAYOUT:
                self._backend.layout()
                return 0

            case n.WM_NCDESTROY:
                _windows.pop(self.hwnd, None)

        # let windows do the rest
        return None
