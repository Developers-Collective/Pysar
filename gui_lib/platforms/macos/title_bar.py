import logging
from typing import Any

import AppKit

logger = logging.getLogger(__name__)

BUTTONS = (AppKit.NSWindowCloseButton, AppKit.NSWindowMiniaturizeButton, AppKit.NSWindowZoomButton)

EDGE_MARGIN = 12.0

# The buttons' width and distance between their centers come from AppKit's own
# layout, since they depend on the look. They are:
#     - 14pt and 20pt apart before macOS 26
#     - 14pt and 23pt apart with macOS 26's (see runtime.py)
#
BUTTON_WIDTH = 14.0
SPACING = 20.0

MIN_BUTTON_WIDTH = 8.0
MAX_BUTTON_WIDTH = 24.0
MAX_SPACING = 40.0

# Bars shorter than this squeeze the spacing like AppKit does
STANDARD_HEIGHT = 28.0

# NSLayoutConstraint.h
LEFT, RIGHT, TOP, BOTTOM, WIDTH, HEIGHT, CENTER_X, CENTER_Y = 1, 2, 3, 4, 7, 8, 9, 10
NOT_AN_ATTRIBUTE = 0
EQUAL = 0


def _center_x(frame: Any) -> float:
    """The horizontal center of an NSRect"""
    return frame.origin.x + frame.size.width / 2


def _constrain(item: Any, attribute: int, other: Any, other_attribute: int, constant: float = 0.0) -> Any:
    return AppKit.NSLayoutConstraint.constraintWithItem_attribute_relatedBy_toItem_attribute_multiplier_constant_(
        item, attribute, EQUAL, other, other_attribute, 1.0, constant)


def _title_bar_views(button: Any) -> tuple[Any, Any, Any] | None:
    ancestors = []
    view = button
    while view is not None:
        ancestors.append(view)
        view = view.superview()

    if len(ancestors) < 4:
        return None

    return ancestors[-1], ancestors[-2], ancestors[1]


class TrafficLights:
    def __init__(self, window: Any) -> None:
        self._window = window
        self._constraints: list[Any] = []
        self._views: list[Any] = []
        self._placed: tuple[float, float] | None = None
        self.width, self.spacing = self._measure()

    @property
    def room(self) -> float:
        return EDGE_MARGIN * 2 + self.width + self.spacing * 2

    @property
    def gap(self) -> float:
        return self.spacing - self.width

    def _measure(self) -> tuple[float, float]:
        close = self._window.standardWindowButton_(AppKit.NSWindowCloseButton)
        minimize = self._window.standardWindowButton_(AppKit.NSWindowMiniaturizeButton)
        if close is None or minimize is None:
            return BUTTON_WIDTH, SPACING

        close_frame, minimize_frame = close.frame(), minimize.frame()
        width = close_frame.size.width
        spacing = _center_x(minimize_frame) - _center_x(close_frame)

        plausible_width = MIN_BUTTON_WIDTH <= width <= MAX_BUTTON_WIDTH
        plausible_spacing = width < spacing <= MAX_SPACING
        if not (plausible_width and plausible_spacing):
            logger.debug(
                "unexpected traffic light layout (width %.1f, spacing %.1f), using the defaults",
                width,
                spacing
            )
            return BUTTON_WIDTH, SPACING

        return width, spacing

    def show(self) -> None:
        for kind in BUTTONS:
            button = self._window.standardWindowButton_(kind)
            if button is not None:
                button.setHidden_(False)

    def place(self, top: float, height: float, force: bool = False) -> None:
        if (top, height) == self._placed and self._constraints and not force:
            return

        self.release()

        if self._window.styleMask() & AppKit.NSWindowStyleMaskFullScreen:
            return

        buttons = [self._window.standardWindowButton_(kind) for kind in BUTTONS]
        views = _title_bar_views(buttons[0]) if None not in buttons else None
        if views is None:
            return
        theme_frame, container, title_bar = views

        constraints = [
            _constrain(container, LEFT, theme_frame, LEFT),
            _constrain(container, WIDTH, theme_frame, WIDTH),
            _constrain(container, TOP, theme_frame, TOP, top),
            _constrain(container, HEIGHT, None, NOT_AN_ATTRIBUTE, height),

            _constrain(title_bar, LEFT, container, LEFT),
            _constrain(title_bar, RIGHT, container, RIGHT),
            _constrain(title_bar, TOP, container, TOP),
            _constrain(title_bar, BOTTOM, container, BOTTOM),
        ]

        # The buttons are lain out in a row from the left edge (and centered vertically)
        # Shortening the bar squeezes them together like AppKit would do
        spacing = self.spacing * min(height / STANDARD_HEIGHT, 1.0)
        for index, button in enumerate(buttons):
            center = EDGE_MARGIN + self.width / 2 + index * spacing
            constraints += [
                _constrain(button, CENTER_X, container, LEFT, center),
                _constrain(button, CENTER_Y, container, CENTER_Y),
            ]

        # Auto Layout only moves views that don't keep their autoresizing frames
        self._views = [container, title_bar, *buttons]
        for view in self._views:
            view.setTranslatesAutoresizingMaskIntoConstraints_(False)
        AppKit.NSLayoutConstraint.activateConstraints_(constraints)
        self._constraints = constraints
        self._placed = (top, height)

        theme_frame.setNeedsLayout_(True)
        theme_frame.layoutSubtreeIfNeeded()

    def release(self) -> None:
        """Hand the buttons back to AppKit"""
        if not self._constraints:
            return

        AppKit.NSLayoutConstraint.deactivateConstraints_(self._constraints)
        for view in self._views:
            view.setTranslatesAutoresizingMaskIntoConstraints_(True)
        self._constraints = []
        self._views = []
        self._placed = None
