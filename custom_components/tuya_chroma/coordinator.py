"""Polling coordinator for a local Tuya light."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, TUYA_SCAN_INTERVAL
from .tuya_device import TuyaError, TuyaLight, TuyaLightState

_LOGGER = logging.getLogger(__name__)


class TuyaCoordinator(DataUpdateCoordinator[TuyaLightState]):
    """Polls a Tuya light; also serializes commands to the device."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: TuyaLight) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=TUYA_SCAN_INTERVAL,
        )
        self.device = device

    async def _async_update_data(self) -> TuyaLightState:
        try:
            return await self.hass.async_add_executor_job(self.device.get_state)
        except TuyaError as err:
            raise UpdateFailed(str(err)) from err
