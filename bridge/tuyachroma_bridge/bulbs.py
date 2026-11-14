"""Send colors to Tuya bulbs over the local network (tinytuya).

Each bulb gets its own thread with a persistent connection. Only the latest
color is sent (older ones are dropped), at most once per ``min_interval``.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
import time
from typing import Any

import tinytuya

from .colors import distance
from .razer import RGB

_LOGGER = logging.getLogger(__name__)

RESTORE_KEYS = ("switch", "mode", "brightness", "colourtemp", "colour")


@dataclass
class BulbConfig:
    name: str
    id: str
    ip: str
    local_key: str
    version: str = "3.3"
    source: str = "keyboard"
    zone: Any = "all"
    mode: str = "vivid"
    brightness: float = 1.0
    min_brightness: float = 0.05
    method: str = "auto"  # auto | music | colour
    min_interval: float = 0.15

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BulbConfig:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        cfg = cls(**known)
        cfg.version = str(cfg.version)
        return cfg


def make_device(cfg: BulbConfig) -> tinytuya.BulbDevice:
    dev = tinytuya.BulbDevice(
        dev_id=cfg.id,
        address=cfg.ip,
        local_key=cfg.local_key,
        version=float(cfg.version),
        persist=True,
        connection_timeout=3,
        connection_retry_limit=1,
        connection_retry_delay=1,
    )
    dev.set_socketPersistent(True)
    return dev


class BulbWorker(threading.Thread):
    """Keeps one bulb in sync with the latest requested color."""

    def __init__(self, cfg: BulbConfig, device_factory=make_device) -> None:
        super().__init__(name=f"bulb-{cfg.name}", daemon=True)
        self.cfg = cfg
        self._factory = device_factory
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._target: RGB | None = None  # None = restore original state
        self._restore_requested = False
        self._sent: RGB | None = None
        self._dev: tinytuya.BulbDevice | None = None
        self._original: dict[str, Any] = {}
        self._in_sync_mode = False
        self.use_music = False
        self.connected = False

    # ------------------------------------------------------------ public API

    def set_color(self, rgb: RGB) -> None:
        with self._lock:
            self._target = rgb
            self._restore_requested = False
        self._wake.set()

    def release(self) -> None:
        """No game effect any more: give the bulb its original state back."""
        with self._lock:
            if self._target is None and not self._in_sync_mode:
                return
            self._target = None
            self._restore_requested = True
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self.release()
        self._stopping.set()
        self._wake.set()
        self.join(timeout)

    # ---------------------------------------------------------------- thread

    def run(self) -> None:
        while not self._stopping.is_set():
            try:
                self._connect()
                self._loop()
            except Exception as err:  # noqa: BLE001 - network errors of all kinds
                _LOGGER.warning("%s: %s", self.cfg.name, err)
                self._disconnect()
                self._stopping.wait(3)
        try:
            if self._in_sync_mode:
                if self._dev is None:
                    self._connect()
                self._restore()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("%s: could not restore state: %s", self.cfg.name, err)
        self._disconnect()

    def _connect(self) -> None:
        dev = self._factory(self.cfg)
        status = dev.status()
        if not status or "dps" not in status:
            raise ConnectionError(f"no status from bulb ({status})")
        if not dev.bulb_configured or not dev.dpset.get("colour"):
            raise RuntimeError("device is not a color (RGB) bulb")
        self._dev = dev
        if not self._original:
            state = dev.state()
            self._original = {k: state.get(k) for k in RESTORE_KEYS if state.get(k) is not None}
        method = self.cfg.method
        self.use_music = bool(dev.dpset.get("music")) and method in ("auto", "music")
        if method == "music" and not dev.dpset.get("music"):
            _LOGGER.warning("%s: music mode not supported, using colour mode", self.cfg.name)
        self._sent = None
        self._in_sync_mode = False
        self.connected = True
        _LOGGER.info(
            "%s: connected (type %s, %s mode)", self.cfg.name, dev.bulb_type,
            "music" if self.use_music else "colour",
        )

    def _disconnect(self) -> None:
        self.connected = False
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:  # noqa: BLE001
                pass
        self._dev = None

    def _loop(self) -> None:
        last_send = 0.0
        last_contact = time.monotonic()
        while not self._stopping.is_set():
            self._wake.wait(timeout=5)
            self._wake.clear()
            if self._stopping.is_set():
                return
            with self._lock:
                target, restore = self._target, self._restore_requested
                self._restore_requested = False
            now = time.monotonic()
            if restore:
                self._restore()
                last_contact = now
                continue
            if target is None:
                if now - last_contact > 8:  # keep the persistent socket alive
                    self._dev.heartbeat(nowait=True)
                    last_contact = now
                continue
            wait = self.cfg.min_interval - (now - last_send)
            if wait > 0:
                self._stopping.wait(wait)
                self._wake.set()  # re-check the newest target after the pause
                continue
            if self._sent is not None and distance(self._sent, target) < 3:
                continue
            self._send(target)
            last_send = last_contact = time.monotonic()

    def _send(self, rgb: RGB) -> None:
        dev = self._dev
        assert dev is not None
        dps = dev.dpset
        r, g, b = rgb
        if max(rgb) == 0:
            r = g = b = 1  # avoid switching off; nearly black
        if self.use_music:
            if not self._in_sync_mode:
                dev.set_multiple_values({dps["switch"]: True, dps["mode"]: "music"}, nowait=True)
            dev.set_music_colour(0, r, g, b, nowait=True)
        else:
            values = {dps["colour"]: tinytuya.BulbDevice.rgb_to_hexvalue(r, g, b, dps["value_hexformat"])}
            if not self._in_sync_mode:
                values[dps["switch"]] = True
                if dps.get("mode"):
                    values[dps["mode"]] = "colour"
            dev.set_multiple_values(values, nowait=True)
        self._in_sync_mode = True
        self._sent = rgb

    def _restore(self) -> None:
        if not self._in_sync_mode or self._dev is None:
            return
        dps = self._dev.dpset
        values = {dps[k]: v for k, v in self._original.items() if dps.get(k)}
        if values:
            self._dev.set_multiple_values(values, nowait=False)
        self._in_sync_mode = False
        self._sent = None
        _LOGGER.info("%s: restored original state", self.cfg.name)
