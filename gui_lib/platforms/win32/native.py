# you don't wanna know how this looked like in Java using FFM ...

import ctypes
import sys
import winreg
from ctypes import wintypes

user32 =ctypes.WinDLL("user32", use_last_error=True)
comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
uxtheme = ctypes.WinDLL("uxtheme")

BUILD = sys.getwindowsversion().build
WINDOWS_11 = BUILD >= 22000

HWND = wintypes.HWND
UINT = wintypes.UINT
WPARAM = wintypes.WPARAM
LPARAM = wintypes.LPARAM
BOOL = wintypes.BOOL
INT = ctypes.c_int
LRESULT = wintypes.LPARAM
UINT_PTR = ctypes.c_size_t
DWORD_PTR = ctypes.c_size_t
LONG_PTR = ctypes.c_ssize_t

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)
SUBCLASSPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM, UINT_PTR, DWORD_PTR)

WM_ACTIVATE = 0x0006
WM_SIZE = 0x0005
WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_ERASEBKGND = 0x0014
WM_STYLECHANGING = 0x007C
WM_NCDESTROY = 0x0082
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCACTIVATE = 0x0086
WM_NCMOUSEMOVE = 0x00A0
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONUP = 0x00A2
WM_NCLBUTTONDBLCLK = 0x00A3
WM_NCRBUTTONDOWN = 0x00A4
WM_NCRBUTTONUP = 0x00A5
WM_SYSCOMMAND = 0x0112
WM_NCMOUSELEAVE = 0x02A2
WM_APP = 0x8000
WA_INACTIVE = 0

HTTRANSPARENT = -1
HTCLIENT = 1
HTCAPTION = 2
HTMINBUTTON = 8
HTMAXBUTTON = 9
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTCLOSE = 20

SC_SIZE = 0xF000
SC_MOVE = 0xF010
SC_MINIMIZE = 0xF020
SC_MAXIMIZE = 0xF030
SC_CLOSE = 0xF060
SC_RESTORE = 0xF120

GWL_STYLE = -16
GWL_EXSTYLE = -20
GWLP_HWNDPARENT = -8
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_EX_APPWINDOW = 0x00040000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_CHILD = 0x40000000
WS_EX_LAYERED = 0x00080000
WS_EX_NOREDIRECTIONBITMAP = 0x00200000
LWA_ALPHA = 0x2

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
HWND_TOP = 0
SW_HIDE = 0
SW_MAXIMIZE = 3
SW_MINIMIZE = 6
SW_RESTORE = 9

TME_LEAVE = 0x0002
TME_NONCLIENT = 0x0010
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
MF_BYCOMMAND = 0x0000
MF_ENABLED = 0x0000
MF_GRAYED = 0x0001
RGN_OR = 2
RGN_DIFF = 4
SM_CYFRAME = 33
SM_CXPADDEDBORDER = 92
IDC_ARROW = 32512
# 19 before Windows 10 20H1
DWMWA_USE_IMMERSIVE_DARK_MODE = 20 if BUILD >= 18985 else 19
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2

SRCCOPY = 0x00CC0020
HALFTONE = 4
DIB_RGB_COLORS = 0
TRANSPARENT = 1
DT_CENTER = 0x0001
DT_VCENTER = 0x0004
DT_SINGLELINE = 0x0020
DT_NOCLIP = 0x0100
FW_NORMAL = 400
DEFAULT_CHARSET = 1
CLEARTYPE_QUALITY = 5

# uxtheme's unnamed exports
# SetPreferredAppMode (AllowDarkModeForApp before 1903) and FlushMenuThemes
SET_PREFERRED_APP_MODE = 135
FLUSH_MENU_THEMES = 136
# SetPreferredAppMode's modes, for the process's popup menus (like the window menu):
# like the system is set to, always dark, or always light
ALLOW_DARK = 1
FORCE_DARK = 2
FORCE_LIGHT = 3


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]


class STYLESTRUCT(ctypes.Structure):
    _fields_ = [("styleOld", wintypes.DWORD), ("styleNew", wintypes.DWORD)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("hwndTrack", HWND),
        ("dwHoverTime", wintypes.DWORD),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", BOOL),
        ("fIncUpdate", BOOL),
        ("rgbReserved", wintypes.BYTE * 32),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", UINT),
        ("style", UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", INT),
        ("cbWndExtra", INT),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


def _function(library, name, restype, *argtypes):
    function = getattr(library, name)
    function.restype = restype
    function.argtypes = argtypes
    return function


SetWindowSubclass = _function(comctl32, "SetWindowSubclass", BOOL, HWND, SUBCLASSPROC, UINT_PTR, DWORD_PTR)
RemoveWindowSubclass = _function(comctl32, "RemoveWindowSubclass", BOOL, HWND, SUBCLASSPROC, UINT_PTR)
DefSubclassProc = _function(comctl32, "DefSubclassProc", LRESULT, HWND, UINT, WPARAM, LPARAM)

DefWindowProcW = _function(user32, "DefWindowProcW", LRESULT, HWND, UINT, WPARAM, LPARAM)
RegisterClassExW = _function(user32, "RegisterClassExW", wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
CreateWindowExW = _function(
    user32, "CreateWindowExW", HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    INT, INT, INT, INT, HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
)

SetLayeredWindowAttributes = _function(
    user32,
    "SetLayeredWindowAttributes",
    BOOL,
    HWND,
    wintypes.COLORREF,
    wintypes.BYTE,
    wintypes.DWORD
)

SetWindowPos = _function(user32, "SetWindowPos", BOOL, HWND, HWND, INT, INT, INT, INT, UINT)
ShowWindow = _function(user32, "ShowWindow", BOOL, HWND, INT)
UpdateWindow = _function(user32, "UpdateWindow", BOOL, HWND)
InvalidateRect = _function(user32, "InvalidateRect", BOOL, HWND, ctypes.c_void_p, BOOL)
SetWindowRgn = _function(user32, "SetWindowRgn", INT, HWND, wintypes.HRGN, BOOL)
SendMessageW = _function(user32, "SendMessageW", LRESULT, HWND, UINT, WPARAM, LPARAM)
PostMessageW = _function(user32, "PostMessageW", BOOL, HWND, UINT, WPARAM, LPARAM)
IsZoomed = _function(user32, "IsZoomed", BOOL, HWND)
GetClientRect = _function(user32, "GetClientRect", BOOL, HWND, ctypes.POINTER(wintypes.RECT))
GetWindowRect = _function(user32, "GetWindowRect", BOOL, HWND, ctypes.POINTER(wintypes.RECT))
ScreenToClient = _function(user32, "ScreenToClient", BOOL, HWND, ctypes.POINTER(wintypes.POINT))
GetDpiForWindow = _function(user32, "GetDpiForWindow", UINT, HWND)
GetSystemMetricsForDpi = _function(user32, "GetSystemMetricsForDpi", INT, INT, UINT)
GetWindowLongPtrW = _function(user32, "GetWindowLongPtrW", LONG_PTR, HWND, INT)
SetWindowLongPtrW = _function(user32, "SetWindowLongPtrW", LONG_PTR, HWND, INT, LONG_PTR)
TrackMouseEvent = _function(user32, "TrackMouseEvent", BOOL, ctypes.POINTER(TRACKMOUSEEVENT))
GetSystemMenu = _function(user32, "GetSystemMenu", wintypes.HMENU, HWND, BOOL)
EnableMenuItem = _function(user32, "EnableMenuItem", BOOL, wintypes.HMENU, UINT, UINT)
SetMenuDefaultItem = _function(user32, "SetMenuDefaultItem", BOOL, wintypes.HMENU, UINT, UINT)
TrackPopupMenu = _function(user32, "TrackPopupMenu", BOOL, wintypes.HMENU, UINT, INT, INT, INT, HWND, ctypes.c_void_p)
LoadCursorW = _function(user32, "LoadCursorW", wintypes.HANDLE, wintypes.HINSTANCE, ctypes.c_void_p)

GetDC = _function(user32, "GetDC", wintypes.HDC, HWND)
ReleaseDC = _function(user32, "ReleaseDC", INT, HWND, wintypes.HDC)
BeginPaint = _function(user32, "BeginPaint", wintypes.HDC, HWND, ctypes.POINTER(PAINTSTRUCT))
EndPaint = _function(user32, "EndPaint", BOOL, HWND, ctypes.POINTER(PAINTSTRUCT))
FillRect = _function(user32, "FillRect", INT, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH)
DrawTextW = _function(
    user32,
    "DrawTextW",
    INT,
    wintypes.HDC,
    wintypes.LPCWSTR,
    INT,
    ctypes.POINTER(wintypes.RECT),
    UINT
)

CreateRectRgn = _function(gdi32, "CreateRectRgn", wintypes.HRGN, INT, INT, INT, INT)
CombineRgn = _function(gdi32, "CombineRgn", INT, wintypes.HRGN, wintypes.HRGN, wintypes.HRGN, INT)
DeleteObject = _function(gdi32, "DeleteObject", BOOL, wintypes.HGDIOBJ)
CreateSolidBrush = _function(gdi32, "CreateSolidBrush", wintypes.HBRUSH, wintypes.COLORREF)
CreateCompatibleDC = _function(gdi32, "CreateCompatibleDC", wintypes.HDC, wintypes.HDC)
DeleteDC = _function(gdi32, "DeleteDC", BOOL, wintypes.HDC)
CreateDIBSection = _function(
    gdi32, "CreateDIBSection", wintypes.HBITMAP, wintypes.HDC, ctypes.POINTER(BITMAPINFO), UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
)
SelectObject = _function(gdi32, "SelectObject", wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
BitBlt = _function(
    gdi32,
    "BitBlt",
    BOOL,
    wintypes.HDC,
    INT,
    INT,
    INT,
    INT,
    wintypes.HDC,
    INT,
    INT,
    wintypes.DWORD
)

StretchBlt = _function(
    gdi32,
    "StretchBlt",
    BOOL,
    wintypes.HDC,
    INT,
    INT,
    INT,
    INT,
    wintypes.HDC,
    INT,
    INT,
    INT,
    INT,
    wintypes.DWORD,
)

SetStretchBltMode = _function(gdi32, "SetStretchBltMode", INT, wintypes.HDC, INT)
CreateFontW = _function(
    gdi32,
    "CreateFontW",
    wintypes.HFONT,
    INT,
    INT,
    INT,
    INT,
    INT,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPCWSTR,
)
SetTextColor = _function(gdi32, "SetTextColor", wintypes.COLORREF, wintypes.HDC, wintypes.COLORREF)
SetBkMode = _function(gdi32, "SetBkMode", INT, wintypes.HDC, INT)

GetModuleHandleW = _function(kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
DwmSetWindowAttribute = _function(
    dwmapi,
    "DwmSetWindowAttribute",
    ctypes.c_long,
    HWND,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD
)


def screen_point(lparam: int) -> wintypes.POINT:
    return wintypes.POINT(ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value)


def resize_border(hwnd: int) -> int:
    dpi = GetDpiForWindow(hwnd) or 96
    return GetSystemMetricsForDpi(SM_CYFRAME, dpi) + GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi)


def has_frame(hwnd: int) -> bool:
    # Fullscreen windows have none
    return bool(GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_THICKFRAME)


def set_window_attribute(hwnd: int, attribute: int, value: int) -> None:
    data = ctypes.c_int(value)
    DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))


def use_dark_mode(hwnd: int, dark: bool = True) -> None:
    set_window_attribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, int(dark))


def set_menu_mode(mode: int = ALLOW_DARK) -> None:
    try:
        set_preferred_app_mode = uxtheme[SET_PREFERRED_APP_MODE]
        flush_menu_themes = uxtheme[FLUSH_MENU_THEMES]
    except AttributeError:
        return
    set_preferred_app_mode.argtypes = [INT]
    set_preferred_app_mode.restype = INT
    flush_menu_themes.argtypes = []
    flush_menu_themes.restype = None
    set_preferred_app_mode(mode)
    # Menus that are already themed pick up the new mode
    flush_menu_themes()


def system_uses_dark_mode() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            uses_light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except OSError:
        # Never set, which means light, Windows' default
        return False
    return uses_light == 0


def colorref(red: int, green: int, blue: int) -> int:
    return red | green << 8 | blue << 16
