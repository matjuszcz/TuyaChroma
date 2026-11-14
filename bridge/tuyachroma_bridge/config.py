"""Configuration file handling."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sys
from typing import Any

from .bulbs import BulbConfig
from .razer import DEVICES

CONFIG_NAME = "config.json"

EXAMPLE: dict[str, Any] = {
    "fps": 20,
    "idle": "restore",
    "bulbs": [
        {
            "name": "Lampka biurko",
            "id": "bf0123456789abcdef",
            "ip": "192.168.1.60",
            "local_key": "0123456789abcdef",
            "version": "3.3",
            "source": "keyboard",
            "zone": "all",
            "mode": "vivid",
            "brightness": 1.0,
            "min_brightness": 0.05,
            "method": "auto",
            "min_interval": 0.15,
        }
    ],
}


class ConfigError(Exception):
    """Invalid configuration."""


@dataclass
class Config:
    bulbs: list[BulbConfig]
    fps: float = 20
    idle: str = "restore"  # restore | keep
    path: Path | None = field(default=None, compare=False)

    @property
    def sources(self) -> list[str]:
        return sorted({b.source for b in self.bulbs})


def default_path() -> Path:
    """config.json next to the .exe (or script); fallback %APPDATA%."""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    local = base / CONFIG_NAME
    if local.exists() or os.access(base, os.W_OK):
        return local
    appdata = Path(os.environ.get("APPDATA", Path.home())) / "TuyaChromaBridge"
    return appdata / CONFIG_NAME


def parse_config(data: dict[str, Any]) -> Config:
    bulbs_raw = data.get("bulbs")
    if not isinstance(bulbs_raw, list) or not bulbs_raw:
        raise ConfigError('"bulbs" must be a non-empty list')
    bulbs = []
    for i, raw in enumerate(bulbs_raw, 1):
        for key in ("id", "ip", "local_key"):
            if not raw.get(key):
                raise ConfigError(f'bulb #{i}: missing "{key}"')
        raw.setdefault("name", raw["id"])
        bulb = BulbConfig.from_dict(raw)
        if bulb.source not in DEVICES:
            raise ConfigError(
                f'bulb "{bulb.name}": source must be one of {", ".join(DEVICES)}'
            )
        if bulb.method not in ("auto", "music", "colour"):
            raise ConfigError(f'bulb "{bulb.name}": method must be auto, music or colour')
        bulbs.append(bulb)
    idle = data.get("idle", "restore")
    if idle not in ("restore", "keep"):
        raise ConfigError('"idle" must be "restore" or "keep"')
    return Config(bulbs=bulbs, fps=float(data.get("fps", 20)), idle=idle)


def load(path: Path) -> Config:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as err:
        raise ConfigError(f"{path} not found") from err
    except json.JSONDecodeError as err:
        raise ConfigError(f"{path}: invalid JSON: {err}") from err
    cfg = parse_config(data)
    cfg.path = path
    return cfg


def write_example(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(EXAMPLE, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
