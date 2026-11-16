"""Effect profiles, like Synapse / Chroma Studio profiles."""

from __future__ import annotations

from dataclasses import dataclass, fields
import logging
import math
import re
from typing import Any

_LOGGER = logging.getLogger(__name__)
_HEX = re.compile(r"#[0-9a-fA-F]{6}")

EFFECTS = [
    "wave", "spectrum", "breathing", "static", "wheel",
    "starlight", "fire", "reactive", "ripple", "ambient",
]
COLOR_MODES = ["single", "dual", "random"]
DIRECTIONS = ["right", "left", "down", "up"]  # wheel: right = clockwise


@dataclass
class Profile:
    name: str = "Wave"
    effect: str = "wave"
    color1: str = "#44d62c"
    color2: str = "#0080ff"
    color_mode: str = "single"  # single | dual | random (breathing, starlight, ripple)
    speed: int = 5  # 1 (slow) .. 10 (fast)
    direction: str = "right"
    density: int = 5  # starlight: how many stars, 1..10
    brightness: float = 1.0

    @property
    def period(self) -> float:
        """Seconds per cycle for the given speed (speed 5 = 3.2 s)."""
        return 16.0 / max(1, min(10, int(self.speed)))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        p = cls(**typed_fields(cls, data))
        p.sanitize()
        return p

    def sanitize(self) -> None:
        if self.effect not in EFFECTS:
            self.effect = "wave"
        if self.color_mode not in COLOR_MODES:
            self.color_mode = "single"
        if self.direction not in DIRECTIONS:
            self.direction = "right"
        for name, default in (("color1", "#44d62c"), ("color2", "#0080ff")):
            if not _HEX.fullmatch(str(getattr(self, name))):
                setattr(self, name, default)
        self.speed = max(1, min(10, int(self.speed)))
        self.density = max(1, min(10, int(self.density)))
        self.brightness = max(0.05, min(1.0, float(self.brightness)))


def typed_fields(cls, data: dict[str, Any]) -> dict[str, Any]:
    """The dataclass's fields from saved data, skipping values of the wrong type
    (a damaged or hand-edited config.json): those keep their default."""
    out: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value, kind = data[f.name], str(f.type).split("[")[0].strip()
        if kind == "bool":
            ok = isinstance(value, bool)
        elif kind in ("int", "float"):
            ok = (isinstance(value, (int, float)) and not isinstance(value, bool)
                  and math.isfinite(value))
            if ok and kind == "int":
                value = int(round(value))
        elif kind in _KINDS:
            ok = isinstance(value, _KINDS[kind])
        else:
            ok = True  # Any, nested types: checked where they are used
        if ok:
            out[f.name] = value
        else:
            _LOGGER.warning("Config: ignoring %s.%s = %r (wrong type)", cls.__name__, f.name, value)
    return out


_KINDS = {"str": str, "list": list, "dict": dict}


def default_profiles() -> list[Profile]:
    return [
        Profile("Wave", "wave"),
        Profile("Spectrum", "spectrum", speed=3),
        Profile("Oddychanie", "breathing", color1="#44d62c", color2="#0080ff", speed=3),
        Profile("Statyczny zielony", "static", color1="#44d62c"),
        Profile("Koło", "wheel", speed=4),
        Profile("Gwiazdy", "starlight", color_mode="random", speed=5, density=5),
        Profile("Ogień", "fire", speed=6),
        Profile("Reaktywny", "reactive", color1="#ff0000", speed=5),
        Profile("Fala po klawiszu", "ripple", color_mode="random", speed=6),
        Profile("Ekran (Ambient)", "ambient"),
    ]
