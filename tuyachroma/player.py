"""Renders the active profile continuously (for Razer devices and bulbs)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
import logging
import threading
import time

from .razer import DEVICES, RGB, RazerUnavailable
from .render import Renderer

_LOGGER = logging.getLogger(__name__)


class EffectPlayer(threading.Thread):
    """Renders frames at ``fps``; optionally sends them to Razer devices.

    ``output_factory`` creates the Razer output (NativeChroma) or is None
    for "bulbs only" mode. While ``paused`` (a game owns the lighting)
    nothing is sent to Razer, so the game is not disturbed.
    """

    def __init__(self, renderer: Renderer, bulb_count: int,
                 output_factory: Callable[[], Any] | None = None, fps: float = 20) -> None:
        super().__init__(name="effect-player", daemon=True)
        self.renderer = renderer
        self.bulb_count = bulb_count
        self._factory = output_factory
        self._period = 1.0 / max(1.0, fps)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._frames: dict[str, list[RGB]] = {}
        self._strip: list[RGB] = []
        self._output = None
        self.paused = False
        self.output_status = "—" if output_factory is None else "łączenie z Chroma SDK…"

    def latest(self) -> tuple[dict[str, list[RGB]], list[RGB]]:
        with self._lock:
            return dict(self._frames), list(self._strip)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        self.join(timeout)

    def run(self) -> None:
        next_try = 0.0
        try:
            while not self._stop_event.is_set():
                started = time.monotonic()
                t = started
                frames = {d: self.renderer.device_frame(d, t) for d in DEVICES}
                strip = self.renderer.strip(self.bulb_count, t)
                with self._lock:
                    self._frames, self._strip = frames, strip
                if self._factory is not None:
                    if self._output is None and started >= next_try:
                        try:
                            self._output = self._factory()
                            self.output_status = "urządzenia Razer: efekt z programu"
                        except RazerUnavailable as err:
                            self.output_status = f"Chroma SDK niedostępne: {err}"
                            next_try = started + 5
                    if self._output is not None:
                        if self.paused:
                            self.output_status = "urządzenia Razer: steruje gra"
                        else:
                            self.output_status = "urządzenia Razer: efekt z programu"
                            self._send(frames)
                spent = time.monotonic() - started
                self._stop_event.wait(max(0.0, self._period - spent))
        finally:
            if self._output is not None:
                self._output.close()
                self._output = None

    def _send(self, frames: dict[str, list[RGB]]) -> None:
        try:
            for device, colors in frames.items():
                self._output.set_frame(device, colors)
        except Exception as err:  # noqa: BLE001 - SDK/service went away
            _LOGGER.warning("Chroma SDK error: %s", err)
            try:
                self._output.close()
            finally:
                self._output = None
