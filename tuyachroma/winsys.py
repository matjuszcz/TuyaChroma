"""Windows integration: autostart, single instance, shutdown/sleep events."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
import logging
import sys
import threading

_LOGGER = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "TuyaChroma"


def autostart_command(minimized: bool = True) -> str:
    flag = " --minimized" if minimized else ""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"{flag}'
    return f'"{sys.executable}" -m tuyachroma{flag}'


def set_autostart(enabled: bool, minimized: bool = True) -> None:
    if sys.platform != "win32":
        return
    import winreg  # noqa: PLC0415

    # CreateKeyEx opens the key, or creates it if this profile has none yet.
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command(minimized))
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def get_autostart() -> bool:
    if sys.platform != "win32":
        return False
    import winreg  # noqa: PLC0415

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False


_instance_handle = None


def acquire_single_instance() -> bool:
    """False if another copy of the program is already running."""
    global _instance_handle
    if sys.platform != "win32":
        return True
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    _instance_handle = k32.CreateMutexW(None, False, "Local\\TuyaChromaSingleInstance")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


# ------------------------------------------------------------------ events

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_POWERBROADCAST = 0x0218
WM_DEVICECHANGE = 0x0219
DBT_DEVNODES_CHANGED = 0x0007
DBT_DEVICEARRIVAL = 0x8000
DBT_DEVICEREMOVECOMPLETE = 0x8004
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012


def start_system_event_listener(
    on_shutdown: Callable[[], None],
    on_suspend: Callable[[], None],
    on_resume: Callable[[], None],
    on_devices_changed: Callable[[], None] | None = None,
) -> threading.Thread | None:
    """Hidden window receiving shutdown/logoff and sleep/resume notifications."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes as W  # noqa: PLC0415

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
            ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR),
            ("lpszClassName", W.LPCWSTR),
        ]

    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    user32.CreateWindowExW.restype = W.HWND
    user32.CreateWindowExW.argtypes = [
        W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID,
    ]
    user32.GetMessageW.argtypes = [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
    user32.ShutdownBlockReasonCreate.argtypes = [W.HWND, W.LPCWSTR]
    kernel32.GetModuleHandleW.restype = W.HMODULE

    state = {"done": False}

    def wndproc(hwnd, msg, wparam, lparam):
        try:
            if msg == WM_QUERYENDSESSION:
                user32.ShutdownBlockReasonCreate(hwnd, "Przywracanie żarówek Tuya")
                return 1
            if msg == WM_ENDSESSION and wparam and not state["done"]:
                state["done"] = True
                on_shutdown()
                return 0
            if msg == WM_DEVICECHANGE:
                if on_devices_changed is not None and wparam in (
                        DBT_DEVNODES_CHANGED, DBT_DEVICEARRIVAL, DBT_DEVICEREMOVECOMPLETE):
                    on_devices_changed()  # a device was plugged in or removed
                return 1
            if msg == WM_POWERBROADCAST:
                if wparam == PBT_APMSUSPEND:
                    on_suspend()
                elif wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                    on_resume()
                return 1
        except Exception:  # noqa: BLE001
            _LOGGER.exception("System event handler failed")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run() -> None:
        proc = WNDPROC(wndproc)
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = proc
        wc.hInstance = hinst
        wc.lpszClassName = "TuyaChromaEvents"
        if not user32.RegisterClassW(ctypes.byref(wc)):
            _LOGGER.warning("RegisterClass failed (%s)", ctypes.get_last_error())
            return
        # A hidden top-level window (message-only windows get no broadcasts).
        hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "TuyaChroma", 0, 0, 0, 0, 0,
                                      None, None, hinst, None)
        if not hwnd:
            _LOGGER.warning("CreateWindow failed (%s)", ctypes.get_last_error())
            return
        msg = W.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        del proc

    thread = threading.Thread(target=run, name="win-events", daemon=True)
    thread.start()
    return thread
