# NOTE:
#    THis is heavily tailored towards pywebview and not the native
#    OS components, meaning I had to modify much stuff to achieve the
#    similar effects I had while using the OS native forms, components, etc.
#    But it was still fun dealing with this! (Now we ned to do the same for MacOS and Linux :O)

import ctypes
import json
import logging
import os
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any

from ...windowing.options import ChromeMode, Theme, WindowPlatform
from ...windowing.regions import TitleBarRegions
from .. import pywebview_backend
from . import message_box, native
from .caption import CaptionButtons
from .frame import Frame, show_system_menu
from .title_bar import TitleBarLayout, TitleBarWindow, lay_out

logger = logging.getLogger(__name__)

# How long a loaded page may take to draw (and say so) before its window shows anyway
READY_TIMEOUT = 0.5

# How long capturing the page for the splash may take before the window shows without it
CAPTURE_TIMEOUT = 0.25

# How long the splash stays up after the page has reported its first frames on screen,
# to be safe that WebView2's frames have really arrived
SPLASH_MARGIN = 0.15
# How long the splash stays up at most if the page never reports its frames
SPLASH_TIMEOUT = 1.0

# Chromium converts the colors to the display color profile, but GDI, of course, does not.
# The problem is, the frame draws the caption buttons with GDI. So we force the color profile here.
BROWSER_ARGUMENTS = "--disable-features=ElasticOverscroll --force-color-profile=srgb"


class Win32WindowBackend(pywebview_backend.PywebviewWindowBackend):
    """A pywebview window (WinForms with WebView2) with the page's title bar as its own"""

    GUI = "edgechromium"
    platform = WindowPlatform.WIN32
    supports_custom_chrome = True

    def __init__(self, owner: Any, options: Any) -> None:
        super().__init__(owner, options)
        self._hwnd = 0                                       # window handle
        self._frame: Frame | None = None                     # window procedure (wndProc)
        self._caption: CaptionButtons | None = None          # the caption buttons
        self._title_bar: TitleBarWindow | None = None        # the invisible window over the page's title bar
        self._regions: TitleBarRegions | None = None         # where the page last said its title bar is
        self._page_cut: tuple | None = None                  # how WebView2's window is cut right now (_cut_page)
        self._preferences_handler: Any = None                # the theme change handler, removed on close
        self._ready_timer: threading.Timer | None = None     # READY_TIMEOUT
        self._revealed = False                               # whether the window has been shown yet

        # How far down the page starts in the client area.
        # On Windows 10 that's 1 pixel while the window is restored
        # to leave room for the top border (see layout())
        self.page_top = 0

        # The title bar in window pixels
        self.title_bar_layout: TitleBarLayout | None = None

        # The page captured before the window shows. This is a cheap trick
        # to convey the roughly 50ms of the content not being drawn by WebView2.
        # Why do we need that? Because it takes time until DMW and WebView2 got their
        # shit together to finally render the content beside the window crame.
        self.splash: tuple[int, int, int] | None = None

    def show(self) -> None:
        if self._shown:
            super().show()
        else:
            self._show_once()

    def set_title_bar_regions(self, regions: TitleBarRegions | None) -> None:
        self._regions = regions

        # The layout has to happen on the GUI thread
        if self._title_bar is not None:
            self._title_bar.request_layout()

    def page_ready(self) -> None:
        """Whether the page has fully drawn - meaning all its content + title bar are ready - or not"""
        self._show_once()

    def toggle_fullscreen(self) -> None:
        super().toggle_fullscreen()
        fullscreen = bool(self.native_window.is_fullscreen)

        # pywebview resets the corners when it leaves fullscreen,
        # and without a caption Win 11 default corners are square
        if not fullscreen and native.WINDOWS_11:
            native.set_window_attribute(self._hwnd, native.DWMWA_WINDOW_CORNER_PREFERENCE, native.DWMWCP_ROUND)

        self._owner._handle_state(fullscreen=fullscreen)
        # A fullscreen window has no title bar
        if self._title_bar is not None:
            self._title_bar.request_layout()

    def message_box(self, box: Any) -> int:
        from System import Action

        form = self.native_window
        title = self._owner.application.name
        if form is None or self._closed or not form.InvokeRequired:
            # On the GUI thread already, or without a window
            return message_box.show(self._hwnd if form is not None else 0, title, box)

        done = threading.Event()
        answer = [box.dismissed]

        def show() -> None:
            try:
                answer[0] = message_box.show(self._hwnd, title, box)
            except Exception:
                logger.exception("couldn't ask the user")
            finally:
                done.set()

        try:
            form.BeginInvoke(Action(show))
        except Exception:
            # The form is gone
            return box.dismissed
        self._wait_for_answer(done)
        return answer[0]

    def show_devtools(self) -> None:
        def show() -> None:
            core = self.native_window.webview.CoreWebView2
            if core is not None:
                core.OpenDevToolsWindow()

        self._on_gui_thread(show)

    def _send_script(self, script: str) -> None:
        """Run a script in the page without waiting for it"""

        # pywebview's run_js waits for the script's result and can wait forever
        # if the script fails, so ExecuteScriptAsync is called directly instead.
        #
        # WebView2 may only be used on the GUI thread, so the
        # call is queued there with BeginInvoke.

        from System import Action

        form = self.native_window
        if form is None:
            return

        def run() -> None:
            try:
                core = form.webview.CoreWebView2
                if core is not None:
                    core.ExecuteScriptAsync(script)
            except Exception:
                logger.debug("couldn't deliver a script to the page", exc_info=True)

        try:
            form.BeginInvoke(Action(run))
        except Exception:
            # The form is gone
            pass

    # Startup
    #
    # The goal is a window that opens like any other Windows app.
    # Because NORMALLY (under Windows 11 confirmed) the screen would start to flicker
    # when the window appears. And THEN the window itself would flicker before its
    # content gets drawn.
    #
    # We want the "smooth" feel of a native Win app opening and closing, so we need
    # to do some work for it. The content has to be drawn IMMEDIATELY.
    #
    # pywebview's own hidden start doesn't do that. It shows the window at opacity 0 and
    # hides it again, which makes the screen flicker and plays the opening animation
    # while nothing can see it.
    #
    # So instead, pywebview's first Show() is held back, and WebView2 is made to draw while
    # the window is still hidden (like Electron's show: false. yes I got it from there).
    # The window then shows once fully ready after the page has drawn.
    #
    # One problem is left tho.
    # WebView2 only hands its frames to Windows once the window is VISIBLE,
    # which is a few frames into the opening animation, so the window would be
    # empty for those frames (remember the splash from above?).
    #
    # That's what the splash is for. So just before the window
    # shows, the page is captured into an image. The frame paints that image, and
    # WebView2 is cut away until its own frames are on screen.
    # Then WebView2 comes back, over the identical image,
    # and nobody can tell the difference.
    #
    # This may appear a bit cheap, but this is better than staring at a black screen
    # for 50ms and wondering why the heck nothing is rendering.
    #
    # So the order is:
    #   _before_show
    #   _webview_initialized
    #   _page_loaded              (page loading)
    #   _present                  (page is ready)
    #   _capture_page
    #   _captured
    #   _reveal
    #   _page_presented           (the page's frames are on screen)
    #   _drop_splash

    def _starts_hidden(self) -> bool:
        # hold back the first Show()
        return False

    def _before_show(self) -> None:
        """Get the window ready before pywebview shows it for the first time"""
        # pywebview calls this on the GUI thread, between creating the window and its
        # first Show(). It has started WebView2 already, which finishes on the message loop.

        form = self.native_window
        self._hwnd = int(form.Handle.ToInt64())
        if self._options.dialog_for is not None:
            self._make_dialog()
        if self._owner.chrome is ChromeMode.CUSTOM:
            self._install_chrome()
        # After the title bar, so the caption buttons get the theme too
        self._install_theme()

        if self._options.show_when_loaded:
            # Hold back pywebview's Show()
            form.Show = lambda: None
            form.webview.CoreWebView2InitializationCompleted += self._webview_initialized
            # The page calls this once its frames are on screen (see _reveal())
            self._owner.content.expose("window.presented", self._page_presented)

    def _make_dialog(self) -> None:
        hwnd = self._hwnd
        owner = int(self._options.dialog_for.native_window.Handle.ToInt64())
        native.SetWindowLongPtrW(hwnd, native.GWLP_HWNDPARENT, owner)
        style = native.GetWindowLongPtrW(hwnd, native.GWL_STYLE)
        native.SetWindowLongPtrW(hwnd, native.GWL_STYLE, style & ~(native.WS_MINIMIZEBOX | native.WS_MAXIMIZEBOX))
        extended = native.GetWindowLongPtrW(hwnd, native.GWL_EXSTYLE)
        native.SetWindowLongPtrW(hwnd, native.GWL_EXSTYLE, extended & ~native.WS_EX_APPWINDOW)

    def _webview_initialized(self, sender: Any, args: Any) -> None:
        """Let WebView2 draw while the window is still hidden"""
        # WebView2 only draws while its controller is visible.
        # The WinForms control makes it visible together with the window,
        # which would be too late, so it's made visible here already.
        #
        # The controller is private, so it takes reflection to get to it.

        if not args.IsSuccess or self._shown:
            return

        try:
            from System.Reflection import BindingFlags

            field = sender.GetType().GetField("_coreWebView2Controller", BindingFlags.Instance | BindingFlags.NonPublic)
            controller = field.GetValue(sender) if field is not None else None
            if controller is not None:
                controller.IsVisible = True
        except Exception:
            logger.debug("couldn't let WebView2 draw before the window shows", exc_info=True)

    def _page_loaded(self) -> None:
        if not self._options.show_when_loaded or self._shown:
            return

        # timeout for whenever the page decides to never tell us anything
        # (stinky little bastard)
        self._ready_timer = threading.Timer(READY_TIMEOUT, self._show_once)
        self._ready_timer.daemon = True
        self._ready_timer.start()

    def _present(self) -> None:
        """Show the window for the first time"""
        from System import Action

        if self._ready_timer is not None:
            self._ready_timer.cancel()
        self.native_window.BeginInvoke(Action(self._capture_page))

    def _capture_page(self) -> None:
        """Capture the page into an image for the splash and then show the window"""
        from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
        from System import Action
        from System.IO import MemoryStream
        from System.Threading.Tasks import Task, TaskScheduler

        form = self.native_window
        core = form.webview.CoreWebView2 if self._title_bar is not None else None
        if core is None:
            self._reveal()
            return

        # The capture is a PNG written into a stream.
        # _captured() continues on the GUI thread once it's done
        stream = MemoryStream()
        try:
            task = core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream)
            task.ContinueWith(Action[Task](lambda done: self._captured(done, stream)),
                              TaskScheduler.FromCurrentSynchronizationContext())
        except Exception:
            logger.debug("couldn't capture the page for the splash", exc_info=True)
            self._reveal()
            return

        # If capturing takes too long, the window shows without the splash
        threading.Timer(CAPTURE_TIMEOUT, lambda: self._on_gui_thread(self._reveal)).start()

    def _captured(self, task: Any, stream: Any) -> None:
        """Turn the capture into the splash and show the window"""
        try:
            if not self._revealed and not task.IsFaulted and stream.Length:
                from System.Drawing import Bitmap

                stream.Position = 0
                bitmap = Bitmap(stream)
                self.splash = (int(bitmap.GetHbitmap().ToInt64()), bitmap.Width, bitmap.Height)
                bitmap.Dispose()
        except Exception:
            logger.debug("couldn't use the page's capture", exc_info=True)
        finally:
            stream.Dispose()

        self._reveal()

    def _reveal(self) -> None:
        """Show the window together with a splash"""
        if self._revealed:
            return

        self._revealed = True
        form = self.native_window

        # With a splash WebView2 is cut away completely (see _cut_page())
        # so the frame's splash is all that shows
        if self.splash is not None:
            self._page_cut = None
            self.layout()

        # Take away the stand-in from _before_show(), so Show() works again
        try:
            del form.Show
        except AttributeError:
            pass
        form.Show()

        # Paint the window right now, so the splash is in it before Windows puts the
        # window's first frame on screen
        native.UpdateWindow(self._hwnd)
        form.Activate()

        if self.splash is not None:
            # The page counts four frames on-screen before calling window.presented (_page_presented).
            # SPLASH_TIMEOUT drops the splash in case it never does
            self._send_script(
                "(() => { let n = 4; const f = () => --n ? requestAnimationFrame(f)"
                " : window.pywebview.api.call('window.presented'); requestAnimationFrame(f); })()")
            threading.Timer(SPLASH_TIMEOUT, lambda: self._on_gui_thread(self._drop_splash)).start()

    def _page_presented(self) -> None:
        # The page's frames are on screen
        # Keep the splash a tid bit longer to be sure tho
        threading.Timer(SPLASH_MARGIN, lambda: self._on_gui_thread(self._drop_splash)).start()

    def _drop_splash(self) -> None:
        """Bring WebView2 back and remove the splash"""
        if self.splash is None:
            return
        hbitmap = self.splash[0]
        self.splash = None

        self._page_cut = None
        self.layout()
        native.DeleteObject(hbitmap)
        native.InvalidateRect(self._hwnd, None, False)

    def _on_gui_thread(self, function: Any) -> None:
        """Run a function on the GUI thread, without waiting for it"""
        from System import Action

        try:
            self.native_window.BeginInvoke(Action(function))
        except Exception:
            # The form is gone
            pass

    def _closed_event(self) -> None:
        if self._ready_timer is not None:
            self._ready_timer.cancel()

        if self.splash is not None:
            native.DeleteObject(self.splash[0])
            self.splash = None

        # SystemEvents lives as long as the process, so the handler has to be removed.
        # Otherwise, it would keep calling into the closed window
        if self._preferences_handler is not None:
            from Microsoft.Win32 import SystemEvents

            SystemEvents.UserPreferenceChanged -= self._preferences_handler
            self._preferences_handler = None

        super()._closed_event()

    #
    # The custom title bar, on the GUI thread
    #

    def _install_chrome(self) -> None:
        """Try to make the page's title bar the "main title bar". Otherwise, fall back to the default Windows one."""
        try:
            self._install()
        except Exception:
            logger.exception("couldn't give the window the page's title bar")
            # Undo what was done
            # Windows wins this round and is allowed to keep its own title bar
            if self._frame is not None:
                self._frame.remove()
            self._frame = self._caption = self._title_bar = None
            self.supports_custom_chrome = False

    def _install(self) -> None:
        from System.Drawing import Color

        form = self.native_window
        self._hwnd = hwnd = int(form.Handle.ToInt64())
        self._focus_page_plainly(form.webview)

        # Until Chromium has drawn, the WebView2 control
        # shows its own background color
        color = self._options.background_color
        red, green, blue = color >> 16 & 0xFF, color >> 8 & 0xFF, color & 0xFF
        form.webview.BackColor = Color.FromArgb(255, red, green, blue)

        # What the frame paints behind the page.
        # On Windows 10, Windows only draws the window's top border
        # over black pixels. So the frame paints black, and the page
        # starts a pixel lower to leave that row free (see layout())
        background = 0 if not native.WINDOWS_11 else native.colorref(red, green, blue)

        self._caption = CaptionButtons(hwnd, self)
        self._frame = Frame(hwnd, self, self._caption, background)
        self._title_bar = TitleBarWindow(hwnd, self)

        # Round corners on Windows 11 (without a caption, its default corners are square)
        if native.WINDOWS_11:
            native.set_window_attribute(hwnd, native.DWMWA_WINDOW_CORNER_PREFERENCE, native.DWMWCP_ROUND)

        self.layout()

    @staticmethod
    def _focus_page_plainly(webview: Any) -> None:
        """Stop WebView2 from focusing the page's first button when the window activates"""
        # This is basically the other half of the annoying focusing
        # fucking Windows does when opening the window (see chrome.js)
        #
        # TLDR; we suppress windows from fucking focusing the menu button on
        # startup.

        webview.TabStop = False
        try:
            from Microsoft.Web.WebView2.Core import CoreWebView2MoveFocusReason
            from System.Reflection import BindingFlags

            field = webview.GetType().GetField("_lastMoveFocusReason", BindingFlags.Instance | BindingFlags.NonPublic)
            if field is not None:
                field.SetValue(webview, CoreWebView2MoveFocusReason.Programmatic)
        except Exception:
            logger.debug("couldn't reset WebView2's focus reason", exc_info=True)

    def layout(self) -> None:
        """Lay out the content to the window's size and state"""
        # The frame calls this whenever the window's size changes and the title bar window
        # whenever the page reports a new title bar

        if self._title_bar is None:
            return

        form = self.native_window
        maximized = bool(native.IsZoomed(self._hwnd))
        fullscreen = bool(getattr(form, "is_fullscreen", False))

        # Windows 10's top border needs the client area's top row (see _install()), but
        # only while the window is restored. Padding pushes WebView2 down by that row
        if not native.WINDOWS_11:
            page_top = 0 if maximized or fullscreen else 1
            if page_top != self.page_top:
                from System.Windows.Forms import Padding

                self.page_top = page_top
                form.Padding = Padding(0, page_top, 0, 0)

        client = wintypes.RECT()
        native.GetClientRect(self._hwnd, ctypes.byref(client))

        # A fullscreen window has no title bar
        regions = None if fullscreen else self._regions
        layout = lay_out(regions, self.page_top, client.right, native.resize_border(self._hwnd), maximized)

        self.title_bar_layout = layout
        self._title_bar.place(layout)
        self._cut_page(form, layout, client)
        self._caption.place(layout)

    def _cut_page(self, form: Any, layout: TitleBarLayout | None, client: wintypes.RECT) -> None:
        caption = layout.caption if layout is not None else None

        # Setting a region makes WebView2 redraw, and layout() runs on every size change,
        # so nothing is done when the cut hasn't changed
        if self.splash is not None:
            cut = ("splash",)
        elif caption is not None:
            cut = (caption, client.right, client.bottom, self.page_top)
        else:
            cut = None
        if cut == self._page_cut:
            return
        self._page_cut = cut

        page = int(form.webview.Handle.ToInt64())

        # We cut away the webview entirely while the splash is up
        if self.splash is not None:
            nothing = native.CreateRectRgn(0, 0, 0, 0)
            if not native.SetWindowRgn(page, nothing, True):
                native.DeleteObject(nothing)
            return

        # WebView2 gets its whole window back if there are no caption buttons
        if caption is None:
            native.SetWindowRgn(page, None, True)
            return

        # Cut the caption buttons out. WebView2 starts page_top pixels lower than the
        # client area, which the caption's box is measured in
        left, top, right, bottom = caption
        region = native.CreateRectRgn(0, 0, client.right, client.bottom - self.page_top)
        hole = native.CreateRectRgn(left, top - self.page_top, right, bottom - self.page_top)
        native.CombineRgn(region, region, hole, native.RGN_DIFF)
        native.DeleteObject(hole)

        # Once set, the region belongs to the system
        if not native.SetWindowRgn(page, region, True):
            native.DeleteObject(region)

    #
    # Light or dark
    #

    def set_theme(self, theme: Theme) -> None:
        """Set the theme of the window"""
        self._on_gui_thread(self._apply_theme)

    def _install_theme(self) -> None:
        """Install the window's theme (also on settings change)"""
        from Microsoft.Win32 import SystemEvents, UserPreferenceChangedEventHandler

        self._preferences_handler = UserPreferenceChangedEventHandler(
            lambda sender,
            args: self._on_gui_thread(self._apply_theme)
        )

        SystemEvents.UserPreferenceChanged += self._preferences_handler

        # The page's color scheme can only be set once WebView2 is ready
        self.native_window.webview.CoreWebView2InitializationCompleted += \
            lambda sender, args: self._apply_page_theme()

        self._apply_theme()

    def _apply_theme(self) -> None:
        """Actually MAKE the window follow the installed theme"""
        theme = self._owner.theme
        dark = theme is Theme.DARK or (theme is Theme.SYSTEM and native.system_uses_dark_mode())

        native.use_dark_mode(self._hwnd, dark)

        # The window menu. Menus are themed for the whole process, not for each window,
        # and ALLOW_DARK follows the system by itself

        # Make the menu follow the theme (since they follow the process theming)
        menu_modes = {
            Theme.SYSTEM: native.ALLOW_DARK,
            Theme.DARK: native.FORCE_DARK,
            Theme.LIGHT: native.FORCE_LIGHT
        }
        native.set_menu_mode(menu_modes[theme])

        if self._caption is not None:
            self._caption.set_dark(dark)
        self._apply_page_theme()

        self._owner._handle_state(dark=dark)

    def _apply_page_theme(self) -> None:
        """Apply the theme to the page"""
        # WebView2 has a setting for this (CoreWebView2Profile.PreferredColorScheme)
        # which, of course, does not work for me (tested on version Edge 155).
        # So we again have to do it ourselves...

        theme = self._owner.theme
        try:
            from Microsoft.Web.WebView2.Core import CoreWebView2PreferredColorScheme

            core = self.native_window.webview.CoreWebView2
            if core is None:
                return

            # An empty value (and Auto) means follow the system
            value = {Theme.SYSTEM: "", Theme.LIGHT: "light", Theme.DARK: "dark"}[theme]
            features = json.dumps({"features": [{"name": "prefers-color-scheme", "value": value}]})
            core.CallDevToolsProtocolMethodAsync("Emulation.setEmulatedMedia", features)

            schemes = {
                Theme.SYSTEM: CoreWebView2PreferredColorScheme.Auto,
                Theme.LIGHT: CoreWebView2PreferredColorScheme.Light,
                Theme.DARK: CoreWebView2PreferredColorScheme.Dark,
            }
            core.Profile.PreferredColorScheme = schemes[theme]
        except Exception:
            logger.debug("couldn't set the page's color scheme", exc_info=True)

    #
    # Called by the frame and the caption buttons
    #

    def activated(self, active: bool) -> None:
        self._owner._handle_state(focused=active)

    def caption_changed(self, hover: str | None, pressed: str | None) -> None:
        self._owner._handle_caption(hover, pressed)

    def caption_clicked(self, button: str) -> None:
        if button == "minimize":
            native.ShowWindow(self._hwnd, native.SW_MINIMIZE)
        elif button == "maximize":
            native.ShowWindow(self._hwnd, native.SW_RESTORE if native.IsZoomed(self._hwnd) else native.SW_MAXIMIZE)
        else:
            # Closing goes through WM_CLOSE
            native.PostMessageW(self._hwnd, native.WM_CLOSE, 0, 0)

    def show_system_menu(self, x: int, y: int) -> None:
        """Open the window menu (Restore, Move, Size, ...) at a point on the screen"""
        if self._frame is not None:
            show_system_menu(self._hwnd, self._frame.menu, x, y)


def run(*, debug: bool, icon: Path | None) -> None:
    # Apply the theme FIRST, because for the popup menus, it is applied PROCESS
    # wide. So we need to do that before the window is made.
    native.set_menu_mode()

    # WebView2 reads extra Chromium arguments from this variable.
    # Ours are added to whatever is in it already
    existing = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "").strip()
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"{existing} {BROWSER_ARGUMENTS}".strip()

    pywebview_backend.run(Win32WindowBackend.GUI, debug=debug, icon=icon)
