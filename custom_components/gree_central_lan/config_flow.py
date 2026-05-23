"""Config flow for the Gree Central LAN integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, UnitOfTemperature
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, selector

from .const import (
    ATTR_TEMPERATURE_SENSOR,
    CONF_AREA_ID,
    CONF_AREA_IDS,
    CONF_DISPLAY_NAMES,
    CONF_SUBDEVICES,
    CONF_SYNC_INTERVAL_SECONDS,
    CONF_TEMPERATURE_SENSORS,
    DEFAULT_SYNC_INTERVAL_SECONDS,
    DEFAULT_PORT,
    DOMAIN,
)
from .models import BridgeInfo, SubDeviceInfo
from .protocol import GreeProtocolError, async_discover_bridges, async_probe_bridge

LOGGER = logging.getLogger(__name__)

STEP_BRIDGE = "bridge"
STEP_GENERAL = "general"
STEP_SENSOR = "sensor"
TEMPERATURE_UNITS = {
    UnitOfTemperature.CELSIUS,
    UnitOfTemperature.FAHRENHEIT,
    UnitOfTemperature.KELVIN,
}


def _is_temperature_sensor(entry: er.RegistryEntry) -> bool:
    """Return whether an entity registry entry represents a temperature sensor."""
    if entry.domain != "sensor" or entry.disabled_by is not None:
        return False

    device_class = entry.device_class or entry.original_device_class
    return (
        device_class == "temperature"
        or entry.unit_of_measurement in TEMPERATURE_UNITS
    )


def _temperature_sensor_entities_for_area(hass, area_id: str) -> list[str]:
    """Return temperature sensors assigned to a room, directly or via devices."""
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    entity_ids: set[str] = set()

    for entry in er.async_entries_for_area(entity_registry, area_id):
        if _is_temperature_sensor(entry):
            entity_ids.add(entry.entity_id)

    for device in dr.async_entries_for_area(device_registry, area_id):
        for entry in er.async_entries_for_device(entity_registry, device.id):
            if _is_temperature_sensor(entry):
                entity_ids.add(entry.entity_id)

    return sorted(
        entity_ids,
        key=lambda entity_id: (
            hass.states.get(entity_id).name
            if hass.states.get(entity_id) is not None
            else entity_id
        ),
    )


def _default_area_temperature_sensor(hass, area_id: str) -> str:
    """Return the area's configured temperature entity, if it is a sensor."""
    area = ar.async_get(hass).async_get_area(area_id)
    if area is None or not area.temperature_entity_id:
        return ""
    return (
        area.temperature_entity_id
        if area.temperature_entity_id.startswith("sensor.")
        else ""
    )


def _area_name(hass, area_id: str) -> str:
    """Return the display name for one area."""
    area = ar.async_get(hass).async_get_area(area_id)
    return area.name if area is not None else area_id


def _existing_area_id_for_subdevice(hass, subdevice_mac: str) -> str:
    """Look up the current room assignment for one indoor unit device."""
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, subdevice_mac)})
    return device.area_id if device and device.area_id else ""


class GreeCentralLanConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle guided UI setup for Gree central HVAC controllers."""

    VERSION = 1

    def __init__(self) -> None:
        self._bridge: BridgeInfo | None = None
        self._discovered_bridges: dict[str, BridgeInfo] = {}
        self._subdevices: list[SubDeviceInfo] = []
        self._unit_index = 0
        self._unit_settings: dict[str, dict[str, str]] = {}
        self._sync_interval_seconds = DEFAULT_SYNC_INTERVAL_SECONDS
        self._pending_area_id: str | None = None
        self._pending_name: str | None = None

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
        """Configure name and room for one indoor unit."""
        if self._bridge is None or self._unit_index >= len(self._subdevices):
            return self.async_abort(reason="cannot_connect")

        subdevice = self._subdevices[self._unit_index]
        defaults = self._existing_defaults(subdevice)

        if user_input is not None:
            self._pending_name = user_input[CONF_NAME].strip() or defaults[CONF_NAME]
            self._pending_area_id = str(user_input[CONF_AREA_ID])
            return await self.async_step_sensor()

        area_field: Any
        if defaults.get(CONF_AREA_ID):
            area_field = vol.Required(
                CONF_AREA_ID,
                default=defaults[CONF_AREA_ID],
            )
        else:
            area_field = vol.Required(CONF_AREA_ID)

        return self.async_show_form(
            step_id="unit",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
                    area_field: selector.AreaSelector(),
                }
            ),
            description_placeholders={
                "unit_name": defaults[CONF_NAME],
                "unit_mac": subdevice.mac,
            },
        )

    async def async_step_sensor(self, user_input: dict[str, Any] | None = None):
        """Configure the room thermometer for one indoor unit."""
        if self._bridge is None or self._unit_index >= len(self._subdevices):
            return self.async_abort(reason="cannot_connect")

        subdevice = self._subdevices[self._unit_index]
        defaults = self._existing_defaults(subdevice)
        area_id = self._pending_area_id or defaults.get(CONF_AREA_ID)
        if area_id is None:
            return await self.async_step_unit()

        if user_input is not None:
            self._unit_settings[subdevice.mac] = {
                CONF_NAME: self._pending_name or defaults[CONF_NAME],
                CONF_AREA_ID: area_id,
                ATTR_TEMPERATURE_SENSOR: user_input.get(ATTR_TEMPERATURE_SENSOR) or "",
            }
            self._pending_name = None
            self._pending_area_id = None
            self._unit_index += 1

            if self._unit_index >= len(self._subdevices):
                return self._create_config_entry()

            return await self.async_step_unit()

        sensor_candidates = _temperature_sensor_entities_for_area(self.hass, area_id)
        default_sensor = defaults.get(ATTR_TEMPERATURE_SENSOR, "")
        if default_sensor not in sensor_candidates:
            default_sensor = _default_area_temperature_sensor(self.hass, area_id)
            if default_sensor not in sensor_candidates:
                default_sensor = ""

        sensor_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(
                include_entities=sensor_candidates,
                multiple=False,
            )
        )
        schema_field = (
            vol.Optional(ATTR_TEMPERATURE_SENSOR, default=default_sensor)
            if default_sensor
            else vol.Optional(ATTR_TEMPERATURE_SENSOR)
        )
        area_name = _area_name(self.hass, area_id)

        return self.async_show_form(
            step_id=STEP_SENSOR,
            data_schema=vol.Schema({schema_field: sensor_selector}),
            description_placeholders={
                "unit_name": self._pending_name or defaults[CONF_NAME],
                "unit_mac": subdevice.mac,
                "area_name": area_name,
            },
        )

    def _existing_defaults(self, subdevice: SubDeviceInfo) -> dict[str, str]:
        """Resolve defaults from the controller, current options, and device registry."""
        name = self._existing_display_name(subdevice)
        sensor = self._existing_temperature_sensor(subdevice)
        area_id = self._existing_area_id(subdevice)
        if not name:
            name = f"Gree {subdevice.mac[-6:]}"
        return {
            CONF_NAME: name,
            CONF_AREA_ID: area_id or "",
            ATTR_TEMPERATURE_SENSOR: sensor,
        }

    def _existing_display_name(self, subdevice: SubDeviceInfo) -> str:
        """Return the current display name for a subdevice."""
        device = dr.async_get(self.hass).async_get_device(
            identifiers={(DOMAIN, subdevice.mac)}
        )
        if device is not None:
            if device.name_by_user:
                return device.name_by_user
            if device.name:
                return device.name
        return str(subdevice.name or f"Gree {subdevice.mac[-6:]}")

    def _existing_temperature_sensor(self, subdevice: SubDeviceInfo) -> str:
        """Return the currently configured room thermometer, if any."""
        return ""

    def _existing_area_id(self, subdevice: SubDeviceInfo) -> str:
        """Return the current area assignment, if any."""
        return _existing_area_id_for_subdevice(self.hass, subdevice.mac)

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
        area_ids = {
            mac: settings[CONF_AREA_ID]
            for mac, settings in self._unit_settings.items()
            if settings.get(CONF_AREA_ID)
        }

        return self.async_create_entry(
            title=self._bridge.name,
            data=self._bridge.as_entry_data(),
            options={
                CONF_AREA_IDS: area_ids,
                CONF_DISPLAY_NAMES: display_names,
                CONF_TEMPERATURE_SENSORS: temperature_sensors,
                CONF_SYNC_INTERVAL_SECONDS: self._sync_interval_seconds,
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
        self._sync_interval_seconds = int(
            config_entry.options.get(
                CONF_SYNC_INTERVAL_SECONDS,
                DEFAULT_SYNC_INTERVAL_SECONDS,
            )
        )
        self._pending_area_id: str | None = None
        self._pending_name: str | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Entry point for editing options."""
        return await self.async_step_general(user_input)

    async def async_step_general(self, user_input: dict[str, Any] | None = None):
        """Edit integration-wide options before unit-specific settings."""
        if user_input is not None:
            self._sync_interval_seconds = max(
                0,
                int(user_input[CONF_SYNC_INTERVAL_SECONDS]),
            )
            if not self._subdevices:
                return self.async_create_entry(
                    title="",
                    data={CONF_SYNC_INTERVAL_SECONDS: self._sync_interval_seconds},
                )
            return await self.async_step_unit()

        return self.async_show_form(
            step_id=STEP_GENERAL,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SYNC_INTERVAL_SECONDS,
                        default=self._sync_interval_seconds,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=3600,
                            step=5,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                }
            ),
        )

    async def async_step_unit(self, user_input: dict[str, Any] | None = None):
        """Edit the display name and room for one indoor unit."""
        subdevice = self._subdevices[self._unit_index]
        defaults = self._defaults_for(subdevice)

        if user_input is not None:
            self._pending_name = user_input[CONF_NAME].strip() or defaults[CONF_NAME]
            self._pending_area_id = str(user_input[CONF_AREA_ID])
            return await self.async_step_sensor()

        area_field: Any
        if defaults.get(CONF_AREA_ID):
            area_field = vol.Required(
                CONF_AREA_ID,
                default=defaults[CONF_AREA_ID],
            )
        else:
            area_field = vol.Required(CONF_AREA_ID)

        return self.async_show_form(
            step_id="unit",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
                    area_field: selector.AreaSelector(),
                }
            ),
            description_placeholders={
                "unit_name": defaults[CONF_NAME],
                "unit_mac": subdevice.mac,
            },
        )

    async def async_step_sensor(self, user_input: dict[str, Any] | None = None):
        """Edit the room thermometer for one indoor unit."""
        subdevice = self._subdevices[self._unit_index]
        defaults = self._defaults_for(subdevice)
        area_id = self._pending_area_id or defaults.get(CONF_AREA_ID)
        if area_id is None:
            return await self.async_step_unit()

        if user_input is not None:
            self._unit_settings[subdevice.mac] = {
                CONF_NAME: self._pending_name or defaults[CONF_NAME],
                CONF_AREA_ID: area_id,
                ATTR_TEMPERATURE_SENSOR: user_input.get(ATTR_TEMPERATURE_SENSOR) or "",
            }
            self._pending_name = None
            self._pending_area_id = None
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
                area_ids = {
                    mac: settings[CONF_AREA_ID]
                    for mac, settings in self._unit_settings.items()
                    if settings.get(CONF_AREA_ID)
                }
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_AREA_IDS: area_ids,
                        CONF_DISPLAY_NAMES: display_names,
                        CONF_TEMPERATURE_SENSORS: sensors,
                        CONF_SYNC_INTERVAL_SECONDS: self._sync_interval_seconds,
                    },
                )
            return await self.async_step_unit()

        sensor_candidates = _temperature_sensor_entities_for_area(self.hass, area_id)
        default_sensor = defaults.get(ATTR_TEMPERATURE_SENSOR, "")
        if default_sensor not in sensor_candidates:
            default_sensor = _default_area_temperature_sensor(self.hass, area_id)
            if default_sensor not in sensor_candidates:
                default_sensor = ""

        sensor_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(
                include_entities=sensor_candidates,
                multiple=False,
            )
        )
        schema_field = (
            vol.Optional(ATTR_TEMPERATURE_SENSOR, default=default_sensor)
            if default_sensor
            else vol.Optional(ATTR_TEMPERATURE_SENSOR)
        )

        return self.async_show_form(
            step_id=STEP_SENSOR,
            data_schema=vol.Schema({schema_field: sensor_selector}),
            description_placeholders={
                "unit_name": self._pending_name or defaults[CONF_NAME],
                "unit_mac": subdevice.mac,
                "area_name": _area_name(self.hass, area_id),
            },
        )

    def _defaults_for(self, subdevice: SubDeviceInfo) -> Mapping[str, str]:
        """Resolve the current option defaults for one indoor unit."""
        area_ids = self._config_entry.options.get(CONF_AREA_IDS, {})
        display_names = self._config_entry.options.get(CONF_DISPLAY_NAMES, {})
        sensors = self._config_entry.options.get(CONF_TEMPERATURE_SENSORS, {})

        return {
            CONF_NAME: str(display_names.get(subdevice.mac, subdevice.name)),
            CONF_AREA_ID: str(
                area_ids.get(
                    subdevice.mac,
                    _existing_area_id_for_subdevice(self.hass, subdevice.mac),
                )
            ),
            ATTR_TEMPERATURE_SENSOR: str(sensors.get(subdevice.mac, "")),
        }
