"""Light entities for Razer Chroma devices and local Tuya lights."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ATTR_WHITE,
    ColorMode,
    LightEntity,
)
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import color as color_util

from . import ChromaRuntime, TuyaChromaConfigEntry
from .chroma import ChromaError
from .const import (
    CHROMA_DEVICES,
    CONF_DEVICE_ID,
    CONF_ENTRY_TYPE,
    DOMAIN,
    ENTRY_TYPE_CHROMA,
    TUYA_MAX_KELVIN,
    TUYA_MIN_KELVIN,
)
from .coordinator import TuyaCoordinator
from .tuya_device import TuyaError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TuyaChromaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up lights for an entry."""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_CHROMA:
        runtime: ChromaRuntime = entry.runtime_data  # type: ignore[assignment]
        lights = [ChromaLight(entry, runtime, device) for device in runtime.devices]
        runtime.lights.extend(lights)
        async_add_entities(lights)
    else:
        async_add_entities([TuyaLightEntity(entry, entry.runtime_data)])  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Razer Chroma
# --------------------------------------------------------------------------


class ChromaLight(LightEntity, RestoreEntity):
    """One Razer Chroma device class (keyboard, mouse, ...) as a light.

    The Chroma SDK cannot report the current color, so the state is
    optimistic and restored after a Home Assistant restart.
    """

    _attr_has_entity_name = True
    _attr_assumed_state = True
    _attr_color_mode = ColorMode.HS
    _attr_supported_color_modes = {ColorMode.HS}
    _attr_should_poll = False

    def __init__(self, entry: TuyaChromaConfigEntry, runtime: ChromaRuntime, device: str) -> None:
        self._runtime = runtime
        self._device = device
        self._attr_name = CHROMA_DEVICES.get(device, device)
        self._attr_unique_id = f"{entry.entry_id}_{device}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Razer",
            model="Chroma SDK (Synapse)",
            configuration_url=runtime.client.base_url,
        )
        self._attr_is_on = False
        self._attr_hs_color = (0.0, 0.0)
        self._attr_brightness = 255

    async def async_added_to_hass(self) -> None:
        """Restore the last known color and push it to the device."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == STATE_ON
            if (hs := last.attributes.get(ATTR_HS_COLOR)) is not None:
                self._attr_hs_color = (float(hs[0]), float(hs[1]))
            if (bri := last.attributes.get(ATTR_BRIGHTNESS)) is not None:
                self._attr_brightness = int(bri)
        if self._attr_is_on:
            await self.async_reapply()

    @callback
    def set_available(self, available: bool) -> None:
        """Update availability from the heartbeat."""
        if self._attr_available != available:
            self._attr_available = available
            self.async_write_ha_state()

    def _rgb(self) -> tuple[int, int, int]:
        hue, sat = self._attr_hs_color or (0.0, 0.0)
        value = (self._attr_brightness or 0) / 255 * 100
        return color_util.color_hsv_to_RGB(hue, sat, value)

    async def _push(self) -> None:
        try:
            await self._runtime.client.async_set_color(
                self._device, self._rgb() if self._attr_is_on else None
            )
        except ChromaError as err:
            raise HomeAssistantError(f"Razer Chroma: {err}") from err

    async def async_reapply(self) -> None:
        """Send the remembered state again (e.g. after a session reconnect)."""
        try:
            await self._push()
        except HomeAssistantError as err:
            _LOGGER.debug("Could not reapply %s: %s", self.entity_id, err)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if (hs := kwargs.get(ATTR_HS_COLOR)) is not None:
            self._attr_hs_color = hs
        if (bri := kwargs.get(ATTR_BRIGHTNESS)) is not None:
            self._attr_brightness = bri
        elif not self._attr_brightness:
            self._attr_brightness = 255
        self._attr_is_on = True
        await self._push()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        await self._push()
        self.async_write_ha_state()


# --------------------------------------------------------------------------
# Tuya (local)
# --------------------------------------------------------------------------


def _kelvin_to_fraction(kelvin: int) -> float:
    kelvin = max(TUYA_MIN_KELVIN, min(TUYA_MAX_KELVIN, kelvin))
    return (kelvin - TUYA_MIN_KELVIN) / (TUYA_MAX_KELVIN - TUYA_MIN_KELVIN)


def _fraction_to_kelvin(fraction: float) -> int:
    return int(round(TUYA_MIN_KELVIN + fraction * (TUYA_MAX_KELVIN - TUYA_MIN_KELVIN)))


class TuyaLightEntity(CoordinatorEntity[TuyaCoordinator], LightEntity):
    """A Tuya bulb / LED strip controlled over the local network."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_min_color_temp_kelvin = TUYA_MIN_KELVIN
    _attr_max_color_temp_kelvin = TUYA_MAX_KELVIN

    def __init__(self, entry: TuyaChromaConfigEntry, coordinator: TuyaCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entry.data[CONF_DEVICE_ID]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name=entry.title,
            manufacturer="Tuya",
            model="Local light",
        )
        state = coordinator.data
        modes: set[ColorMode] = set()
        if state.supports_colour:
            modes.add(ColorMode.HS)
            modes.add(ColorMode.COLOR_TEMP if state.supports_colour_temp else ColorMode.WHITE)
        elif state.supports_colour_temp:
            modes.add(ColorMode.COLOR_TEMP)
        elif state.supports_brightness:
            modes.add(ColorMode.BRIGHTNESS)
        else:
            modes.add(ColorMode.ONOFF)
        self._attr_supported_color_modes = modes

    @property
    def _in_colour_mode(self) -> bool:
        data = self.coordinator.data
        return ColorMode.HS in self._attr_supported_color_modes and data.mode == "colour"

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.is_on

    @property
    def color_mode(self) -> ColorMode:
        modes = self._attr_supported_color_modes
        if self._in_colour_mode:
            return ColorMode.HS
        for mode in (ColorMode.COLOR_TEMP, ColorMode.WHITE, ColorMode.BRIGHTNESS):
            if mode in modes:
                return mode
        return ColorMode.ONOFF

    @property
    def brightness(self) -> int | None:
        data = self.coordinator.data
        value = data.colour_value if self._in_colour_mode else data.brightness
        if value is None:
            return None
        return max(1, int(round(value * 255)))

    @property
    def hs_color(self) -> tuple[float, float] | None:
        return self.coordinator.data.hs_color

    @property
    def color_temp_kelvin(self) -> int | None:
        if (frac := self.coordinator.data.colour_temp) is None:
            return None
        return _fraction_to_kelvin(frac)

    async def _run(self, func, *args) -> None:
        device = self.coordinator.device

        def _command_then_read():
            func(*args)
            return device.get_state()

        try:
            state = await self.hass.async_add_executor_job(_command_then_read)
        except TuyaError as err:
            raise HomeAssistantError(f"Tuya {self.coordinator.config_entry.title}: {err}") from err
        # Publish the fresh state immediately (no debounced refresh), so that
        # mirrored lights follow without delay.
        self.coordinator.async_set_updated_data(state)

    async def async_turn_on(self, **kwargs: Any) -> None:
        device = self.coordinator.device
        data = self.coordinator.data
        bri = kwargs.get(ATTR_BRIGHTNESS)
        bri_frac = bri / 255 if bri is not None else None

        if (hs := kwargs.get(ATTR_HS_COLOR)) is not None:
            value = bri_frac if bri_frac is not None else (data.colour_value or 1.0)
            await self._run(device.set_hs, hs[0], hs[1], value)
        elif (kelvin := kwargs.get(ATTR_COLOR_TEMP_KELVIN)) is not None:
            value = bri_frac if bri_frac is not None else (data.brightness or 1.0)
            await self._run(device.set_white, value, _kelvin_to_fraction(kelvin))
        elif (white := kwargs.get(ATTR_WHITE)) is not None:
            await self._run(device.set_white, white / 255, None)
        elif bri_frac is not None and data.supports_brightness:
            if self._in_colour_mode and data.hs_color is not None:
                await self._run(device.set_hs, data.hs_color[0], data.hs_color[1], bri_frac)
            else:
                await self._run(device.set_white, bri_frac, None)
        else:
            await self._run(device.turn_on)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._run(self.coordinator.device.turn_off)
