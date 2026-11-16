"""Background sync: Razer Chroma -> Tuya bulbs (and, in full sync, profile -> Razer)."""

from __future__ import annotations

from collections.abc import Callable
import dataclasses
import functools
import logging
import math
import os
import threading
import time
from typing import Any

from .bulbs import STATUS_UNSUPPORTED, BulbConfig, BulbWorker, NativeScene
from .colors import adjust, reduce_colors, zone_indices
from .config import AppConfig
from .inputhook import KeyboardHook
from .player import EffectPlayer
from .profiles import Profile
from .razer import (
    DEVICES,
    ChromaApps,
    ChromaReader,
    Frame,
    RazerUnavailable,
    RGB,
    razer_service_running,
)
from .razer_out import NativeChroma
from . import razer_devices
from .render import KEYBOARD_AREAS, Renderer
from .screen import ScreenSampler

SERVICE_CHECK_INTERVAL = 3.0
KEY_EFFECTS = {"reactive", "ripple"}
DEVICE_EFFECTS = {"reactive", "ripple", "ambient"}  # bulbs sample a device zone
# Profiles in which a light with a captured built-in effect plays that instead
SCENE_EFFECTS = {"wave", "spectrum", "wheel"}

_LOGGER = logging.getLogger(__name__)


def bulb_color(cfg: BulbConfig, frame: Frame, keyboard_area=None) -> RGB:
    """The color a bulb should show for a device frame."""
    assert frame.colors is not None
    return zone_color(cfg, frame.colors, keyboard_area)


LINK_LEDS = 5  # Chroma Link: one "LED" per linked device


def link_slot(workers: list, worker) -> tuple[int, int] | None:
    """(position, count) of a light among the lights on the same whole Razer device.

    Averaging a whole device mixes its colors (a wave turns grey). Instead, the
    lights on one device share it: on Chroma Link one LED each (one color per
    linked device), on other devices one part each from left to right, and a
    single light shows the device's middle. The keyboard is handled as the
    wave across all lights instead (see follows_bulb_strip).
    """
    cfg = worker.cfg
    if cfg.zone != "all" or getattr(cfg, "source", "keyboard") == "keyboard":
        return None
    group = [w for w in workers if w.cfg.zone == "all" and w.cfg.source == cfg.source]
    if worker not in group:
        return None
    return group.index(worker), len(group)


def link_color(cfg: BulbConfig, colors: list[RGB], slot: tuple[int, int] | int) -> RGB:
    """The color of this light's part of its Razer device."""
    position, count = slot if isinstance(slot, tuple) else (slot, 1)
    spec = DEVICES[cfg.source]
    if not colors:
        return (0, 0, 0)
    if cfg.source == "chromalink":
        rgb = colors[position % len(colors)]
        if max(rgb) < 8 and max(colors[0]) >= 8:
            rgb = colors[0]  # a game that only sets the first LED: don't go dark
        return adjust(rgb, cfg.brightness, cfg.min_brightness)
    w = spec.width
    if count <= 1:
        cols = [w // 2]  # one light: the middle of the device (e.g. the mouse logo)
    else:
        first = position * w // count
        cols = list(range(first, max(first + 1, (position + 1) * w // count)))
    part = [colors[y * w + x] for y in range(spec.height) for x in cols if y * w + x < len(colors)]
    rgb = reduce_colors(part, cfg.mode)
    if max(rgb) < 8:
        rgb = reduce_colors(colors, cfg.mode)  # that part is dark (e.g. no LED there): whole device
    return adjust(rgb, cfg.brightness, cfg.min_brightness)


def zone_color(cfg: BulbConfig, colors: list[RGB], keyboard_area=None) -> RGB:
    area = keyboard_area if cfg.source == "keyboard" else None
    leds = [colors[i] for i in zone_indices(cfg.source, cfg.zone, area) if i < len(colors)]
    return adjust(reduce_colors(leds, cfg.mode), cfg.brightness, cfg.min_brightness)


def _alive(worker) -> bool:
    is_alive = getattr(worker, "is_alive", None)
    return bool(is_alive()) if callable(is_alive) else False


def detect_keyboard_layout() -> str:
    return razer_devices.keyboard_layout(razer_devices.detect())


# Chroma apps that are not games (Synapse itself, this program)
NOT_GAMES = ("razer", "synapse", "tuyachroma")


def other_chroma_apps(apps: ChromaApps | None, own_pid: int,
                      alive: Callable[[int], bool] | None = None) -> dict[int, str]:
    """Running Chroma apps other than this program and Synapse: games (or e.g. Wallpaper Engine)."""
    if apps is None:
        return {}
    if alive is None:
        from .winsys import process_alive as alive  # noqa: PLC0415
    return {pid: name for pid, name in apps.apps.items()
            if pid != own_pid and not any(n in name.lower() for n in NOT_GAMES) and alive(pid)}


class AliveCache:
    """process_alive with results kept for a moment (it is asked every frame)."""

    def __init__(self, alive: Callable[[int], bool] | None = None, ttl: float = 2.0) -> None:
        self._alive = alive
        self._ttl = ttl
        self._seen: dict[int, tuple[float, bool]] = {}

    def __call__(self, pid: int) -> bool:
        now = time.monotonic()
        hit = self._seen.get(pid)
        if hit is None or now - hit[0] > self._ttl:
            alive = self._alive
            if alive is None:
                from .winsys import process_alive as alive  # noqa: PLC0415
            hit = (now, bool(alive(pid)))
            self._seen[pid] = hit
        return hit[1]


# A newly seen Chroma app gets the Razer devices for this long; if it shows no
# effect in that time it is a background app (e.g. a wallpaper program), not a
# game, and the program takes the devices back and ignores it.
# A game's effect that pauses for a moment (Cyberpunk 2077: ~70 ms every
# ~0.7 s) is held: the lights keep the game's last colors instead of
# flickering to the program's profile and back.
GAME_HOLD = 2.0
GAME_PROBATION = 60.0
# Full sync: after a game the Razer devices must show our effect again. If
# Synapse keeps its own profile (current app not us) this long, the Chroma SDK
# is opened again - every RECLAIM_EVERY s, at most RECLAIM_TRIES times.
RECLAIM_AFTER = 3.0
RECLAIM_EVERY = 5.0
RECLAIM_TRIES = 5  # games may show effects only after loading


def is_game_active(frames: dict[str, Frame | None], apps: ChromaApps | None,
                   own_pid: int, full_sync: bool,
                   alive: Callable[[int], bool] | None = None) -> bool:
    """Does a game (another Chroma app) control the lighting right now?"""
    any_active = any(f is not None and f.active for f in frames.values())
    if not any_active:
        return False
    if apps is not None and apps.current_pid:
        if apps.current_pid == own_pid:
            return False
        # Synapse shows another app's effect - unless that app is gone (a game
        # that crashed stays "current" with its last frame in the memory)
        return alive is None or alive(apps.current_pid)
    # No app list: in full sync our own effect is in the frames, so we can't tell.
    return not full_sync


SCREEN_W, SCREEN_H = 48, 27  # the screen sampled for Ambient (16:9)


def ambient_color(screen, cfg, index: int, count: int) -> RGB | None:
    """Ambient: the color of this light's part of the screen.

    Lights split the screen from left to right in list order (3 lights: left,
    middle, right part); a light set to a zone (left/center/right/top/bottom)
    shows that part. Colorful areas count more than dark ones (taskbar, black
    bars), so a light shows what stands out on the screen.
    """
    try:
        pixels = screen(SCREEN_W, SCREEN_H)
    except Exception:  # noqa: BLE001 - screen capture can fail (locked screen, UAC)
        return None
    if not pixels or len(pixels) != SCREEN_W * SCREEN_H:
        return None
    zone = cfg.zone if isinstance(cfg.zone, str) else "all"
    cols, rows = range(SCREEN_W), range(SCREEN_H)
    third = SCREEN_W // 3
    if zone == "left":
        cols = range(0, third)
    elif zone == "center":
        cols = range(third, SCREEN_W - third)
    elif zone == "right":
        cols = range(SCREEN_W - third, SCREEN_W)
    elif zone == "top":
        rows = range(0, SCREEN_H // 2)
    elif zone == "bottom":
        rows = range(SCREEN_H // 2, SCREEN_H)
    elif count > 1:
        first = index * SCREEN_W // count
        cols = range(first, max(first + 1, (index + 1) * SCREEN_W // count))
    part = [pixels[y * SCREEN_W + x] for y in rows for x in cols]
    return adjust(reduce_colors(part, cfg.mode), cfg.brightness, cfg.min_brightness)


def follows_bulb_strip(cfg, profile: Profile) -> bool:
    """Is this light one point of the profile running across all lights?

    Lights set to the whole keyboard are (a wave runs from light to light).
    A light set to another Razer device (mouse, Chroma Link, ...) or to a part
    of the keyboard shows the color of that device/part at the same moment.
    """
    return (cfg.zone == "all" and getattr(cfg, "source", "keyboard") == "keyboard"
            and profile.effect not in DEVICE_EFFECTS)


def dispatch(
    workers: list,
    frames: dict[str, Frame | None],
    idle: str,
    game_active: bool = True,
    profile_frames: dict[str, list[RGB]] | None = None,
    strip: list[RGB] | None = None,
    profile: Profile | None = None,
    allow_effect: Callable[[Any], bool] = lambda _w: True,
    keyboard_area=None,
    effect_color: Callable[[Any, int, int], RGB] | None = None,
    effect_source: Callable[[Any, int, int], tuple[Any, Callable[[], RGB]]] | None = None,
) -> bool:
    """Send colors to the bulb workers. Returns True if a game color was used."""
    used_game = False
    # While a game runs, a light whose own Razer device gets nothing from the game
    # follows the game's other devices (e.g. only the keyboard), not the profile.
    fallback = None
    if game_active:
        fallback = next((frames[s] for s in ("keyboard", "chromalink", "mouse", "mousepad",
                                             "headset", "keypad")
                         if frames.get(s) is not None and frames[s].active), None)
    for index, worker in enumerate(workers):
        frame = frames.get(worker.cfg.source)
        slot = link_slot(workers, worker)
        if game_active and frame is not None and frame.active and slot is not None:
            worker.set_color(link_color(worker.cfg, frame.colors, slot))
            used_game = True
        elif game_active and frame is not None and frame.active:
            worker.set_color(bulb_color(worker.cfg, frame, keyboard_area))
            used_game = True
        elif game_active and fallback is not None:
            cfg = (dataclasses.replace(worker.cfg, source=fallback.device, zone="all")
                   if dataclasses.is_dataclass(worker.cfg) else worker.cfg)
            worker.set_color(bulb_color(cfg, fallback, keyboard_area))
            used_game = True
        elif (idle in ("full", "effect") and profile is not None and profile.effect in SCENE_EFFECTS
              and getattr(worker.cfg, "scene", None) and hasattr(worker, "set_scene")):
            worker.set_scene(NativeScene.from_state(worker.cfg.scene))  # the light animates it
        elif idle in ("full", "effect") and effect_source is not None:
            key, source = effect_source(worker, index, len(workers))
            if hasattr(worker, "set_color_source"):
                worker.set_color_source(source, key)  # the bulb computes the color when sending
            else:
                worker.set_color(source())
        elif idle in ("full", "effect") and effect_color is not None:
            if allow_effect(worker):  # bulbs change at most bulb_effect_rate times per second
                worker.set_color(effect_color(worker, index, len(workers)))
        elif idle in ("full", "effect") and profile_frames is not None:
            if not allow_effect(worker):
                continue  # bulbs change at most bulb_effect_rate times per second
            if (strip and index < len(strip)
                    and (profile is None or follows_bulb_strip(worker.cfg, profile))):
                rgb = adjust(strip[index], worker.cfg.brightness, worker.cfg.min_brightness)
            elif slot is not None and profile_frames.get(worker.cfg.source):
                rgb = link_color(worker.cfg, profile_frames[worker.cfg.source], slot)
            else:
                rgb = zone_color(worker.cfg, profile_frames.get(worker.cfg.source, []), keyboard_area)
            worker.set_color(rgb)
        elif idle == "restore":
            worker.release()
    return used_game


class EffectClock:
    """One clock for the effect colors of all bulbs.

    Bulbs connect one by one (and reconnect after a hiccup). With a separate
    schedule each, they sampled a moving effect at different moments and
    showed different colors at once. Now every bulb changes on the same tick,
    to the color of that tick's moment; one that connects late joins at once.
    """

    def __init__(self, step: float) -> None:
        self.step = step
        self.tick = 0
        self._last: dict[int, int] = {}

    def update(self, now: float) -> None:
        self.tick = math.floor(now / self.step)

    @property
    def time(self) -> float:
        """The moment of the effect a bulb shows during this tick.

        The middle of the tick: the bulb holds one color for the whole tick
        while the Razer devices keep moving, so it is never more than half a
        tick ahead of or behind them (with the tick's start it lagged a whole
        tick - 1/5 of the color wheel in Spectrum at 1 change per second).
        """
        return (self.tick + 0.5) * self.step

    def reset(self) -> None:
        """New profile: every bulb changes right away, together with the Razer devices."""
        self._last.clear()

    def allow(self, worker) -> bool:
        if self._last.get(id(worker)) != self.tick:
            self._last[id(worker)] = self.tick
            return True
        return False


class SyncService:
    """Owns the bulb workers, the Razer reader and (optionally) the effect player."""

    def __init__(
        self,
        reader_factory: Callable[[list[str]], ChromaReader] = ChromaReader,
        worker_factory: Callable[[BulbConfig], BulbWorker] = BulbWorker,
        service_check: Callable[[], bool] = razer_service_running,
        output_factory: Callable[[], Any] = NativeChroma,
        keyhook_factory: Callable[[Callable[[int, int], None]], Any] = KeyboardHook,
        screen_factory: Callable[[], Any] = ScreenSampler,
        own_pid: int | None = None,
        layout_detector: Callable[[], str] | None = None,
    ) -> None:
        self._layout_detector = layout_detector or (lambda: detect_keyboard_layout())
        self.keyboard_layout = "full"
        self._reader_factory = reader_factory
        self._worker_factory = worker_factory
        self._service_check = service_check
        self._output_factory = output_factory
        self._keyhook_factory = keyhook_factory
        self._screen_factory = screen_factory
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.workers: list[BulbWorker] = []
        self.player: EffectPlayer | None = None
        self._hook = None
        self.razer_status = "zatrzymane"
        self.razer_level = "warn"  # "ok" / "warn" / "error", set with every status
        self._settings = None
        self._background_apps: set[int] = set()
        self.game_active = False
        self.game_name = ""
        self.other_apps: dict[int, str] = {}  # running Chroma apps treated as games
        self.razer_kept_by_synapse = False  # Synapse didn't give the devices back
        self._profile: Profile | None = None
        self._sources: list[str] = []
        # Workers still giving their bulb back after a stop/update. A new
        # connection to the same bulb waits for them: many Tuya bulbs accept
        # only one local connection at a time.
        self._retiring: list[BulbWorker] = []

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, config: AppConfig) -> None:
        with self._lock:
            if self.running:
                return
            s = config.settings
            self.keyboard_layout = self._resolve_layout(s.keyboard_layout)
            configs = [d.bulb_config() for d in config.enabled_devices]
            self._wait_for_retiring({c.id for c in configs})
            self.workers = [self._worker_factory(c) for c in configs]
            for w in self.workers:
                w.start()
            self.player = None
            self._profile = s.profile
            self._settings = s
            if s.idle in ("full", "effect"):
                profile = s.profile
                screen = self._screen_factory() if profile.effect == "ambient" else None
                renderer = Renderer(profile, screen=screen)
                renderer.keyboard_area = KEYBOARD_AREAS[self.keyboard_layout]
                self.player = EffectPlayer(
                    renderer, bulb_count=len(self.workers),
                    output_factory=self._output_factory if s.idle == "full" else None,
                    fps=s.fps,
                )
                self.player.paused = True  # until we know no game is running
                self.player.start()
                if profile.effect in KEY_EFFECTS:
                    self._hook = self._keyhook_factory(renderer.key_pressed)
                    self._hook.start()
            self._stop.clear()
            sources = self._needed_sources(configs, s.idle)
            self._sources = sources
            self._thread = threading.Thread(target=self._run, args=(sources, s), name="razer-reader",
                                            daemon=True)
            self._thread.start()
            _LOGGER.info("Sync started (%s) for %d bulb(s)", s.idle, len(self.workers))

    def heal(self, config: AppConfig) -> list[str]:
        """Watchdog: bring back whatever stopped by an unexpected error.

        The sync thread, the effect player and every light run in their own
        threads and catch their errors - but a bug could still end one. Then
        the light (or everything) would stay dead until the program restarts.
        Returns what was restarted (for the log / status).
        """
        healed: list[str] = []
        with self._lock:
            thread, player = self._thread, self.player
            if thread is None or self._stop.is_set():
                return healed  # not running on purpose
            broken = (not thread.is_alive() and bool(self._sources)) or (
                player is not None and not player.is_alive())
            if not broken:
                for i, worker in enumerate(self.workers):
                    if not hasattr(worker, "is_alive") or worker.is_alive():
                        continue
                    if getattr(worker, "status", "") == STATUS_UNSUPPORTED:
                        continue  # ended on purpose: a white-only light
                    _LOGGER.error("%s: its thread stopped unexpectedly - starting it again",
                                  worker.cfg.name)
                    fresh = self._worker_factory(worker.cfg)
                    fresh.start()
                    self.workers[i] = fresh
                    healed.append(worker.cfg.name)
                return healed
        _LOGGER.error("Sync %s stopped unexpectedly - restarting the sync",
                      "loop" if not thread.is_alive() else "effect player")
        self.restart(config)
        return ["synchronizacja"]

    def stop(self, timeout: float = 5.0) -> None:
        """Give all bulbs back (restore their state), hand Razer devices back to Synapse."""
        with self._lock:
            deadline = time.monotonic() + timeout
            self._stop.set()
            for w in self.workers:
                w.request_stop()
            if self._hook is not None:
                self._hook.stop()
                self._hook = None
            if self.player is not None:
                self.player.stop(max(0.1, deadline - time.monotonic()))
                self.player = None
            for w in self.workers:
                w.join(max(0.1, deadline - time.monotonic()))
            # still restoring (slow bulb): remember them so a restart doesn't collide
            self._retiring += [w for w in self.workers if _alive(w)]
            if self._thread is not None:
                self._thread.join(max(0.1, deadline - time.monotonic()))
            self._thread = None
            self.workers = []
            self._set_status("zatrzymane", "warn")
            self.game_active = False
            self.game_name = ""
            _LOGGER.info("Sync stopped")

    @staticmethod
    def _needed_sources(configs: list[BulbConfig], idle: str) -> list[str]:
        sources = sorted({c.source for c in configs})
        if idle == "full" and "keyboard" not in sources:
            # detect games (they nearly always drive the keyboard) and follow them
            sources = sorted({*sources, "keyboard"})
        return sources

    def _wait_for_retiring(self, bulb_ids: set[str], timeout: float = 8.0) -> None:
        """Let previous connections to these bulbs finish first."""
        deadline = time.monotonic() + timeout
        for w in list(self._retiring):
            if w.cfg.id in bulb_ids and _alive(w):
                w.join(max(0.0, deadline - time.monotonic()))
        self._retiring = [w for w in self._retiring if _alive(w)]

    def update_bulbs(self, config: AppConfig) -> bool:
        """Apply a changed bulb selection/settings without a full restart.

        Unchanged bulbs keep running (no flicker) and the Razer devices keep
        their effect. Returns False if a full restart is needed instead.
        """
        with self._lock:
            if not self.running:
                return False
            configs = [d.bulb_config() for d in config.enabled_devices]
            wanted_sources = self._needed_sources(configs, config.settings.idle)
            if wanted_sources != self._sources:
                if not wanted_sources or not self._sources:
                    return False  # nothing was / will be read: a full (re)start
                # Another Razer device must be read: only the reader reopens, the
                # lights and the Razer devices keep running (no flicker).
                self._sources = wanted_sources
                _LOGGER.info("Reading Razer devices: %s", ", ".join(wanted_sources))
            current = {w.cfg.id: w for w in self.workers}
            keep: dict[str, BulbWorker] = {}
            for bulb_id, worker in current.items():
                wanted = next((c for c in configs if c.id == bulb_id), None)
                if wanted is not None and wanted == worker.cfg:
                    keep[bulb_id] = worker
                elif (wanted is not None and hasattr(worker, "update_config")
                      and not worker.needs_reconnect(wanted)):
                    worker.update_config(wanted)  # applied live: the light stays taken over
                    keep[bulb_id] = worker
                else:
                    worker.request_stop()  # removed or changed: give it back first
                    self._retiring.append(worker)
            workers = []
            for cfg in configs:
                worker = keep.get(cfg.id)
                if worker is None:
                    self._wait_for_retiring({cfg.id})
                    worker = self._worker_factory(cfg)
                    worker.start()
                workers.append(worker)
            self.workers = workers
            if self.player is not None:
                self.player.bulb_count = len(workers)
            _LOGGER.info("Bulbs updated: %d bulb(s), %d unchanged", len(workers), len(keep))
            return True

    def _resolve_layout(self, setting: str) -> str:
        if setting in KEYBOARD_AREAS:
            return setting
        try:
            layout = self._layout_detector()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Keyboard layout detection failed")
            layout = "full"
        _LOGGER.info("Keyboard layout: %s", layout)
        return layout if layout in KEYBOARD_AREAS else "full"

    def apply_profile(self, config: AppConfig) -> bool:
        """Switch the playing profile without restarting (no flicker).

        Returns False if a restart is needed instead (e.g. not running).
        """
        with self._lock:
            if not self.running or self.player is None:
                return False
            profile = config.settings.profile
            renderer = self.player.renderer
            renderer.screen = self._screen_factory() if profile.effect == "ambient" else None
            renderer.set_profile(profile)
            if profile.effect in KEY_EFFECTS and self._hook is None:
                self._hook = self._keyhook_factory(renderer.key_pressed)
                self._hook.start()
            elif profile.effect not in KEY_EFFECTS and self._hook is not None:
                self._hook.stop()
                self._hook = None
            self._profile = profile
            _LOGGER.info("Profile switched to %s", profile.name)
            return True

    def restart(self, config: AppConfig) -> None:
        self.stop()
        if config.settings.sync_enabled:
            self.start(config)

    # Effects whose color at a moment depends only on the time (rendering them for
    # the window doesn't disturb the lights' own rendering state)
    # (Ambient isn't one: rendering it grabs the screen, which mustn't run on the
    # window's thread; the window shows the screen colors last sent instead.)
    STATELESS_EFFECTS = {"static", "spectrum", "breathing", "wave", "wheel"}

    def bulb_colors(self) -> dict[str, RGB | str | None]:
        """What each light shows now, for the window: a color, "scene" (its own
        effect) or None (not showing ours). Lights are sent effect colors ahead of
        time; the window shows the color of this moment, like the Razer devices."""
        player, profile, settings = self.player, self._profile, self._settings
        workers = list(self.workers)
        now = time.monotonic()
        live = (player is not None and profile is not None and settings is not None
                and not self.game_active and profile.effect in self.STATELESS_EFFECTS)
        out: dict[str, RGB | str | None] = {}
        for index, w in enumerate(workers):
            if not getattr(w, "_in_sync_mode", False):
                out[w.cfg.id] = None
            elif getattr(w, "_scene_sent", None) is not None:
                out[w.cfg.id] = "scene"
            elif live and callable(getattr(w, "_target", None)):
                try:
                    out[w.cfg.id] = self._bulb_effect_color(w, index, len(workers), settings, profile,
                                                            at=now, ahead=False)
                except Exception:  # noqa: BLE001 - e.g. stopping right now
                    out[w.cfg.id] = getattr(w, "_sent", None)
            else:
                out[w.cfg.id] = getattr(w, "_sent", None)
        return out

    def bulb_statuses(self) -> dict[str, str]:
        return {w.cfg.id: w.status for w in self.workers}

    def bulb_details(self) -> dict[str, dict]:
        """What each running bulb reported and how it behaves (for the settings advisor)."""
        from .bulbs import learned_max_age, learned_slow_interval, rate_not_the_cause  # noqa: PLC0415

        return {w.cfg.id: {
            "status": w.status,
            "connected": getattr(w, "connected", False),
            "latency_ms": getattr(w, "latency_ms", None),
            "dps": list(getattr(w, "dps", [])),
            "bulb_type": getattr(getattr(w, "_dev", None), "bulb_type", ""),
            "dropped_off": learned_slow_interval(w.cfg.id),
            "renew_every": learned_max_age(w.cfg.id),
            "rate_not_the_cause": rate_not_the_cause(w.cfg.id),
            "no_ack": getattr(w, "no_ack", False),
        } for w in self.workers}

    def bulb_latencies(self) -> dict[str, float | None]:
        """Measured delay per bulb (ms): color sent -> bulb confirmed."""
        return {w.cfg.id: getattr(w, "latency_ms", None) for w in self.workers}

    @staticmethod
    def latency_offset_ms(worker, settings) -> float:
        """How far ahead (ms) an effect color is sent to this bulb."""
        if settings.latency_mode == "auto":
            base = getattr(worker, "latency_ms", None) or 0.0
            base += getattr(worker, "fade_ms", 0.0)  # the light's fade ends on time
        elif settings.latency_mode == "manual":
            base = float(settings.latency_ms)
        else:
            base = 0.0
        return max(-1000.0, min(3000.0, base + getattr(worker.cfg, "latency_adjust_ms", 0)))

    def _bulb_effect_color(self, worker, index: int, count: int, settings, profile,
                           at: float | None = None, ahead: bool = True) -> RGB:
        """The profile's color for one bulb, rendered ahead by the bulb's delay.

        Effects are computed by the program, so the color the bulb will need a
        moment from now is known now: sending it early hides the Wi-Fi/bulb lag.
        """
        player = self.player
        assert player is not None
        cfg = worker.cfg
        base = time.monotonic() if at is None else at
        if at is None:
            # Precise sync: the light holds this color until its next one, so show
            # the middle of that time (ticks are already sampled in the middle).
            interval = getattr(worker, "_interval", None)
            base += interval() / 2 if callable(interval) else 0.0
        t = base + (self.latency_offset_ms(worker, settings) / 1000 if ahead else 0.0)
        renderer = player.renderer
        if profile.effect == "ambient" and renderer.screen is not None:
            rgb = ambient_color(renderer.screen, cfg, index, count)
            if rgb is not None:
                return rgb
        if follows_bulb_strip(cfg, profile):
            rgb = renderer.frame(f"bulbs@{cfg.id}", max(1, count), 1, t)[min(index, count - 1)]
            return adjust(rgb, cfg.brightness, cfg.min_brightness)
        spec = DEVICES[cfg.source]
        colors = renderer.frame(f"{cfg.source}@{cfg.id}", spec.width, spec.height, t)
        slot = link_slot(self.workers, worker)
        if slot is not None:
            return link_color(cfg, colors, slot)
        return zone_color(cfg, colors, KEYBOARD_AREAS[self.keyboard_layout])

    def _bulb_effect_source(self, worker, index: int, count: int, settings, profile, key):
        """(key, color function) for a bulb that computes its color when sending.

        All bulbs and the Razer devices draw the effect from one clock
        (time.monotonic); each bulb asks for the color of the moment it will
        show it (now + its delay), as often as it can take colors.
        """
        layout = self.keyboard_layout

        def color() -> RGB:
            return self._bulb_effect_color(worker, index, count, settings, profile)

        slot = link_slot(self.workers, worker)
        return (key, index, count, layout, worker.cfg.min_interval, slot), color

    # ---------------------------------------------------------------- thread

    def _release_all(self) -> None:
        for worker in self.workers:
            worker.release()
        self.game_active = False
        if self.player is not None:
            self.player.paused = True

    def _run(self, sources: list[str], settings) -> None:
        if not sources:
            self._set_status("nie wybrano żadnej żarówki", "warn")
            return
        idle = settings.idle
        full_sync = idle == "full"
        period = 1.0 / max(1.0, settings.fps)
        step = 1.0 / max(0.5, settings.bulb_effect_rate)
        clock = EffectClock(step)
        last_profile: tuple | None = None
        warned_no_apps = False
        reader: ChromaReader | None = None
        last_check = time.monotonic()

        last_error = ""
        last_others: dict[int, str] = {}
        was_game = False
        alive = AliveCache()
        last_good: dict[str, tuple[float, Frame]] = {}  # last game frame per device
        game_seen = 0.0                                  # last moment a game effect was seen
        held_name = ""
        on_trial: dict[int, float] = {}   # pid -> when we handed it the devices
        # Chroma apps that never showed an effect: kept across restarts, so an
        # always-running app (e.g. Wallpaper Engine) isn't handed the devices again
        background = self._background_apps
        self.other_apps = {}
        not_ours_since: float | None = None
        last_shown: int | None = None  # which app Synapse shows (logged on change)
        last_reclaim = 0.0
        reclaim_tries = 0
        try:
            while not self._stop.is_set():
                try:
                    if reader is not None and time.monotonic() - last_check > SERVICE_CHECK_INTERVAL:
                        last_check = time.monotonic()
                        if not self._service_check():
                            # Synapse closed / PC shutting down: shared memory keeps the
                            # last frame, so don't trust it - give the bulbs back.
                            _LOGGER.warning("Razer SDK service stopped, releasing bulbs")
                            self._release_all()
                            reader.close()
                            reader = None
                            continue
                    if reader is None:
                        try:
                            reader = self._reader_factory(sources)
                            self._set_status("połączono z Razer Chroma", "ok")
                        except (RazerUnavailable, OSError) as err:
                            self._set_status(f"Razer niedostępny: {err}", "error")
                            self._stop.wait(5)
                            continue
                    started = time.monotonic()
                    reader.wait(int(period * 1000))
                    try:
                        if self._sources != sources:  # a light now follows another device
                            reader.close()
                            reader = None
                            sources = list(self._sources)
                            continue
                        frames = {src: reader.read(src) for src in sources}
                        apps = reader.read_apps() if hasattr(reader, "read_apps") else None
                    except OSError as err:
                        self._set_status(f"utracono połączenie: {err}", "error")
                        reader.close()
                        reader = None
                        continue
                    if apps is None and full_sync and not warned_no_apps:
                        warned_no_apps = True
                        _LOGGER.warning("Synapse's Chroma app list is not available: games can't be "
                                        "told apart from our own effect in full sync mode")
                    others = other_chroma_apps(apps, self._own_pid, alive)
                    if others != last_others:
                        _LOGGER.info("Chroma apps running: %s",
                                     ", ".join(f"{n} ({p})" for p, n in others.items()) or "none besides us")
                        last_others = others
                    now = time.monotonic()
                    any_active = any(f is not None and f.active for f in frames.values())
                    current = apps.current_pid if apps is not None else 0
                    if not full_sync:
                        on_trial.clear()  # we don't hold the Razer devices: nothing to hand over
                    for pid in list(on_trial) if full_sync else []:
                        if pid not in others:
                            del on_trial[pid]
                        elif current == pid and any_active:
                            on_trial[pid] = float("inf")  # shows its effect: a game, keep it
                        elif now - on_trial[pid] > GAME_PROBATION:
                            del on_trial[pid]
                            background.add(pid)
                            _LOGGER.info("%s shows no Chroma effect - a background app, taking the "
                                         "Razer devices back", others[pid])
                    for pid in list(background):
                        if current == pid and any_active:
                            # a slow-loading game: its first effect came after the probation
                            background.discard(pid)
                            on_trial[pid] = float("inf")
                            _LOGGER.info("%s shows a Chroma effect - a game after all, handing it "
                                         "the Razer devices", others.get(pid, pid))
                    background &= set(others)
                    for pid in others if full_sync else []:
                        if pid not in on_trial and pid not in background:
                            on_trial[pid] = now
                    games = {p: n for p, n in others.items() if p not in background}
                    self.other_apps = games
                    if self.player is not None:
                        # Full sync: a game is running - hand it the Razer devices.
                        self.player.yielded = full_sync and bool(games)
                    raw_game = is_game_active(frames, apps, self._own_pid, full_sync, alive)
                    if raw_game:
                        game_seen = now
                        held_name = apps.current_name if apps else ""
                        for src, f in frames.items():
                            if f is not None and f.active:
                                last_good[src] = (now, f)
                    elif now - game_seen < GAME_HOLD:
                        # a short pause in the game's effect: keep its last colors
                        frames = {src: (last_good[src][1] if src in last_good
                                        and now - last_good[src][0] < GAME_HOLD else f)
                                  for src, f in frames.items()}
                    else:
                        last_good.clear()
                    self.game_active = raw_game or now - game_seen < GAME_HOLD
                    self.game_name = (apps.current_name if raw_game and apps else held_name) \
                        if self.game_active else ""
                    if apps is not None and apps.current_pid != last_shown:
                        last_shown = apps.current_pid
                        _LOGGER.info("Synapse shows: %s", "this program" if last_shown == self._own_pid
                                     else f"{apps.current_name or '?'} ({last_shown})" if last_shown
                                     else "its own profile")
                    player = self.player
                    if (full_sync and player is not None and apps is not None and not raw_game
                            and not player.yielded and getattr(player, "output_ready", False)):
                        if apps.current_pid == self._own_pid:
                            if reclaim_tries:
                                _LOGGER.info("Razer devices show the program's effect again")
                            not_ours_since, reclaim_tries = None, 0
                            self.razer_kept_by_synapse = False
                        elif not_ours_since is None:
                            not_ours_since = now
                        elif (now - not_ours_since > RECLAIM_AFTER and now - last_reclaim > RECLAIM_EVERY
                              and reclaim_tries < RECLAIM_TRIES):
                            reclaim_tries += 1
                            last_reclaim = now
                            _LOGGER.warning(
                                "Synapse shows %s instead of the program's effect - reconnecting the "
                                "Chroma SDK (%d/%d)",
                                f"{apps.current_name} ({apps.current_pid})" if apps.current_pid
                                else "its own profile", reclaim_tries, RECLAIM_TRIES)
                            player.reconnect()
                            if reclaim_tries == RECLAIM_TRIES:
                                self.razer_kept_by_synapse = True
                                _LOGGER.warning("Razer devices stay on the Synapse profile: in Synapse "
                                                "check Chroma > Chroma Apps (TuyaChroma allowed, on top)")
                    else:
                        not_ours_since = None
                        if raw_game or (player is not None and player.yielded):
                            # a new game: after it, the devices get their own fresh tries
                            reclaim_tries = 0
                            self.razer_kept_by_synapse = False
                    if self.game_active != was_game:
                        was_game = self.game_active
                        if self.game_active:
                            _LOGGER.info("Game effect active: %s (sources: %s)", self.game_name or "?",
                                         ", ".join(s for s, f in frames.items() if f is not None and f.active))
                        else:
                            _LOGGER.info("Game effect ended")
                    profile_frames = strip = None
                    if self.player is not None:
                        self.player.paused = self.game_active
                        profile_frames, strip = self.player.latest()
                    profile = self._profile or settings.profile
                    effect_color = effect_source = None
                    clock.update(time.monotonic())
                    key = dataclasses.astuple(profile)  # any change in the profile
                    if key != last_profile:
                        if last_profile is not None:
                            clock.reset()
                        last_profile = key
                    if self.player is not None and settings.effect_sync == "precise":
                        effect_source = functools.partial(self._bulb_effect_source,
                                                          settings=settings, profile=profile, key=key)
                    elif self.player is not None:
                        # every bulb shows the effect at the same moment: the tick's middle
                        effect_color = functools.partial(self._bulb_effect_color,
                                                         settings=settings, profile=profile,
                                                         at=clock.time)
                    dispatch(self.workers, frames, idle, self.game_active, profile_frames, strip,
                             profile, clock.allow, KEYBOARD_AREAS[self.keyboard_layout], effect_color,
                             effect_source)
                    self._set_status(*self._status_text(idle, profile.name))
                    spent = time.monotonic() - started
                    if spent < period:
                        self._stop.wait(period - spent)
                    last_error = ""
                except Exception as err:  # noqa: BLE001
                    # Never stop syncing for good: give the bulbs back, then try again.
                    if repr(err) != last_error:
                        _LOGGER.exception("Sync loop error - retrying in 5 s")
                        last_error = repr(err)
                    self._set_status("błąd odczytu Razer (szczegóły w logu) – ponawiam…", "error")
                    self._release_all()
                    if reader is not None:
                        try:
                            reader.close()
                        except Exception:  # noqa: BLE001
                            pass
                        reader = None
                    self._stop.wait(5)
        finally:
            if reader is not None:
                reader.close()  # releases the mutexes from the owning thread

    def _set_status(self, text: str, level: str) -> None:
        self.razer_level = level
        self.razer_status = text

    def status_level(self) -> str:
        """"ok", "warn" or "error" for razer_status (the window's status dot)."""
        return self.razer_level

    def _status_text(self, idle: str, profile_name: str) -> tuple[str, str]:
        """The status line and its level."""
        if self.game_active:
            name = f" ({self.game_name})" if self.game_name else ""
            return f"efekt z gry aktywny{name}", "ok"
        if idle == "full":
            player = self.player
            if getattr(self, "razer_kept_by_synapse", False):
                return ("urządzenia Razer zostały przy profilu Synapse – w Synapse: Chroma → "
                        "Aplikacje Chroma, zezwól TuyaChroma"), "error"
            if player is not None and player.yielded:
                names = ", ".join(getattr(self, "other_apps", {}).values())
                return f"gra działa ({names}) – urządzenia Razer oddane grze", "ok"
            if player is not None and "niedostępne" in player.output_status:
                return player.output_status, "error"  # e.g. Chroma Apps disabled in Synapse
            return f"pełna synchronizacja – profil „{profile_name}”", "ok"
        if idle == "effect":
            return f"brak gry – profil „{profile_name}” na żarówkach", "ok"
        return "połączono, czekam na grę", "ok"
