"""Application configuration stored in %APPDATA%\\TuyaChroma\\config.json."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any

from . import dpapi, redact
from .bulbs import BulbConfig
from .profiles import Profile, default_profiles, typed_fields
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
    codes: list[str] = field(default_factory=list)  # data points from the Tuya account
    ip: str = ""
    version: str = ""
    enabled: bool = False  # controlled by Razer?
    source: str = "keyboard"
    zone: Any = "all"
    mode: str = "vivid"
    brightness: float = 1.0
    min_brightness: float = 0.05
    method: str = "auto"  # auto: colour mode with smooth fades (music/jump only when chosen)
    min_interval: float = 0.15
    latency_adjust_ms: int = 0  # per-bulb correction for effect timing (ms)
    # The light's own effect captured from the Tuya app (e.g. a wave along a
    # strip), played in Wave/Spectrum/Wheel profiles when use_scene is on.
    scene: dict[str, Any] = field(default_factory=dict)
    use_scene: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Device:
        return cls(**typed_fields(cls, data))

    def bulb_config(self) -> BulbConfig:
        from .advisor import product_hint  # noqa: PLC0415

        hint = product_hint(self)
        # A model known to choke on faster colors (Battletron: < 0.2 s) never gets them.
        min_interval = max(self.min_interval, hint[0]) if hint is not None else self.min_interval
        return BulbConfig(
            name=self.name, id=self.id, ip=self.ip, local_key=self.local_key,
            version=self.version or "3.3", source=self.source, zone=self.zone,
            mode=self.mode, brightness=self.brightness, min_brightness=self.min_brightness,
            method=self.method, min_interval=min_interval, codes=tuple(self.codes),
            latency_adjust_ms=int(self.latency_adjust_ms),
            scene=dict(self.scene) if (self.use_scene and self.scene) else None,
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
    # precise: each bulb computes the effect color when sending, as often as its
    # "Szybkość zmian" allows; ticks: all bulbs change bulb_effect_rate times/s
    effect_sync: str = "precise"
    bulb_effect_rate: float = 1.0  # bulb color changes per second ("ticks" mode)
    keyboard_layout: str = "auto"  # auto (detected) | full | tkl | mini (60%)
    auto_update: bool = True  # check for program and device database updates daily
    # Effects: send bulbs their color ahead of time to make up for their delay.
    latency_mode: str = "auto"  # auto (measured per bulb) | manual (latency_ms) | off
    latency_ms: int = 300
    fps: float = 20
    sync_enabled: bool = True
    config_rev: int = 2  # one-time migrations of saved settings (files without it: 0)
    theme: str = "auto"  # window look: auto (like Windows) | dark | light

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
    # not saved: loading couldn't decrypt something (the file is kept on the next save)
    read_errors: bool = field(default=False, repr=False, compare=False)

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
                    codes=list(getattr(cd, "codes", ())),
                ))
                new += 1
            else:
                dev.name, dev.local_key = cd.name, cd.local_key
                dev.category, dev.product_name = cd.category, cd.product_name
                dev.codes = list(getattr(cd, "codes", ()))
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
        _keep_aside(path)
        return AppConfig()
    if not isinstance(data, dict):
        _LOGGER.error("Cannot read %s: not a settings file", path)
        _keep_aside(path)
        return AppConfig()
    devices = []
    read_errors = False  # something saved couldn't be decrypted (DPAPI): keep the file
    raw_devices = data.get("devices")
    for raw in raw_devices if isinstance(raw_devices, list) else []:
        if not isinstance(raw, dict):
            continue
        try:
            raw = dict(raw)
            raw["local_key"] = dpapi.unprotect(raw.get("local_key", ""))
            devices.append(Device.from_dict(raw))
            redact.remember(raw["local_key"])
        except (OSError, TypeError, ValueError) as err:
            _LOGGER.warning("Skipping device %s: %s", raw.get("id"), err)
            read_errors = True
    s = dict(data["settings"]) if isinstance(data.get("settings"), dict) else {}
    legacy_fx = s.pop("effect", None)  # 1.1: a single effect
    raw_profiles = s.pop("profiles", None)
    settings = Settings(**typed_fields(Settings, s))
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
    if settings.effect_sync not in ("precise", "ticks"):
        settings.effect_sync = "precise"
    if settings.keyboard_layout not in ("auto", "full", "tkl", "mini"):
        settings.keyboard_layout = "auto"
    if settings.theme not in ("auto", "dark", "light"):
        settings.theme = "auto"
    if settings.latency_mode not in ("auto", "manual", "off"):
        settings.latency_mode = "auto"
    settings.latency_ms = max(0, min(3000, int(settings.latency_ms)))
    settings.fps = max(1.0, min(60.0, float(settings.fps)))
    if settings.config_rev < 2 or "config_rev" not in s:
        # 1.9.12: "Kolor" was only the default, so it became "auto". (auto is
        # colour mode with smooth fades again; music/jump only when chosen.)
        for dev in devices:
            if dev.method == "colour":
                dev.method = "auto"
        settings.config_rev = 2
    session: dict[str, Any] = {}
    if isinstance(data.get("tuya_session"), str) and data["tuya_session"]:
        try:
            session = json.loads(dpapi.unprotect(data["tuya_session"]))
            redact.remember_session(session)
        except (OSError, ValueError, TypeError) as err:
            _LOGGER.warning("Saved Tuya login unreadable: %s", err)
            read_errors = True
    return AppConfig(
        devices=devices, settings=settings,
        username=str(data.get("username") or ""), user_code=str(data.get("user_code") or ""),
        tuya_session=session, read_errors=read_errors,
    )


def _keep_aside(path: Path) -> None:
    """A settings file that can't be read is kept (the next save would replace
    it, losing the lights, their keys and the Tuya login)."""
    try:
        backup = path.with_name(path.name + ".bad")
        backup.write_bytes(path.read_bytes())
        _LOGGER.error("The unreadable settings file was kept as %s", backup)
    except OSError as err:
        _LOGGER.error("Cannot keep the unreadable settings file: %s", err)


def save(config: AppConfig, path: Path | None = None) -> None:
    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if config.read_errors and path.exists():
        # Lights or the login couldn't be decrypted when loading (e.g. Windows
        # not ready at autostart): keep that file before it's replaced.
        _keep_aside(path)
        config.read_errors = False
    redact.remember(*(dev.local_key for dev in config.devices))
    redact.remember_session(config.tuya_session)
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
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows: another save replacing it right now, or antivirus reading it
            if attempt == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05 * (attempt + 1))
