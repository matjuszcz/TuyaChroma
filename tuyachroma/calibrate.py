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

from array import array
import bisect
from collections import deque
from dataclasses import dataclass, field
import logging
import math
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
CLIPPED = 250.0                # gray level: overexposed (the camera can't show more)
SPREAD_MAX = 150.0             # ms: trials further apart than this are not trusted


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
        self._thread.join(3.0)
        if self._thread.is_alive():
            return  # a read still hangs in the driver: releasing now could crash it
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
    base = [statistics.fmean(c) for c in zip(*before[-6:])]
    # The change is measured to its peak (3 frames averaged against noise), not
    # to the last frames: a webcam's auto-exposure pulls a brightened picture
    # back down, which made a slowly fading light look early.
    smooth = [[statistics.fmean(c) for c in zip(*[g for _t, g in after[i:i + 3]])]
              for i in range(max(1, len(after) - 2))]
    final = [max(column) for column in zip(*smooth)]
    delta = [f - b for f, b in zip(final, base)]
    # (areas at white are clipped by the camera: their change is cut short)
    usable = [d if f < CLIPPED else 0.0 for d, f in zip(delta, final)]
    strongest = max(usable)
    if strongest < MIN_CHANGE:
        return None
    cells = [i for i, d in enumerate(usable) if d >= 0.6 * strongest]  # (bright steps: up)

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
    uncertain: bool = False            # the trials disagreed (movement in the picture?)


@dataclass
class Command:
    who: str          # "keyboard" or a light id
    at: float         # monotonic time it was sent
    bright: bool      # dark -> bright (measured) or back


@dataclass
class Result:
    keyboard_ms: float | None = None
    lights: list[LightResult] = field(default_factory=list)
    message: str = ""
    commands: list[Command] = field(default_factory=list)  # for a phone video
    step: float = STEP
    spread: dict[str, float] = field(default_factory=dict)  # who -> ms between trials


class Interrupted(Exception):
    """A game took the Razer devices, or the window was closed."""


_run_lock = threading.Lock()  # one calibration at a time


def run(service, camera, progress: Callable[[str], None] = lambda _t: None,
        trials: int = TRIALS, step: float = STEP, stop: threading.Event | None = None,
        between: Callable[[], bool] | None = None) -> Result:
    """Step the keyboard and every light; the sync is paused meanwhile.

    With a camera the responses are measured right away. Without one
    (``camera=None``: a phone films it) only the command times are kept, for
    analyze_video() to match the recording against. ``between`` is called after
    the keyboard (e.g. to turn the camera from the keyboard to the room); it
    returns False to give up.
    """
    stop = stop or threading.Event()
    player = service.player
    workers = list(service.workers)
    result = Result(step=step)
    if player is None or not getattr(player, "output_ready", False):
        result.message = ("Urządzenia Razer nie są teraz sterowane przez program – kalibracja potrzebuje "
                          "klawiatury (Ustawienia → „Pełna synchronizacja”).")
        return result
    if service.game_active:
        result.message = "Gra steruje teraz urządzeniami Razer – zamknij ją i spróbuj ponownie."
        return result
    if not workers:
        result.message = "Żadne światło nie jest teraz sterowane – zaznacz światła w zakładce „Światła”."
        return result
    if not _run_lock.acquire(blocking=False):
        result.message = "Kalibracja już trwa."
        return result

    def check() -> None:
        if stop.is_set():
            raise Interrupted("")
        if (service.game_active or getattr(player, "yielded", False)
                or getattr(player, "paused", False) or not getattr(player, "output_ready", True)):
            raise Interrupted("Gra przejęła urządzenia Razer – pomiar przerwany. Spróbuj po zamknięciu gry.")

    service.calibrating = True
    try:
        player.override = DARK
        for w in workers:
            w.set_color(DARK)
        _wait(SETTLE, stop)

        def keyboard_step(color) -> float | None:
            asked = time.monotonic()
            player.override = color
            return _wait_for(lambda: _sent_after(player.override_sent, color, asked), 1.0, stop)

        progress("Klawiatura…")
        result.keyboard_ms = _measure(camera, keyboard_step, "keyboard", result, trials, step, stop, check)
        if camera is not None and result.keyboard_ms is None:
            result.message = ("Kamera nie widzi zmiany na klawiaturze – ustaw ją tak, żeby widziała "
                              "klawiaturę (albo jej poświatę) i spróbuj ponownie.")
            return result
        if between is not None:
            progress("Skieruj kamerę na pokój…")
            if not between():
                raise Interrupted("")
            _wait(SETTLE, stop)  # (the camera's exposure settles on the new picture)
        settings = service._settings
        for n, w in enumerate(workers, 1):
            check()
            progress(f"{w.cfg.name} ({n}/{len(workers)})…")
            light = LightResult(w.cfg.id, w.cfg.name)
            light.current_ms = (service.latency_offset_ms(w, settings)
                                - getattr(w.cfg, "latency_adjust_ms", 0))

            def light_step(color, w=w) -> float | None:
                asked = time.monotonic()
                w.set_color(color)
                return _wait_for(lambda: _sent_after(getattr(w, "last_sent", None), color, asked),
                                 1.5, stop)

            light.response_ms = _measure(camera, light_step, w.cfg.id, result, trials, step, stop, check)
            result.lights.append(light)
            w.set_color(DARK)
        _finish(result)
        progress("Gotowe.")
        return result
    except Interrupted as err:
        result.message = str(err) or "Pomiar przerwany."
        result.lights = []
        return result
    finally:
        player.override = None
        service.calibrating = False
        _run_lock.release()


def _finish(result: Result) -> None:
    """Corrections from the responses: each light's fade meets the keyboard's change."""
    for light in result.lights:
        if light.response_ms is None or result.keyboard_ms is None:
            light.relative_ms = light.adjust_ms = None
            continue
        light.relative_ms = light.response_ms - result.keyboard_ms
        light.uncertain = max(result.spread.get(light.dev_id, 0.0),
                              result.spread.get("keyboard", 0.0)) > SPREAD_MAX
        # (an uncertain measurement isn't applied: measure again)
        light.adjust_ms = None if light.uncertain else max(
            -1000, min(1000, round(light.relative_ms - light.current_ms)))
    if result.keyboard_ms is not None and result.lights:
        _LOGGER.info("Camera calibration: keyboard %.0f ms; %s", result.keyboard_ms, "; ".join(
            f"{r.name}: {'-' if r.response_ms is None else round(r.response_ms)} ms "
            f"(correction {r.adjust_ms})" for r in result.lights))


def _measure(camera, step_to, who: str, result: Result, trials: int, step: float,
             stop: threading.Event, check: Callable[[], None] = lambda: None) -> float | None:
    """Median response (ms) over a few dark -> bright steps; None if not seen."""
    times = []
    for _ in range(trials):
        check()
        command_at = step_to(BRIGHT)
        if command_at is None:
            continue
        result.commands.append(Command(who, command_at, True))
        _wait(step, stop)
        check()
        if camera is not None:
            seen = response_time(camera.samples(command_at - 0.6), command_at)
            if seen is not None:
                times.append(seen * 1000)
        back = step_to(DARK)
        if back is not None:
            result.commands.append(Command(who, back, False))
        _wait(step, stop)
    if camera is None or len(times) < max(1, trials - 1):
        return None
    result.spread[who] = max(times) - min(times)
    return statistics.median(times)


# ---------------------------------------------------------------- a phone video


MAX_VIDEO = 20 * 60.0  # s of a recording read at most (a wrong file must not fill the memory)


def read_video(path: str, max_seconds: float = MAX_VIDEO,
               stop: threading.Event | None = None) -> list[Sample]:
    """The frames of a recording, as (seconds from its start, grid)."""
    import cv2  # noqa: PLC0415

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise OSError("nie udało się otworzyć filmu")
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or not math.isfinite(fps) or fps <= 0:
        fps = 30.0
    samples: list[Sample] = []
    n = 0
    try:
        while stop is None or not stop.is_set():
            ok, frame = cap.read()
            if not ok:
                break
            ms = cap.get(cv2.CAP_PROP_POS_MSEC)
            t = ms / 1000 if ms and ms > 0 and math.isfinite(ms) else n / fps
            if t > max_seconds:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            grid = cv2.resize(gray, (GRID_W, GRID_H), interpolation=cv2.INTER_AREA)
            samples.append((t, array("f", grid.flatten().tolist())))  # (4 bytes a value)
            n += 1
    finally:
        cap.release()
    if len(samples) < 30:
        raise OSError("film jest za krótki albo w nieobsługiwanym formacie (w iPhonie: "
                      "Ustawienia → Aparat → Formaty → Najbardziej zgodny)")
    return samples


def analyze_video(samples: list[Sample], result: Result) -> Result:
    """Match a recording of the steps (filmed with a phone) to their command times.

    The phone's clock is unknown: the offset that lines the commands up with the
    moments the picture changes is found first, then each step is measured as
    with a webcam. A constant offset cancels between the keyboard and the lights.
    """
    if not result.commands:
        result.message = "Najpierw wykonaj pomiar, nagrywając go telefonem."
        return result
    # The picture's overall brightness over time (smoothed over 3 frames)
    times = [t for t, _g in samples]
    level = [statistics.fmean(g) for _t, g in samples]
    level = [statistics.fmean(level[max(0, i - 1):i + 2]) for i in range(len(level))]
    first = result.commands[0].at
    rel = [c.at - first for c in result.commands]
    signs = [1.0 if c.bright else -1.0 for c in result.commands]
    span = rel[-1] + result.step
    if times[-1] - times[0] < span - 1.0:
        result.message = ("Film jest krótszy niż pomiar – nagrywaj od chwili przed kliknięciem "
                          "„Rozpocznij” do napisu „Gotowe”.")
        return result

    def at(t: float) -> float:
        return level[min(len(level) - 1, bisect.bisect_left(times, t))]

    # The offset: where in the video the first command was given. A candidate
    # scores how much the picture brightens (bright steps) or darkens (dark
    # ones) from each command to WINDOW later - signed, so a shift by one step
    # doesn't fit. Every offset putting each command before its change and the
    # change within the window scores the same (a plateau); its late end is
    # where a command meets the earliest change (the keyboard's) - just before
    # it every command precedes its change, as the measurement needs. A constant
    # error cancels between the keyboard and the lights.
    window = 1.1
    step = (times[-1] - times[0]) / max(1, len(times) - 1)
    offsets, scores = [], []
    offset = times[0]
    while offset + span <= times[-1] + 1.0:
        offsets.append(offset)
        scores.append(sum(sign * (at(offset + r + window) - at(offset + r))
                          for r, sign in zip(rel, signs)))
        offset += step
    if not scores:
        result.message = "Nie udało się dopasować filmu do pomiaru."
        return result
    top = max(range(len(scores)), key=scores.__getitem__)
    k = top
    while (k + 1 < len(scores) and scores[k + 1] >= 0.97 * scores[top]
           and offsets[k + 1] - offsets[top] < 0.6):
        k += 1
    best = offsets[k] - 0.12
    shifted = [(t - best + first, g) for t, g in samples]
    by_who: dict[str, list[float]] = {}
    for c in result.commands:
        if not c.bright:
            continue
        window = [s for s in shifted if c.at - 0.6 <= s[0] <= c.at + result.step]
        seen = response_time(window, c.at)
        if seen is not None:
            by_who.setdefault(c.who, []).append(seen * 1000)

    def median(who: str) -> float | None:
        values = by_who.get(who, [])
        if len(values) < 2:
            return None
        result.spread[who] = max(values) - min(values)
        return statistics.median(values)

    result.keyboard_ms = median("keyboard")
    if result.keyboard_ms is None:
        result.message = ("Na filmie nie widać zmiany klawiatury – nagraj tak, żeby w kadrze była "
                          "klawiatura (albo jej poświata) i pokój.")
        return result
    for light in result.lights:
        light.response_ms = median(light.dev_id)
    result.message = ""
    _finish(result)
    return result


def _sent_after(sent, color, asked: float) -> float | None:
    """When the color went out - only a send made after it was asked for."""
    if sent and tuple(sent[1]) == tuple(color) and sent[0] >= asked:
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
