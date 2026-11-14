"""Screen colors for the Ambient effect (like Synapse's Ambient Awareness)."""

from __future__ import annotations

import threading
import time

from .razer import RGB


class ScreenSampler:
    """Grabs a small copy of the screen at most ~10 times per second."""

    def __init__(self, max_rate: float = 10.0) -> None:
        self._lock = threading.Lock()
        self._image = None
        self._taken = 0.0
        self._interval = 1.0 / max_rate

    def _grab(self):
        from PIL import ImageGrab  # noqa: PLC0415

        return ImageGrab.grab().convert("RGB").resize((48, 27))

    def __call__(self, w: int, h: int) -> list[RGB]:
        with self._lock:
            now = time.monotonic()
            if self._image is None or now - self._taken > self._interval:
                self._image = self._grab()
                self._taken = now
            img = self._image
        return list(img.resize((w, h)).getdata())
