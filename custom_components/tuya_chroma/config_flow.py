"""Config flow for Tuya Chroma."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.config_entries import (
    SOURCE_IMPORT,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .chroma import ChromaClient, ChromaError
from .cloud import CloudDevice, TuyaCloudError, TuyaCloudLogin, devices_from_manager
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
    PROBE_VERSIONS,
    PROTOCOL_VERSIONS,
)
from .discovery import async_discover
from .tuya_device import TuyaError, TuyaLight, probe_version

_LOGGER = logging.getLogger(__name__)

CONF_USER_CODE = "user_code"
CONF_DEVICES = "devices"
CORE_TUYA_DOMAIN = "tuya"


def _version_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=PROTOCOL_VERSIONS, mode=selector.SelectSelectorMode.DROPDOWN
        )
    )


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
            ): _version_selector(),
        }
    )


def _qr_schema(data: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional("QR"): selector.QrCodeSelector(
                selector.QrCodeSelectorConfig(
                    data=data,
                    scale=5,
                    error_correction_level=selector.QrErrorCorrectionLevel.QUARTILE,
                )
            )
        }
    )


class TuyaChromaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Razer Chroma PC or Tuya lights (manually or from a Tuya account)."""

    VERSION = 1

    def __init__(self) -> None:
        self._login = TuyaCloudLogin()
        self._cloud_devices: dict[str, CloudDevice] = {}
        self._lan: dict[str, dict[str, Any]] = {}
        self._selected: list[str] = []

    # ------------------------------------------------------------------ menu

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = ["tuya_cloud", "tuya", "chroma"]
        if self._core_tuya_managers():
            options.insert(0, "tuya_core")
        return self.async_show_menu(step_id="user", menu_options=options)

    # ---------------------------------------------------------------- chroma

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

    # ----------------------------------------------------------- tuya manual

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

    # ------------------------------------------------- tuya account (QR code)

    async def async_step_tuya_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the user code shown in the Smart Life / Tuya Smart app."""
        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        if user_input is not None:
            try:
                await self.hass.async_add_executor_job(
                    self._login.request_qr, user_input[CONF_USER_CODE].strip()
                )
            except TuyaCloudError as err:
                errors["base"] = "login_error"
                placeholders["error"] = str(err)
            except Exception as err:  # noqa: BLE001 - network errors from requests
                _LOGGER.debug("QR request failed", exc_info=True)
                errors["base"] = "login_error"
                placeholders["error"] = str(err)
            else:
                return await self.async_step_tuya_scan()
        return self.async_show_form(
            step_id="tuya_cloud",
            data_schema=vol.Schema(
                {vol.Required(CONF_USER_CODE, default=(user_input or {}).get(CONF_USER_CODE, "")): str}
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_tuya_scan(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the QR code; on submit finish login and list devices."""
        if user_input is None:
            return self.async_show_form(
                step_id="tuya_scan",
                data_schema=_qr_schema(self._login.qr_data),
                description_placeholders={"error": ""},
            )
        try:
            devices = await self.hass.async_add_executor_job(self._login.fetch_devices)
        except Exception as err:  # noqa: BLE001 - TuyaCloudError or network errors
            _LOGGER.debug("Tuya login failed", exc_info=True)
            try:  # the old QR code is single-use; get a fresh one
                await self.hass.async_add_executor_job(self._login.request_qr, self._login.user_code)
            except Exception:  # noqa: BLE001
                return self.async_abort(reason="login_error")
            return self.async_show_form(
                step_id="tuya_scan",
                data_schema=_qr_schema(self._login.qr_data),
                errors={"base": "login_error"},
                description_placeholders={"error": str(err)},
            )
        return await self._async_got_devices(devices)

    # ------------------------------- import from HA's built-in Tuya integration

    def _core_tuya_managers(self) -> list[Any]:
        managers = []
        for entry in self.hass.config_entries.async_entries(CORE_TUYA_DOMAIN):
            if entry.state is not ConfigEntryState.LOADED:
                continue
            runtime = getattr(entry, "runtime_data", None)
            if (manager := getattr(runtime, "manager", None)) is not None:
                managers.append(manager)
        return managers

    async def async_step_tuya_core(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Take local keys from the already configured built-in Tuya integration."""
        devices: dict[str, CloudDevice] = {}
        for manager in self._core_tuya_managers():
            for dev in devices_from_manager(manager):
                devices[dev.id] = dev
        return await self._async_got_devices(list(devices.values()))

    # --------------------------------------------------- pick & create devices

    async def _async_got_devices(self, devices: list[CloudDevice]) -> ConfigFlowResult:
        configured = self._async_current_ids(include_ignore=False)
        self._cloud_devices = {d.id: d for d in devices if d.id not in configured}
        if not self._cloud_devices:
            return self.async_abort(reason="no_new_devices")
        self._lan = await async_discover(set(self._cloud_devices))
        return await self.async_step_tuya_pick()

    async def async_step_tuya_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Let the user choose which devices to add."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._selected = user_input[CONF_DEVICES]
            if not self._selected:
                errors[CONF_DEVICES] = "no_devices"
            elif any(d not in self._lan for d in self._selected):
                return await self.async_step_tuya_ips()
            else:
                return await self._async_create_selected({})

        options = []
        for dev in self._cloud_devices.values():
            lan = self._lan.get(dev.id)
            where = f"{lan['ip']}, v{lan['version']}" if lan else "IP ?"
            kind = dev.product_name or dev.category
            options.append(
                selector.SelectOptionDict(value=dev.id, label=f"{dev.name} ({kind}; {where})")
            )
        default = [d.id for d in self._cloud_devices.values() if d.is_light]
        return self.async_show_form(
            step_id="tuya_pick",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICES, default=default): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options, multiple=True)
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "total": str(len(self._cloud_devices)),
                "found": str(sum(1 for d in self._cloud_devices if d in self._lan)),
            },
        )

    def _ip_field(self, dev: CloudDevice) -> str:
        return f"{dev.name} [{dev.id[-6:]}]"

    async def async_step_tuya_ips(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for IP addresses that LAN discovery did not find."""
        missing = [self._cloud_devices[d] for d in self._selected if d not in self._lan]
        if user_input is not None:
            ips = {
                dev.id: user_input[self._ip_field(dev)].strip()
                for dev in missing
                if user_input.get(self._ip_field(dev), "").strip()
            }
            return await self._async_create_selected(ips)
        return self.async_show_form(
            step_id="tuya_ips",
            data_schema=vol.Schema({vol.Optional(self._ip_field(dev)): str for dev in missing}),
            description_placeholders={"count": str(len(missing))},
        )

    async def _async_create_selected(self, manual_ips: dict[str, str]) -> ConfigFlowResult:
        entries: list[dict[str, Any]] = []
        for dev_id in self._selected:
            dev = self._cloud_devices[dev_id]
            lan = self._lan.get(dev_id)
            host = lan["ip"] if lan else manual_ips.get(dev_id)
            if not host:
                _LOGGER.warning("Skipping %s: no IP address", dev.name)
                continue
            entries.append(
                {
                    CONF_NAME: dev.name,
                    CONF_ENTRY_TYPE: ENTRY_TYPE_TUYA,
                    CONF_HOST: host,
                    CONF_DEVICE_ID: dev.id,
                    CONF_LOCAL_KEY: dev.local_key,
                    CONF_PROTOCOL_VERSION: (lan or {}).get("version") or "",
                }
            )
        if not entries:
            return self.async_abort(reason="no_devices_added")

        # One flow can create one entry; the others go through the import step.
        first, *rest = entries
        for data in rest:
            self.hass.async_create_task(
                self.hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": SOURCE_IMPORT}, data=data
                )
            )
        return await self.async_step_import(first)

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Create a Tuya entry from account data (version is probed if unknown)."""
        data = dict(import_data)
        name = data.pop(CONF_NAME)
        await self.async_set_unique_id(data[CONF_DEVICE_ID])
        self._abort_if_unique_id_configured(updates=data)

        versions = [data[CONF_PROTOCOL_VERSION]] if data.get(CONF_PROTOCOL_VERSION) else []
        versions += [v for v in PROBE_VERSIONS if v not in versions]
        working = await self.hass.async_add_executor_job(
            probe_version, data[CONF_DEVICE_ID], data[CONF_HOST], data[CONF_LOCAL_KEY], versions
        )
        if working is None:
            _LOGGER.warning(
                "Could not connect to %s at %s; adding it anyway, fix IP/version in its options",
                name,
                data[CONF_HOST],
            )
        data[CONF_PROTOCOL_VERSION] = working or versions[0]
        return self.async_create_entry(title=name, data=data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TuyaChromaOptionsFlow()


class TuyaChromaOptionsFlow(OptionsFlow):
    """Options: light to mirror; for Tuya also IP / key / protocol version."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        is_tuya = entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_TUYA
        if user_input is not None:
            if is_tuya:
                new_data = {
                    **entry.data,
                    CONF_HOST: user_input.pop(CONF_HOST).strip(),
                    CONF_LOCAL_KEY: user_input.pop(CONF_LOCAL_KEY).strip(),
                    CONF_PROTOCOL_VERSION: user_input.pop(CONF_PROTOCOL_VERSION),
                }
                if new_data != dict(entry.data):
                    self.hass.config_entries.async_update_entry(entry, data=new_data)
            return self.async_create_entry(data=user_input)

        current = entry.options.get(CONF_FOLLOW_ENTITY)
        fields: dict[Any, Any] = {}
        if is_tuya:
            fields[vol.Required(CONF_HOST, default=entry.data[CONF_HOST])] = str
            fields[vol.Required(CONF_LOCAL_KEY, default=entry.data[CONF_LOCAL_KEY])] = str
            fields[
                vol.Required(CONF_PROTOCOL_VERSION, default=entry.data[CONF_PROTOCOL_VERSION])
            ] = _version_selector()
        fields[
            vol.Optional(
                CONF_FOLLOW_ENTITY,
                description={"suggested_value": current} if current else None,
            )
        ] = selector.EntitySelector(selector.EntitySelectorConfig(domain=LIGHT_DOMAIN))
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields))
