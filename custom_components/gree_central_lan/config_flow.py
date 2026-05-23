"""Config flow for the Gree Central LAN integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.helpers import selector

from .const import (
    ATTR_TEMPERATURE_SENSOR,
    CONF_DISPLAY_NAMES,
    CONF_SUBDEVICES,
    CONF_TEMPERATURE_SENSORS,
    DEFAULT_PORT,
    DOMAIN,
)
from .models import BridgeInfo, SubDeviceInfo
from .protocol import GreeProtocolError, async_discover_bridges, async_probe_bridge

LOGGER = logging.getLogger(__name__)

STEP_BRIDGE = "bridge"


class GreeCentralLanConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle guided UI setup for Gree central HVAC controllers."""

    VERSION = 1

    def __init__(self) -> None:
        self._bridge: BridgeInfo | None = None
        self._discovered_bridges: dict[str, BridgeInfo] = {}
        self._subdevices: list[SubDeviceInfo] = []
        self._unit_index = 0
        self._unit_settings: dict[str, dict[str, str]] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Discover controllers on the current LAN and let the user pick one."""
        if user_input is not None:
            bridge_key = user_input[STEP_BRIDGE]
            if bridge_key == "manual":
                return await self.async_step_manual()

            self._bridge = self._discovered_bridges[bridge_key]
            await self.async_set_unique_id(self._bridge.mac)
            self._abort_if_unique_id_configured()

            self._subdevices = list(self._bridge.subdevices)
            self._unit_index = 0
            self._unit_settings.clear()
            return await self.async_step_unit()

        bridges = await async_discover_bridges(self.hass, DEFAULT_PORT)
        self._discovered_bridges = {
            f"{bridge.mac}@{bridge.host}": bridge for bridge in bridges
        }

        if not self._discovered_bridges:
            return await self.async_step_manual()

        options = {
            key: f"{bridge.name} ({bridge.host}) - {len(bridge.subdevices)} indoor units"
            for key, bridge in self._discovered_bridges.items()
        }
        options["manual"] = "Manual IP entry"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(STEP_BRIDGE): vol.In(options)}),
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None):
        """Set up a controller by directly probing a known IP."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                bridge = await async_probe_bridge(user_input[CONF_HOST], user_input[CONF_PORT])
            except GreeProtocolError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(bridge.mac)
                self._abort_if_unique_id_configured()

                self._bridge = bridge
                self._subdevices = list(bridge.subdevices)
                self._unit_index = 0
                self._unit_settings.clear()
                return await self.async_step_unit()

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                }
            ),
            errors=errors,
        )

    async def async_step_unit(self, user_input: dict[str, Any] | None = None):
        """Configure one indoor unit at a time."""
        if self._bridge is None or self._unit_index >= len(self._subdevices):
            return self.async_abort(reason="cannot_connect")

        subdevice = self._subdevices[self._unit_index]
        defaults = self._existing_defaults(subdevice)

        if user_input is not None:
            chosen_sensor = user_input.get(ATTR_TEMPERATURE_SENSOR)
            unit_name = user_input[CONF_NAME].strip() or defaults[CONF_NAME]

            self._unit_settings[subdevice.mac] = {
                CONF_NAME: unit_name,
                ATTR_TEMPERATURE_SENSOR: chosen_sensor or "",
            }
            self._unit_index += 1

            if self._unit_index >= len(self._subdevices):
                return self._create_config_entry()

            return await self.async_step_unit()

        schema_fields: dict[Any, Any] = {
            vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
        }

        sensor_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        )
        if defaults[ATTR_TEMPERATURE_SENSOR]:
            schema_fields[
                vol.Optional(
                    ATTR_TEMPERATURE_SENSOR,
                    default=defaults[ATTR_TEMPERATURE_SENSOR],
                )
            ] = sensor_selector
        else:
            schema_fields[vol.Optional(ATTR_TEMPERATURE_SENSOR)] = sensor_selector

        return self.async_show_form(
            step_id="unit",
            data_schema=vol.Schema(schema_fields),
            description_placeholders={
                "unit_name": defaults[CONF_NAME],
                "unit_mac": subdevice.mac,
            },
        )

    def _existing_defaults(self, subdevice: SubDeviceInfo) -> dict[str, str]:
        """Use the old gree2 entity as a migration hint when present."""
        state = self.hass.states.get(f"climate.gree2_{subdevice.mac}")
        name = state.name if state is not None else subdevice.name
        sensor = ""
        if state is not None:
            sensor = str(state.attributes.get(ATTR_TEMPERATURE_SENSOR, ""))
        if not name:
            name = f"Gree {subdevice.mac[-6:]}"
        return {
            CONF_NAME: name,
            ATTR_TEMPERATURE_SENSOR: sensor,
        }

    def _create_config_entry(self):
        """Finish the flow and store unit-specific UI choices in options."""
        if self._bridge is None:
            return self.async_abort(reason="cannot_connect")

        display_names = {
            mac: settings[CONF_NAME]
            for mac, settings in self._unit_settings.items()
        }
        temperature_sensors = {
            mac: settings[ATTR_TEMPERATURE_SENSOR]
            for mac, settings in self._unit_settings.items()
            if settings[ATTR_TEMPERATURE_SENSOR]
        }

        return self.async_create_entry(
            title=self._bridge.name,
            data=self._bridge.as_entry_data(),
            options={
                CONF_DISPLAY_NAMES: display_names,
                CONF_TEMPERATURE_SENSORS: temperature_sensors,
            },
        )

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Return the options flow handler."""
        return GreeCentralLanOptionsFlow(config_entry)


class GreeCentralLanOptionsFlow(config_entries.OptionsFlowWithReload):
    """Edit per-unit display names and external thermometers."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._subdevices = [
            SubDeviceInfo.from_dict(item)
            for item in config_entry.data.get(CONF_SUBDEVICES, [])
        ]
        self._unit_index = 0
        self._unit_settings: dict[str, dict[str, str]] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Start the sequential unit editor."""
        if not self._subdevices:
            return self.async_create_entry(title="", data={})
        return await self.async_step_unit()

    async def async_step_unit(self, user_input: dict[str, Any] | None = None):
        """Edit one indoor unit at a time."""
        subdevice = self._subdevices[self._unit_index]
        defaults = self._defaults_for(subdevice)

        if user_input is not None:
            self._unit_settings[subdevice.mac] = {
                CONF_NAME: user_input[CONF_NAME].strip() or defaults[CONF_NAME],
                ATTR_TEMPERATURE_SENSOR: user_input.get(ATTR_TEMPERATURE_SENSOR) or "",
            }
            self._unit_index += 1
            if self._unit_index >= len(self._subdevices):
                display_names = {
                    mac: settings[CONF_NAME]
                    for mac, settings in self._unit_settings.items()
                }
                sensors = {
                    mac: settings[ATTR_TEMPERATURE_SENSOR]
                    for mac, settings in self._unit_settings.items()
                    if settings[ATTR_TEMPERATURE_SENSOR]
                }
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_DISPLAY_NAMES: display_names,
                        CONF_TEMPERATURE_SENSORS: sensors,
                    },
                )
            return await self.async_step_unit()

        schema_fields: dict[Any, Any] = {
            vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
        }
        sensor_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        )
        if defaults[ATTR_TEMPERATURE_SENSOR]:
            schema_fields[
                vol.Optional(
                    ATTR_TEMPERATURE_SENSOR,
                    default=defaults[ATTR_TEMPERATURE_SENSOR],
                )
            ] = sensor_selector
        else:
            schema_fields[vol.Optional(ATTR_TEMPERATURE_SENSOR)] = sensor_selector

        return self.async_show_form(
            step_id="unit",
            data_schema=vol.Schema(schema_fields),
            description_placeholders={
                "unit_name": defaults[CONF_NAME],
                "unit_mac": subdevice.mac,
            },
        )

    def _defaults_for(self, subdevice: SubDeviceInfo) -> Mapping[str, str]:
        """Resolve the current option defaults for one indoor unit."""
        display_names = self._config_entry.options.get(CONF_DISPLAY_NAMES, {})
        sensors = self._config_entry.options.get(CONF_TEMPERATURE_SENSORS, {})

        return {
            CONF_NAME: str(display_names.get(subdevice.mac, subdevice.name)),
            ATTR_TEMPERATURE_SENSOR: str(sensors.get(subdevice.mac, "")),
        }
