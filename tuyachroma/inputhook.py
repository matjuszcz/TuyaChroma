"""Global keyboard hook for the Reactive and Ripple effects (Windows).

Only key positions are used (to light the pressed key); nothing is stored.
"""

from __future__ import annotations

from collections.abc import Callable
import ctypes
import logging
import sys
import threading

from .keymap import key_position

_LOGGER = logging.getLogger(__name__)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_QUIT = 0x0012
LLKHF_EXTENDED = 0x01


class KeyboardHook:
    def __init__(self, on_key: Callable[[int, int], None]) -> None:
        self._on_key = on_key
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()

    def start(self) -> None:
        if sys.platform != "win32" or self._thread is not None:
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="keyboard-hook", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._ready.wait(2)  # make sure the thread id is known before posting WM_QUIT
        if self._thread is not None and self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(2)
        self._thread = None

    def _run(self) -> None:
        from ctypes import wintypes as W  # noqa: PLC0415

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", W.DWORD), ("scanCode", W.DWORD), ("flags", W.DWORD),
                        ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, W.WPARAM, W.LPARAM)
        user32.SetWindowsHookExW.restype = W.HHOOK
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, W.HINSTANCE, W.DWORD]
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = [W.HHOOK, ctypes.c_int, W.WPARAM, W.LPARAM]
        user32.UnhookWindowsHookEx.argtypes = [W.HHOOK]
        user32.GetMessageW.argtypes = [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
        kernel32.GetModuleHandleW.restype = W.HMODULE

        def proc(code, wparam, lparam):
            if code >= 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                try:
                    info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    pos = key_position(info.vkCode, bool(info.flags & LLKHF_EXTENDED))
                    if pos is not None:
                        self._on_key(*pos)
                except Exception:  # noqa: BLE001 - never break the user's keyboard
                    pass
            return user32.CallNextHookEx(None, code, wparam, lparam)

        callback = HOOKPROC(proc)
        self._thread_id = kernel32.GetCurrentThreadId()
        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, callback, kernel32.GetModuleHandleW(None), 0)
        self._ready.set()
        if not hook:
            _LOGGER.warning("Keyboard hook failed (%s)", ctypes.get_last_error())
            return
        msg = W.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(hook)
        del callback
