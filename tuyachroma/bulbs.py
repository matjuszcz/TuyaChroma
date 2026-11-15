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

STATUS_CONNECTING = "łączenie…"
STATUS_READY = "gotowa (stan z HA)"
STATUS_SYNC = "sterowana przez Razer"
STATUS_UNSUPPORTED = "nieobsługiwana (brak koloru RGB)"
STATUS_OFFLINE = "wyłączona / poza siecią"

# tinytuya error codes meaning "nothing answers at this IP" (bulb without power,
# out of Wi-Fi range, or a new IP address)
OFFLINE_ERRORS = {"901", "905"}
RETRY_FIRST = 5.0
RETRY_MAX = 15.0  # seconds between connection attempts to an unreachable bulb


class UnsupportedBulb(Exception):
    """The device cannot show RGB colors."""


class BulbOffline(ConnectionError):
    """Nothing answers: the bulb is switched off (or has another IP)."""


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
    method: str = "colour"  # colour (reliable) | music (faster, not on every bulb) | auto
    min_interval: float = 0.15
    latency_adjust_ms: int = 0  # extra per-bulb correction for effect timing

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
        self.status = STATUS_CONNECTING
        self.latency_ms: float | None = None  # measured: color sent -> bulb confirmed

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

    def request_stop(self) -> None:
        """Ask the thread to give the bulb back and finish (non-blocking)."""
        self.release()
        self._stopping.set()
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self.request_stop()
        self.join(timeout)

    # ---------------------------------------------------------------- thread

    def run(self) -> None:
        failures = 0
        while not self._stopping.is_set():
            try:
                if not failures:
                    self.status = STATUS_CONNECTING
                self._connect()
                if failures:
                    _LOGGER.info("%s: reachable again", self.cfg.name)
                failures = 0
                self._loop()
            except UnsupportedBulb as err:
                _LOGGER.warning("%s: %s", self.cfg.name, err)
                self.status = STATUS_UNSUPPORTED
                self._disconnect()
                return
            except Exception as err:  # noqa: BLE001 - network errors of all kinds
                failures += 1
                offline = isinstance(err, (BulbOffline, TimeoutError))
                if failures == 1:
                    _LOGGER.warning("%s: %s%s", self.cfg.name, err,
                                    " - retrying less and less often" if offline else "")
                else:
                    _LOGGER.debug("%s: %s", self.cfg.name, err)
                self.status = STATUS_OFFLINE if offline else f"błąd: {err}"
                self._disconnect()
                # 5 s, 10 s, then every 15 s: a switched-off bulb
                # costs nothing, and one switched on is taken over ~15 s after it joins Wi-Fi.
                self._stopping.wait(min(RETRY_FIRST * 2 ** (failures - 1), RETRY_MAX))
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
        if isinstance(status, dict) and str(status.get("Err")) in OFFLINE_ERRORS:
            dev.close()
            raise BulbOffline("not reachable (switched off?)")
        if not status or "dps" not in status:
            raise ConnectionError(f"no status from bulb ({status})")
        if not dev.bulb_configured or not dev.dpset.get("colour"):
            dev.close()
            raise UnsupportedBulb("device is not a color (RGB) bulb")
        self._dev = dev
        method = self.cfg.method
        self.use_music = bool(dev.dpset.get("music")) and method in ("auto", "music")
        if method == "music" and not dev.dpset.get("music"):
            _LOGGER.warning("%s: music mode not supported, using colour mode", self.cfg.name)
        self._sent = None
        # _in_sync_mode survives reconnects: the bulb may still show our color.
        self.connected = True
        self.status = STATUS_SYNC if self._in_sync_mode else STATUS_READY
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
        with self._lock:
            if self._in_sync_mode and self._target is None:
                # A restore was interrupted by a connection error: retry it now.
                self._restore_requested = True
                self._wake.set()
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
                    self._dev.heartbeat(nowait=False)
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
        if not self._in_sync_mode:
            # Taking over: remember how HA / the Tuya app left the bulb, to give it
            # back later. Ask the bulb now - tinytuya's cache may be outdated.
            fresh = dev.status()
            if fresh and "dps" in fresh and "Error" not in fresh:
                state = dev.state()
                if "Error" not in state:
                    self._original = {
                        k: state.get(k) for k in RESTORE_KEYS if state.get(k) is not None
                    }
            else:
                _LOGGER.warning("%s: could not read state before takeover: %s", self.cfg.name, fresh)
        if self.use_music:
            if not self._in_sync_mode:
                dev.set_multiple_values({dps["switch"]: True, dps["mode"]: "music"})
            started = time.perf_counter()
            dev.set_music_colour(0, r, g, b)
        else:
            values = {dps["colour"]: tinytuya.BulbDevice.rgb_to_hexvalue(r, g, b, dps["value_hexformat"])}
            if not self._in_sync_mode:
                values[dps["switch"]] = True
                if dps.get("mode"):
                    values[dps["mode"]] = "colour"
            started = time.perf_counter()
            dev.set_multiple_values(values)
        self._record_latency((time.perf_counter() - started) * 1000)
        self._in_sync_mode = True
        self.status = STATUS_SYNC
        self._sent = rgb

    def _record_latency(self, ms: float) -> None:
        """Smoothed send->confirm time; ignores single outliers (e.g. Wi-Fi hiccups)."""
        ms = max(0.0, min(ms, 5000.0))
        if self.latency_ms is None:
            self.latency_ms = ms
        else:
            self.latency_ms += 0.2 * (min(ms, self.latency_ms * 3 + 50) - self.latency_ms)

    def _restore(self) -> None:
        if not self._in_sync_mode or self._dev is None:
            return
        dps = self._dev.dpset
        values = {dps[k]: v for k, v in self._original.items() if dps.get(k)}
        if values:
            self._dev.set_multiple_values(values, nowait=False)
        self._in_sync_mode = False
        self._sent = None
        self.status = STATUS_READY
        _LOGGER.info("%s: restored original state", self.cfg.name)


PROBE_VERSIONS = ("3.3", "3.4", "3.5", "3.1")


def probe_version(dev_id: str, ip: str, local_key: str, preferred: str = "") -> str | None:
    """Find the protocol version a bulb answers to (None if unreachable)."""
    versions = [preferred] if preferred else []
    versions += [v for v in PROBE_VERSIONS if v not in versions]
    for version in versions:
        dev = tinytuya.BulbDevice(
            dev_id=dev_id, address=ip, local_key=local_key, version=float(version),
            connection_timeout=2, connection_retry_limit=1, connection_retry_delay=0,
        )
        try:
            status = dev.status()
        except Exception:  # noqa: BLE001
            status = None
        finally:
            dev.close()
        if status and "dps" in status:
            return version
    return None
