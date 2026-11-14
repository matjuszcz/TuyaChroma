"""Turn a device's LED grid into one color for a bulb."""

from __future__ import annotations

import colorsys

from .razer import DEVICES, RGB

ZONES = ("all", "left", "center", "right", "top", "bottom")


def zone_indices(device: str, zone: str | int | list[int]) -> list[int]:
    """LED indices (row-major) of a named zone, a single LED, or a list of LEDs."""
    spec = DEVICES[device]
    if isinstance(zone, int):
        zone = [zone]
    if isinstance(zone, list):
        return [i for i in zone if 0 <= i < spec.count] or list(range(spec.count))
    w, h = spec.width, spec.height
    third = max(1, w // 3)
    columns = {
        "left": range(0, third),
        "center": range(third, w - third) if w > 2 else range(w),
        "right": range(w - third, w),
    }
    if zone in columns:
        return [y * w + x for y in range(h) for x in columns[zone]]
    if zone == "top":
        return list(range(0, max(1, h // 2) * w))
    if zone == "bottom":
        return list(range((h // 2) * w, h * w)) if h > 1 else list(range(w))
    return list(range(spec.count))


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
