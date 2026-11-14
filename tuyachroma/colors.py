"""Turn a device's LED grid into one color for a bulb."""

from __future__ import annotations

import colorsys

from .razer import DEVICES, RGB

ZONES = ("all", "left", "center", "right", "top", "bottom")


def zone_indices(device: str, zone: str | int | list[int],
                 area: tuple[int, int, int, int] | None = None) -> list[int]:
    """LED indices (row-major) of a named zone, a single LED, or a list of LEDs.

    ``area`` = (first col, last col, first row, last row) with LEDs, e.g. the
    keys of a 60% keyboard inside the 6x22 grid; zones are split within it.
    """
    spec = DEVICES[device]
    if isinstance(zone, int):
        zone = [zone]
    if isinstance(zone, list):
        return [i for i in zone if 0 <= i < spec.count] or list(range(spec.count))
    w = spec.width
    c0, c1, r0, r1 = area if area is not None else (0, w - 1, 0, spec.height - 1)
    cols = list(range(c0, c1 + 1))
    rows = list(range(r0, r1 + 1))
    n, h = len(cols), len(rows)
    third = max(1, n // 3)
    pick_cols = {
        "left": cols[:third],
        "center": cols[third:n - third] if n > 2 else cols,
        "right": cols[n - third:],
    }
    if zone in pick_cols:
        return [y * w + x for y in rows for x in pick_cols[zone]]
    if zone == "top":
        return [y * w + x for y in rows[:max(1, h // 2)] for x in cols]
    if zone == "bottom":
        return [y * w + x for y in (rows[h // 2:] if h > 1 else rows) for x in cols]
    return [y * w + x for y in rows for x in cols]


def reduce_colors(colors: list[RGB], mode: str = "vivid") -> RGB:
    """Reduce many LED colors to one.

    * ``average``: plain mean of all LEDs (dark LEDs pull it down).
    * ``vivid``: mean weighted by saturation and brightness, so a few bright
      colored keys win over a dim or grey background.
    """
    if not colors:
        return (0, 0, 0)
    if mode == "average":
        n = len(colors)
        return tuple(round(sum(c[i] for c in colors) / n) for i in range(3))  # type: ignore[return-value]

    total_w = 0.0
    acc = [0.0, 0.0, 0.0]
    for r, g, b in colors:
        mx, mn = max(r, g, b), min(r, g, b)
        weight = (mx - mn) + 0.1 * mx  # chroma, plus a bit for bright whites
        if weight <= 0:
            continue
        total_w += weight
        acc[0] += r * weight
        acc[1] += g * weight
        acc[2] += b * weight
    if total_w == 0:
        return (0, 0, 0)
    return tuple(min(255, round(c / total_w)) for c in acc)  # type: ignore[return-value]


def adjust(rgb: RGB, brightness: float = 1.0, min_brightness: float = 0.0) -> RGB:
    """Scale brightness; keep lit colors at least ``min_brightness`` (0..1)."""
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    if v <= 0.02:
        return (0, 0, 0)
    v = min(1.0, v * brightness)
    v = min_brightness + v * (1 - min_brightness)
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return round(r * 255), round(g * 255), round(b * 255)


def distance(a: RGB, b: RGB) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))
