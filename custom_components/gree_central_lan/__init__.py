"""Home Assistant entry points for the Gree Central LAN integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DEFAULT_SYNC_INTERVAL_SECONDS, DOMAIN, CONF_SYNC_INTERVAL_SECONDS
from .protocol import GreeCentralClient, GreeProtocolError

LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE]
GreeCentralConfigEntry = ConfigEntry


@dataclass(slots=True)
class GreeCentralRuntimeData:
    """Runtime objects attached to a config entry."""

    client: GreeCentralClient


async def async_setup_entry(hass: HomeAssistant, entry: GreeCentralConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    client = GreeCentralClient(
        hass,
        entry.data,
        sync_interval_seconds=int(
            entry.options.get(CONF_SYNC_INTERVAL_SECONDS, DEFAULT_SYNC_INTERVAL_SECONDS)
        ),
    )
    try:
        bridge = await client.async_setup()
    except GreeProtocolError as err:
        await client.async_shutdown()
        raise ConfigEntryNotReady(str(err)) from err

    try:
        device_registry = dr.async_get(hass)
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, bridge.mac)},
            manufacturer=bridge.brand,
            model=bridge.model,
            name=bridge.name,
            sw_version=bridge.version or None,
        )

        new_data = bridge.as_entry_data()
        if entry.data != new_data:
            hass.config_entries.async_update_entry(entry, data=new_data)

        entry.runtime_data = GreeCentralRuntimeData(client=client)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await client.async_shutdown()
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: GreeCentralConfigEntry) -> bool:
    """Unload the integration and close its socket."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.client.async_shutdown()
    return unload_ok
