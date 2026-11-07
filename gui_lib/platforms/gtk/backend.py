# man it was almost as worse as windows
# had to test this on several distros, namely:
#
# Ubuntu 22.04
# Ubuntu 24.04 (tested the most with)
# Debian 13
# Fedora
# Arch

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

from ...windowing.options import Backdrop, ChromeMode, Theme, WindowPlatform
from .. import pywebview_backend
from ..file_drags import uri_paths

if TYPE_CHECKING:
    from ...windowing.dialogs import MessageBox
    from ...windowing.regions import TitleBarRegions

logger = logging.getLogger(__name__)

# How long a loaded page may take to draw before its window shows anyway.
# Pysar's UI takes about 0.6s without a GPU, so this leaves it some room
READY_TIMEOUT = 1.0

# Themes give title bars a minimum height. The empty one has to have none
TITLE_BAR_CLASS = "gui-lib-titlebar"
TITLE_BAR_CSS = f".{TITLE_BAR_CLASS} {{ min-height: 0; padding: 0; margin: 0; border: none; }}".encode()

SEE_THROUGH_CLASS = "gui-lib-see-through"
SEE_THROUGH_CSS = f".{SEE_THROUGH_CLASS} {{ background-color: transparent; background-image: none; }}".encode()
_css_installed = False

# Without a compositor (X11 with i3, Openbox, ...) GTK draws a solid frame instead of a
# shadow.
#
# Ubuntu's Yaru keeps that frame white even in dark mode, so it gets the window's
# own background. The frame is drawn by the window's decoration node,
# which is what the rule has to name
SOLID_FRAME_CSS = ("window.solid-csd, window.solid-csd decoration "
                   "{{ background-color: {color}; border-color: {color}; box-shadow: none; }}")


class GtkWindowBackend(pywebview_backend.PywebviewWindowBackend):
    GUI = "gtk"
    platform = WindowPlatform.GTK
    supports_custom_chrome = True

    def __init__(self, owner: Any, options: Any) -> None:
        super().__init__(owner, options)
        self._gtk_window: Any = None
        self._webview: Any = None
        self._regions: TitleBarRegions | None = None
        self._handlers: list[tuple[Any, int]] = []

        # Where a press on the title bar was
        self._press: tuple[float, float] | None = None

        self._caption_layout: dict[str, list[str]] = {"left": [], "right": ["minimize", "maximize", "close"]}
        # Whether a compositor can show what's behind the window
        self._composited = False
        self._ready_timer: threading.Timer | None = None
        self._mapped = False

    def _frameless(self) -> bool:
        # This is NOT frameless. We only remove the title bar (see _install_chrome).
        return False

    def chrome_details(self) -> dict[str, Any]:
        if self._owner.chrome is not ChromeMode.CUSTOM:
            return {}

        # The page draws the caption buttons but the desktop env says
        # which ones and on which side (GNOME has close only by default)
        return {"captionButtons": self._caption_layout}

    #
    # Startup
    #
    # Like on macOS, WebKit doesn't draw a page whose window isn't on screen.
    # So the window goes on screen once the page has loaded, with the page itself still
    # invisible (pywebview starts it that way).
    # The page appears once chrome.js says it has drawn (window.ready).
    # Until then the window shows its background color.

    def _before_show(self) -> None:
        from webview.platforms.gtk import BrowserView

        self._gtk_window = self.native_window
        self._webview = BrowserView.instances[self._window.uid].webview
        self._caption_layout = _caption_layout()
        self._connect(self._gtk_window, "window-state-event", self._state_changed)
        # pywebview's handler runs first and adds the paths to its list
        self._connect(self._webview, "drag-data-received", self._drag_data)
        self._connect(self._webview, "drag-leave", lambda webview, context, time: self._dragged.left())
        if self._options.dialog_for is not None:
            self._make_dialog()
        self._apply_theme()
        self._prepare_backdrop()
        if self._owner.chrome is ChromeMode.CUSTOM:
            self._install_chrome()

    def _make_dialog(self) -> None:
        """Tell the window manager the window is a dialog of its main window.
        GNOME and KDE then keep it above that window and center it there.
        """
        window = self._gtk_window
        window.set_transient_for(self._options.dialog_for.native_window)
        window.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        window.set_skip_taskbar_hint(True)

    def _install_chrome(self) -> None:
        try:
            _install_css()
            # GTK keeps drawing the decorations (shadow, rounded corners, resize edges)
            # around a window with a title bar widget of its own, even an empty one
            title_bar = Gtk.Box()
            title_bar.get_style_context().add_class(TITLE_BAR_CLASS)
            self._gtk_window.set_titlebar(title_bar)

            frame = Gtk.CssProvider()
            frame.load_from_data(SOLID_FRAME_CSS.format(color=f"#{self._options.background_color:06x}").encode())
            self._gtk_window.get_style_context().add_provider(frame, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            self._connect(self._webview, "button-press-event", self._pressed)
            self._connect(self._webview, "motion-notify-event", self._moved)
            self._connect(self._webview, "button-release-event", self._released)
        except Exception:
            logger.exception("couldn't give the window the page's title bar")
            self.supports_custom_chrome = False

    def _page_loaded(self) -> None:
        if not self._options.show_when_loaded or self._shown:
            return

        GLib.idle_add(self._map_without_page)
        self._ready_timer = threading.Timer(READY_TIMEOUT, self._show_once)
        self._ready_timer.daemon = True
        self._ready_timer.start()

    def _map_without_page(self) -> bool:
        if self._mapped or self._closed:
            return False

        self._mapped = True
        # keep the page hidden until the page has drawn
        self._webview.set_opacity(0.0)
        self._gtk_window.show_all()
        return False

    def page_ready(self) -> None:
        self._show_once()

    def _present(self) -> None:
        if self._ready_timer is not None:
            self._ready_timer.cancel()
        GLib.idle_add(self._reveal)

    def _reveal(self) -> bool:
        if self._closed:
            return False

        if not self._mapped:
            # The page never loaded
            self._mapped = True
            self._gtk_window.show_all()
        self._webview.set_opacity(1.0)
        self._gtk_window.present()
        return False

    def show(self) -> None:
        if self._shown:
            GLib.idle_add(self._gtk_window.present)
        else:
            self._show_once()

    def _patterns(self, extension: str) -> list[str]:
        # GTK 3 matches a file dialog's patterns case by case, so *.nds alone would hide a
        # GAME.NDS (macOS and Windows don't care about the case)
        return list(dict.fromkeys((f"*.{extension}", f"*.{extension.lower()}", f"*.{extension.upper()}")))

    def message_box(self, box: "MessageBox") -> int:
        if threading.current_thread() is threading.main_thread():
            dialog = self._dialog(box)
            response = dialog.run()
            dialog.destroy()
            return self._answer(box, response)

        done = threading.Event()
        answer = [box.dismissed]

        def responded(dialog: Any, response: int) -> None:
            dialog.destroy()
            answer[0] = self._answer(box, response)
            done.set()

        def show() -> bool:
            try:
                dialog = self._dialog(box)
                dialog.connect("response", responded)
                dialog.show()
            except Exception:
                logger.exception("couldn't ask the user")
                done.set()
            return False

        GLib.idle_add(show)
        self._wait_for_answer(done)
        return answer[0]

    def _dialog(self, box: "MessageBox") -> Any:
        dialog = Gtk.MessageDialog(
            transient_for=self._gtk_window if not self._closed else None,
            modal=True,
            destroy_with_parent=True,
            message_type=Gtk.MessageType.WARNING if box.warning else Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=box.message,
        )
        if box.detail:
            dialog.format_secondary_text(box.detail)
        for index in box.order(default_first=False):
            # Labels take mnemonics so a "_" would underline the letter after it
            button = dialog.add_button(box.buttons[index].replace("_", "__"), index)
            if index == box.destructive:
                # Adwaita's red button
                button.get_style_context().add_class("destructive-action")
        dialog.set_default_response(box.default)
        return dialog

    @staticmethod
    def _answer(box: "MessageBox", response: int) -> int:
        return response if 0 <= response < len(box.buttons) else box.dismissed

    def _drag_data(self, webview: Any, context: Any, x: int, y: int, data: Any, info: int, time: int) -> None:
        try:
            self._dragged.heard(context, uri_paths(data.get_uris() or (data.get_text() or "").splitlines()))
        except Exception:
            logger.exception("couldn't read the dragged files")

    def show_devtools(self) -> None:
        def show() -> bool:
            self._webview.get_inspector().show()
            return False

        GLib.idle_add(show)

    #
    # title bar
    #

    def set_title_bar_regions(self, regions: "TitleBarRegions | None") -> None:
        self._regions = regions

    def _pressed(self, webview: Any, event: Any) -> bool:
        regions = self._regions
        # The webview's coordinates are the page's CSS pixels
        if regions is None or self._owner.state.fullscreen or not regions.is_drag(event.x, event.y):
            return False

        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self._press = None
            if event.button == 1:
                self._double_click(event)
            return True

        if event.type != Gdk.EventType.BUTTON_PRESS:
            return True

        if event.button == 1:
            # Like GTK's own title bar, the move starts once the pointer has moved a
            # little, so that a click stays a click and double clicks still work
            self._press = (event.x_root, event.y_root)
        elif event.button == 3:
            self._show_window_menu(event)
        return True

    def _moved(self, webview: Any, event: Any) -> bool:
        if self._press is None:
            return False

        if not event.state & Gdk.ModifierType.BUTTON1_MASK:
            self._press = None
            return False

        x, y = self._press
        threshold = Gtk.Settings.get_default().props.gtk_dnd_drag_threshold
        if (event.x_root - x) ** 2 + (event.y_root - y) ** 2 < threshold ** 2:
            return True

        self._press = None
        # The compositor moves the window from here and snaps it to screen edges like any other window
        self._gtk_window.begin_move_drag(1, int(x), int(y), event.time)
        return True

    def _released(self, webview: Any, event: Any) -> bool:
        if self._press is None or event.button != 1:
            return False

        self._press = None
        return True

    def _double_click(self, event: Any) -> None:
        # What the desktop is set to do (GNOME Tweaks, KDE's window behavior)
        action = Gtk.Settings.get_default().props.gtk_titlebar_double_click
        if action == "toggle-maximize":
            self._toggle_maximize()
        elif action == "minimize":
            self._gtk_window.iconify()
        elif action == "lower":
            self._gtk_window.get_window().lower()
        elif action == "menu":
            self._show_window_menu(event)

    def _show_window_menu(self, event: Any) -> None:
        window = self._gtk_window.get_window()
        if window is not None:
            window.show_window_menu(event)

    #
    # Window commands
    #

    def minimize(self) -> None:
        GLib.idle_add(self._gtk_window.iconify)

    def toggle_maximize(self) -> None:
        GLib.idle_add(self._toggle_maximize)

    def _toggle_maximize(self) -> bool:
        if self._gtk_window.is_maximized():
            self._gtk_window.unmaximize()
        else:
            self._gtk_window.maximize()
        return False

    def toggle_fullscreen(self) -> None:
        def toggle() -> bool:
            if self._owner.state.fullscreen:
                self._gtk_window.unfullscreen()
            else:
                self._gtk_window.fullscreen()
            return False

        GLib.idle_add(toggle)

    #
    # window state
    #

    def _track_window_state(self, events: Any) -> None:
        pass

    def _state_changed(self, window: Any, event: Any) -> bool:
        state = event.new_window_state
        self._owner._handle_state(
            maximized=bool(state & Gdk.WindowState.MAXIMIZED),
            fullscreen=bool(state & Gdk.WindowState.FULLSCREEN),
            minimized=bool(state & Gdk.WindowState.ICONIFIED),
            focused=bool(state & Gdk.WindowState.FOCUSED),
        )
        return False

    def _prepare_backdrop(self) -> None:
        screen = self._gtk_window.get_screen()
        visual = screen.get_rgba_visual()
        self._composited = screen.is_composited() and visual is not None
        if self._composited:
            # Only possible before the window is realized, so always, and opaque until
            # the page wants otherwise
            self._gtk_window.set_visual(visual)
        _install_css()
        self._apply_backdrop()

    def backdrops(self) -> tuple[Backdrop, ...]:
        return (Backdrop.NONE, Backdrop.CLEAR) if self._composited else (Backdrop.NONE,)

    def set_backdrop(self, backdrop: Backdrop) -> None:
        GLib.idle_add(self._apply_backdrop)

    def _apply_backdrop(self) -> bool:
        if self._gtk_window is None or self._closed:
            return False

        see_through = self._composited and self._owner.backdrop is not Backdrop.NONE
        widget = self._webview.get_parent()
        while widget is not None:
            style = widget.get_style_context()
            (style.add_class if see_through else style.remove_class)(SEE_THROUGH_CLASS)
            widget = widget.get_parent()
        if see_through:
            self._webview.set_background_color(Gdk.RGBA(0, 0, 0, 0))
        else:
            color = self._options.background_color
            self._webview.set_background_color(Gdk.RGBA(
                (color >> 16 & 0xFF) / 255, (color >> 8 & 0xFF) / 255, (color & 0xFF) / 255, 1))
        return False

    #
    # GTK's dark preference decides both meaning:
    #    - GTK draws the decorations
    #    - WebkitGTK takes prefers-clor-scheme from it
    #

    def set_theme(self, theme: Theme) -> None:
        GLib.idle_add(self._apply_theme)

    def _apply_theme(self) -> bool:
        theme = self._owner.theme
        dark = theme is Theme.DARK or (theme is Theme.SYSTEM and _system_prefers_dark())
        Gtk.Settings.get_default().props.gtk_application_prefer_dark_theme = dark
        self._owner._handle_state(dark=dark)
        return False

    #
    # Closing
    #

    def _connect(self, widget: Any, signal: str, handler: Callable[..., bool]) -> None:
        self._handlers.append((widget, widget.connect(signal, handler)))

    def _closed_event(self) -> None:
        if self._ready_timer is not None:
            self._ready_timer.cancel()

        from gi.repository import GObject

        for widget, handler in self._handlers:
            if GObject.signal_handler_is_connected(widget, handler):
                widget.disconnect(handler)
        self._handlers.clear()

        super()._closed_event()


def _caption_layout() -> dict[str, list[str]]:
    settings = Gtk.Settings.get_default()
    layout = settings.props.gtk_decoration_layout if settings is not None else None

    left, _, right = (layout or "menu:minimize,maximize,close").partition(":")
    known = ("minimize", "maximize", "close")

    return {
        "left": [name for name in left.split(",") if name in known],
        "right": [name for name in right.split(",") if name in known],
    }


def _system_prefers_dark() -> bool:
    """Whether the desktop is set to dark as reported in the settings"""
    try:
        from gi.repository import Gio

        portal = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Settings",
            None
        )

        value = portal.call_sync("Read", GLib.Variant("(ss)", ("org.freedesktop.appearance", "color-scheme")),
                                 Gio.DBusCallFlags.NONE, 500, None)

        # 1 is "prefer dark", 2 "prefer light", 0 "no preference"
        scheme = value.unpack()[0]
        while isinstance(scheme, GLib.Variant):
            scheme = scheme.unpack()
        return scheme == 1
    except Exception:
        settings = Gtk.Settings.get_default()
        name = settings.props.gtk_theme_name if settings is not None else ""
        return bool(name) and name.lower().endswith("-dark")


def _install_css() -> None:
    global _css_installed
    if _css_installed:
        return

    screen = Gdk.Screen.get_default()
    if screen is None:
        return
    for css, priority in ((TITLE_BAR_CSS, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION),
                          (SEE_THROUGH_CSS, Gtk.STYLE_PROVIDER_PRIORITY_USER)):
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(screen, provider, priority)
    _css_installed = True


def run(*, debug: bool, icon: Path | None) -> None:
    pywebview_backend.run(GtkWindowBackend.GUI, debug=debug, icon=icon)
