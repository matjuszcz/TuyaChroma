"""Mirror another light entity (e.g. a Tuya bulb) onto this entry's lights."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    DOMAIN as LIGHT_DOMAIN,
    ColorMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .const import CONF_FOLLOW_ENTITY

_LOGGER = logging.getLogger(__name__)


def build_service_call(state: State) -> tuple[str, dict[str, Any]] | None:
    """Translate a source light state into a light service call."""
    if state.state == STATE_OFF:
        return SERVICE_TURN_OFF, {}
    if state.state != STATE_ON:
        return None  # unavailable / unknown: leave targets alone

    data: dict[str, Any] = {}
    attrs = state.attributes
    if (brightness := attrs.get(ATTR_BRIGHTNESS)) is not None:
        data[ATTR_BRIGHTNESS] = brightness
    color_mode = attrs.get(ATTR_COLOR_MODE)
    if color_mode == ColorMode.COLOR_TEMP and attrs.get(ATTR_COLOR_TEMP_KELVIN):
        data[ATTR_COLOR_TEMP_KELVIN] = attrs[ATTR_COLOR_TEMP_KELVIN]
    elif attrs.get(ATTR_HS_COLOR) is not None:
        data[ATTR_HS_COLOR] = attrs[ATTR_HS_COLOR]
    return SERVICE_TURN_ON, data


@callback
def async_setup_follow(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Start mirroring the configured source light, if any."""
    source = entry.options.get(CONF_FOLLOW_ENTITY)
    if not source:
        return

    def _targets() -> list[str]:
        registry = er.async_get(hass)
        return [
            ent.entity_id
            for ent in er.async_entries_for_config_entry(registry, entry.entry_id)
            if ent.domain == LIGHT_DOMAIN and ent.entity_id != source and not ent.disabled
        ]

    async def _apply(state: State | None) -> None:
        if state is None or (call := build_service_call(state)) is None:
            return
        if not (targets := _targets()):
            return
        service, data = call
        _LOGGER.debug("Mirroring %s -> %s: %s %s", source, targets, service, data)
        await hass.services.async_call(
            LIGHT_DOMAIN,
            service,
            {ATTR_ENTITY_ID: targets, **data},
            blocking=False,
        )

    async def _on_change(event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if new is None:
            return
        if old is not None and old.state == new.state and old.attributes == new.attributes:
            return
        await _apply(new)

    entry.async_on_unload(async_track_state_change_event(hass, [source], _on_change))
    # Apply the current state right away once the entities exist.
    entry.async_create_background_task(
        hass, _apply(hass.states.get(source)), f"tuya_chroma follow {source}"
    )
