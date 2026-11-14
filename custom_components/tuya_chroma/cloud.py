"""Fetch Tuya devices (incl. local keys) from a Smart Life / Tuya Smart account.

Uses the same QR-code authorization as Home Assistant's built-in Tuya
integration (tuya-device-sharing-sdk): the user enters the "User code" from the
app, scans a QR code with the app, and we can list the account's devices.
The cloud is only used during setup; lights are controlled locally afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from tuya_sharing import LoginControl, Manager

_LOGGER = logging.getLogger(__name__)

try:  # Reuse the client registered for Home Assistant's Tuya integration.
    from homeassistant.components.tuya.const import TUYA_CLIENT_ID, TUYA_SCHEMA
except ImportError:  # pragma: no cover - fallback if core moves the constants
    TUYA_CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
    TUYA_SCHEMA = "haauthorize"

# Tuya product categories that are lights (same as HA's Tuya light platform).
LIGHT_CATEGORIES = {
    "dc", "dd", "dj", "dsd", "fsd", "fwd", "fwl", "gyd", "mbd",
    "sxd", "tgkg", "tgq", "tyndj", "xdd",
}


class TuyaCloudError(Exception):
    """Login or API error."""


@dataclass
class CloudDevice:
    """A device as listed by the Tuya cloud."""

    id: str
    name: str
    local_key: str
    category: str
    product_name: str

    @property
    def is_light(self) -> bool:
        return self.category in LIGHT_CATEGORIES


def device_from_customer(device: Any) -> CloudDevice | None:
    """Convert a tuya_sharing CustomerDevice; None if it can't be used locally."""
    local_key = getattr(device, "local_key", None)
    if not local_key or getattr(device, "sub", False):
        return None  # Zigbee/BLE sub-devices have no local key of their own
    return CloudDevice(
        id=device.id,
        name=getattr(device, "name", None) or device.id,
        local_key=local_key,
        category=getattr(device, "category", "") or "",
        product_name=getattr(device, "product_name", "") or "",
    )


class TuyaCloudLogin:
    """Blocking helpers; run them in the executor."""

    def __init__(self) -> None:
        self._control = LoginControl()
        self.user_code: str | None = None
        self.qr_token: str | None = None

    def request_qr(self, user_code: str) -> str:
        """Request a QR login token for the given app user code."""
        response = self._control.qr_code(TUYA_CLIENT_ID, TUYA_SCHEMA, user_code)
        if not response.get("success"):
            raise TuyaCloudError(
                f"{response.get('msg', 'Unknown error')} (code {response.get('code', 0)})"
            )
        self.user_code = user_code
        self.qr_token = response["result"]["qrcode"]
        return self.qr_token

    @property
    def qr_data(self) -> str:
        return f"tuyaSmart--qrLogin?token={self.qr_token}"

    def fetch_devices(self) -> list[CloudDevice]:
        """Finish the QR login and return the account's devices."""
        if not self.qr_token or not self.user_code:
            raise TuyaCloudError("QR code was not requested")
        ok, info = self._control.login_result(self.qr_token, TUYA_CLIENT_ID, self.user_code)
        if not ok:
            raise TuyaCloudError(
                f"{info.get('msg', 'Login not confirmed')} (code {info.get('code', 0)})"
            )
        manager = Manager(
            TUYA_CLIENT_ID,
            self.user_code,
            info["terminal_id"],
            info["endpoint"],
            {
                "t": info["t"],
                "uid": info["uid"],
                "expire_time": info["expire_time"],
                "access_token": info["access_token"],
                "refresh_token": info["refresh_token"],
            },
        )
        manager.update_device_cache()
        return devices_from_manager(manager)


def devices_from_manager(manager: Any) -> list[CloudDevice]:
    """List usable devices of a tuya_sharing Manager."""
    devices = [
        dev
        for customer in manager.device_map.values()
        if (dev := device_from_customer(customer)) is not None
    ]
    devices.sort(key=lambda d: (not d.is_light, d.name.lower()))
    return devices
