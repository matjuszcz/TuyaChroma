"""End-to-end tests for Tuya Chroma."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID, CONF_HOST, CONF_NAME, CONF_PORT, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.tuya_chroma.chroma import rgb_to_bgr_int
from custom_components.tuya_chroma.const import (
    CONF_CHROMA_DEVICES,
    CONF_DEVICE_ID,
    CONF_ENTRY_TYPE,
    CONF_FOLLOW_ENTITY,
    CONF_LOCAL_KEY,
    CONF_PROTOCOL_VERSION,
    DOMAIN,
    ENTRY_TYPE_CHROMA,
    ENTRY_TYPE_TUYA,
)

HOST = "192.168.1.50"
SDK = f"http://{HOST}:54235/razer/chromasdk"
SESSION = f"http://{HOST}:54236/chromasdk"


def mock_sdk(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(SDK, json={"core": "3.36", "version": "3.36.1"})
    # Synapse answers with "localhost"; the client must swap in the real host.
    aioclient_mock.post(SDK, json={"sessionid": 1, "uri": "http://localhost:54236/chromasdk"})
    aioclient_mock.put(f"{SESSION}/heartbeat", json={"tick": 1})
    for dev in ("keyboard", "mouse"):
        aioclient_mock.put(f"{SESSION}/{dev}", json={"result": 0})
    aioclient_mock.delete(SESSION, json={"result": 0})


def sdk_puts(aioclient_mock: AiohttpClientMocker, device: str) -> list[dict]:
    return [
        call[2]
        for call in aioclient_mock.mock_calls
        if call[0] == "PUT" and str(call[1]) == f"{SESSION}/{device}"
    ]


def chroma_entry(options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Gaming PC",
        unique_id=f"chroma_{HOST}_54235",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_CHROMA,
            CONF_HOST: HOST,
            CONF_PORT: 54235,
            CONF_CHROMA_DEVICES: ["keyboard", "mouse"],
        },
        options=options or {},
    )


def tuya_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Desk lamp",
        unique_id="bf123",
        data={
            CONF_ENTRY_TYPE: ENTRY_TYPE_TUYA,
            CONF_HOST: "192.168.1.60",
            CONF_DEVICE_ID: "bf123",
            CONF_LOCAL_KEY: "secret",
            CONF_PROTOCOL_VERSION: "3.3",
        },
    )


async def test_chroma_flow(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_sdk(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "chroma"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Gaming PC", CONF_HOST: HOST, CONF_PORT: 54235, CONF_CHROMA_DEVICES: ["keyboard"]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ENTRY_TYPE] == ENTRY_TYPE_CHROMA


async def test_tuya_flow(hass: HomeAssistant, fake_tuya) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "tuya"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Desk lamp",
            CONF_HOST: "192.168.1.60",
            CONF_DEVICE_ID: "bf123",
            CONF_LOCAL_KEY: "secret",
            CONF_PROTOCOL_VERSION: "3.3",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "bf123"


async def test_chroma_light(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    mock_sdk(aioclient_mock)
    entry = chroma_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_id = "light.gaming_pc_keyboard"
    assert hass.states.get(entity_id).state == STATE_OFF

    await hass.services.async_call(
        LIGHT_DOMAIN, "turn_on",
        {ATTR_ENTITY_ID: entity_id, ATTR_HS_COLOR: (0, 100), ATTR_BRIGHTNESS: 255},
        blocking=True,
    )
    assert hass.states.get(entity_id).state == STATE_ON
    assert sdk_puts(aioclient_mock, "keyboard")[-1] == {
        "effect": "CHROMA_STATIC",
        "param": {"color": rgb_to_bgr_int(255, 0, 0)},
    }

    await hass.services.async_call(LIGHT_DOMAIN, "turn_off", {ATTR_ENTITY_ID: entity_id}, blocking=True)
    assert sdk_puts(aioclient_mock, "keyboard")[-1] == {"effect": "CHROMA_NONE"}

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert any(c[0] == "DELETE" for c in aioclient_mock.mock_calls)


async def test_tuya_light(hass: HomeAssistant, fake_tuya) -> None:
    entry = tuya_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = "light.desk_lamp"
    bulb = fake_tuya.instances[-1]

    await hass.services.async_call(
        LIGHT_DOMAIN, "turn_on",
        {ATTR_ENTITY_ID: entity_id, ATTR_HS_COLOR: (120, 100), ATTR_BRIGHTNESS: 128},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert bulb.calls[-1][0] == "set_hs"
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    assert state.attributes["color_mode"] == "hs"
    assert state.attributes[ATTR_BRIGHTNESS] == 128

    await hass.services.async_call(
        LIGHT_DOMAIN, "turn_on",
        {ATTR_ENTITY_ID: entity_id, ATTR_COLOR_TEMP_KELVIN: 6500},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert bulb.calls[-1][0] == "set_white" and bulb.calls[-1][2] == 1.0
    assert hass.states.get(entity_id).attributes["color_mode"] == "color_temp"

    await hass.services.async_call(LIGHT_DOMAIN, "turn_off", {ATTR_ENTITY_ID: entity_id}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_chroma_follows_tuya(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fake_tuya
) -> None:
    mock_sdk(aioclient_mock)
    tuya = tuya_entry()
    tuya.add_to_hass(hass)
    assert await hass.config_entries.async_setup(tuya.entry_id)
    await hass.async_block_till_done()

    chroma = chroma_entry({CONF_FOLLOW_ENTITY: "light.desk_lamp"})
    chroma.add_to_hass(hass)
    assert await hass.config_entries.async_setup(chroma.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        LIGHT_DOMAIN, "turn_on",
        {ATTR_ENTITY_ID: "light.desk_lamp", ATTR_HS_COLOR: (240, 100), ATTR_BRIGHTNESS: 255},
        blocking=True,
    )
    await hass.async_block_till_done()

    blue = {"effect": "CHROMA_STATIC", "param": {"color": rgb_to_bgr_int(0, 0, 255)}}
    assert sdk_puts(aioclient_mock, "keyboard")[-1] == blue
    assert sdk_puts(aioclient_mock, "mouse")[-1] == blue
    assert hass.states.get("light.gaming_pc_mouse").state == STATE_ON

    await hass.services.async_call(LIGHT_DOMAIN, "turn_off", {ATTR_ENTITY_ID: "light.desk_lamp"}, blocking=True)
    await hass.async_block_till_done()
    assert sdk_puts(aioclient_mock, "keyboard")[-1] == {"effect": "CHROMA_NONE"}
