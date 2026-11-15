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
# How long a light fades to each new color (sent this much earlier in effects,
# so the fade ends together with the Razer devices).
COLOUR_FADE_MS = 250.0  # colour mode: the light's own fade
MUSIC_FADE_MS = 100.0   # music data with transition "gradient": a short, quick fade
# Colour mode: every color waits for the light's answer (it answers a changed
# value). 1.9.18 sent most without reading the answers; they piled up unread on
# the connection, which may hang a light (RGB Biuro 2: error 914 since). 0 = all.
CONFIRM_EVERY = 0.0
# How long to wait for an answer once connected: a light that skips one blocks
# for this long, not for the 3 s connect timeout.
# GU10s answer in 60-110 ms but skip an answer now and then; this long the light
# then gets no new color (was 0.7 s: a visible stop in smooth effects).
REPLY_TIMEOUT = 0.35
# A "confirmation" this slow is the reply timeout running out, not the light's
# delay (must stay below REPLY_TIMEOUT, or a missing answer counts as 0.7 s delay).
NO_ACK_MS = REPLY_TIMEOUT * 1000 * 0.9
HEARTBEAT_EVERY = 8.0  # keep the persistent socket alive (and notice a light that rebooted)
REFRESH_EVERY = 10.0  # resend an unchanged color: a bulb that rebooted shows it again
SLOW_MAX = 1.0        # slowest sending rate (s) for a bulb that can't keep up
SLOW_RECOVER = 30.0   # after this long without problems, try sending faster again

# Bulbs that dropped off Wi-Fi while getting colors (cheap ones reboot when
# flooded): they keep the slower rate until the program is restarted.
_learned_slow: dict[str, float] = {}

# Lights that hung (error 914) while getting music data: colour mode for them
# until the program restarts.
_music_hangs: set[str] = set()


# Some lights reboot after a fixed time on one connection (Lampka Biurko, a
# Battletron light bar: always ~183 s after connecting). Once two drops happen
# after about the same time, the connection is renewed before that.
_drop_ages: dict[str, list[float]] = {}
_max_age: dict[str, float] = {}
SAME_AGE = 15.0      # drops this close together count as "the same time"
RENEW_EARLY = 30.0   # renew this long before the learned limit
MIN_AGE = 60.0       # only learn limits above this


class _Renew(Exception):
    """Internal: renew the connection now (no error, no pause)."""


def _record_drop_age(dev_id: str, name: str, age: float) -> None:
    if age < MIN_AGE:
        return
    ages = _drop_ages.setdefault(dev_id, [])
    ages.append(age)
    del ages[:-3]
    close = [a for a in ages if abs(a - age) <= SAME_AGE]
    if len(close) >= 2 and dev_id not in _max_age:
        _max_age[dev_id] = min(close) - RENEW_EARLY
        # Not too many colors after all: the connection's age is the cause.
        _learned_slow.pop(dev_id, None)
        _LOGGER.info("%s: drops off ~%.0f s after connecting - renewing the connection every "
                     "%.0f s from now on", name, min(close), _max_age[dev_id])


# Lights that kept dropping off even at the slowest rate: the rate is not the
# cause (power supply, Wi-Fi, firmware), so they are not slowed down any more.
_rate_innocent: set[str] = set()


def rate_not_the_cause(dev_id: str) -> bool:
    return dev_id in _rate_innocent


def learned_max_age(dev_id: str) -> float:
    """> 0: renew this light's connection after this many seconds."""
    return _max_age.get(dev_id, 0.0)


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
    # auto:   colour mode - the light's own smooth fade, sent ahead so it ends
    #         with the Razer devices. Music data was tried for the GU10s (1.9.18-
    #         1.9.22): quick fades, but RGB Biuro 2 hung after 10-20 min each time
    #         (error 914 until switched off); never in colour mode.
    # colour: the same              music: music data, short fades (opt-in)
    # jump:   music data, instant changes (opt-in)
    method: str = "auto"
    codes: tuple[str, ...] = ()  # the light's data points from the Tuya account
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
        self.transition = 1
        self.fade_ms = COLOUR_FADE_MS  # how long the light takes to fade to a new color
        self.connected = False
        self.status = STATUS_CONNECTING
        self.latency_ms: float | None = None  # measured: color sent -> bulb confirmed
        self.no_ack = False  # the light never confirms colors (only timeouts)
        self._last_value: tuple | None = None  # what the light shows now (mode, transition, hex)
        self._last_probe = 0.0  # last color sent with a confirmation
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
        self._connected_at = 0.0

    # ------------------------------------------------------------ public API

    def set_color(self, rgb: RGB) -> None:
        with self._lock:
            self._target = rgb
            self._source_key = None
            self._restore_requested = False
        self._wake.set()

    # Settings that need a new connection; everything else applies on the fly.
    CONNECTION_FIELDS = ("id", "ip", "local_key", "version", "method", "codes")

    def needs_reconnect(self, cfg: BulbConfig) -> bool:
        return any(getattr(cfg, f) != getattr(self.cfg, f) for f in self.CONNECTION_FIELDS)

    def update_config(self, cfg: BulbConfig) -> None:
        """New source, zone, brightness, rate, ...: no reconnect, no flash of the HA state."""
        with self._lock:
            self.cfg = cfg
            self._source_key = None  # recompute the effect color with the new settings
            self._sent = None        # and send it even if it looks the same
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
        renewing = False
        while not self._stopping.is_set():
            try:
                if not failures and not renewing:
                    self.status = STATUS_CONNECTING
                self._connect(quiet=renewing)
                renewing = False
                if failures:
                    _LOGGER.info("%s: reachable again", self.cfg.name)
                failures = 0
                sends_before = self._send_count
                self._connected_at = time.monotonic()
                try:
                    self._loop()
                except _Renew:
                    _LOGGER.debug("%s: renewing the connection", self.cfg.name)
                    renewing = True  # no "connecting" status, no log line every time
                    self._disconnect()
                    continue
                except Exception as err:  # noqa: BLE001
                    if self.use_music and ("914" in str(err) or "device key" in str(err)):
                        _music_hangs.add(self.cfg.id)
                        _LOGGER.warning("%s: stopped answering properly in music mode (error 914) "
                                        "- using colour mode from now on; if it keeps failing, "
                                        "switch it off and on", self.cfg.name)
                    if isinstance(err, BulbOffline):
                        _record_drop_age(self.cfg.id, self.cfg.name,
                                         time.monotonic() - self._connected_at)
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

    def _connect(self, quiet: bool = False) -> None:
        dev = self._factory(self.cfg)
        status = dev.status()
        if isinstance(status, dict) and str(status.get("Err")) in OFFLINE_ERRORS:
            dev.close()
            raise BulbOffline("not reachable (switched off?)")
        if not status or "dps" not in status:
            dev.close()
            raise ConnectionError(f"no status from bulb ({status})")
        if not dev.bulb_configured or not dev.dpset.get("colour"):
            dev.close()
            raise UnsupportedBulb("device is not a color (RGB) bulb")
        set_timeout = getattr(dev, "set_socketTimeout", None)
        if callable(set_timeout):
            set_timeout(REPLY_TIMEOUT)  # connected: don't wait 3 s for a missing answer
        self._dev = dev
        self.dps = sorted((str(k) for k in status["dps"]), key=lambda k: (len(k), k))
        method = self.cfg.method
        can_music = bool(dev.dpset.get("music"))
        self.use_music = can_music and method in ("music", "jump")
        if self.use_music and self.cfg.id in _music_hangs:
            self.use_music = False  # it hung in music mode before: colour mode instead
        # Razer profiles flow smoothly, so the lights fade too (music data with
        # transition 1 = gradient); only "jump" changes instantly (transition 0).
        self.transition = 0 if method == "jump" else 1
        if self.use_music:
            self.fade_ms = 0.0 if self.transition == 0 else MUSIC_FADE_MS
        else:
            self.fade_ms = COLOUR_FADE_MS
        if method == "music" and not dev.dpset.get("music"):
            _LOGGER.warning("%s: music mode not supported, using colour mode", self.cfg.name)
        self._sent = None
        self._last_value = None  # unknown after (re)connecting: send in full
        self._scene_sent = None  # a light that rebooted lost its effect: send it again
        # _in_sync_mode survives reconnects: the bulb may still show our color.
        self.connected = True
        self.status = STATUS_SYNC if self._in_sync_mode else STATUS_READY
        _LOGGER.log(
            logging.DEBUG if quiet else logging.INFO,
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
        last_beat = time.monotonic()
        with self._lock:
            if self._in_sync_mode and self._target is None:
                # A restore was interrupted by a connection error: retry it now.
                self._restore_requested = True
                self._wake.set()
            elif self._target is not None:
                self._wake.set()  # (re)connected: show the current color right away
        while not self._stopping.is_set():
            following = callable(self._target)
            timeout = self._interval() if following else min(5.0, HEARTBEAT_EVERY)
            elapsed = time.monotonic()
            timeout = min(timeout, max(0.01, HEARTBEAT_EVERY - (elapsed - last_beat)))
            max_age = _max_age.get(self.cfg.id, 0.0)
            if max_age:  # wake up in time to renew the connection
                timeout = min(timeout, max(0.01, max_age - (elapsed - self._connected_at)))
            self._wake.wait(timeout=timeout)
            self._wake.clear()
            if self._stopping.is_set():
                return
            with self._lock:
                target, restore = self._target, self._restore_requested
                self._restore_requested = False
            now = time.monotonic()
            max_age = _max_age.get(self.cfg.id, 0.0)
            if max_age and now - self._connected_at > max_age and self._capture is None:
                raise _Renew
            if now - last_beat > HEARTBEAT_EVERY:
                # Always, also while colors are flowing: some lights expect it.
                check_reply(self._dev.heartbeat(nowait=False), "heartbeat")
                last_beat = now
            if self._capture is not None:
                done, result = self._capture
                self._capture = None
                try:
                    reply = self._dev.status()
                    if isinstance(reply, dict) and "dps" in reply:
                        result["dps"] = dict(reply["dps"])
                finally:
                    done.set()  # never leave the caller waiting, even on a network error
                continue
            if self._hold:
                continue  # the heartbeat above keeps the connection alive
            if restore:
                self._restore()
                self._scene_sent = None
                continue
            if isinstance(target, NativeScene):
                if self._scene_sent != target:
                    self._send_scene(target)
                    last_send = time.monotonic()
                continue
            if target is None:
                continue  # idle: only the heartbeat above
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
            last_send = time.monotonic()

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
        full = not self._in_sync_mode or scene_was_on
        hexval = tinytuya.BulbDevice.rgb_to_hexvalue(r, g, b, dps["value_hexformat"])
        value_key = (self.use_music, self.transition, hexval)
        if not full and value_key == self._last_value:
            # Exactly what the light already shows (a refresh, or two colors that
            # round to the same value). Tuya lights only answer when a value
            # changes: waiting for an answer here blocked the light for the 3 s
            # timeout (the GU10s in breathing; "lights freeze now and then").
            if self.use_music:
                dev.set_music_colour(self.transition, r, g, b, nowait=True)
            else:
                dev.set_multiple_values({dps["colour"]: hexval}, nowait=True)
            self._sent = rgb
            return
        if self.use_music:
            if full:
                dev.set_multiple_values({dps["switch"]: True, dps["mode"]: "music"})
            # Lights never answer music data: don't wait for it (that stalled
            # every color for the 3 s timeout); their delay is not measured.
            dev.set_music_colour(self.transition, r, g, b, nowait=True)
            self._last_value = value_key
            self._in_sync_mode = True
            self.status = STATUS_SYNC
            self._sent = rgb
            self._send_count += 1
            return
        else:
            values = {dps["colour"]: hexval}
            if full:
                values[dps["switch"]] = True
                if dps.get("mode"):
                    values[dps["mode"]] = "colour"
            now = time.monotonic()
            if not full and now - self._last_probe < CONFIRM_EVERY:
                # Lights don't always answer a color (GU10s: 3 s stalls in Spectrum, 1.9.17
                # log). Waiting for every answer froze the light; only every few seconds
                # one color is confirmed - to measure the delay and notice a lost light.
                dev.set_multiple_values(values, nowait=True)
                self._last_value = value_key
                self._in_sync_mode = True
                self.status = STATUS_SYNC
                self._sent = rgb
                self._send_count += 1
                return
            self._last_probe = now
            started = time.perf_counter()
            check_reply(dev.set_multiple_values(values), "color")
        self._last_value = value_key
        took = (time.perf_counter() - started) * 1000
        if took >= NO_ACK_MS:
            # No confirmation, only the timeout (seen with music data on GU10 bulbs):
            # that is not the light's delay - don't send effects 3 s ahead for it.
            if not self.no_ack:
                _LOGGER.warning("%s: a color was not confirmed (waited %.0f ms) in %s mode%s",
                                self.cfg.name, took, "music" if self.use_music else "colour",
                                " - choose 'Automatycznie'" if self.use_music else "")
            self.no_ack = True
        else:
            self._record_latency(took)
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
        self._last_value = None
        self._scene_sent = scene
        self._sent = None
        self.status = STATUS_SCENE
        self._send_count += 1
        _LOGGER.info("%s: playing its own effect %s", self.cfg.name, values)

    def _interval(self) -> float:
        return max(self.cfg.min_interval, self.slow_interval)

    def _slow_down(self, dropped_off: bool = False) -> None:
        """The bulb stopped confirming colors: send them less often for a while."""
        dev_id = self.cfg.id
        if dropped_off and dev_id in _rate_innocent:
            return  # slowing down didn't help before: keep the colors in sync
        if dropped_off and _learned_slow.get(dev_id, 0.0) >= SLOW_MAX:
            _rate_innocent.add(dev_id)
            _learned_slow.pop(dev_id, None)
            self.slow_interval = 0.0
            _LOGGER.warning("%s: keeps dropping off the network even at 1 color per second - the "
                            "rate is not the cause (check its power supply and Wi-Fi); back to "
                            "the normal rate", self.cfg.name)
            return
        self.slow_interval = min(max(self.slow_interval * 2, self.cfg.min_interval * 2, 0.3), SLOW_MAX)
        self._last_problem = time.monotonic()
        if dropped_off and self.cfg.id in _max_age:
            _LOGGER.info("%s: sending at most every %.2f s for a while", self.cfg.name, self.slow_interval)
        elif dropped_off:
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
        self._last_value = None
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
