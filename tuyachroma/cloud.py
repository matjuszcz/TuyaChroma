"""Log in with a Smart Life / Tuya Smart account and list its devices.

The user enters the "User code" from the app and scans a QR code with the
app. This is the same user-level authorization that Home Assistant's
built-in Tuya integration uses (via tuya-device-sharing-sdk); the app shows
it as an authorization for "Home Assistant". The account is only used to
read device names and local keys; bulbs are then controlled locally.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import time
from typing import Any

from . import redact

_LOGGER = logging.getLogger(__name__)

# Client registered by Home Assistant for the Smart Life QR authorization.
TUYA_CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
TUYA_SCHEMA = "haauthorize"

# Data points that mean "can show RGB colors".
RGB_CODES = {"colour_data", "colour_data_v2", "colour_data_raw"}

# Tuya product categories that are lights.
LIGHT_CATEGORIES = {
    "dc", "dd", "dj", "dsd", "fsd", "fwd", "fwl", "gyd", "mbd",
    "sxd", "tgkg", "tgq", "tyndj", "xdd",
}


class TuyaCloudError(Exception):
    """Login or API error."""


@dataclass
class CloudDevice:
    id: str
    name: str
    local_key: str
    category: str
    product_name: str
    rgb: bool = False
    codes: tuple[str, ...] = ()  # the device's data points ("colour_data", "music_data", ...)


def _device(customer: Any) -> CloudDevice | None:
    key = getattr(customer, "local_key", None)
    if not key or getattr(customer, "sub", False):
        return None  # Zigbee/BLE sub-devices have no local key of their own
    category = getattr(customer, "category", "") or ""
    codes = set(getattr(customer, "function", None) or {}) | set(
        getattr(customer, "status_range", None) or {}
    ) | set(getattr(customer, "status", None) or {})
    # Prefer the device's own data points; without them, trust the category.
    rgb = bool(codes & RGB_CODES) if codes else category in LIGHT_CATEGORIES
    return CloudDevice(
        id=customer.id,
        name=getattr(customer, "name", None) or customer.id,
        local_key=key,
        category=category,
        product_name=getattr(customer, "product_name", "") or "",
        rgb=rgb and category in LIGHT_CATEGORIES,
        codes=tuple(sorted(str(c) for c in codes)),
    )


class TuyaAccount:
    """QR-code login flow. All methods block; call them from a worker thread."""

    def __init__(self) -> None:
        from tuya_sharing import LoginControl  # noqa: PLC0415

        self._control = LoginControl()
        self.user_code: str | None = None
        self.qr_token: str | None = None
        self.username: str | None = None

    def request_qr(self, user_code: str) -> str:
        """Returns the text to encode in the QR code."""
        user_code = user_code.strip()
        response = self._control.qr_code(TUYA_CLIENT_ID, TUYA_SCHEMA, user_code)
        if not response.get("success"):
            raise TuyaCloudError(
                f"{response.get('msg', 'Nieznany błąd')} (kod {response.get('code', 0)})"
            )
        self.user_code = user_code
        self.qr_token = response["result"]["qrcode"]
        return self.qr_data

    @property
    def qr_data(self) -> str:
        return f"tuyaSmart--qrLogin?token={self.qr_token}"

    def poll_login(self) -> dict[str, Any] | None:
        """Login info once the QR code was scanned and confirmed, else None."""
        if not self.qr_token or not self.user_code:
            raise TuyaCloudError("Najpierw wygeneruj kod QR")
        ok, info = self._control.login_result(self.qr_token, TUYA_CLIENT_ID, self.user_code)
        if not ok:
            return None
        self.username = info.get("username")
        return info

    def session(self, info: dict[str, Any]) -> dict[str, Any]:
        """What is needed to list devices again later without a new QR code."""
        return {
            "user_code": self.user_code,
            "terminal_id": info["terminal_id"],
            "endpoint": info["endpoint"],
            "token_info": {k: info[k] for k in ("t", "uid", "expire_time", "access_token", "refresh_token")},
        }

    def fetch_devices(self, info: dict[str, Any]) -> list[CloudDevice]:
        devices, _session = fetch_with_session(self.session(info))
        return devices


# Errors of a saved login that can't be renewed (needs a new QR login).
EXPIRED_MARKERS = ("token is expired", "sign invalid", "(1010)", "token invalid")


def _token_listener(updated: dict[str, Any], on_token: Callable[[dict[str, Any]], None] | None):
    from tuya_sharing import SharingTokenListener  # noqa: PLC0415

    class Listener(SharingTokenListener):
        def update_token(self, token_info: dict[str, Any]) -> None:
            # Tuya hands out a NEW refresh token with every refresh and the old one
            # stops working: save it at once, even if what follows fails (losing it
            # was the "sign invalid" / expired login of the 1.9.19 log).
            updated["token_info"] = dict(token_info)
            redact.remember_session(updated)
            if on_token is not None:
                on_token(dict(updated))

    return Listener()


def keep_session_alive(session: dict[str, Any],
                       on_token: Callable[[dict[str, Any]], None] | None = None) -> bool | None:
    """Renew the saved login when its access token (valid ~2 h) runs out.

    Run regularly while the program runs (like Home Assistant's Tuya
    integration does), so the chain of refresh tokens never lapses.
    Returns True (valid), False (Tuya refused it: a new QR login is needed) or
    None (no answer, e.g. no internet - try again later, nothing is lost).
    """
    from tuya_sharing.customerapi import CustomerApi, CustomerTokenInfo  # noqa: PLC0415

    try:
        token = CustomerTokenInfo(session["token_info"])
        api = CustomerApi(token, TUYA_CLIENT_ID, session["user_code"], session["endpoint"], None)
    except KeyError:
        return False
    if token.expire_time - 5 * 60 * 1000 > time.time() * 1000:
        return True  # still valid for a while
    # Renew here instead of the SDK's refresh_access_token_if_need(), which
    # swallows errors: "no internet" must not look like "login expired".
    api.refresh_token = True  # the SDK's own guard: don't let get() refresh by itself
    try:
        response = api.get("/v1.0/m/token/" + token.refresh_token)
    except Exception as err:  # noqa: BLE001 - network trouble
        # (the error text holds the request URL, which holds the refresh token)
        _LOGGER.info("Tuya login renewal: no answer (%s), will retry", type(err).__name__)
        return None
    if not isinstance(response, dict) or not response.get("success"):
        if isinstance(response, dict):
            _LOGGER.warning("Tuya login renewal refused: code=%s msg=%s",
                            response.get("code"), response.get("msg"))
        else:
            _LOGGER.warning("Tuya login renewal refused: %s", type(response).__name__)
        return False
    result = response.get("result", {})
    _token_listener(dict(session), on_token).update_token({
        "t": response["t"], "expire_time": result["expireTime"], "uid": result["uid"],
        "access_token": result["accessToken"], "refresh_token": result["refreshToken"],
    })
    return True


def fetch_with_session(session: dict[str, Any],
                       on_token: Callable[[dict[str, Any]], None] | None = None,
                       ) -> tuple[list[CloudDevice], dict[str, Any]]:
    """List the account's devices with a saved login (refreshes the token if needed).

    Returns the devices and the (possibly updated) session to save; a refreshed
    login is also passed to ``on_token`` right away. Raises TuyaCloudError if
    the login expired and a new QR login is needed.
    """
    from tuya_sharing import Manager  # noqa: PLC0415

    updated = dict(session)

    try:
        manager = Manager(
            TUYA_CLIENT_ID, session["user_code"], session["terminal_id"], session["endpoint"],
            session["token_info"], _token_listener(updated, on_token),
        )
        manager.update_device_cache()
    except KeyError as err:
        raise TuyaCloudError("Brak zapisanego logowania") from err
    except Exception as err:  # noqa: BLE001 - network / auth errors from the SDK
        text = str(err).lower()
        if any(m in text for m in EXPIRED_MARKERS):
            # The saved login can't be renewed any more (token expired and the
            # refresh is rejected: "1010 token is expired", "sign invalid").
            raise TuyaCloudError("Logowanie do konta Tuya wygasło – zaloguj się ponownie kodem QR "
                                 "(zakładka „Konto Tuya”). Światła i ustawienia zostają.") from err
        raise TuyaCloudError(f"Nie udało się pobrać urządzeń: {err}") from err
    if not manager.user_homes and not manager.device_map:
        raise TuyaCloudError("Logowanie wygasło – zaloguj się ponownie kodem QR")
    devices = [d for c in manager.device_map.values() if (d := _device(c)) is not None]
    devices.sort(key=lambda d: d.name.lower())
    return devices, updated
