"""Razer device database: how connected devices are recognised.

A copy ships with the program (data/razer_devices.json). A newer version can be
downloaded from the GitHub repository without updating the program; it is
cached in %APPDATA%\\TuyaChroma\\razer_devices.json and used when its
"version" is higher than the built-in one.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sys
import threading
from typing import Any
import urllib.request

from . import __version__

_LOGGER = logging.getLogger(__name__)

DB_URL = "https://raw.githubusercontent.com/matjuszcz/TuyaChroma/main/data/razer_devices.json"
CATEGORIES = {"keyboard", "mouse", "mousepad", "headset", "keypad", "chromalink", "controller", "other"}
_lock = threading.Lock()
_active: dict[str, Any] | None = None


class DatabaseError(Exception):
    """Invalid or unreachable device database."""


def bundled_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / "data" / "razer_devices.json"


def cache_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "TuyaChroma" / "razer_devices.json"


def validate(db: Any) -> dict[str, Any]:
    """Check the structure; raises DatabaseError if something is wrong."""
    if (not isinstance(db, dict) or not isinstance(db.get("version"), int)
            or isinstance(db["version"], bool) or not 0 < db["version"] < 10**6):
        raise DatabaseError("brak numeru wersji bazy")
    rules = db.get("rules")
    if not isinstance(rules, list) or not rules:
        raise DatabaseError("brak reguł rozpoznawania")
    for rule in rules:
        if (not isinstance(rule, dict) or rule.get("category") not in CATEGORIES
                or not isinstance(rule.get("keywords"), list)
                or not all(isinstance(k, str) and k for k in rule["keywords"])):
            raise DatabaseError(f"błędna reguła: {rule!r}")
    layouts = db.get("keyboard_layouts", {})
    if not isinstance(layouts, dict) or not all(
            k in ("mini", "tkl") and isinstance(v, list)
            and all(isinstance(w, str) and w for w in v) for k, v in layouts.items()):
        raise DatabaseError("błędne układy klawiatur")
    if not isinstance(db.get("notes", []), list):
        raise DatabaseError("błędne uwagi")
    for note in db.get("notes", []):
        if not isinstance(note, dict) or not isinstance(note.get("match"), str) \
                or not note["match"] or not isinstance(note.get("note"), str):
            raise DatabaseError(f"błędna uwaga: {note!r}")
    pids = db.get("pids", {})
    if not isinstance(pids, dict) or not all(
            isinstance(v, dict) and v.get("category") in CATEGORIES for v in pids.values()):
        raise DatabaseError("błędna lista PID")
    return db


def _read(path: Path) -> dict[str, Any] | None:
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except Exception as err:  # noqa: BLE001 - a damaged cache must never stop the program
        _LOGGER.warning("Ignoring device database %s: %s", path, err)
        return None


def load() -> dict[str, Any]:
    """The newest valid database (downloaded copy or built-in)."""
    global _active
    with _lock:
        if _active is None:
            builtin = _read(bundled_path())
            cached = _read(cache_path())
            candidates = [d for d in (builtin, cached) if d is not None]
            if not candidates:
                raise DatabaseError("brak bazy urządzeń")
            _active = max(candidates, key=lambda d: d["version"])
        return _active


def reset() -> None:
    global _active
    with _lock:
        _active = None


def fetch_remote(url: str = DB_URL, timeout: float = 10) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": f"TuyaChroma/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
            raw = resp.read(2_000_000)
    except OSError as err:
        raise DatabaseError(f"nie udało się pobrać bazy: {err}") from err
    try:
        return validate(json.loads(raw.decode("utf-8")))
    except (ValueError, TypeError, AttributeError, KeyError) as err:
        raise DatabaseError(f"pobrana baza jest uszkodzona: {err}") from err


def update(fetch=fetch_remote) -> tuple[bool, int]:
    """Download the database; keep it if newer. Returns (updated, active version)."""
    remote = fetch()
    current = load()
    if remote["version"] <= current["version"]:
        return False, current["version"]
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(remote, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    reset()
    _LOGGER.info("Razer device database updated to version %s", remote["version"])
    return True, load()["version"]
