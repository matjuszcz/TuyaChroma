"""Camera calibration: measure when the lights really change, against the keyboard.

The program can't see the lights: it only knows when a light acknowledged a
color, not when it showed it, nor how long its own fade takes. A webcam can.
For the keyboard and then each light in turn, the color steps from dark to
white a few times; the camera records how the picture brightens. The time from
the command to the middle of that brightening is the light's real response.
The camera's own delay is the same for every measurement, so comparing each
light with the keyboard cancels it: what remains is how much earlier the light
must get its colors to change together with the keyboard.

The camera only has to see the keyboard (or its glow) and the room the lights
shine into; the lights themselves needn't be in the picture.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import logging
import statistics
import sys
import threading
import time
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

GRID_W, GRID_H = 16, 12        # the picture is judged in this many areas
DARK = (0, 0, 0)               # lights show their dimmest color (they don't switch off)
BRIGHT = (255, 255, 255)
STEP = 1.6                     # s each step is held (the camera's exposure settles too)
TRIALS = 3
MIN_CHANGE = 6.0               # gray levels: less than this, the camera didn't see it
SETTLE = 1.2                   # s of dark before the first step


Sample = tuple[float, list[float]]  # (monotonic time, gray level per area)


# ---------------------------------------------------------------- the camera


class Camera:
    """A webcam read in its own thread: timestamped frames, reduced to a grid."""

    def __init__(self, index: int, opener: Callable[[int], Any] | None = None) -> None:
        self.index = index
        self._cap = (opener or _open_cv)(index)
        self._samples: deque[Sample] = deque(maxlen=600)
        self._preview: Any = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, name="camera", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        import cv2  # noqa: PLC0415

        while not self._stop.is_set():
            ok, frame = self._cap.read()
            now = time.monotonic()
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            grid = cv2.resize(gray, (GRID_W, GRID_H), interpolation=cv2.INTER_AREA)
            with self._lock:
                self._samples.append((now, [float(v) for v in grid.flatten()]))
                self._preview = frame

    def samples(self, since: float) -> list[Sample]:
        with self._lock:
            return [s for s in self._samples if s[0] >= since]

    def preview(self, width: int) -> Any:
        """The latest picture as a PIL image (for aiming the camera), or None."""
        with self._lock:
            frame = self._preview
        if frame is None:
            return None
        import cv2  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        return img.resize((width, max(1, round(width * img.height / img.width))))

    def close(self) -> None:
        self._stop.set()
        self._thread.join(1.0)
        try:
            self._cap.release()
        except Exception:  # noqa: BLE001
            pass


def _open_cv(index: int):
    import cv2  # noqa: PLC0415

    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        raise OSError(f"kamera {index} niedostępna")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
    cap.set(cv2.CAP_PROP_FPS, 30)
    return cap


def available() -> bool:
    """Is camera support (OpenCV) part of this build?"""
    import importlib.util  # noqa: PLC0415

    return importlib.util.find_spec("cv2") is not None


def find_cameras(limit: int = 4) -> list[int]:
    found = []
    for index in range(limit):
        try:
            cap = _open_cv(index)
        except OSError:
            continue
        ok, _frame = cap.read()
        cap.release()
        if ok:
            found.append(index)
    return found


# ---------------------------------------------------------------- analysis


def response_time(samples: list[Sample], command_at: float) -> float | None:
    """Seconds from the command to the middle of the picture's change (None: not seen).

    Judged on the areas that changed most (the keyboard, the lit wall), so a
    small light in a big picture still counts.
    """
    before = [g for t, g in samples if t < command_at]
    after = [(t, g) for t, g in samples if t >= command_at]
    if len(before) < 2 or len(after) < 4:
        return None
    end = after[-1][0]
    late = [g for t, g in after if t >= end - 0.35]
    base = [statistics.fmean(c) for c in zip(*before[-6:])]
    final = [statistics.fmean(c) for c in zip(*late)]
    delta = [f - b for f, b in zip(final, base)]
    strongest = max(abs(d) for d in delta)
    if strongest < MIN_CHANGE:
        return None
    cells = [i for i, d in enumerate(delta) if abs(d) >= 0.6 * strongest]

    def level(grid: list[float]) -> float:
        return statistics.fmean((grid[i] - base[i]) / delta[i] for i in cells)

    prev_t, prev_v = command_at, 0.0
    for t, grid in after:
        v = level(grid)
        if v >= 0.5:
            if v == prev_v:
                return t - command_at
            # between two frames: where the picture crossed the middle
            return (prev_t + (t - prev_t) * (0.5 - prev_v) / (v - prev_v)) - command_at
        prev_t, prev_v = t, v
    return None


# ---------------------------------------------------------------- the run


@dataclass
class LightResult:
    dev_id: str
    name: str
    response_ms: float | None = None   # command -> middle of its change
    relative_ms: float | None = None   # later than the keyboard by this much
    current_ms: float = 0.0            # how far ahead it is sent now (without correction)
    adjust_ms: int | None = None       # the correction that makes it change with the keyboard


@dataclass
class Result:
    keyboard_ms: float | None = None
    lights: list[LightResult] = field(default_factory=list)
    message: str = ""


def run(service, camera, progress: Callable[[str], None] = lambda _t: None,
        trials: int = TRIALS, step: float = STEP, stop: threading.Event | None = None) -> Result:
    """Measure the keyboard and every light; the sync is paused meanwhile."""
    stop = stop or threading.Event()
    player = service.player
    workers = list(service.workers)
    result = Result()
    if player is None or not getattr(player, "output_ready", False):
        result.message = "Urządzenia Razer nie są teraz sterowane przez program – kalibracja potrzebuje klawiatury."
        return result
    if service.game_active:
        result.message = "Gra steruje teraz urządzeniami Razer – zamknij ją i spróbuj ponownie."
        return result
    service.calibrating = True
    try:
        player.override = DARK
        for w in workers:
            w.set_color(DARK)
        _wait(SETTLE, stop)

        def keyboard_step(color) -> float | None:
            player.override = color
            return _wait_for(lambda: _sent_at(player.override_sent, color), 1.0, stop)

        progress("Klawiatura…")
        result.keyboard_ms = _measure(camera, keyboard_step, trials, step, stop)
        if result.keyboard_ms is None:
            result.message = ("Kamera nie widzi zmiany na klawiaturze – ustaw ją tak, żeby widziała "
                              "klawiaturę (albo jej poświatę) i spróbuj ponownie.")
            return result
        for n, w in enumerate(workers, 1):
            if stop.is_set():
                break
            progress(f"{w.cfg.name} ({n}/{len(workers)})…")
            light = LightResult(w.cfg.id, w.cfg.name)

            def light_step(color, w=w) -> float | None:
                w.set_color(color)
                return _wait_for(lambda: _sent_at(getattr(w, "last_sent", None), color), 1.5, stop)

            light.response_ms = _measure(camera, light_step, trials, step, stop)
            if light.response_ms is not None:
                light.relative_ms = light.response_ms - result.keyboard_ms
                settings = service._settings
                light.current_ms = (service.latency_offset_ms(w, settings)
                                    - getattr(w.cfg, "latency_adjust_ms", 0))
                # changes in the middle of its fade with the keyboard's change
                light.adjust_ms = max(-1000, min(1000, round(light.relative_ms - light.current_ms)))
            result.lights.append(light)
            w.set_color(DARK)
        progress("Gotowe.")
        _LOGGER.info("Camera calibration: keyboard %.0f ms; %s", result.keyboard_ms,
                     "; ".join(f"{r.name}: {r.response_ms if r.response_ms is None else round(r.response_ms)}"
                               f" ms (correction {r.adjust_ms})" for r in result.lights))
        return result
    finally:
        player.override = None
        service.calibrating = False


def _measure(camera, step_to, trials: int, step: float, stop: threading.Event) -> float | None:
    """Median response (ms) over a few dark -> bright steps; None if not seen."""
    times = []
    for _ in range(trials):
        if stop.is_set():
            return None
        command_at = step_to(BRIGHT)
        if command_at is None:
            continue
        _wait(step, stop)
        seen = response_time(camera.samples(command_at - 0.6), command_at)
        if seen is not None:
            times.append(seen * 1000)
        step_to(DARK)
        _wait(step, stop)
    if len(times) < max(1, trials - 1):
        return None
    return statistics.median(times)


def _sent_at(sent, color) -> float | None:
    if sent and tuple(sent[1]) == tuple(color):
        return sent[0]
    return None


def _wait_for(probe: Callable[[], float | None], timeout: float, stop: threading.Event) -> float | None:
    end = time.monotonic() + timeout
    while time.monotonic() < end and not stop.is_set():
        value = probe()
        if value is not None:
            return value
        time.sleep(0.005)
    return None


def _wait(seconds: float, stop: threading.Event) -> None:
    stop.wait(seconds)
