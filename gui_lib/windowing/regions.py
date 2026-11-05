from dataclasses import dataclass
from typing import Any

# (x, y, width, height) in CSS pixels, as getBoundingClientRect measures them
Rect = tuple[float, float, float, float]

CAPTION_BUTTONS = ("minimize", "maximize", "close")

# Caps on what a page can send. Each control is cut out of a native window region and
# the background CSS is parsed whenever the caption buttons repaint.
# So a broken page mustn't be able to make the GUI thread do unbounded work
MAX_CONTROLS = 64
MAX_CSS = 4096


@dataclass(frozen=True)
class TitleBarRegions:
    bar: Rect
    # The bar's controls (buttons, inputs, links, [data-no-drag], …)
    # apart from the caption buttons
    controls: tuple[Rect, ...]

    # The [data-caption] buttons by name, which act as the system's caption buttons
    caption: dict[str, Rect]

    # Physical pixels per CSS pixel (devicePixelRatio of the page)
    scale: float

    # The computed background-image and background-color behind the caption buttons
    # (for platforms that paint the buttons natively over the same background, which I did for Windows)
    background: tuple[str, str] = ("none", "")

    # The box of the element that paints that background
    background_box: Rect | None = None

    # The [data-window-controls] row the system's own window controls sit in
    # (i.e. MacOS traffic lights)
    window_controls: Rect | None = None

    def is_drag(self, x: float, y: float) -> bool:
        if not _inside(self.bar, x, y):
            return False

        if any(_inside(control, x, y) for control in self.controls):
            return False

        return not any(_inside(button, x, y) for button in self.caption.values())

    @classmethod
    def from_page(cls, data: Any) -> "TitleBarRegions | None":
        """Check and convert what the page sent or raise a ValueError for anything malformed"""
        # There is nothing (or it's hidden)
        if data is None:
            return None

        if not isinstance(data, dict):
            raise ValueError("Title bar regions are an object.")

        # A bar without controls or caption buttons may leave them out or send null
        controls = data.get("controls") or []
        caption = data.get("caption") or {}
        if not isinstance(controls, list) or len(controls) > MAX_CONTROLS or not isinstance(caption, dict):
            raise ValueError("Title bar regions have a list of controls and an object of caption buttons.")

        # bool is an int in Python, so True would pass as a scale of 1
        scale = data.get("scale")
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0.25 <= scale <= 8:
            raise ValueError("Title bar regions need the page's scale.")

        # Without a background, the buttons are painted over the window's own color.
        # A background that isn't an object leaves image and color at None, which the
        # check below refuses
        background = data.get("background") or {}
        image = background.get("image", "none") if isinstance(background, dict) else None
        color = background.get("color", "") if isinstance(background, dict) else None

        if not isinstance(image, str) or not isinstance(color, str) or len(image) + len(color) > MAX_CSS:
            raise ValueError("A title bar's background is its computed CSS.")

        # background is an object by now, the check above made sure of that
        box = background.get("rect")

        return cls(
            bar=_rect(data.get("bar")),
            controls=tuple(_rect(control) for control in controls),
            # Unknown names are ignored rather than refused, so one odd button doesn't
            # cost the page its whole title bar :O
            caption={name: _rect(rect) for name, rect in caption.items() if name in CAPTION_BUTTONS},
            scale=float(scale),
            background=(image, color),
            background_box=_rect(box) if box is not None else None,
            window_controls=_rect(row) if (row := data.get("windowControls")) is not None else None,
        )


def _rect(value: Any) -> Rect:
    if (not isinstance(value, list) or len(value) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
        raise ValueError("A rectangle is [x, y, width, height].")

    # getBoundingClientRect never measures negative sizes, so only a broken page sends that.
    # Clamp that so they make an empty region instead of an inverted one
    x, y, width, height = (float(v) for v in value)
    return x, y, max(width, 0.0), max(height, 0.0)


def _inside(rect: Rect, x: float, y: float) -> bool:
    left, top, width, height = rect
    return left <= x < left + width and top <= y < top + height
