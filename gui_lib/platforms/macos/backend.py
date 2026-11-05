# MacOS is SOOOOO much more chill than fucking windows man

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import AppKit
import Foundation
from PyObjCTools import AppHelper

from ...windowing.options import Backdrop, ChromeMode, Theme, WindowPlatform
from .. import pywebview_backend
from .app_config import dock_icon
from .title_bar import EDGE_MARGIN, TrafficLights

if TYPE_CHECKING:
    from ...windowing.dialogs import MessageBox
    from ...windowing.regions import TitleBarRegions

logger = logging.getLogger(__name__)

# How long a loaded page may take to draw
READY_TIMEOUT = 0.5
# How long macOS's closing animation takes which plays before the window really closes
CLOSE_ANIMATION = 0.3
# AppKit lays the title bar out again a moment after leaving fullscreen
RELAYOUT_DELAY = 0.05

# The windows by number, for the mouse monitor
_windows: dict[int, "MacOsWindowBackend"] = {}
_windows_lock = threading.Lock()
_monitor: Any = None


class MacOsWindowBackend(pywebview_backend.PywebviewWindowBackend):
    GUI = "cocoa"
    platform = WindowPlatform.MACOS
    supports_custom_chrome = True

    def __init__(self, owner: Any, options: Any) -> None:
        super().__init__(owner, options)
        self._ns_window: Any = None
        self._web_view: Any = None
        self._background: Any = None
        self._traffic_lights: TrafficLights | None = None
        self._regions: TitleBarRegions | None = None
        self._observers: list[tuple[Any, Any]] = []
        self._ready_timer: threading.Timer | None = None
        self._ordered_in = False
        # AppKit's fullscreen title bar while it's hidden
        self._fullscreen_bar: tuple[Any, bool, bool] | None = None
        self._closing_animated = False

    def _frameless(self) -> bool:
        return self._owner.chrome is ChromeMode.CUSTOM

    def chrome_details(self) -> dict[str, Any]:
        lights = self._traffic_lights
        if self._owner.chrome is not ChromeMode.CUSTOM or lights is None:
            return {}

        # The page leaves the traffic lights their room at its title bar's left edge.
        # In fullscreen they're gone (see _hide_fullscreen_bar) and the page draws its
        # own in their place
        return {"windowControls": {"left": lights.room, "right": 0, "x": EDGE_MARGIN,
                                   "width": lights.width, "gap": lights.gap}}

    #
    # Startup
    #
    # The page should be drawn when the window appears like on Microslop Windows.
    # But ShitKit (WebKit) doesn't draw a page whose window isn't on screen, meaning
    # our workaorund for Windows won't work ofc.
    # Instead the window goes on screen fully transparent once the page has loaded
    # which counts as visible for ShitKit.
    # When chrome.js says the page has drawn (window.ready), the window fades in.
    #
    #   _before_show      (before the page loads)
    #   _page_loaded      (window on screen, transparent)
    #   page_ready        (the page has drawn its first frames)
    #   _present          (fade in)

    def _before_show(self) -> None:
        from webview.platforms.cocoa import BrowserView

        self._ns_window = window = self.native_window
        self._web_view = BrowserView.instances[self._window.uid].webview
        self._background = window.backgroundColor()
        with _windows_lock:
            _windows[window.windowNumber()] = self

        self._place()
        self._observe_window()
        self._observe_appearance()
        self._apply_theme()
        self._apply_backdrop()
        window.setDocumentEdited_(self._owner.edited)
        if self._owner.chrome is ChromeMode.CUSTOM:
            self._install_chrome()

    def _install_chrome(self) -> None:
        try:
            self._traffic_lights = TrafficLights(self._ns_window)
            self._traffic_lights.show()
            _install_monitor()
        except Exception:
            logger.exception("couldn't give the window the page's title bar")
            for kind in (AppKit.NSWindowCloseButton, AppKit.NSWindowMiniaturizeButton, AppKit.NSWindowZoomButton):
                button = self._ns_window.standardWindowButton_(kind)
                if button is not None:
                    button.setHidden_(False)
            self._traffic_lights = None

    def _page_loaded(self) -> None:
        if not self._options.show_when_loaded or self._shown:
            return

        AppHelper.callAfter(self._order_in_transparent)
        self._ready_timer = threading.Timer(READY_TIMEOUT, self._show_once)
        self._ready_timer.daemon = True
        self._ready_timer.start()

    def _order_in_transparent(self) -> None:
        if self._ordered_in or self._shown or self._closed:
            return

        self._ordered_in = True
        window = self._ns_window
        window.setAlphaValue_(0.0)
        window.setAnimationBehavior_(AppKit.NSWindowAnimationBehaviorNone)
        window.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

    def page_ready(self) -> None:
        self._show_once()

    def _present(self) -> None:
        if self._ready_timer is not None:
            self._ready_timer.cancel()
        AppHelper.callAfter(self._reveal)

    def _reveal(self) -> None:
        if self._closed:
            return

        # We finished rendering the page, so we can disable and reenable it
        # in order to make the "pop out" animation normally seen on macos when
        # opening application windows

        window = self._ns_window
        if self._ordered_in:
            window.orderOut_(None)
        self._ordered_in = True
        window.setAlphaValue_(1.0)
        window.setAnimationBehavior_(AppKit.NSWindowAnimationBehaviorDocumentWindow)
        window.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

    def show(self) -> None:
        if self._shown:
            super().show()
        else:
            self._show_once()

    #
    # Closing
    #
    # A Mac window plays its closing animation as it leaves the screen.
    # pywebview takes the page out of the window as soon as the window closes
    # and ends the app with its last window, which cut the animation short or
    # played it on an empty window.
    # So the window leaves the screen first, animated, and only closes once
    # that's done.

    def close(self) -> None:
        AppHelper.callAfter(self._close_animated)

    def _closing(self) -> bool | None:
        # AppKit asks here before it closes the window, and once the window's closing
        # handlers agree, a window closed on purpose closes the animated way instead.
        if self._closing_animated:
            return False
        quitting = not self._closed_on_purpose()
        if not self._owner._handle_closing(quitting):
            return False
        if quitting:
            # Quitting (Cmd+Q, the app menu, the Dock, logging out) takes the windows
            # away at once, without their animation like in any app
            return None
        AppHelper.callAfter(self._close_animated)
        return False

    def _closed_on_purpose(self) -> bool:
        event = AppKit.NSApp.currentEvent()
        if event is None or event.window() != self._ns_window:
            return False

        if event.type() == AppKit.NSEventTypeKeyDown:
            return event.charactersIgnoringModifiers().lower() == "w"

        return True

    def _close_animated(self) -> None:
        if self._closed or self._closing_animated:
            return
        self._closing_animated = True

        window = self._ns_window
        # A fullscreen or minimized window leaves with its own animation
        if window.styleMask() & AppKit.NSWindowStyleMaskFullScreen or window.isMiniaturized():
            window.close()
            return
        window.orderOut_(None)
        AppHelper.callLater(CLOSE_ANIMATION, window.close)

    def message_box(self, box: "MessageBox") -> int:
        if threading.current_thread() is threading.main_thread():
            alert, order = self._alert(box)
            return self._answer(box, order, alert.runModal())

        done = threading.Event()
        answer = [box.dismissed]

        def finished(choice: int) -> None:
            answer[0] = choice
            done.set()

        AppHelper.callAfter(self._begin_alert, box, finished)
        self._wait_for_answer(done)
        return answer[0]

    def _begin_alert(self, box: "MessageBox", finished: Callable[[int], None]) -> None:
        try:
            alert, order = self._alert(box)
            window = self._ns_window
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            if window is None or self._closed or window.attachedSheet() is not None or not (
                    window.isVisible() or window.isMiniaturized()):
                finished(self._answer(box, order, alert.runModal()))
                return
            if window.isMiniaturized():
                window.deminiaturize_(None)
            window.makeKeyAndOrderFront_(None)
            alert.beginSheetModalForWindow_completionHandler_(
                window, lambda response, alert=alert: finished(self._answer(box, order, response)))
        except Exception:
            logger.exception("couldn't ask the user")
            finished(box.dismissed)

    @staticmethod
    def _alert(box: "MessageBox") -> tuple[Any, list[int]]:
        order = box.order(default_first=True)
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(box.message)
        alert.setInformativeText_(box.detail)
        alert.setAlertStyle_(AppKit.NSAlertStyleWarning if box.warning else AppKit.NSAlertStyleInformational)
        for index in order:
            button = alert.addButtonWithTitle_(box.buttons[index])
            if index == box.cancel and index != box.default:
                button.setKeyEquivalent_("\x1b")

            # Its title in red (macOS 11 and later)
            if index == box.destructive and button.respondsToSelector_(b"setHasDestructiveAction:"):
                button.setHasDestructiveAction_(True)
        return alert, order

    @staticmethod
    def _answer(box: "MessageBox", order: list[int], response: int) -> int:
        position = response - AppKit.NSAlertFirstButtonReturn
        return order[position] if 0 <= position < len(order) else box.dismissed

    def set_edited(self, edited: bool) -> None:
        AppHelper.callAfter(self._apply_edited)

    def _apply_edited(self) -> None:
        if self._ns_window is not None and not self._closed:
            self._ns_window.setDocumentEdited_(self._owner.edited)

    def show_devtools(self) -> None:
        def show() -> None:
            from webview.platforms.cocoa import BrowserView

            if self._web_view is None or not BrowserView._open_web_inspector(self._web_view):
                logger.info("couldn't open the web inspector! it's in the page's context menu (Inspect Element)")

        AppHelper.callAfter(show)

    #
    # The title bar
    #

    def set_title_bar_regions(self, regions: "TitleBarRegions | None") -> None:
        self._regions = regions
        if self._traffic_lights is not None:
            AppHelper.callAfter(self._place_traffic_lights)

    def _place_traffic_lights(self, force: bool = False) -> None:
        if self._traffic_lights is None or self._closed:
            return

        regions = self._regions
        if regions is None:
            return

        _, top, _, height = regions.window_controls or regions.bar
        if height > 0:
            self._traffic_lights.place(top, height, force=force)

    def mouse_down(self, event: Any) -> bool:
        window = self._ns_window
        if self._traffic_lights is None or window.styleMask() & AppKit.NSWindowStyleMaskFullScreen:
            return False

        content = window.contentView()
        if content is None or content.superview() is None:
            return False

        point = event.locationInWindow()
        hit = content.superview().hitTest_(point)
        if hit is None or not (hit == content or hit.isDescendantOf_(content)):
            return False

        regions = self._regions
        if regions is None or not regions.is_drag(point.x, content.frame().size.height - point.y):
            return False

        if event.clickCount() == 2:
            self._double_click()
        else:
            window.performWindowDragWithEvent_(event)
        return True

    def _double_click(self) -> None:
        action = Foundation.NSUserDefaults.standardUserDefaults().stringForKey_("AppleActionOnDoubleClick")
        if action == "Minimize":
            self._ns_window.miniaturize_(None)
        elif action not in ("None", "Do Nothing"):
            self._ns_window.zoom_(None)

    def frame(self) -> tuple[int, int, int, int]:
        window = self._ns_window
        if window is None:
            return super().frame()

        def measure() -> tuple[int, int, int, int]:
            frame = window.frame()
            top = AppKit.NSScreen.screens()[0].frame().size.height
            return (round(frame.origin.x), round(top - frame.origin.y - frame.size.height),
                    round(frame.size.width), round(frame.size.height))

        return _on_gui_thread(measure)

    def _place(self) -> None:
        options = self._options
        if options.x is None or options.y is None:
            return
        top = AppKit.NSScreen.screens()[0].frame().size.height
        self._ns_window.setFrameTopLeftPoint_(AppKit.NSMakePoint(options.x, top - options.y))

    def minimize(self) -> None:
        AppHelper.callAfter(lambda: self._ns_window.miniaturize_(None))

    def toggle_maximize(self) -> None:
        def toggle() -> None:
            window = self._ns_window
            if window.styleMask() & AppKit.NSWindowStyleMaskFullScreen:
                window.toggleFullScreen_(None)
            else:
                window.zoom_(None)

        AppHelper.callAfter(toggle)

    def toggle_fullscreen(self) -> None:
        AppHelper.callAfter(lambda: self._ns_window.toggleFullScreen_(None))

    def _track_window_state(self, events: Any) -> None:
        pass

    def _observe_window(self) -> None:
        window = self._ns_window
        state = self._owner._handle_state

        def entered_fullscreen() -> None:
            # After AppKit has set up its fullscreen bar
            AppHelper.callLater(0, self._hide_fullscreen_bar)
            state(fullscreen=True, maximized=False)

        def will_enter_fullscreen() -> None:
            if self._traffic_lights is not None:
                self._traffic_lights.release()

        def exited_fullscreen() -> None:
            self._restore_fullscreen_bar()
            self._place_traffic_lights(force=True)
            # AppKit lays its title bar out once more after the animation
            AppHelper.callLater(RELAYOUT_DELAY, lambda: self._place_traffic_lights(force=True))
            state(fullscreen=False, maximized=bool(window.isZoomed()))

        def became_key() -> None:
            # AppKit brings its fullscreen bar back when the window becomes key again
            AppHelper.callLater(0, self._hide_fullscreen_bar)
            state(focused=True)

        def resized() -> None:
            if not window.styleMask() & AppKit.NSWindowStyleMaskFullScreen:
                state(maximized=bool(window.isZoomed()))

        self._observe(Foundation.NSNotificationCenter.defaultCenter(), window, {
            AppKit.NSWindowWillEnterFullScreenNotification: will_enter_fullscreen,
            AppKit.NSWindowDidEnterFullScreenNotification: entered_fullscreen,
            AppKit.NSWindowDidExitFullScreenNotification: exited_fullscreen,
            AppKit.NSWindowDidResizeNotification: resized,
            AppKit.NSWindowDidBecomeKeyNotification: became_key,
            AppKit.NSWindowDidResignKeyNotification: lambda: state(focused=False),
            AppKit.NSWindowDidMiniaturizeNotification: lambda: state(minimized=True),
            AppKit.NSWindowDidDeminiaturizeNotification: lambda: state(minimized=False),
        })

    #
    # Fullscreen
    #
    # In fullscreen, AppKit moves the traffic lights into a title bar of its own that
    # slides down over the page when the pointer reaches the screen's top.
    # The page draws its own buttons there instead

    def _hide_fullscreen_bar(self) -> None:
        window = self._ns_window
        if self._traffic_lights is None or self._closed:
            return

        # AppKit resets these for fullscreen
        window.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        window.setTitlebarAppearsTransparent_(True)

        close = window.standardWindowButton_(AppKit.NSWindowCloseButton)
        bar = close.window() if close is not None else None
        if not window.styleMask() & AppKit.NSWindowStyleMaskFullScreen or bar is None or bar == window:
            bar = None

        if self._fullscreen_bar is not None and self._fullscreen_bar[0] != bar:
            self._restore_fullscreen_bar()
        if bar is None:
            return

        if self._fullscreen_bar is None:
            self._fullscreen_bar = (bar, bool(bar.contentView().isHidden()), bool(bar.ignoresMouseEvents()))
        bar.contentView().setHidden_(True)
        bar.setIgnoresMouseEvents_(True)

    def _restore_fullscreen_bar(self) -> None:
        if self._fullscreen_bar is None:
            return

        bar, hidden, ignores = self._fullscreen_bar
        self._fullscreen_bar = None
        bar.contentView().setHidden_(hidden)
        bar.setIgnoresMouseEvents_(ignores)

    def _observe(self, center: Any, sender: Any, handlers: dict[str, Callable[[], None]]) -> None:
        for name, handler in handlers.items():
            def notified(notification: Any, handler: Callable[[], None] = handler) -> None:
                try:
                    handler()
                except Exception:
                    logger.exception("a window notification handler failed")

            observer = center.addObserverForName_object_queue_usingBlock_(
                name, sender, Foundation.NSOperationQueue.mainQueue(), notified)
            self._observers.append((center, observer))

    def backdrops(self) -> tuple[Backdrop, ...]:
        return Backdrop.NONE, Backdrop.CLEAR

    def set_backdrop(self, backdrop: Backdrop) -> None:
        AppHelper.callAfter(self._apply_backdrop)

    def _apply_backdrop(self) -> None:
        window, web_view = self._ns_window, self._web_view
        if window is None or web_view is None:
            return

        see_through = self._owner.backdrop is not Backdrop.NONE
        window.setOpaque_(not see_through)
        window.setBackgroundColor_(AppKit.NSColor.clearColor() if see_through else self._background)
        web_view.setValue_forKey_(not see_through, "drawsBackground")
        window.setIgnoresMouseEvents_(False)
        # The shadow follows the window's opaque parts
        window.invalidateShadow()

    #
    # Light or dark
    #
    # AppKit draws the traffic lights for it and WKWebView takes
    # prefers-color-scheme from it. Theme.SYSTEM clears it, so the
    # window follows the system again.

    def set_theme(self, theme: Theme) -> None:
        AppHelper.callAfter(self._apply_theme)

    def _apply_theme(self) -> None:
        window = self._ns_window
        if window is None:
            return

        names = {Theme.LIGHT: AppKit.NSAppearanceNameAqua, Theme.DARK: AppKit.NSAppearanceNameDarkAqua}
        name = names.get(self._owner.theme)
        window.setAppearance_(AppKit.NSAppearance.appearanceNamed_(name) if name else None)
        self._report_dark()

    def _report_dark(self) -> None:
        appearance = self._ns_window.effectiveAppearance()
        best = appearance.bestMatchFromAppearancesWithNames_([AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua])
        self._owner._handle_state(dark=best == AppKit.NSAppearanceNameDarkAqua)

    def _observe_appearance(self) -> None:
        # Posted when the system switches between light and dark (also by Auto at sunset).
        # The window's effective appearance follows a moment later
        def changed() -> None:
            if self._owner.theme is Theme.SYSTEM and not self._closed:
                AppHelper.callLater(0.1, self._report_dark)

        self._observe(Foundation.NSDistributedNotificationCenter.defaultCenter(), None,
                      {"AppleInterfaceThemeChangedNotification": changed})

    #
    # Closing
    #

    def _closed_event(self) -> None:
        if self._ready_timer is not None:
            self._ready_timer.cancel()

        for center, observer in self._observers:
            center.removeObserver_(observer)
        self._observers.clear()

        with _windows_lock:
            for number, backend in list(_windows.items()):
                if backend is self:
                    del _windows[number]

        super()._closed_event()


def _on_gui_thread(function: Callable[[], Any]) -> Any:
    if threading.current_thread() is threading.main_thread():
        return function()
    done = threading.Event()
    result: list[Any] = []

    def run() -> None:
        try:
            result.append(function())
        finally:
            done.set()

    AppHelper.callAfter(run)
    done.wait()
    if not result:
        raise RuntimeError("AppKit couldn't say.")
    return result[0]


def backend_of(ns_window: Any) -> MacOsWindowBackend | None:
    if ns_window is None:
        return None
    with _windows_lock:
        return _windows.get(ns_window.windowNumber())


def _install_monitor() -> None:
    global _monitor
    if _monitor is not None:
        return

    def pressed(event: Any) -> Any:
        window = event.window()
        if window is None:
            return event

        with _windows_lock:
            backend = _windows.get(window.windowNumber())

        try:
            # A press the title bar took never reaches the page
            if backend is not None and backend.mouse_down(event):
                return None
        except Exception:
            logger.exception("the title bar couldn't handle a press")
        return event

    _monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskLeftMouseDown, pressed)


def run(*, debug: bool, icon: Path | None) -> None:
    pywebview_backend.run(MacOsWindowBackend.GUI, debug=debug, icon=dock_icon(icon))
