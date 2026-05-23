"""Home Assistant entry points for the Gree Central LAN integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .protocol import GreeCentralClient

LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE]
GreeCentralConfigEntry = ConfigEntry


@dataclass(slots=True)
class GreeCentralRuntimeData:
    """Runtime objects attached to a config entry."""

    client: GreeCentralClient


async def async_setup_entry(hass: HomeAssistant, entry: GreeCentralConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    client = GreeCentralClient(hass, entry.data)
    bridge = await client.async_setup()

    new_data = bridge.as_entry_data()
    if entry.data != new_data:
        hass.config_entries.async_update_entry(entry, data=new_data)

    entry.runtime_data = GreeCentralRuntimeData(client=client)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GreeCentralConfigEntry) -> bool:
    """Unload the integration and close its socket."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.client.async_shutdown()
    return unload_ok

