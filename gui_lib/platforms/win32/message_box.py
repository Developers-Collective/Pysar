import ctypes
import functools
from ctypes import wintypes
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...windowing.dialogs import MessageBox

# winuser.h
MB_OK = 0x0
MB_OKCANCEL = 0x1
MB_ABORTRETRYIGNORE = 0x2
MB_YESNOCANCEL = 0x3
MB_YESNO = 0x4
MB_ICONQUESTION = 0x20
MB_ICONWARNING = 0x30
MB_DEFBUTTON = (0x000, 0x100, 0x200)
IDOK, IDCANCEL, IDABORT, IDRETRY, IDIGNORE, IDYES, IDNO = 1, 2, 3, 4, 5, 6, 7
WH_CBT = 5
HCBT_ACTIVATE = 5

KINDS = {
    (1, False): (MB_OK, (IDOK,)),
    (2, True): (MB_OKCANCEL, (IDOK, IDCANCEL)),
    (2, False): (MB_YESNO, (IDYES, IDNO)),
    (3, True): (MB_YESNOCANCEL, (IDYES, IDNO, IDCANCEL)),
    (3, False): (MB_ABORTRETRYIGNORE, (IDABORT, IDRETRY, IDIGNORE)),
}


def layout(box: "MessageBox") -> tuple[int, tuple[int, ...], list[int]]:
    cancels = box.cancel is not None and len(box.buttons) > 1
    order = [index for index in range(len(box.buttons)) if not (cancels and index == box.cancel)]
    if cancels:
        order.append(box.cancel)
    style, ids = KINDS[(len(order), cancels)]
    style |= MB_DEFBUTTON[order.index(box.default)]
    style |= MB_ICONWARNING if box.warning else MB_ICONQUESTION
    return style, ids, order


def show(owner: int, title: str, box: "MessageBox") -> int:
    api = _api()
    style, ids, order = layout(box)
    hook: Any = None

    def activated(code: int, wparam: int, lparam: int) -> int:
        if code == HCBT_ACTIVATE:
            for button, index in zip(ids, order):
                api.SetDlgItemTextW(wparam, button, box.buttons[index])
        return api.CallNextHookEx(hook, code, wparam, lparam)

    callback = api.HOOKPROC(activated)
    hook = api.SetWindowsHookExW(WH_CBT, callback, None, api.GetCurrentThreadId())
    try:
        text = f"{box.message}\n\n{box.detail}" if box.detail else box.message
        answer = api.MessageBoxW(owner or None, text, title, style)
    finally:
        if hook:
            api.UnhookWindowsHookEx(hook)
    # 0 when MessageBox failed
    return order[ids.index(answer)] if answer in ids else box.dismissed


@functools.cache
def _api() -> Any:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hook_proc = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    def function(library: Any, name: str, restype: Any, *argtypes: Any) -> Any:
        bound = getattr(library, name)
        bound.restype = restype
        bound.argtypes = argtypes
        return bound

    class Api:
        HOOKPROC = hook_proc
        SetWindowsHookExW = function(user32, "SetWindowsHookExW", wintypes.HHOOK, ctypes.c_int, hook_proc,
                                     wintypes.HINSTANCE, wintypes.DWORD)
        CallNextHookEx = function(user32, "CallNextHookEx", wintypes.LPARAM, wintypes.HHOOK, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM)
        UnhookWindowsHookEx = function(user32, "UnhookWindowsHookEx", wintypes.BOOL, wintypes.HHOOK)
        SetDlgItemTextW = function(user32, "SetDlgItemTextW", wintypes.BOOL, wintypes.HWND, ctypes.c_int,
                                   wintypes.LPCWSTR)
        MessageBoxW = function(user32, "MessageBoxW", ctypes.c_int, wintypes.HWND, wintypes.LPCWSTR,
                               wintypes.LPCWSTR, wintypes.UINT)
        GetCurrentThreadId = function(kernel32, "GetCurrentThreadId", wintypes.DWORD)

    return Api
