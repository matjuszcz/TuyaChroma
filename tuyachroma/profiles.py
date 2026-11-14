"""Effect profiles, like Synapse / Chroma Studio profiles."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

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
        names = {f.name for f in fields(cls)}
        p = cls(**{k: v for k, v in data.items() if k in names})
        p.sanitize()
        return p

    def sanitize(self) -> None:
        if self.effect not in EFFECTS:
            self.effect = "wave"
        if self.color_mode not in COLOR_MODES:
            self.color_mode = "single"
        if self.direction not in DIRECTIONS:
            self.direction = "right"
        self.speed = max(1, min(10, int(self.speed)))
        self.density = max(1, min(10, int(self.density)))
        self.brightness = max(0.05, min(1.0, float(self.brightness)))


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
