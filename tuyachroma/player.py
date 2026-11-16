"""Renders the active profile continuously (for Razer devices and bulbs)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
import logging
import threading
import time

from .razer import DEVICES, RGB, RazerUnavailable
from .razer_out import EXTENDED_SIZES
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
        # A game runs: let go of the Chroma SDK so the game gets the Razer devices
        # (otherwise Synapse may keep showing our effect instead of the game's).
        self.yielded = False
        self._reconnect = False
        self.init_error = ""
        self.output_status = "—" if output_factory is None else "łączenie z Chroma SDK…"

    @property
    def output_ready(self) -> bool:
        return self._output is not None

    def reconnect(self) -> None:
        """Close and open the Chroma SDK again (Synapse kept its own profile)."""
        self._reconnect = True

    def latest(self) -> tuple[dict[str, list[RGB]], list[RGB]]:
        with self._lock:
            return dict(self._frames), list(self._strip)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        self.join(timeout)

    SEND_RETRY = 2.0  # s after a failed send before the SDK is opened again

    def run(self) -> None:
        next_try = 0.0
        self._send_failed_at = None
        try:
            while not self._stop_event.is_set():
                started = time.monotonic()
                t = started
                try:
                    frames = {d: self.renderer.device_frame(d, t) for d in DEVICES}
                    strip = self.renderer.strip(self.bulb_count, t)
                except Exception:  # noqa: BLE001 - a renderer bug must not freeze the devices
                    if not getattr(self, "_render_failed", False):
                        _LOGGER.exception("Effect rendering failed - retrying")
                    self._render_failed = True
                    self._stop_event.wait(self._period)
                    continue
                self._render_failed = False
                with self._lock:
                    self._frames, self._strip = frames, strip
                if self._factory is not None and self.yielded:
                    if self._output is not None:
                        self._output.close()
                        self._output = None
                        _LOGGER.info("Razer devices handed over to the game")
                    self.output_status = "urządzenia Razer: steruje gra"
                elif self._factory is not None:
                    if self._reconnect:
                        self._reconnect = False
                        if self._output is not None:
                            self._output.close()
                            self._output = None
                        next_try = 0.0
                    if self._send_failed_at is not None:
                        # the SDK/service went away: don't reopen it every frame
                        next_try = max(next_try, self._send_failed_at + self.SEND_RETRY)
                        self._send_failed_at = None
                    if self._output is None and started >= next_try:
                        try:
                            self._output = self._factory()
                            self.output_status = "urządzenia Razer: efekt z programu"
                            self.init_error = ""
                        except RazerUnavailable as err:
                            if str(err) != self.init_error:
                                _LOGGER.warning("Razer devices: %s (retrying every 5 s)", err)
                            self.init_error = str(err)
                            self.output_status = f"Chroma SDK niedostępne: {err}"
                            next_try = started + 5
                    if self._output is not None:
                        if self.paused:
                            self.output_status = "urządzenia Razer: steruje gra"
                        else:
                            self.output_status = "urządzenia Razer: efekt z programu"
                            try:
                                extended = {name: self.renderer.frame(name, w, h, t)
                                            for name, (w, h) in EXTENDED_SIZES.items()}
                            except Exception:  # noqa: BLE001 - as above: keep the player alive
                                _LOGGER.debug("Extended frame rendering failed", exc_info=True)
                                extended = {}
                            self._send({**frames, **extended})
                spent = time.monotonic() - started
                self._stop_event.wait(max(0.0, self._period - spent))
        finally:
            if self._output is not None:
                self._output.close()
                self._output = None

    def _send(self, frames: dict[str, list[RGB]]) -> None:
        try:
            self._output.send_frames(frames)
        except Exception as err:  # noqa: BLE001 - SDK/service went away
            _LOGGER.warning("Chroma SDK error: %s", err)
            self._send_failed_at = time.monotonic()
            try:
                self._output.close()
            finally:
                self._output = None
