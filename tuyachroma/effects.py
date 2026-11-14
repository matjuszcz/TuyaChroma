"""Ambient effects played on the bulbs while Razer runs but no game drives Chroma.

Synapse profile effects (Chroma Studio) are not readable without a Razer App
ID, so the program plays a matching effect itself.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass
import math

from .razer import RGB

EFFECTS = ["wave", "spectrum", "breathing", "static"]


@dataclass
class EffectSettings:
    effect: str = "wave"
    color: str = "#44d62c"  # breathing / static
    period: float = 8.0  # seconds per full cycle
    brightness: float = 1.0


def parse_hex(color: str) -> RGB:
    color = color.strip().lstrip("#")
    if len(color) != 6:
        return (68, 214, 44)
    try:
        return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    except ValueError:
        return (68, 214, 44)


def _hsv(h: float, s: float = 1.0, v: float = 1.0) -> RGB:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return round(r * 255), round(g * 255), round(b * 255)


def effect_color(fx: EffectSettings, t: float, index: int = 0, count: int = 1) -> RGB:
    """Color of bulb ``index`` (of ``count``) at time ``t`` seconds."""
    period = max(0.5, fx.period)
    phase = (t / period) % 1.0
    level = max(0.0, min(1.0, fx.brightness))
    if fx.effect == "wave":
        # the rainbow moves across the bulbs: each one is a step behind the previous
        offset = index / max(1, count) if count > 1 else 0.0
        return _hsv(phase - offset, 1.0, level)
    if fx.effect == "spectrum":
        return _hsv(phase, 1.0, level)
    base = parse_hex(fx.color)
    if fx.effect == "breathing":
        factor = 0.08 + 0.92 * (0.5 - 0.5 * math.cos(2 * math.pi * phase))
        return tuple(round(c * factor * level) for c in base)  # type: ignore[return-value]
    return tuple(round(c * level) for c in base)  # type: ignore[return-value]
