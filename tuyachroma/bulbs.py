"""Send colors to Tuya bulbs over the local network (tinytuya).

Each bulb gets its own thread with a persistent connection. Only the latest
color is sent (older ones are dropped), at most once per ``min_interval``.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
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
STATUS_SCENE = "własny efekt listwy (z aplikacji)"

# DPs not replayed from a captured scene: countdown timer, plain color
SCENE_SKIP_DPS = {"24", "26"}


@dataclass(frozen=True)
class NativeScene:
    """A light's built-in effect (e.g. a wave along a strip), captured from its state."""

    dps: tuple[tuple[str, Any], ...]

    @classmethod
    def from_state(cls, dps: dict) -> NativeScene:
        return cls(tuple(sorted((str(k), v) for k, v in dps.items() if str(k) not in SCENE_SKIP_DPS)))

    def values(self) -> dict[str, Any]:
        return dict(self.dps)

# tinytuya error codes meaning "nothing answers at this IP" (bulb without power,
# out of Wi-Fi range, or a new IP address)
OFFLINE_ERRORS = {"901", "905"}
# 902 = no answer in time, 914 = connection garbled (key/version): the link is broken
LINK_ERRORS = {"902", "914"}
RETRY_FIRST = 5.0
REFRESH_EVERY = 10.0  # resend an unchanged color: a bulb that rebooted shows it again
SLOW_MAX = 1.0        # slowest sending rate (s) for a bulb that can't keep up
SLOW_RECOVER = 30.0   # after this long without problems, try sending faster again

# Bulbs that dropped off Wi-Fi while getting colors (cheap ones reboot when
# flooded): they keep the slower rate until the program is restarted.
_learned_slow: dict[str, float] = {}


def learned_slow_interval(dev_id: str) -> float:
    """> 0 if the bulb dropped off Wi-Fi while getting colors in this session."""
    return _learned_slow.get(dev_id, 0.0)
RETRY_MAX = 15.0  # seconds between connection attempts to an unreachable bulb


class UnsupportedBulb(Exception):
    """The device cannot show RGB colors."""


class BulbOffline(ConnectionError):
    """Nothing answers: the bulb is switched off (or has another IP)."""


def check_reply(result: Any, what: str) -> None:
    """tinytuya reports failures as a returned dict, not as an exception."""
    if not isinstance(result, dict) or "Err" not in result:
        return
    code = str(result.get("Err"))
    message = f"bulb did not confirm {what}: {result.get('Error') or code}"
    if code in OFFLINE_ERRORS:
        raise BulbOffline(message)
    if code in LINK_ERRORS:
        raise ConnectionError(message)
    _LOGGER.debug("%s (ignored)", message)  # e.g. an odd but harmless reply


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
    scene: dict | None = None  # built-in effect to play instead of moving colors

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
        # pause between colors for a bulb that can't keep up (>= cfg.min_interval)
        self.slow_interval = _learned_slow.get(cfg.id, 0.0)
        self._last_problem = 0.0
        self._send_count = 0
        self._source_key: Any = None
        self.dps: list[str] = []  # data points the bulb reported over the LAN
        self._scene_sent: NativeScene | None = None
        self._hold = False
        self._capture: tuple[threading.Event, dict] | None = None
        self._reported = False

    # ------------------------------------------------------------ public API

    def set_color(self, rgb: RGB) -> None:
        with self._lock:
            self._target = rgb
            self._source_key = None
            self._restore_requested = False
        self._wake.set()

    def set_scene(self, scene: NativeScene) -> None:
        """Play the light's own effect (sent once; the light animates it by itself)."""
        with self._lock:
            if self._target == scene:
                return
            self._target = scene
            self._source_key = None
            self._restore_requested = False
        self._wake.set()

    def hold(self, on: bool) -> None:
        """Stop sending anything (the user is setting the light in the Tuya app)."""
        self._hold = on
        self._wake.set()

    def read_state(self, timeout: float = 6.0) -> dict | None:
        """The light's current data points, read over this worker's connection."""
        done = threading.Event()
        result: dict = {}
        self._capture = (done, result)
        self._wake.set()
        if not done.wait(timeout):
            self._capture = None
            return None
        return result.get("dps")

    def set_color_source(self, source: Callable[[], RGB], key: Any = None) -> None:
        """Follow a moving effect: ``source()`` gives the color for "now".

        It is called right before each send, so the color is as fresh as it can
        be, as often as this bulb's rate allows. Calls with the same ``key`` as
        the current source are ignored (the engine calls this every frame).
        """
        with self._lock:
            if key is not None and key == self._source_key and callable(self._target):
                return
            self._target = source
            self._source_key = key
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
                sends_before = self._send_count
                try:
                    self._loop()
                except Exception as err:  # noqa: BLE001
                    if self._send_count == sends_before:
                        raise  # nothing got through since connecting: normal retry with pauses
                    # It worked a moment ago: reconnect right away, not after a pause.
                    self._slow_down(dropped_off=isinstance(err, BulbOffline))
                    _LOGGER.warning("%s: %s - reconnecting", self.cfg.name, err)
                    self._disconnect()
                    self._stopping.wait(0.5)
                    continue
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
        self.dps = sorted((str(k) for k in status["dps"]), key=lambda k: (len(k), k))
        method = self.cfg.method
        self.use_music = bool(dev.dpset.get("music")) and method in ("auto", "music")
        if method == "music" and not dev.dpset.get("music"):
            _LOGGER.warning("%s: music mode not supported, using colour mode", self.cfg.name)
        self._sent = None
        # _in_sync_mode survives reconnects: the bulb may still show our color.
        self.connected = True
        self.status = STATUS_SYNC if self._in_sync_mode else STATUS_READY
        _LOGGER.info(
            "%s: connected (type %s, %s mode, DPs %s)", self.cfg.name, dev.bulb_type,
            "music" if self.use_music else "colour", ",".join(self.dps),
        )
        if not self._reported:
            self._reported = True
            _LOGGER.info("%s: state %s", self.cfg.name, status["dps"])

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
            following = callable(self._target)
            self._wake.wait(timeout=self._interval() if following else 5)
            self._wake.clear()
            if self._stopping.is_set():
                return
            with self._lock:
                target, restore = self._target, self._restore_requested
                self._restore_requested = False
            now = time.monotonic()
            if self._capture is not None:
                done, result = self._capture
                self._capture = None
                reply = self._dev.status()
                if isinstance(reply, dict) and "dps" in reply:
                    result["dps"] = dict(reply["dps"])
                done.set()
                last_contact = now
                continue
            if self._hold:
                if now - last_contact > 8:
                    check_reply(self._dev.heartbeat(nowait=False), "heartbeat")
                    last_contact = now
                continue
            if restore:
                self._restore()
                self._scene_sent = None
                last_contact = now
                continue
            if isinstance(target, NativeScene):
                if self._scene_sent != target:
                    self._send_scene(target)
                    last_send = last_contact = time.monotonic()
                elif now - last_contact > 8:
                    check_reply(self._dev.heartbeat(nowait=False), "heartbeat")
                    last_contact = now
                continue
            if target is None:
                if now - last_contact > 8:  # keep the persistent socket alive
                    check_reply(self._dev.heartbeat(nowait=False), "heartbeat")
                    last_contact = now
                continue
            floor = _learned_slow.get(self.cfg.id, 0.0)
            if self.slow_interval > floor and now - self._last_problem > SLOW_RECOVER:
                halved = self.slow_interval / 2
                self.slow_interval = max(floor, halved if halved > 0.1 else 0.0)
                self._last_problem = now
            wait = self._interval() - (now - last_send)
            if wait > 0:
                self._stopping.wait(wait)
                self._wake.set()  # re-check the newest target after the pause
                continue
            if callable(target):
                try:
                    target = target()  # the effect's color for this very moment
                except Exception as err:  # noqa: BLE001 - e.g. the effect was just stopped
                    _LOGGER.debug("%s: effect color: %s", self.cfg.name, err)
                    continue
            if (self._sent is not None and distance(self._sent, target) < 3
                    and now - last_send < REFRESH_EVERY):
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
            self._remember_original()
        scene_was_on = self._scene_sent is not None
        self._scene_sent = None
        if self.use_music:
            if not self._in_sync_mode:
                dev.set_multiple_values({dps["switch"]: True, dps["mode"]: "music"})
            started = time.perf_counter()
            check_reply(dev.set_music_colour(0, r, g, b), "color")
        else:
            values = {dps["colour"]: tinytuya.BulbDevice.rgb_to_hexvalue(r, g, b, dps["value_hexformat"])}
            if not self._in_sync_mode or scene_was_on:
                values[dps["switch"]] = True
                if dps.get("mode"):
                    values[dps["mode"]] = "colour"
            started = time.perf_counter()
            check_reply(dev.set_multiple_values(values), "color")
        self._record_latency((time.perf_counter() - started) * 1000)
        self._in_sync_mode = True
        self.status = STATUS_SYNC
        self._sent = rgb
        self._send_count += 1

    def _remember_original(self) -> None:
        """Taking over: remember how HA / the Tuya app left the bulb, to give it
        back later. Ask the bulb now - tinytuya's cache may be outdated."""
        dev = self._dev
        assert dev is not None
        fresh = dev.status()
        if fresh and "dps" in fresh and "Error" not in fresh:
            state = dev.state()
            if "Error" not in state:
                self._original = {
                    k: state.get(k) for k in RESTORE_KEYS if state.get(k) is not None
                }
        else:
            _LOGGER.warning("%s: could not read state before takeover: %s", self.cfg.name, fresh)

    def _send_scene(self, scene: NativeScene) -> None:
        dev = self._dev
        assert dev is not None
        if not self._in_sync_mode:
            self._remember_original()
        values = scene.values()
        values[dev.dpset["switch"]] = True
        check_reply(dev.set_multiple_values(values), "effect")
        self._in_sync_mode = True
        self._scene_sent = scene
        self._sent = None
        self.status = STATUS_SCENE
        self._send_count += 1
        _LOGGER.info("%s: playing its own effect %s", self.cfg.name, values)

    def _interval(self) -> float:
        return max(self.cfg.min_interval, self.slow_interval)

    def _slow_down(self, dropped_off: bool = False) -> None:
        """The bulb stopped confirming colors: send them less often for a while."""
        self.slow_interval = min(max(self.slow_interval * 2, self.cfg.min_interval * 2, 0.3), SLOW_MAX)
        self._last_problem = time.monotonic()
        if dropped_off:
            # It vanished from the network (probably rebooted): stay slower for good.
            _learned_slow[self.cfg.id] = self.slow_interval
            _LOGGER.info("%s: dropped off the network while syncing - sending at most every "
                         "%.2f s until the program restarts (set 'Szybkość zmian' to make it "
                         "permanent)", self.cfg.name, self.slow_interval)
        else:
            _LOGGER.info("%s: sending at most every %.2f s", self.cfg.name, self.slow_interval)

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
