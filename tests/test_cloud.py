"""Tests for importing Tuya devices from a Tuya / Smart Life account."""

from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tinytuya

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.tuya_chroma.const import (
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_PROTOCOL_VERSION,
    DOMAIN,
)
from custom_components.tuya_chroma.discovery import async_discover, parse_broadcast


def encrypted_broadcast(payload: dict) -> bytes:
    """A 3.3-style broadcast: 55AA frame with AES-ECB payload (udpkey)."""
    enc = tinytuya.AESCipher(tinytuya.udpkey).encrypt(json.dumps(payload).encode(), False)
    # Device frames start the payload with a 4-byte return code.
    msg = tinytuya.TuyaMessage(0, 0x13, 0, b"\x00" * 4 + enc, 0, True, tinytuya.PREFIX_55AA_VALUE, False)
    return tinytuya.pack_message(msg)


def test_parse_broadcast() -> None:
    data = encrypted_broadcast({"ip": "192.168.1.61", "gwId": "bf_lamp", "version": "3.3"})
    assert parse_broadcast(data) == {"id": "bf_lamp", "ip": "192.168.1.61", "version": "3.3"}
    assert parse_broadcast(b"garbage") is None


async def test_async_discover_receives_udp(socket_enabled) -> None:
    async def send_later() -> None:
        await asyncio.sleep(0.2)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(
            encrypted_broadcast({"ip": "192.168.1.61", "gwId": "bf_lamp", "version": "3.4"}),
            ("127.0.0.1", 6667),
        )
        sock.close()

    with patch("tinytuya.scanner.send_discovery_request"):
        task = asyncio.create_task(send_later())
        found = await async_discover({"bf_lamp"}, timeout=3)
        await task
    assert found["bf_lamp"]["ip"] == "192.168.1.61"
    assert found["bf_lamp"]["version"] == "3.4"


def customer(dev_id: str, name: str, category: str, key: str | None = "k" * 16, sub=False):
    return SimpleNamespace(
        id=dev_id, name=name, local_key=key, category=category, product_name="", sub=sub
    )


async def test_account_import_flow(hass: HomeAssistant, fake_tuya) -> None:
    login = MagicMock()
    login.qr_code.return_value = {"success": True, "result": {"qrcode": "tok123"}}
    login.login_result.return_value = (
        True,
        {
            "t": 1, "uid": "u", "expire_time": 7200, "access_token": "a",
            "refresh_token": "r", "terminal_id": "term", "endpoint": "https://apigw.tuyaeu.com",
        },
    )
    manager = MagicMock()
    manager.device_map = {
        "bf_lamp": customer("bf_lamp", "Desk lamp", "dj"),
        "bf_strip": customer("bf_strip", "LED strip", "dd"),
        "bf_plug": customer("bf_plug", "Plug", "cz"),
        "zb_sub": customer("zb_sub", "Zigbee bulb", "dj", sub=True),
    }
    discovered = {"bf_lamp": {"id": "bf_lamp", "ip": "192.168.1.61", "version": "3.4"}}

    with (
        patch("custom_components.tuya_chroma.cloud.LoginControl", return_value=login),
        patch("custom_components.tuya_chroma.cloud.Manager", return_value=manager),
        patch("custom_components.tuya_chroma.config_flow.async_discover", return_value=discovered),
        patch(
            "custom_components.tuya_chroma.config_flow.probe_version",
            side_effect=lambda _id, _h, _k, versions: versions[0],
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert "tuya_cloud" in result["menu_options"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "tuya_cloud"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"user_code": "ABC123"}
        )
        assert result["step_id"] == "tuya_scan"
        login.qr_code.assert_called_once()

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "tuya_pick"
        schema = result["data_schema"].schema
        key = next(iter(schema))
        options = [o["value"] for o in schema[key].config["options"]]
        assert "zb_sub" not in options  # sub-devices have no own local key
        assert key.default() == ["bf_lamp", "bf_strip"]  # lights preselected

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"devices": ["bf_lamp", "bf_strip"]}
        )
        assert result["step_id"] == "tuya_ips"  # strip was not found on the LAN
        field = next(iter(result["data_schema"].schema))
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {str(field): "192.168.1.62"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()

    entries = {e.unique_id: e for e in hass.config_entries.async_entries(DOMAIN)}
    assert set(entries) == {"bf_lamp", "bf_strip"}
    assert entries["bf_lamp"].data[CONF_PROTOCOL_VERSION] == "3.4"
    assert entries["bf_lamp"].data[CONF_LOCAL_KEY] == "k" * 16
    assert entries["bf_strip"].data["host"] == "192.168.1.62"
    assert entries["bf_strip"].data[CONF_PROTOCOL_VERSION] == "3.3"
    assert entries["bf_strip"].title == "LED strip"
    assert entries["bf_strip"].data[CONF_DEVICE_ID] == "bf_strip"


async def test_account_login_not_confirmed(hass: HomeAssistant) -> None:
    login = MagicMock()
    login.qr_code.return_value = {"success": True, "result": {"qrcode": "tok"}}
    login.login_result.return_value = (False, {"msg": "not scanned", "code": 1})
    with patch("custom_components.tuya_chroma.cloud.LoginControl", return_value=login):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "tuya_cloud"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"user_code": "ABC"}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "tuya_scan"
    assert result["errors"] == {"base": "login_error"}
    assert login.qr_code.call_count == 2  # fresh QR code offered


async def test_import_from_core_tuya(hass: HomeAssistant, fake_tuya) -> None:
    from homeassistant.config_entries import ConfigEntryState
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    core = MockConfigEntry(domain="tuya", state=ConfigEntryState.LOADED)
    core.add_to_hass(hass)
    manager = MagicMock()
    manager.device_map = {"bf_lamp": customer("bf_lamp", "Desk lamp", "dj")}
    core.runtime_data = SimpleNamespace(manager=manager)
    discovered = {"bf_lamp": {"id": "bf_lamp", "ip": "192.168.1.61", "version": "3.5"}}

    with (
        patch("custom_components.tuya_chroma.config_flow.async_discover", return_value=discovered),
        patch(
            "custom_components.tuya_chroma.config_flow.probe_version",
            side_effect=lambda _id, _h, _k, versions: versions[0],
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["menu_options"][0] == "tuya_core"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "tuya_core"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"devices": ["bf_lamp"]}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PROTOCOL_VERSION] == "3.5"
