"""Render Chroma Studio-like effects onto LED grids.

One Renderer draws the active profile for every Razer device category
(keyboard 6x22, mouse 9x7, mousepad 15, headset 5, keypad 4x5, Chroma Link 5)
and for a virtual 1xN strip made of the Tuya bulbs.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import colorsys
import math
import random
import threading
import time
from typing import Any

from .profiles import Profile
from .razer import DEVICES, RGB

BLACK: RGB = (0, 0, 0)
KEYBOARD_W, KEYBOARD_H = 22, 6


def parse_hex(color: str, default: RGB = (68, 214, 44)) -> RGB:
    color = (color or "").strip().lstrip("#")
    if len(color) != 6:
        return default
    try:
        return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    except ValueError:
        return default


def hsv(h: float, s: float = 1.0, v: float = 1.0) -> RGB:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, max(0.0, min(1.0, v)))
    return round(r * 255), round(g * 255), round(b * 255)


def scale(rgb: RGB, f: float) -> RGB:
    f = max(0.0, min(1.0, f))
    return round(rgb[0] * f), round(rgb[1] * f), round(rgb[2] * f)


class Renderer:
    """Draws the active profile. Thread-safe for key presses."""

    def __init__(self, profile: Profile, screen: Callable[[int, int], Any] | None = None,
                 seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._presses: list[tuple[int, int, float, RGB]] = []
        # Filled by the keyboard hook thread without taking a lock: the hook must
        # never wait, or typing would lag system-wide.
        self._incoming: deque[tuple[int, int, float, RGB]] = deque(maxlen=64)
        self._state: dict[str, Any] = {}
        self._last_t: dict[str, float] = {}
        self.screen = screen  # (w, h) -> list[RGB] row-major, or None
        self.profile = profile

    # ------------------------------------------------------------ inputs

    def set_profile(self, profile: Profile) -> None:
        with self._lock:
            self.profile = profile
            self._state.clear()
            self._last_t.clear()
            self._presses.clear()
            self._incoming.clear()

    def key_pressed(self, row: int, col: int, t: float | None = None) -> None:
        t = time.monotonic() if t is None else t
        self._incoming.append((row, col, t, self._pick_color()))  # atomic, lock-free

    # ----------------------------------------------------------- outputs

    def device_frame(self, device: str, t: float) -> list[RGB]:
        spec = DEVICES[device]
        return self.frame(device, spec.width, spec.height, t)

    def strip(self, count: int, t: float) -> list[RGB]:
        """Virtual horizontal strip: one color per bulb (wave runs across bulbs)."""
        return self.frame("bulbs", max(1, count), 1, t)

    def frame(self, target: str, w: int, h: int, t: float) -> list[RGB]:
        p = self.profile
        fn = getattr(self, f"_fx_{p.effect}", self._fx_static)
        with self._lock:
            dt = max(0.0, t - self._last_t.get(target, t))
            self._last_t[target] = t
            colors = fn(target, w, h, t, dt)
        level = p.brightness
        return [scale(c, level) for c in colors]

    # ----------------------------------------------------------- helpers

    def _pick_color(self) -> RGB:
        p = self.profile
        if p.color_mode == "random":
            return hsv(self._rng.random())
        if p.color_mode == "dual":
            return parse_hex(p.color1) if self._rng.random() < 0.5 else parse_hex(p.color2)
        return parse_hex(p.color1)

    def _positions(self, w: int, h: int):
        for y in range(h):
            for x in range(w):
                yield x, y

    # ----------------------------------------------------------- effects

    def _fx_static(self, target, w, h, t, dt) -> list[RGB]:
        return [parse_hex(self.profile.color1)] * (w * h)

    def _fx_spectrum(self, target, w, h, t, dt) -> list[RGB]:
        return [hsv(t / self.profile.period)] * (w * h)

    def _fx_breathing(self, target, w, h, t, dt) -> list[RGB]:
        p = self.profile
        cycle, phase = divmod(t / p.period, 1.0)
        level = 0.5 - 0.5 * math.cos(2 * math.pi * phase)
        if p.color_mode == "dual":
            base = parse_hex(p.color1 if int(cycle) % 2 == 0 else p.color2)
        elif p.color_mode == "random":
            base = hsv(random.Random(int(cycle) * 7919 + 13).random())
        else:
            base = parse_hex(p.color1)
        return [scale(base, level)] * (w * h)

    def _fx_wave(self, target, w, h, t, dt) -> list[RGB]:
        p = self.profile
        shift = t / p.period
        out = []
        for x, y in self._positions(w, h):
            # x / w (not x / (w-1)): the LEDs share one rainbow without the first
            # and the last one getting the same color (2 bulbs = opposite colors)
            pos = {"right": x / w, "left": (w - 1 - x) / w,
                   "down": y / h, "up": (h - 1 - y) / h}[p.direction]
            out.append(hsv(shift - pos))
        return out

    def _fx_wheel(self, target, w, h, t, dt) -> list[RGB]:
        p = self.profile
        cx, cy = (w - 1) / 2, (h - 1) / 2
        sign = -1 if p.direction in ("right", "down") else 1
        out = []
        for x, y in self._positions(w, h):
            angle = math.atan2(y - cy, x - cx) / (2 * math.pi)
            out.append(hsv(angle + sign * t / p.period))
        return out

    def _fx_starlight(self, target, w, h, t, dt) -> list[RGB]:
        p = self.profile
        stars: dict[int, tuple[float, RGB]] = self._state.setdefault(target, {})
        life = p.period * 0.8
        n = w * h
        expected = p.density * 0.06 * n * dt / max(0.2, life / 2)
        spawn = int(expected) + (1 if self._rng.random() < expected % 1 else 0)
        for _ in range(spawn):
            i = self._rng.randrange(n)
            if i not in stars:
                stars[i] = (t, self._pick_color())
        out = [BLACK] * n
        for i, (born, color) in list(stars.items()):
            age = (t - born) / life
            if age >= 1 or age < 0:
                del stars[i]
                continue
            out[i] = scale(color, math.sin(math.pi * age))
        return out

    def _fx_fire(self, target, w, h, t, dt) -> list[RGB]:
        heat: list[float] = self._state.setdefault(target, [0.0] * (w * h))
        steps = max(1, int(dt * (6 + 2 * self.profile.speed)))
        for _ in range(min(steps, 5)):
            for x in range(w):  # bottom row: new sparks
                heat[(h - 1) * w + x] = 0.55 + 0.45 * self._rng.random()
            for y in range(h - 1):  # heat rises and cools
                for x in range(w):
                    below = heat[(y + 1) * w + x]
                    side = heat[(y + 1) * w + max(0, x - 1)] + heat[(y + 1) * w + min(w - 1, x + 1)]
                    heat[y * w + x] = max(0.0, (below * 2 + side) / 4 * 0.86 - 0.04 * self._rng.random())
            if h == 1:
                for x in range(w):
                    heat[x] = 0.5 + 0.5 * self._rng.random()
        return [self._fire_color(v) for v in heat]

    @staticmethod
    def _fire_color(v: float) -> RGB:
        v = max(0.0, min(1.0, v))
        if v < 0.4:
            return (round(255 * v / 0.4), 0, 0)
        if v < 0.8:
            return (255, round(140 * (v - 0.4) / 0.4), 0)
        return (255, round(140 + 115 * (v - 0.8) / 0.2), round(80 * (v - 0.8) / 0.2))

    def _recent_presses(self, t: float, life: float) -> list[tuple[int, int, float, RGB]]:
        while self._incoming:
            self._presses.append(self._incoming.popleft())
        self._presses = [pr for pr in self._presses[-40:] if t - pr[2] < life]
        return self._presses

    def _fx_reactive(self, target, w, h, t, dt) -> list[RGB]:
        life = self.profile.period * 0.4
        presses = self._recent_presses(t, life)
        n = w * h
        out = [BLACK] * n
        if target in ("keyboard", "keyboard_ext"):
            off = 1 if target == "keyboard_ext" else 0  # 6x22 keys sit inside the 8x24 grid
            for row, col, t0, color in presses:
                row, col = row + off, col + off
                if 0 <= row < h and 0 <= col < w:
                    i = row * w + col
                    f = 1 - (t - t0) / life
                    if f > max(out[i]) / 255:
                        out[i] = scale(color, f)
            return out
        # other devices light up as a whole on any key press
        if presses:
            row, col, t0, color = presses[-1]
            return [scale(color, 1 - (t - t0) / life)] * n
        return out

    def _fx_ripple(self, target, w, h, t, dt) -> list[RGB]:
        life = self.profile.period * 0.6
        speed = KEYBOARD_W / max(0.3, life)  # keys per second
        presses = self._recent_presses(t, life)
        out = [BLACK] * (w * h)
        for row, col, t0, color in presses:
            age = t - t0
            radius = age * speed
            fade = 1 - age / life
            if target in ("keyboard", "keyboard_ext"):
                off = 1 if target == "keyboard_ext" else 0
                cx, cy, sx, sy = col + off, row + off, 1.0, 1.0
            else:  # start from the device's center, scaled to keyboard size
                cx, cy = (w - 1) / 2, (h - 1) / 2
                sx, sy = KEYBOARD_W / max(1, w), KEYBOARD_H / max(1, h) if h > 1 else 1.0
            for x, y in self._positions(w, h):
                d = math.hypot((x - cx) * sx, (y - cy) * sy)
                f = max(0.0, 1 - abs(d - radius) / 1.6) * fade
                i = y * w + x
                if f > max(out[i]) / 255:
                    out[i] = scale(color, f)
        return out

    def _fx_ambient(self, target, w, h, t, dt) -> list[RGB]:
        if self.screen is None:
            return [BLACK] * (w * h)
        try:
            colors = self.screen(w, h)
        except Exception:  # noqa: BLE001 - screen capture can fail (locked screen, UAC)
            colors = None
        return colors if colors and len(colors) == w * h else [BLACK] * (w * h)
