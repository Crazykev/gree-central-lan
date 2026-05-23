"""Constants for the Gree Central LAN integration."""

from homeassistant.components.climate import HVACMode

DOMAIN = "gree_central_lan"

DEFAULT_PORT = 7000
DEFAULT_TIMEOUT = 5.0
DEFAULT_DISCOVERY_TIMEOUT = 1.5
DEFAULT_MAIN_KEY = "a3K8Bx%2r8Y7#xDh"

CONF_MAIN_MAC = "main_mac"
CONF_MAIN_NAME = "main_name"
CONF_BRAND = "brand"
CONF_MODEL = "model"
CONF_VERSION = "version"
CONF_SUBDEVICES = "subdevices"
CONF_DISPLAY_NAMES = "display_names"
CONF_TEMPERATURE_SENSORS = "temperature_sensors"
CONF_SYNC_INTERVAL_SECONDS = "sync_interval_seconds"

ATTR_TEMPERATURE_SENSOR = "temperature_sensor"

MIN_TEMP = 16
MAX_TEMP = 30
TARGET_TEMP_STEP = 1
DEFAULT_SYNC_INTERVAL_SECONDS = 0

FAN_AUTO = "auto"
FAN_LOW = "low"
FAN_MEDIUM_LOW = "medium_low"
FAN_MEDIUM = "medium"
FAN_MEDIUM_HIGH = "medium_high"
FAN_HIGH = "high"

FAN_MODES = [
    FAN_AUTO,
    FAN_LOW,
    FAN_MEDIUM_LOW,
    FAN_MEDIUM,
    FAN_MEDIUM_HIGH,
    FAN_HIGH,
]

DEVICE_TO_HVAC = {
    0: HVACMode.AUTO,
    1: HVACMode.COOL,
    2: HVACMode.DRY,
    3: HVACMode.FAN_ONLY,
    4: HVACMode.HEAT,
}
HVAC_TO_DEVICE = {value: key for key, value in DEVICE_TO_HVAC.items()}

STATUS_COLUMNS = ("Pow", "Mod", "SetTem", "WdSpd")
