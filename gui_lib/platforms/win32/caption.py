import ctypes
import functools
import math
import re
from ctypes import wintypes
from typing import TYPE_CHECKING

import numpy as np

from . import native as n

if TYPE_CHECKING:
    from .backend import Win32WindowBackend
    from .title_bar import Box, TitleBarLayout

BUTTONS = {n.HTMINBUTTON: "minimize", n.HTMAXBUTTON: "maximize", n.HTCLOSE: "close"}

GLYPHS = {
    n.HTMINBUTTON: 0xE921,  # ChromeMinimize
    n.HTMAXBUTTON: 0xE922,  # ChromeMaximize
    n.HTCLOSE:     0xE8BB,  # ChromeClose
}

# The maximize button icon while the window is maximized
RESTORE = 0xE923  # ChromeRestore

FONT = "Segoe Fluent Icons" if n.WINDOWS_11 else "Segoe MDL2 Assets"
GLYPH_SIZE     = 10
# While the window is inactive, its icons are the background mixed with this much
# of the highlight color
INACTIVE_GLYPH = 0.45

# The icon color and the highlight color laid over a hovered or pressed button.
DARK_MODE_GLYPH      = (233, 231, 241)
DARK_MODE_HIGHLIGHT  = (255, 255, 255)
LIGHT_MODE_GLYPH     = (28, 28, 28)
LIGHT_MODE_HIGHLIGHT = (0, 0, 0)

# How much of the highlight color is laid over a caption button while it's hovered or pressed
HOVER_HIGHLIGHT   = 0.06
PRESSED_HIGHLIGHT = 0.04

# The close button turns red while it's hovered, and lets a little of the
# background show through while it's pressed
CLOSE_RED             = (196, 43, 28)
CLOSE_PRESSED_OPACITY = 0.9

# Painted behind the buttons when the page's background is transparent or can't
# be read. It's the window's default background (WindowOptions.background_color)
FALLBACK_BACKGROUND = (0x12, 0x12, 0x14)


class CaptionButtons:
    """The window's caption buttons so minimize, maximize, and close"""

    def __init__(self, hwnd: int, backend: "Win32WindowBackend") -> None:
        self.hwnd = hwnd
        self._backend = backend
        self._layout: "TitleBarLayout | None" = None
        self._hover: int | None = None
        self._pressed: int | None = None
        self._active = True
        self._dark = True
        self._tracking = False
        self._fonts: dict[int, int] = {}

    def place(self, layout: "TitleBarLayout | None") -> None:
        self._layout = layout
        self.redraw()

    def hit_test(self, x: int, y: int) -> int | None:
        if self._layout is not None:
            for code, (left, top, right, bottom) in self._layout.buttons:
                if left <= x < right and top <= y < bottom:
                    return code
        return None

    def handle(self, message: int, wparam: int) -> int | None:
        # our handle, now for the window buttons

        code = wparam if wparam in BUTTONS else None
        if message == n.WM_NCMOUSEMOVE:
            self._track_leave()
            self._show(code, self._pressed if self._pressed == code else None)
            # Windows' own handling opens Snap Layouts over HTMAXBUTTON
            return None
        
        if message == n.WM_NCMOUSELEAVE:
            self._tracking = False
            self._show(None, None)
            return None

        if code is None:
            return None

        if message in (n.WM_NCLBUTTONDOWN, n.WM_NCLBUTTONDBLCLK):
            self._show(code, code)
            return 0

        if message == n.WM_NCLBUTTONUP:
            if code == self._pressed:
                self._backend.caption_clicked(BUTTONS[code])
            self._show(code, None)
            return 0
        # No window menu or classic button tracking on the buttons
        return 0

    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self.redraw()

    def set_dark(self, dark: bool) -> None:
        """Switch between the dark mode and the light mode colors"""
        if dark != self._dark:
            self._dark = dark
            self.redraw()

    def redraw(self) -> None:
        hdc = n.GetDC(self.hwnd)
        if not hdc:
            return
        try:
            self.paint(hdc)
        finally:
            n.ReleaseDC(self.hwnd, hdc)

    def paint(self, hdc: int) -> None:
        """Draw the caption buttons onto the frame.

        This is call when:
            - the frame paints something
            - through redraw() when a button's state changed
            - the window's activation state changes
        """
        WHITE = (255, 255, 255)

        if self._dark:
            normal_glyph, highlight = DARK_MODE_GLYPH, DARK_MODE_HIGHLIGHT
        else:
            normal_glyph, highlight = LIGHT_MODE_GLYPH, LIGHT_MODE_HIGHLIGHT

        layout = self._layout
        caption = layout.caption if layout is not None else None

        # nothing to draw here
        if caption is None:
            return

        left, top, right, bottom = caption

        # The buttons take up no space, so there is nothing to draw
        if right <= left or bottom <= top:
            return

        pixels = _background(layout, caption)
        glyphs = []

        for code, (button_left, button_top, button_right, button_bottom) in layout.buttons:
            area = pixels[button_top - top:button_bottom - top, button_left - left:button_right - left]
            hovered = code == self._hover
            pressed = code == self._pressed

            if code == n.HTCLOSE and (hovered or pressed):
                # The close button turns red, with a white icon
                _blend(area, CLOSE_RED, CLOSE_PRESSED_OPACITY if pressed else 1.0)
                glyph_color = WHITE
            elif pressed:
                _blend(area, highlight, PRESSED_HIGHLIGHT)
                glyph_color = normal_glyph
            elif hovered:
                _blend(area, highlight, HOVER_HIGHLIGHT)
                glyph_color = normal_glyph
            elif self._active:
                glyph_color = normal_glyph
            else:
                # While the window is inactive, the icon fades towards the background
                glyph_color = _mix(area, highlight, INACTIVE_GLYPH)

            # A maximized window shows the restore icon instead of maximize
            if code == n.HTMAXBUTTON and layout.maximized:
                glyph = RESTORE
            else:
                glyph = GLYPHS[code]

            # Position of the icon, measured from the bitmap's top left corner
            rect = wintypes.RECT(button_left - left, button_top - top, button_right - left, button_bottom - top)
            glyphs.append((glyph, glyph_color, rect))

        # Copy the bitmap onto the frame, with the icons drawn on top
        self._blit(hdc, pixels, left, top, glyphs)

    def _blit(self, hdc: int, pixels: np.ndarray, left: int, top: int, glyphs: list) -> None:
        """Copy the buttons' pixels onto the frame at (left, top), with their icons on top """
        # GDI can't draw onto a numpy array, so the pixels are copied into a GDI bitmap.
        # The icons are drawn onto that bitmap, and then the finished bitmap is
        # copied onto the frame in a single step, so the buttons never show half drawn.
        #
        # glyphs holds a (code point, color, rect) for each icon. Each rect is measured
        # from the bitmap's top left corner.

        height, width = pixels.shape[:2]

        info = n.BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(n.BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height     # negative height means the rows go from top to bottom
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32        # 32 bits per pixel

        # GDI only draws through a device context, so the bitmap gets one of its own.
        # CreateDIBSection also gives us a pointer to the bitmap's pixels.
        memory = n.CreateCompatibleDC(hdc)
        if not memory:
            return

        bits = ctypes.c_void_p()
        bitmap = n.CreateDIBSection(memory, ctypes.byref(info), n.DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        if not bitmap or not bits:
            # GDI is out of resources, so skip this and try again
            if bitmap:
                n.DeleteObject(bitmap)
            n.DeleteDC(memory)
            return

        ctypes.memmove(bits, pixels.ctypes.data, pixels.nbytes)

        # Draw onto the bitmap with the icon font. SelectObject returns whatever was
        # selected before, which has to be put back before the bitmap can be deleted
        old_bitmap = n.SelectObject(memory, bitmap)
        old_font = n.SelectObject(memory, self._font())

        # Draw just the icons
        n.SetBkMode(memory, n.TRANSPARENT)
        for glyph, color, rect in glyphs:
            n.SetTextColor(memory, n.colorref(*color))

            # Draw the character, but since the icon always fits inside the button,
            # we don't need to clip it
            n.DrawTextW(memory, chr(glyph), 1, ctypes.byref(rect), n.DT_CENTER | n.DT_VCENTER | n.DT_SINGLELINE | n.DT_NOCLIP)

        # Copy the finished bitmap onto the frame
        n.BitBlt(hdc, left, top, width, height, memory, 0, 0, n.SRCCOPY)

        # Put the old objects back, then delete the bitmap and its device context.
        # The font stays, so it's being kept alive for _font().
        n.SelectObject(memory, old_font)
        n.SelectObject(memory, old_bitmap)
        n.DeleteObject(bitmap)
        n.DeleteDC(memory)

    def _font(self) -> int:
        """The icon font at the window's current DPI"""

        dpi = n.GetDpiForWindow(self.hwnd) or 96    # 96 DPI is 100% scale; 0 means the handle isn't valid
        font = self._fonts.get(dpi)
        if font is not None:
            return font

        font = n.CreateFontW(
            -round(GLYPH_SIZE * dpi / 96),    # height (negative means the em size, like CSS's font-size)
            0,                                # width (0 picks the one that matches the height)
            0,                                # escapement (the text's angle)
            0,                                # orientation (each character's angle)
            n.FW_NORMAL,                      # weight
            0,                                # italic
            0,                                # underline
            0,                                # strikeout
            n.DEFAULT_CHARSET,                # character set
            0,                                # output precision (default)
            0,                                # clipping precision (default)
            n.CLEARTYPE_QUALITY,              # smoothing (ClearType)
            0,                                # pitch and family (default)
            FONT,                             # the font's name
        )
        self._fonts[dpi] = font
        return font

    def _track_leave(self) -> None:
        """Report whether the mouse left the non-client are or not"""

        if self._tracking:
            return

        event = n.TRACKMOUSEEVENT(
            ctypes.sizeof(n.TRACKMOUSEEVENT),
            n.TME_LEAVE | n.TME_NONCLIENT,       # what to report (leaving the non-client area)
            self.hwnd,                           # the window to watch
            0,                                   # hover time (only for hover messages, which we don't ask for)
        )
        self._tracking = bool(n.TrackMouseEvent(ctypes.byref(event)))

    def _show(self, hover: int | None, pressed: int | None) -> None:
        """Change which button is hovered and which is pressed, and show the change"""
        # hover and pressed are hit test codes (n.HTCLOSE and so on), or None for no button

        # Nothing changed (true for most mouse moves), so skip the repaint
        if (hover, pressed) == (self._hover, self._pressed):
            return

        self._hover = hover
        self._pressed = pressed
        self.redraw()

        # Tell the page too (chrome.js puts data-hover and data-pressed on its caption
        # buttons). It gets the buttons' names ("minimize" and so on), not hit test codes
        self._backend.caption_changed(BUTTONS.get(hover), BUTTONS.get(pressed))


def _blend(area: np.ndarray, color: tuple[int, int, int], alpha: float) -> None:
    """Lay a color over every pixel of an area"""

    # BGRA color
    color_bgr = np.array(color[::-1], dtype=np.float32)
    blended = area[..., :3] * (1 - alpha) + color_bgr * alpha
    area[..., :3] = np.rint(blended).astype(np.uint8)


def _mix(area: np.ndarray, color: tuple[int, int, int], alpha: float) -> tuple[int, int, int]:
    """Mix an area's average color with another color and return the result as (red, green, blue)"""

    blue, green, red = area[..., :3].reshape(-1, 3).mean(axis=0)
    average = (red, green, blue)
    return tuple(round(base * (1 - alpha) + value * alpha) for base, value in zip(average, color))


def _background(layout: "TitleBarLayout", box: "Box") -> np.ndarray:
    # The frame draws the caption buttons, not the page. To make the page's gradient carry
    # on behind them, it's painted again here for just the pixels under the buttons.

    left, top, right, bottom = box
    angle, stops = _fill(*layout.background)
    bar_x, bar_y, bar_width, bar_height = layout.background_box

    # The middle of every pixel column and row in the box in CSS pixels.
    # measured from the top left corner of the element that paints the background
    xs = (np.arange(left, right) + 0.5) / layout.scale - bar_x
    ys = (np.arange(top, bottom) - layout.page_top + 0.5) / layout.scale - bar_y

    # The direction the gradient runs in.
    # CSS angles turn clockwise from straight up and on screen y grows downwards
    sin = math.sin(math.radians(angle))
    cos = math.cos(math.radians(angle))

    # How long the gradient line is (1 for an element without a size)
    length = abs(bar_width * sin) + abs(bar_height * cos) or 1.0

    # How far along the gradient line each pixel lies, from 0 to 1.
    #
    # xs[None, :] and ys[:, None] turn the columns and rows into a grid
    # with one value per pixel
    along = ((xs[None, :] - bar_width / 2) * sin - (ys[:, None] - bar_height / 2) * cos) / length + 0.5

    # Interpolate each color channel between the stops. Before the first stop and after
    # the last one, np.interp keeps their colors
    positions = [position for position, _ in stops]
    reds = [color[0] for _, color in stops]
    greens = [color[1] for _, color in stops]
    blues = [color[2] for _, color in stops]

    pixels = np.empty((bottom - top, right - left, 4), dtype=np.uint8)
    pixels[..., 0] = np.rint(np.interp(along, positions, blues))
    pixels[..., 1] = np.rint(np.interp(along, positions, greens))
    pixels[..., 2] = np.rint(np.interp(along, positions, reds))
    pixels[..., 3] = 255    # fully opaque
    return pixels


# A color as computed styles write it:
#     rgb(50, 44, 67)
#     rgba(0, 0, 0, 0)
#     rgb(50 44 67 / 50%)
_COLOR = re.compile(r"rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+)(%?))?\s*\)")

# The directions a linear-gradient() can be given as words or as angles
_SIDES = {"to top": 0.0, "to right": 90.0, "to bottom": 180.0, "to left": 270.0}



# maybe a bit unnecessary and ugly, but I like typing my stuff properly
RGB = tuple[int, int, int]
# Where the color sits along the gradient (0 to 1)
ColorStop = tuple[float, RGB]
# Its angle in degrees and its color stops
Gradient = tuple[float, tuple[ColorStop, ...]]


@functools.lru_cache(maxsize=16)
def _fill(image: str, color: str) -> Gradient:
    gradient = _linear_gradient(image)
    if gradient is not None:
        return gradient

    solid = _color(color) or FALLBACK_BACKGROUND
    return 180.0, ((0.0, solid), (1.0, solid))    # the same color from top to bottom


def _linear_gradient(image: str) -> Gradient | None:
    start = image.find("linear-gradient(")
    if start < 0:
        return None

    # Split the gradient's arguments
    parts, depth, current = [], 0, ""
    for character in image[start + len("linear-gradient("):]:
        if character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                break
            depth -= 1

        if character == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += character
    parts.append(current.strip())

    # The first argument is the direction (110deg, to right, ...) unless it's already a color.
    # Without a direction, the gradient runs to the bottom.
    angle = 180.0
    if parts and not _COLOR.search(parts[0]):
        angle = _angle(parts.pop(0))

    # Every other argument is a color stop
    stops: list[tuple[float | None, RGB]] = []
    for part in parts:
        match = _COLOR.search(part)

        # a stop without a color is invalid
        if match is None:
            return None

        rest = part[match.end():].strip().split()
        position = float(rest[0][:-1]) / 100 if rest and rest[0].endswith("%") else None
        # A transparent stop can't be painted over nothing, so it gets the fallback color
        stops.append((position, _color(match.group(0)) or FALLBACK_BACKGROUND))

    # CSS needs at least two stops for a gradient
    if len(stops) < 2:
        return None

    return angle, _place_stops(stops)


def _angle(text: str) -> float:
    text = text.strip()
    if text in _SIDES:
        return _SIDES[text]
    for unit, factor in (("deg", 1.0), ("grad", 0.9), ("rad", 180 / math.pi), ("turn", 360.0)):
        if text.endswith(unit):
            try:
                return float(text[:-len(unit)]) * factor
            except ValueError:
                break
    return 180.0


def _place_stops(stops: list[tuple[float | None, RGB]]) -> tuple[ColorStop, ...]:
    """Give every color stop a position like CSS does"""

    positions = [position for position, _ in stops]

    # 1. Without a position, the first stop sits at the start, and the last at the end
    if positions[0] is None:
        positions[0] = 0.0
    if positions[-1] is None:
        positions[-1] = 1.0

    # 2. Positions never go backwards. A stop placed before an earlier one is moved up
    #    to the furthest position so far, which makes a hard edge between the two colors
    furthest = positions[0]
    for index, position in enumerate(positions):
        if position is not None:
            furthest = max(furthest, position)
            positions[index] = furthest

    # 3. Stops that still have no position are spread out evenly between the stops on
    #    either side of them
    index = 1
    while index < len(positions):
        if positions[index] is not None:
            index += 1
            continue

        # The stops from index up to (but not including) end have no position.
        # The last stop always has one, so end is always found
        end = index
        while positions[end] is None:
            end += 1

        before, after = positions[index - 1], positions[end]
        gaps = end - index + 1
        for step, missing in enumerate(range(index, end), start=1):
            positions[missing] = before + (after - before) * step / gaps
        index = end

    return tuple((position, color) for position, (_, color) in zip(positions, stops))


def _color(text: str) -> tuple[int, ...] | None:
    match = _COLOR.search(text or "")
    if match is None:
        return None

    alpha = match.group(4)
    if alpha is not None and float(alpha) == 0:
        return None

    return tuple(min(255, round(float(value))) for value in match.group(1, 2, 3))
