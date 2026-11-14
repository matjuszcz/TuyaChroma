"""Background sync: Razer Chroma frames -> Tuya bulb workers."""

from __future__ import annotations

from collections.abc import Callable
import logging
import threading
import time

from .bulbs import BulbConfig, BulbWorker
from .colors import adjust, reduce_colors, zone_indices
from .config import AppConfig
from .effects import EffectSettings, effect_color
from .razer import ChromaReader, Frame, RazerUnavailable, razer_service_running

SERVICE_CHECK_INTERVAL = 3.0

_LOGGER = logging.getLogger(__name__)


def bulb_color(cfg: BulbConfig, frame: Frame) -> tuple[int, int, int]:
    """The color a bulb should show for a device frame."""
    assert frame.colors is not None
    leds = [frame.colors[i] for i in zone_indices(cfg.source, cfg.zone)]
    return adjust(reduce_colors(leds, cfg.mode), cfg.brightness, cfg.min_brightness)


def dispatch(
    workers: list,
    frames: dict[str, Frame | None],
    idle: str,
    fx: EffectSettings | None = None,
    now: float | None = None,
) -> bool:
    """Send colors to workers. Returns True if any game effect is active.

    Game colors win. Bulbs without a game effect follow ``idle``: play the
    ambient effect, give the bulb back (restore), or keep the last color.
    """
    active = False
    now = time.monotonic() if now is None else now
    count = len(workers)
    for index, worker in enumerate(workers):
        frame = frames.get(worker.cfg.source)
        if frame is not None and frame.active:
            worker.set_color(bulb_color(worker.cfg, frame))
            active = True
        elif idle == "effect" and fx is not None:
            rgb = effect_color(fx, now, index, count)
            worker.set_color(adjust(rgb, worker.cfg.brightness, worker.cfg.min_brightness))
        elif idle == "restore":
            worker.release()
    return active


class SyncService:
    """Owns the bulb workers and the Razer reader thread."""

    def __init__(
        self,
        reader_factory: Callable[[list[str]], ChromaReader] = ChromaReader,
        worker_factory: Callable[[BulbConfig], BulbWorker] = BulbWorker,
        service_check: Callable[[], bool] = razer_service_running,
    ) -> None:
        self._reader_factory = reader_factory
        self._worker_factory = worker_factory
        self._service_check = service_check
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.workers: list[BulbWorker] = []
        self.razer_status = "zatrzymane"
        self.game_active = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, config: AppConfig) -> None:
        with self._lock:
            if self.running:
                return
            self.workers = [self._worker_factory(d.bulb_config()) for d in config.enabled_devices]
            for w in self.workers:
                w.start()
            self._stop.clear()
            sources = sorted({w.cfg.source for w in self.workers})
            self._thread = threading.Thread(
                target=self._run,
                args=(sources, config.settings.fps, config.settings.idle, config.settings.effect),
                name="razer-reader", daemon=True,
            )
            self._thread.start()
            _LOGGER.info("Sync started for %d bulb(s)", len(self.workers))

    def stop(self, timeout: float = 5.0) -> None:
        """Give all bulbs back (restore their state) and stop. Blocks up to timeout."""
        with self._lock:
            deadline = time.monotonic() + timeout
            self._stop.set()
            for w in self.workers:
                w.request_stop()
            for w in self.workers:
                w.join(max(0.1, deadline - time.monotonic()))
            if self._thread is not None:
                self._thread.join(max(0.1, deadline - time.monotonic()))
            self._thread = None
            self.workers = []
            self.razer_status = "zatrzymane"
            self.game_active = False
            _LOGGER.info("Sync stopped")

    def restart(self, config: AppConfig) -> None:
        self.stop()
        if config.settings.sync_enabled:
            self.start(config)

    def bulb_statuses(self) -> dict[str, str]:
        return {w.cfg.id: w.status for w in self.workers}

    # ---------------------------------------------------------------- thread

    def _run(self, sources: list[str], fps: float, idle: str, fx: EffectSettings) -> None:
        if not sources:
            self.razer_status = "nie wybrano żadnej żarówki"
            return
        period = 1.0 / max(1.0, fps)
        reader: ChromaReader | None = None
        last_check = time.monotonic()
        try:
            while not self._stop.is_set():
                if reader is not None and time.monotonic() - last_check > SERVICE_CHECK_INTERVAL:
                    last_check = time.monotonic()
                    if not self._service_check():
                        # Synapse died: its shared memory keeps the last frame, so
                        # don't trust it - give the bulbs back to Home Assistant.
                        _LOGGER.warning("Razer SDK service stopped, releasing bulbs")
                        for worker in self.workers:
                            worker.release()
                        self.game_active = False
                        reader.close()
                        reader = None
                        continue
                if reader is None:
                    try:
                        reader = self._reader_factory(sources)
                        self.razer_status = "połączono z Razer Chroma"
                    except RazerUnavailable as err:
                        self.razer_status = f"Razer niedostępny: {err}"
                        self._stop.wait(5)
                        continue
                    except OSError as err:
                        _LOGGER.warning("Cannot open Razer Chroma: %s", err)
                        self.razer_status = f"Razer niedostępny: {err}"
                        self._stop.wait(5)
                        continue
                started = time.monotonic()
                reader.wait(int(period * 1000))
                try:
                    frames = {src: reader.read(src) for src in sources}
                except OSError as err:
                    self.razer_status = f"utracono połączenie: {err}"
                    reader.close()
                    reader = None
                    continue
                self.game_active = dispatch(self.workers, frames, idle, fx)
                if self.game_active:
                    self.razer_status = "efekt z gry aktywny"
                elif idle == "effect":
                    self.razer_status = "brak gry – odtwarzam efekt"
                else:
                    self.razer_status = "połączono, czekam na grę"
                spent = time.monotonic() - started
                if spent < period:
                    self._stop.wait(period - spent)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Razer reader crashed")
            self.razer_status = "błąd odczytu Razer (szczegóły w logu)"
            for worker in self.workers:  # don't leave bulbs on a game color
                worker.release()
            self.game_active = False
        finally:
            if reader is not None:
                reader.close()  # releases the mutexes from the owning thread
