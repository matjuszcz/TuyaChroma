"""Tuya Chroma: local Tuya lights + Razer Chroma for Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .chroma import ChromaClient, ChromaError
from .const import (
    CHROMA_HEARTBEAT_INTERVAL,
    CONF_CHROMA_DEVICES,
    CONF_DEVICE_ID,
    CONF_ENTRY_TYPE,
    CONF_LOCAL_KEY,
    CONF_PROTOCOL_VERSION,
    DEFAULT_CHROMA_DEVICES,
    ENTRY_TYPE_CHROMA,
)
from .coordinator import TuyaCoordinator
from .sync import async_setup_follow
from .tuya_device import TuyaLight

if TYPE_CHECKING:
    from .light import ChromaLight

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LIGHT]


@dataclass
class ChromaRuntime:
    """Runtime data of a Razer Chroma entry."""

    client: ChromaClient
    devices: list[str]
    lights: list[ChromaLight] = field(default_factory=list)


type TuyaChromaConfigEntry = ConfigEntry[ChromaRuntime | TuyaCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: TuyaChromaConfigEntry) -> bool:
    """Set up an entry (either a Chroma PC or a Tuya light)."""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_CHROMA:
        await _async_setup_chroma(hass, entry)
    else:
        device = TuyaLight(
            entry.data[CONF_DEVICE_ID],
            entry.data[CONF_HOST],
            entry.data[CONF_LOCAL_KEY],
            entry.data[CONF_PROTOCOL_VERSION],
        )
        coordinator = TuyaCoordinator(hass, entry, device)
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_setup_follow(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    return True


async def _async_setup_chroma(hass: HomeAssistant, entry: TuyaChromaConfigEntry) -> None:
    devices = entry.data.get(CONF_CHROMA_DEVICES, DEFAULT_CHROMA_DEVICES)
    client = ChromaClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        devices,
    )
    try:
        await client.async_connect()
    except ChromaError as err:
        raise ConfigEntryNotReady(f"Razer Chroma SDK not reachable: {err}") from err

    runtime = ChromaRuntime(client=client, devices=devices)
    entry.runtime_data = runtime

    async def _heartbeat(_now: datetime) -> None:
        reconnecting = not client.connected
        try:
            await client.async_heartbeat()
        except ChromaError as err:
            _LOGGER.debug("Chroma heartbeat failed: %s", err)
            for light in runtime.lights:
                light.set_available(False)
            return
        for light in runtime.lights:
            light.set_available(True)
            if reconnecting:
                # A fresh session starts blank; push the remembered colors again.
                await light.async_reapply()

    entry.async_on_unload(
        async_track_time_interval(hass, _heartbeat, CHROMA_HEARTBEAT_INTERVAL)
    )

    async def _disconnect() -> None:
        await client.async_disconnect()

    entry.async_on_unload(_disconnect)


async def async_unload_entry(hass: HomeAssistant, entry: TuyaChromaConfigEntry) -> bool:
    """Unload an entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_on_update(hass: HomeAssistant, entry: TuyaChromaConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
