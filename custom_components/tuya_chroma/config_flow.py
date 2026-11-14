"""Config flow for Tuya Chroma."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .chroma import ChromaClient, ChromaError
from .const import (
    CHROMA_DEVICES,
    CONF_CHROMA_DEVICES,
    CONF_DEVICE_ID,
    CONF_ENTRY_TYPE,
    CONF_FOLLOW_ENTITY,
    CONF_LOCAL_KEY,
    CONF_PROTOCOL_VERSION,
    DEFAULT_CHROMA_DEVICES,
    DEFAULT_CHROMA_PORT,
    DEFAULT_PROTOCOL_VERSION,
    DOMAIN,
    ENTRY_TYPE_CHROMA,
    ENTRY_TYPE_TUYA,
    PROTOCOL_VERSIONS,
)
from .tuya_device import TuyaError, TuyaLight

_LOGGER = logging.getLogger(__name__)


def _chroma_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "Razer Chroma")): str,
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_CHROMA_PORT)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(
                CONF_CHROMA_DEVICES,
                default=defaults.get(CONF_CHROMA_DEVICES, DEFAULT_CHROMA_DEVICES),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(CHROMA_DEVICES),
                    multiple=True,
                    translation_key="chroma_devices",
                )
            ),
        }
    )


def _tuya_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "Tuya light")): str,
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_DEVICE_ID, default=defaults.get(CONF_DEVICE_ID, "")): str,
            vol.Required(CONF_LOCAL_KEY, default=defaults.get(CONF_LOCAL_KEY, "")): str,
            vol.Required(
                CONF_PROTOCOL_VERSION,
                default=defaults.get(CONF_PROTOCOL_VERSION, DEFAULT_PROTOCOL_VERSION),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=PROTOCOL_VERSIONS,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class TuyaChromaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow: add a Razer Chroma PC or a local Tuya light."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["chroma", "tuya"])

    async def async_step_chroma(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            if not user_input[CONF_CHROMA_DEVICES]:
                errors[CONF_CHROMA_DEVICES] = "no_devices"
            else:
                await self.async_set_unique_id(f"chroma_{host}_{port}")
                self._abort_if_unique_id_configured()
                client = ChromaClient(
                    async_get_clientsession(self.hass), host, port, user_input[CONF_CHROMA_DEVICES]
                )
                try:
                    await client.async_probe()
                except ChromaError as err:
                    _LOGGER.debug("Chroma probe failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=user_input[CONF_NAME],
                        data={
                            CONF_ENTRY_TYPE: ENTRY_TYPE_CHROMA,
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_CHROMA_DEVICES: user_input[CONF_CHROMA_DEVICES],
                        },
                    )
        return self.async_show_form(
            step_id="chroma", data_schema=_chroma_schema(user_input or {}), errors=errors
        )

    async def async_step_tuya(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_ENTRY_TYPE: ENTRY_TYPE_TUYA,
                CONF_HOST: user_input[CONF_HOST].strip(),
                CONF_DEVICE_ID: user_input[CONF_DEVICE_ID].strip(),
                CONF_LOCAL_KEY: user_input[CONF_LOCAL_KEY].strip(),
                CONF_PROTOCOL_VERSION: user_input[CONF_PROTOCOL_VERSION],
            }
            await self.async_set_unique_id(data[CONF_DEVICE_ID])
            self._abort_if_unique_id_configured(updates=data)
            device = TuyaLight(
                data[CONF_DEVICE_ID], data[CONF_HOST], data[CONF_LOCAL_KEY], data[CONF_PROTOCOL_VERSION]
            )
            try:
                await self.hass.async_add_executor_job(device.get_state)
            except TuyaError as err:
                _LOGGER.debug("Tuya probe failed: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - tinytuya can raise anything
                _LOGGER.exception("Unexpected error talking to Tuya device")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=user_input[CONF_NAME], data=data)
        return self.async_show_form(
            step_id="tuya", data_schema=_tuya_schema(user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TuyaChromaOptionsFlow()


class TuyaChromaOptionsFlow(OptionsFlow):
    """Options: choose a light whose color this entry should mirror."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(CONF_FOLLOW_ENTITY)
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_FOLLOW_ENTITY,
                    description={"suggested_value": current} if current else None,
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain=LIGHT_DOMAIN)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
