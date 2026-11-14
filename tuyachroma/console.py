"""Make console output safe for Polish characters on any Windows code page."""

from __future__ import annotations

import sys


def setup_console() -> None:
    if sys.platform == "win32":
        try:
            import ctypes  # noqa: PLC0415

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)  # UTF-8
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        if stream is None:  # windowed exe has no console
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
