"""Detect Razer devices connected to this computer (USB, wireless dongles, Bluetooth).

Uses the Windows device list (SetupAPI, like Device Manager): Razer's USB
vendor id is 0x1532; Bluetooth devices are matched by name. Each device is
mapped to its Chroma SDK category so the user can see what gets effects.
"""

from __future__ import annotations

import ctypes as C
from dataclasses import dataclass
import logging
import re
import sys

from . import devices_db

_LOGGER = logging.getLogger(__name__)

RAZER_VID = "VID_1532"

CATEGORY_LABELS = {
    "keyboard": "Klawiatura",
    "mouse": "Mysz",
    "mousepad": "Podkładka / stacja",
    "headset": "Słuchawki",
    "keypad": "Keypad",
    "chromalink": "Chroma Link",
    "controller": "Kontroler",
    "other": "Inne",
}

# Chroma SDK category used for each detected type
SDK_CATEGORY = {
    "keyboard": "keyboard", "mouse": "mouse", "mousepad": "mousepad", "headset": "headset",
    "keypad": "keypad", "chromalink": "chromalink", "controller": "chromalink", "other": "chromalink",
}


@dataclass
class RazerDevice:
    name: str
    category: str
    connection: str  # "USB", "bezprzewodowo (odbiornik)", "Bluetooth"
    pid: str = ""

    @property
    def sdk_category(self) -> str:
        return SDK_CATEGORY[self.category]

    @property
    def note(self) -> str:
        low = self.name.lower()
        for entry in devices_db.load().get("notes", []):
            if entry["match"] in low:
                return entry["note"]
        if self.category in ("controller", "other"):
            return "przez Chroma Link, jeśli działa w Synapse"
        return ""


def categorize(name: str) -> str:
    low = f" {name.lower()} "
    for rule in devices_db.load()["rules"]:
        if any(w in low for w in rule["keywords"]):
            return rule["category"]
    return "other"


def _pid_info(pid: str) -> dict | None:
    return devices_db.load().get("pids", {}).get(pid.upper()) if pid else None


def _connection(instance_id: str, name: str) -> str:
    up = instance_id.upper()
    if up.startswith(("BTHLE", "BTHENUM", "BTHLEDEVICE")):
        return "Bluetooth"
    low = name.lower()
    if any(w in low for w in ("dongle", "receiver", "hyperspeed", "wireless")):
        return "odbiornik bezprzew."
    return "USB"


_GENERIC = re.compile(r"^(usb|hid)[ -]|input device|composite device|keyboard device$|"
                      r"^hid-compliant|^usb input|^usb composite", re.I)


def group_devices(entries: list[tuple[str, str]]) -> list[RazerDevice]:
    """[(instance_id, name)] from the device list -> one RazerDevice per product."""
    by_key: dict[str, RazerDevice] = {}
    for instance_id, name in entries:
        name = (name or "").strip()
        up = instance_id.upper()
        is_razer_usb = RAZER_VID in up
        if not is_razer_usb and "razer" not in name.lower():
            continue
        if not name or _GENERIC.search(name):
            if not is_razer_usb:
                continue
            name = ""
        m = re.search(r"PID_([0-9A-F]{4})", up)
        pid = m.group(1) if m else ""
        key = pid or name.lower()
        if not key:
            continue
        dev = by_key.get(key)
        pretty = name if name.lower().startswith("razer") or not name else f"Razer {name}"
        if dev is None:
            by_key[key] = RazerDevice(name=pretty, category=categorize(pretty) if pretty else "other",
                                      connection=_connection(instance_id, pretty), pid=pid)
        elif pretty and (not dev.name or len(pretty) > len(dev.name)):
            dev.name = pretty
            dev.category = categorize(pretty)
            dev.connection = _connection(instance_id, pretty)
    devices = []
    for dev in by_key.values():
        info = _pid_info(dev.pid)
        if not dev.name and info is not None:  # Windows gave no name: use the database
            dev.name = info.get("name") or ""
            dev.category = info["category"]
        if not dev.name:
            dev.name = f"Urządzenie Razer (PID {dev.pid})"
        devices.append(dev)
    devices.sort(key=lambda d: (list(CATEGORY_LABELS).index(d.category), d.name.lower()))
    return devices


def list_present_devices() -> list[tuple[str, str]]:
    """(instance id, best name) of every present device (Windows SetupAPI)."""
    if sys.platform != "win32":
        return []
    from ctypes import wintypes as W  # noqa: PLC0415

    setupapi = C.WinDLL("setupapi", use_last_error=True)

    class GUID(C.Structure):
        _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD), ("Data4", C.c_ubyte * 8)]

    class SP_DEVINFO_DATA(C.Structure):
        _fields_ = [("cbSize", W.DWORD), ("ClassGuid", GUID), ("DevInst", W.DWORD),
                    ("Reserved", C.c_size_t)]

    class DEVPROPKEY(C.Structure):
        _fields_ = [("fmtid", GUID), ("pid", W.ULONG)]

    HDEVINFO = C.c_void_p
    setupapi.SetupDiGetClassDevsW.restype = HDEVINFO
    setupapi.SetupDiGetClassDevsW.argtypes = [C.c_void_p, W.LPCWSTR, W.HWND, W.DWORD]
    setupapi.SetupDiEnumDeviceInfo.argtypes = [HDEVINFO, W.DWORD, C.POINTER(SP_DEVINFO_DATA)]
    setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
        HDEVINFO, C.POINTER(SP_DEVINFO_DATA), W.LPWSTR, W.DWORD, C.POINTER(W.DWORD)]
    setupapi.SetupDiGetDevicePropertyW.argtypes = [
        HDEVINFO, C.POINTER(SP_DEVINFO_DATA), C.POINTER(DEVPROPKEY), C.POINTER(W.ULONG),
        C.c_void_p, W.DWORD, C.POINTER(W.DWORD), W.DWORD]
    setupapi.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        HDEVINFO, C.POINTER(SP_DEVINFO_DATA), W.DWORD, C.POINTER(W.DWORD), C.c_void_p, W.DWORD,
        C.POINTER(W.DWORD)]
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [HDEVINFO]

    DIGCF_PRESENT, DIGCF_ALLCLASSES = 0x2, 0x4
    SPDRP_DEVICEDESC, SPDRP_FRIENDLYNAME = 0x0, 0xC
    # DEVPKEY_Device_BusReportedDeviceDesc: the product name the device itself reports
    bus_desc = DEVPROPKEY(GUID(0x540B947E, 0x8B40, 0x45BC,
                               (C.c_ubyte * 8)(0xA8, 0xA2, 0x6A, 0x0B, 0x89, 0x4C, 0xBD, 0xA2)), 4)

    handle = setupapi.SetupDiGetClassDevsW(None, None, None, DIGCF_PRESENT | DIGCF_ALLCLASSES)
    if not handle or handle == C.c_void_p(-1).value:
        _LOGGER.warning("SetupDiGetClassDevs failed (%s)", C.get_last_error())
        return []
    out: list[tuple[str, str]] = []
    buf = C.create_unicode_buffer(1024)
    try:
        index = 0
        while True:
            data = SP_DEVINFO_DATA()
            data.cbSize = C.sizeof(SP_DEVINFO_DATA)
            if not setupapi.SetupDiEnumDeviceInfo(handle, index, C.byref(data)):
                break
            index += 1
            if not setupapi.SetupDiGetDeviceInstanceIdW(handle, C.byref(data), buf, len(buf), None):
                continue
            instance_id = buf.value
            names = []
            ptype = W.ULONG()
            if setupapi.SetupDiGetDevicePropertyW(handle, C.byref(data), C.byref(bus_desc), C.byref(ptype),
                                                  buf, C.sizeof(buf), None, 0):
                names.append(buf.value)
            for prop in (SPDRP_FRIENDLYNAME, SPDRP_DEVICEDESC):
                if setupapi.SetupDiGetDeviceRegistryPropertyW(handle, C.byref(data), prop, None, buf,
                                                              C.sizeof(buf), None):
                    names.append(buf.value)
            best = next((n for n in names if n and "razer" in n.lower()), names[0] if names else "")
            out.append((instance_id, best))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(handle)
    return out


def detect() -> list[RazerDevice]:
    try:
        devices = group_devices(list_present_devices())
    except Exception:  # noqa: BLE001 - never break the app because of device listing
        _LOGGER.exception("Razer device detection failed")
        return []
    _LOGGER.info("Razer devices: %s", "; ".join(
        f"{d.name} [{d.category}, PID {d.pid or '-'}, {d.connection}]" for d in devices) or "none")
    return devices


def keyboard_layout(devices: list[RazerDevice]) -> str:
    """Guess the keyboard layout from the connected keyboards' names."""
    names = [d.name.lower() for d in devices if d.category == "keyboard"]
    if not names:
        return "full"
    layouts = devices_db.load().get("keyboard_layouts", {})
    mini = layouts.get("mini", [])
    small = mini + layouts.get("tkl", [])
    if all(any(w in n for w in mini) for n in names):
        return "mini"
    if all(any(w in n for w in small) for n in names):
        return "tkl"
    return "full"
