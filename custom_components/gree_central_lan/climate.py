"""Climate entities for the Gree Central LAN integration."""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, CONF_NAME, UnitOfTemperature
from homeassistant.core import Event, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_conversion import TemperatureConverter

from . import GreeCentralConfigEntry
from .const import (
    ATTR_TEMPERATURE_SENSOR,
    CONF_DISPLAY_NAMES,
    CONF_TEMPERATURE_SENSORS,
    DEVICE_TO_HVAC,
    DOMAIN,
    FAN_MODES,
    HVAC_TO_DEVICE,
    MAX_TEMP,
    MIN_TEMP,
    TARGET_TEMP_STEP,
)
from .models import ClimateState, SubDeviceInfo
from .protocol import GreeCentralClient

LOGGER = logging.getLogger(__name__)

FAN_MODE_TO_DEVICE = {mode: index for index, mode in enumerate(FAN_MODES)}


async def async_setup_entry(
    hass,
    entry: GreeCentralConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the climate platform for one controller config entry."""
    client: GreeCentralClient = entry.runtime_data.client
    display_names = entry.options.get(CONF_DISPLAY_NAMES, {})
    sensors = entry.options.get(CONF_TEMPERATURE_SENSORS, {})

    entities = [
        GreeCentralClimateEntity(
            client=client,
            subdevice=SubDeviceInfo.from_dict(subdevice),
            display_name=str(display_names.get(subdevice["mac"], subdevice["name"])),
            temperature_sensor=sensors.get(subdevice["mac"]),
        )
        for subdevice in entry.data.get("subdevices", [])
    ]
    async_add_entities(entities)


class GreeCentralClimateEntity(ClimateEntity):
    """A single indoor unit exposed as a Home Assistant climate entity."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_target_temperature_step = TARGET_TEMP_STEP
    _attr_hvac_modes = [*HVAC_TO_DEVICE.keys(), HVACMode.OFF]
    _attr_fan_modes = FAN_MODES
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP

    def __init__(
        self,
        *,
        client: GreeCentralClient,
        subdevice: SubDeviceInfo,
        display_name: str,
        temperature_sensor: str | None,
    ) -> None:
        self._client = client
        self._subdevice = subdevice
        self._display_name = display_name
        self._temperature_sensor = temperature_sensor or None
        self._sensor_temperature: float | None = None
        self._unsubscribe_state_listener: Callable[[], None] | None = None
        self._unsubscribe_sensor_listener: Callable[[], None] | None = None

        self._attr_unique_id = subdevice.mac
        bridge = client.bridge
        manufacturer = bridge.brand if bridge is not None else "Gree"
        model = bridge.model if bridge is not None else "gree"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._subdevice.mac)},
            name=self._display_name,
            manufacturer=manufacturer,
            model=f"{model} indoor unit",
            suggested_area=None,
            via_device=(DOMAIN, client.main_mac),
        )

    async def async_added_to_hass(self) -> None:
        """Register listeners after Home Assistant adds the entity."""
        self._unsubscribe_state_listener = self._client.async_add_listener(
            self._handle_client_update
        )

        if self._temperature_sensor:
            self._unsubscribe_sensor_listener = async_track_state_change_event(
                self.hass,
                [self._temperature_sensor],
                self._async_sensor_changed,
            )
            initial_state = self.hass.states.get(self._temperature_sensor)
            if initial_state is not None:
                self._update_sensor_temperature(initial_state.state, initial_state.attributes.get("unit_of_measurement"))

    async def async_will_remove_from_hass(self) -> None:
        """Remove callbacks."""
        if self._unsubscribe_state_listener is not None:
            self._unsubscribe_state_listener()
            self._unsubscribe_state_listener = None
        if self._unsubscribe_sensor_listener is not None:
            self._unsubscribe_sensor_listener()
            self._unsubscribe_sensor_listener = None

    @property
    def available(self) -> bool:
        """Return whether the controller is reachable and the unit has state."""
        return self._client.available and self._state is not None and self._state.power is not None

    @property
    def current_temperature(self) -> float | None:
        """Return the linked external room temperature, if configured."""
        return self._sensor_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the setpoint currently cached for the indoor unit."""
        return self._state.target_temperature if self._state else None

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode."""
        if self._state is None or self._state.power == 0:
            return HVACMode.OFF
        return DEVICE_TO_HVAC.get(self._state.mode, HVACMode.COOL)

    @property
    def hvac_action(self) -> HVACAction:
        """Surface a best-effort action based on the selected mode."""
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if self.hvac_mode == HVACMode.COOL:
            return HVACAction.COOLING
        if self.hvac_mode == HVACMode.HEAT:
            return HVACAction.HEATING
        if self.hvac_mode == HVACMode.DRY:
            return HVACAction.DRYING
        if self.hvac_mode == HVACMode.FAN_ONLY:
            return HVACAction.FAN
        return HVACAction.IDLE

    @property
    def fan_mode(self) -> str | None:
        """Return the current fan speed."""
        if self._state is None or self._state.fan_speed is None:
            return None
        if 0 <= self._state.fan_speed < len(FAN_MODES):
            return FAN_MODES[self._state.fan_speed]
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the linked thermometer for the HA climate card."""
        return {
            ATTR_TEMPERATURE_SENSOR: self._temperature_sensor,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set a new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        updates: dict[str, int] = {"SetTem": int(round(float(temperature)))}
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        if hvac_mode is not None and hvac_mode != HVACMode.OFF:
            updates["Pow"] = 1
            updates["Mod"] = HVAC_TO_DEVICE[hvac_mode]
        elif hvac_mode == HVACMode.OFF:
            await self.async_set_hvac_mode(HVACMode.OFF)
            return

        await self._client.async_send_command(self._subdevice.mac, updates)
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set a new fan speed."""
        if fan_mode not in FAN_MODE_TO_DEVICE:
            raise ValueError(f"Unsupported fan mode {fan_mode}")
        await self._client.async_send_command(
            self._subdevice.mac,
            {"WdSpd": FAN_MODE_TO_DEVICE[fan_mode]},
        )
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set a new HVAC mode."""
        if hvac_mode == HVACMode.OFF:
            await self._client.async_send_command(self._subdevice.mac, {"Pow": 0})
            self.async_write_ha_state()
            return

        if hvac_mode not in HVAC_TO_DEVICE:
            raise ValueError(f"Unsupported HVAC mode {hvac_mode}")

        await self._client.async_send_command(
            self._subdevice.mac,
            {
                "Pow": 1,
                "Mod": HVAC_TO_DEVICE[hvac_mode],
            },
        )
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Turn on the indoor unit."""
        await self._client.async_send_command(self._subdevice.mac, {"Pow": 1})
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn off the indoor unit."""
        await self._client.async_send_command(self._subdevice.mac, {"Pow": 0})
        self.async_write_ha_state()

    @callback
    def _handle_client_update(self, subdevice_mac: str) -> None:
        """Write state when the shared UDP client receives a push update."""
        if subdevice_mac != self._subdevice.mac:
            return
        self.async_write_ha_state()

    @callback
    def _update_sensor_temperature(self, value: str, source_unit: str | None) -> None:
        """Parse and convert the linked room thermometer."""
        if value in {"unknown", "unavailable", ""}:
            self._sensor_temperature = None
            return

        try:
            numeric = float(value)
        except ValueError:
            self._sensor_temperature = None
            return

        if source_unit in {UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT}:
            self._sensor_temperature = TemperatureConverter.convert(
                numeric,
                source_unit,
                self.temperature_unit,
            )
            return

        self._sensor_temperature = numeric

    @callback
    def _async_sensor_changed(self, event: Event[Any]) -> None:
        """Push linked sensor changes straight into the climate entity."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        self._update_sensor_temperature(
            new_state.state,
            new_state.attributes.get("unit_of_measurement"),
        )
        self.async_write_ha_state()

    @property
    def _state(self) -> ClimateState | None:
        return self._client.get_state(self._subdevice.mac)
