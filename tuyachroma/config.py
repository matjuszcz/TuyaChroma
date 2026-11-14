"""Application configuration stored in %APPDATA%\\TuyaChroma\\config.json."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json
import logging
import os
from pathlib import Path
import threading
from typing import Any

from . import dpapi
from .bulbs import BulbConfig
from .profiles import Profile, default_profiles
from .razer import DEVICES

_LOGGER = logging.getLogger(__name__)

SOURCES = list(DEVICES)
ZONES = ["all", "left", "center", "right", "top", "bottom"]
# When Razer runs but no game drives Chroma:
#   full    = the program plays the profile on all Razer devices AND the bulbs (in sync)
#   effect  = the program plays the profile on the bulbs only (Razer keeps its Synapse profile)
#   restore = give the bulbs back (Home Assistant / Tuya app)
#   keep    = keep the last game color
IDLE_MODES = ["full", "effect", "restore", "keep"]
LEGACY_EFFECTS = {"wave": "wave", "spectrum": "spectrum", "breathing": "breathing", "static": "static"}


@dataclass
class Device:
    """A Tuya device from the account, plus per-bulb sync settings."""

    id: str
    name: str
    local_key: str
    category: str = ""
    product_name: str = ""
    ip: str = ""
    version: str = ""
    enabled: bool = False  # controlled by Razer?
    source: str = "keyboard"
    zone: Any = "all"
    mode: str = "vivid"
    brightness: float = 1.0
    min_brightness: float = 0.05
    method: str = "colour"
    min_interval: float = 0.15

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Device:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    def bulb_config(self) -> BulbConfig:
        return BulbConfig(
            name=self.name, id=self.id, ip=self.ip, local_key=self.local_key,
            version=self.version or "3.3", source=self.source, zone=self.zone,
            mode=self.mode, brightness=self.brightness, min_brightness=self.min_brightness,
            method=self.method, min_interval=self.min_interval,
        )

    @property
    def ready(self) -> bool:
        return bool(self.ip and self.local_key)


@dataclass
class Settings:
    autostart: bool = True
    start_minimized: bool = True
    idle: str = "effect"  # see IDLE_MODES
    profiles: list[Profile] = field(default_factory=default_profiles)
    active_profile: str = "Wave"
    bulb_effect_rate: float = 1.0  # bulb color changes per second while a profile plays
    keyboard_layout: str = "auto"  # auto (detected) | full | tkl | mini (60%)
    fps: float = 20
    sync_enabled: bool = True

    @property
    def profile(self) -> Profile:
        for p in self.profiles:
            if p.name == self.active_profile:
                return p
        return self.profiles[0] if self.profiles else Profile()


@dataclass
class AppConfig:
    devices: list[Device] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    username: str = ""
    user_code: str = ""
    # Saved Tuya login (tokens) to refresh the device list without a new QR code.
    # Stored encrypted with Windows DPAPI.
    tuya_session: dict[str, Any] = field(default_factory=dict)

    @property
    def enabled_devices(self) -> list[Device]:
        return [d for d in self.devices if d.enabled and d.ready]

    def device(self, dev_id: str) -> Device | None:
        return next((d for d in self.devices if d.id == dev_id), None)

    def merge_cloud(self, cloud_devices: list) -> int:
        """Add/update RGB lights from the account; keeps per-bulb settings. Returns #new.

        Only RGB bulbs, lamps and LED strips are kept; devices that disappeared
        from the account (or are not RGB) are removed.
        """
        rgb = [cd for cd in cloud_devices if cd.rgb]
        keep = {cd.id for cd in rgb}
        self.devices = [d for d in self.devices if d.id in keep]
        new = 0
        for cd in rgb:
            dev = self.device(cd.id)
            if dev is None:
                self.devices.append(Device(
                    id=cd.id, name=cd.name, local_key=cd.local_key,
                    category=cd.category, product_name=cd.product_name,
                ))
                new += 1
            else:
                dev.name, dev.local_key = cd.name, cd.local_key
                dev.category, dev.product_name = cd.category, cd.product_name
        return new

    def apply_scan(self, found: dict[str, dict]) -> int:
        """Update IPs/versions from a LAN scan. Returns how many devices matched."""
        matched = 0
        for dev in self.devices:
            if (info := found.get(dev.id)) is not None:
                dev.ip = info["ip"]
                if info.get("version"):
                    dev.version = info["version"]
                matched += 1
        return matched


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "TuyaChroma"


def default_path() -> Path:
    return config_dir() / "config.json"


def load(path: Path | None = None) -> AppConfig:
    path = path or default_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return AppConfig()
    except (OSError, ValueError) as err:
        _LOGGER.error("Cannot read %s: %s", path, err)
        return AppConfig()
    devices = []
    for raw in data.get("devices", []):
        try:
            raw = dict(raw)
            raw["local_key"] = dpapi.unprotect(raw.get("local_key", ""))
            devices.append(Device.from_dict(raw))
        except (OSError, TypeError, ValueError) as err:
            _LOGGER.warning("Skipping device %s: %s", raw.get("id"), err)
    s = dict(data.get("settings", {}))
    legacy_fx = s.pop("effect", None)  # 1.1: a single effect
    raw_profiles = s.pop("profiles", None)
    names = {f.name for f in fields(Settings)}
    settings = Settings(**{k: v for k, v in s.items() if k in names})
    if isinstance(raw_profiles, list) and raw_profiles:
        settings.profiles = [Profile.from_dict(p) for p in raw_profiles if isinstance(p, dict)]
    elif isinstance(legacy_fx, dict):
        # 1.1 -> profiles: keep the user's effect as the active profile
        period = float(legacy_fx.get("period", 8.0) or 8.0)
        mine = Profile(
            name="Mój efekt",
            effect=LEGACY_EFFECTS.get(legacy_fx.get("effect", "wave"), "wave"),
            color1=legacy_fx.get("color", "#44d62c"),
            speed=round(16 / max(1.6, period)),
            brightness=legacy_fx.get("brightness", 1.0),
        )
        mine.sanitize()
        settings.profiles = [mine] + default_profiles()
        settings.active_profile = mine.name
    elif "settings" in data and settings.idle == "restore":
        # Saved by 1.0, where "restore" was only the default: use the new default.
        settings.idle = "effect"
    if not settings.profiles:
        settings.profiles = default_profiles()
    if settings.idle not in IDLE_MODES:
        settings.idle = "effect"
    if settings.active_profile not in {p.name for p in settings.profiles}:
        settings.active_profile = settings.profiles[0].name
    settings.bulb_effect_rate = max(0.5, min(5.0, float(settings.bulb_effect_rate)))
    if settings.keyboard_layout not in ("auto", "full", "tkl", "mini"):
        settings.keyboard_layout = "auto"
    session: dict[str, Any] = {}
    if data.get("tuya_session"):
        try:
            session = json.loads(dpapi.unprotect(data["tuya_session"]))
        except (OSError, ValueError, TypeError) as err:
            _LOGGER.warning("Saved Tuya login unreadable: %s", err)
    return AppConfig(
        devices=devices, settings=settings,
        username=data.get("username", ""), user_code=data.get("user_code", ""),
        tuya_session=session,
    )


def save(config: AppConfig, path: Path | None = None) -> None:
    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    devices = []
    for dev in config.devices:
        raw = asdict(dev)
        raw["local_key"] = dpapi.protect(dev.local_key)
        devices.append(raw)
    data = {
        "username": config.username,
        "user_code": config.user_code,
        "settings": asdict(config.settings),
        "devices": devices,
        "tuya_session": dpapi.protect(json.dumps(config.tuya_session)) if config.tuya_session else "",
    }
    # unique temp name: saves may happen from the UI and a worker thread at once
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
