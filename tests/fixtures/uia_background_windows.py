"""Private, non-activating Win32 fixtures used by the background UIA test."""
import ctypes
import json
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
    wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                            wintypes.WPARAM, wintypes.LPARAM)
class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

remaining = 2
def wndproc(hwnd, message, wparam, lparam):
    global remaining
    if message == 0x0010:  # WM_CLOSE
        user32.DestroyWindow(hwnd)
        return 0
    if message == 0x0002:  # WM_DESTROY
        remaining -= 1
        if remaining <= 0:
            user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)

proc = WNDPROC(wndproc)
instance = kernel32.GetModuleHandleW(None)
cls = WNDCLASSW(0, proc, 0, 0, instance, None, None, None, None, "FaustusBackgroundUiaFixture")
if not user32.RegisterClassW(ctypes.byref(cls)):
    raise OSError(ctypes.get_last_error(), "RegisterClassW failed")

records = []
for i, (title, text) in enumerate((("Faustus UIA fixture target", "initial target"),
                                    ("Faustus UIA fixture bystander", "untouched bystander"))):
    hwnd = user32.CreateWindowExW(0x00000080 | 0x08000000,
        "FaustusBackgroundUiaFixture", title, 0x00CF0000,
        -20000 - i * 400, -20000, 320, 180, None, None, instance, None)
    if not hwnd:
        raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
    edit = user32.CreateWindowExW(0, "EDIT", text,
        0x40000000 | 0x10000000 | 0x00010000 | 0x80, 8, 8, 280, 28,
        hwnd, None, instance, None)
    if not edit:
        raise OSError(ctypes.get_last_error(), "EDIT control creation failed")
    if i == 0:
        readonly = user32.CreateWindowExW(0, "EDIT", "locked text",
            0x40000000 | 0x10000000 | 0x00010000 | 0x0800, 8, 44, 280, 28,
            hwnd, None, instance, None)
        if not readonly:
            raise OSError(ctypes.get_last_error(), "read-only EDIT control creation failed")
    user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
    user32.UpdateWindow(hwnd)
    records.append({"title": title, "hwnd": int(hwnd), "edit_hwnd": int(edit)})

print(json.dumps({"pid": kernel32.GetCurrentProcessId(), "windows": records}), flush=True)
message = wintypes.MSG()
while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
    user32.TranslateMessage(ctypes.byref(message))
    user32.DispatchMessageW(ctypes.byref(message))
