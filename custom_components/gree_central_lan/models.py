"""Data models for the Gree Central LAN integration."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SubDeviceInfo:
    """A single indoor unit hanging off the Gree central controller."""

    mac: str
    mid: str
    name: str

    @classmethod
    def from_dict(cls, data: dict) -> "SubDeviceInfo":
        """Create a sub-device from stored config data."""
        return cls(
            mac=str(data["mac"]),
            mid=str(data.get("mid", "")),
            name=str(data.get("name", data["mac"])),
        )

    def as_dict(self) -> dict[str, str]:
        """Serialize the object into config-entry-safe data."""
        return {
            "mac": self.mac,
            "mid": self.mid,
            "name": self.name,
        }


@dataclass(slots=True)
class BridgeInfo:
    """Metadata for the main Gree central controller."""

    host: str
    port: int
    mac: str
    name: str
    brand: str
    model: str
    version: str
    subdevices: tuple[SubDeviceInfo, ...] = ()

    def as_entry_data(self) -> dict:
        """Serialize the controller into config entry data."""
        return {
            "host": self.host,
            "port": self.port,
            "main_mac": self.mac,
            "main_name": self.name,
            "brand": self.brand,
            "model": self.model,
            "version": self.version,
            "subdevices": [subdevice.as_dict() for subdevice in self.subdevices],
        }


@dataclass(slots=True)
class ClimateState:
    """Current state of a single indoor unit."""

    power: int | None = None
    mode: int | None = None
    target_temperature: int | None = None
    fan_speed: int | None = None
    raw: dict[str, int] = field(default_factory=dict)

    def apply_columns(self, columns: list[str], values: list[int]) -> None:
        """Merge a state update packet into the stored state."""
        for key, value in zip(columns, values, strict=False):
            self.raw[key] = int(value)

        if "Pow" in self.raw:
            self.power = self.raw["Pow"]
        if "Mod" in self.raw:
            self.mode = self.raw["Mod"]
        if "SetTem" in self.raw:
            self.target_temperature = self.raw["SetTem"]
        if "WdSpd" in self.raw:
            self.fan_speed = self.raw["WdSpd"]

