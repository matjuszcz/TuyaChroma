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

from .bulbs import BulbConfig, BulbWorker, NativeScene
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


def is_game_active(frames: dict[str, Frame | None], apps: ChromaApps | None,
                   own_pid: int, full_sync: bool) -> bool:
    """Does a game (another Chroma app) control the lighting right now?"""
    any_active = any(f is not None and f.active for f in frames.values())
    if not any_active:
        return False
    if apps is not None and apps.current_pid:
        return apps.current_pid != own_pid  # Synapse shows another app's effect
    # No app list: in full sync our own effect is in the frames, so we can't tell.
    return not full_sync


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
    for index, worker in enumerate(workers):
        frame = frames.get(worker.cfg.source)
        if game_active and frame is not None and frame.active:
            worker.set_color(bulb_color(worker.cfg, frame, keyboard_area))
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
            if (worker.cfg.zone == "all" and strip and index < len(strip)
                    and (profile is None or profile.effect not in DEVICE_EFFECTS)):
                rgb = adjust(strip[index], worker.cfg.brightness, worker.cfg.min_brightness)
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
        layout_detector: Callable[[], str] = detect_keyboard_layout,
    ) -> None:
        self._layout_detector = layout_detector
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
        self.game_active = False
        self.game_name = ""
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
            self.razer_status = "zatrzymane"
            self.game_active = False
            self.game_name = ""
            _LOGGER.info("Sync stopped")

    @staticmethod
    def _needed_sources(configs: list[BulbConfig], idle: str) -> list[str]:
        sources = sorted({c.source for c in configs})
        if idle == "full" and not sources:
            sources = ["keyboard"]  # still detect games for the Razer devices
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
            if self._needed_sources(configs, config.settings.idle) != self._sources:
                return False  # another Razer device must be read: restart the reader
            current = {w.cfg.id: w for w in self.workers}
            keep: dict[str, BulbWorker] = {}
            for bulb_id, worker in current.items():
                wanted = next((c for c in configs if c.id == bulb_id), None)
                if wanted is not None and wanted == worker.cfg:
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

    def bulb_statuses(self) -> dict[str, str]:
        return {w.cfg.id: w.status for w in self.workers}

    def bulb_details(self) -> dict[str, dict]:
        """What each running bulb reported and how it behaves (for the settings advisor)."""
        from .bulbs import learned_max_age, learned_slow_interval  # noqa: PLC0415

        return {w.cfg.id: {
            "status": w.status,
            "connected": getattr(w, "connected", False),
            "latency_ms": getattr(w, "latency_ms", None),
            "dps": list(getattr(w, "dps", [])),
            "bulb_type": getattr(getattr(w, "_dev", None), "bulb_type", ""),
            "dropped_off": learned_slow_interval(w.cfg.id),
            "renew_every": learned_max_age(w.cfg.id),
        } for w in self.workers}

    def bulb_latencies(self) -> dict[str, float | None]:
        """Measured delay per bulb (ms): color sent -> bulb confirmed."""
        return {w.cfg.id: getattr(w, "latency_ms", None) for w in self.workers}

    @staticmethod
    def latency_offset_ms(worker, settings) -> float:
        """How far ahead (ms) an effect color is sent to this bulb."""
        if settings.latency_mode == "auto":
            base = getattr(worker, "latency_ms", None) or 0.0
        elif settings.latency_mode == "manual":
            base = float(settings.latency_ms)
        else:
            base = 0.0
        return max(-1000.0, min(3000.0, base + getattr(worker.cfg, "latency_adjust_ms", 0)))

    def _bulb_effect_color(self, worker, index: int, count: int, settings, profile,
                           at: float | None = None) -> RGB:
        """The profile's color for one bulb, rendered ahead by the bulb's delay.

        Effects are computed by the program, so the color the bulb will need a
        moment from now is known now: sending it early hides the Wi-Fi/bulb lag.
        """
        player = self.player
        assert player is not None
        cfg = worker.cfg
        base = time.monotonic() if at is None else at
        t = base + self.latency_offset_ms(worker, settings) / 1000
        renderer = player.renderer
        if cfg.zone == "all" and profile.effect not in DEVICE_EFFECTS:
            rgb = renderer.frame(f"bulbs@{cfg.id}", max(1, count), 1, t)[min(index, count - 1)]
            return adjust(rgb, cfg.brightness, cfg.min_brightness)
        spec = DEVICES[cfg.source]
        colors = renderer.frame(f"{cfg.source}@{cfg.id}", spec.width, spec.height, t)
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

        return (key, index, count, layout, worker.cfg.min_interval), color

    # ---------------------------------------------------------------- thread

    def _release_all(self) -> None:
        for worker in self.workers:
            worker.release()
        self.game_active = False
        if self.player is not None:
            self.player.paused = True

    def _run(self, sources: list[str], settings) -> None:
        if not sources:
            self.razer_status = "nie wybrano żadnej żarówki"
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
                            self.razer_status = "połączono z Razer Chroma"
                        except (RazerUnavailable, OSError) as err:
                            self.razer_status = f"Razer niedostępny: {err}"
                            self._stop.wait(5)
                            continue
                    started = time.monotonic()
                    reader.wait(int(period * 1000))
                    try:
                        frames = {src: reader.read(src) for src in sources}
                        apps = reader.read_apps() if hasattr(reader, "read_apps") else None
                    except OSError as err:
                        self.razer_status = f"utracono połączenie: {err}"
                        reader.close()
                        reader = None
                        continue
                    if apps is None and full_sync and not warned_no_apps:
                        warned_no_apps = True
                        _LOGGER.warning("Synapse's Chroma app list is not available: games can't be "
                                        "told apart from our own effect in full sync mode")
                    others = other_chroma_apps(apps, self._own_pid)
                    if others != last_others:
                        _LOGGER.info("Chroma apps running: %s",
                                     ", ".join(f"{n} ({p})" for p, n in others.items()) or "none besides us")
                        last_others = others
                    if self.player is not None:
                        # Full sync: a game is running - hand it the Razer devices.
                        self.player.yielded = full_sync and bool(others)
                    self.game_active = is_game_active(frames, apps, self._own_pid, full_sync)
                    self.game_name = apps.current_name if (self.game_active and apps) else ""
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
                    self.razer_status = self._status_text(idle, profile.name)
                    spent = time.monotonic() - started
                    if spent < period:
                        self._stop.wait(period - spent)
                    last_error = ""
                except Exception as err:  # noqa: BLE001
                    # Never stop syncing for good: give the bulbs back, then try again.
                    if repr(err) != last_error:
                        _LOGGER.exception("Sync loop error - retrying in 5 s")
                        last_error = repr(err)
                    self.razer_status = "błąd odczytu Razer (szczegóły w logu) – ponawiam…"
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

    def _status_text(self, idle: str, profile_name: str) -> str:
        if self.game_active:
            name = f" ({self.game_name})" if self.game_name else ""
            return f"efekt z gry aktywny{name}"
        if idle == "full":
            player = self.player
            if player is not None and "niedostępne" in player.output_status:
                return player.output_status  # e.g. Chroma Apps disabled in Synapse
            return f"pełna synchronizacja – profil „{profile_name}”"
        if idle == "effect":
            return f"brak gry – profil „{profile_name}” na żarówkach"
        return "połączono, czekam na grę"
