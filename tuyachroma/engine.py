"""Background sync: Razer Chroma -> Tuya bulbs (and, in full sync, profile -> Razer)."""

from __future__ import annotations

from collections.abc import Callable
import logging
import os
import threading
import time
from typing import Any

from .bulbs import BulbConfig, BulbWorker
from .colors import adjust, reduce_colors, zone_indices
from .config import AppConfig
from .inputhook import KeyboardHook
from .player import EffectPlayer
from .profiles import Profile
from .razer import (
    ChromaApps,
    ChromaReader,
    Frame,
    RazerUnavailable,
    RGB,
    razer_service_running,
)
from .razer_out import NativeChroma
from .render import Renderer
from .screen import ScreenSampler

SERVICE_CHECK_INTERVAL = 3.0
KEY_EFFECTS = {"reactive", "ripple"}
DEVICE_EFFECTS = {"reactive", "ripple", "ambient"}  # bulbs sample a device zone

_LOGGER = logging.getLogger(__name__)


def bulb_color(cfg: BulbConfig, frame: Frame) -> RGB:
    """The color a bulb should show for a device frame."""
    assert frame.colors is not None
    return zone_color(cfg, frame.colors)


def zone_color(cfg: BulbConfig, colors: list[RGB]) -> RGB:
    leds = [colors[i] for i in zone_indices(cfg.source, cfg.zone) if i < len(colors)]
    return adjust(reduce_colors(leds, cfg.mode), cfg.brightness, cfg.min_brightness)


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
) -> bool:
    """Send colors to the bulb workers. Returns True if a game color was used."""
    used_game = False
    for index, worker in enumerate(workers):
        frame = frames.get(worker.cfg.source)
        if game_active and frame is not None and frame.active:
            worker.set_color(bulb_color(worker.cfg, frame))
            used_game = True
        elif idle in ("full", "effect") and profile_frames is not None:
            if not allow_effect(worker):
                continue  # bulbs change at most bulb_effect_rate times per second
            if (worker.cfg.zone == "all" and strip and index < len(strip)
                    and (profile is None or profile.effect not in DEVICE_EFFECTS)):
                rgb = adjust(strip[index], worker.cfg.brightness, worker.cfg.min_brightness)
            else:
                rgb = zone_color(worker.cfg, profile_frames.get(worker.cfg.source, []))
            worker.set_color(rgb)
        elif idle == "restore":
            worker.release()
    return used_game


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
    ) -> None:
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

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, config: AppConfig) -> None:
        with self._lock:
            if self.running:
                return
            s = config.settings
            self.workers = [self._worker_factory(d.bulb_config()) for d in config.enabled_devices]
            for w in self.workers:
                w.start()
            self.player = None
            self._profile = s.profile
            if s.idle in ("full", "effect"):
                profile = s.profile
                screen = self._screen_factory() if profile.effect == "ambient" else None
                renderer = Renderer(profile, screen=screen)
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
            sources = sorted({w.cfg.source for w in self.workers})
            if s.idle == "full" and not sources:
                sources = ["keyboard"]  # still detect games for the Razer devices
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
            if self._thread is not None:
                self._thread.join(max(0.1, deadline - time.monotonic()))
            self._thread = None
            self.workers = []
            self.razer_status = "zatrzymane"
            self.game_active = False
            self.game_name = ""
            _LOGGER.info("Sync stopped")

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
        last_fx: dict[int, float] = {}
        warned_no_apps = False
        reader: ChromaReader | None = None
        last_check = time.monotonic()

        def allow_effect(worker) -> bool:
            now = time.monotonic()
            if now - last_fx.get(id(worker), 0.0) >= step:
                last_fx[id(worker)] = now
                return True
            return False

        try:
            while not self._stop.is_set():
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
                self.game_active = is_game_active(frames, apps, self._own_pid, full_sync)
                self.game_name = apps.current_name if (self.game_active and apps) else ""
                profile_frames = strip = None
                if self.player is not None:
                    self.player.paused = self.game_active
                    profile_frames, strip = self.player.latest()
                profile = self._profile or settings.profile
                dispatch(self.workers, frames, idle, self.game_active, profile_frames, strip,
                         profile, allow_effect)
                self.razer_status = self._status_text(idle, profile.name)
                spent = time.monotonic() - started
                if spent < period:
                    self._stop.wait(period - spent)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Razer reader crashed")
            self.razer_status = "błąd odczytu Razer (szczegóły w logu)"
            self._release_all()
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
