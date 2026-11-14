"""Constants for the Tuya Chroma integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "tuya_chroma"

# Config entry types
CONF_ENTRY_TYPE: Final = "entry_type"
ENTRY_TYPE_CHROMA: Final = "chroma"
ENTRY_TYPE_TUYA: Final = "tuya"

# Razer Chroma
CONF_CHROMA_DEVICES: Final = "chroma_devices"
DEFAULT_CHROMA_PORT: Final = 54235
CHROMA_HEARTBEAT_INTERVAL: Final = timedelta(seconds=10)
CHROMA_DEVICES: Final = {
    "keyboard": "Keyboard",
    "mouse": "Mouse",
    "mousepad": "Mousepad",
    "headset": "Headset",
    "keypad": "Keypad",
    "chromalink": "Chroma Link",
}
DEFAULT_CHROMA_DEVICES: Final = list(CHROMA_DEVICES)

# Tuya (local)
CONF_DEVICE_ID: Final = "device_id"
CONF_LOCAL_KEY: Final = "local_key"
CONF_PROTOCOL_VERSION: Final = "protocol_version"
PROTOCOL_VERSIONS: Final = ["3.1", "3.2", "3.3", "3.4", "3.5"]
# Order in which versions are tried when the version is unknown.
PROBE_VERSIONS: Final = ["3.3", "3.4", "3.5", "3.1"]
DEFAULT_PROTOCOL_VERSION: Final = "3.3"
TUYA_SCAN_INTERVAL: Final = timedelta(seconds=15)
TUYA_MIN_KELVIN: Final = 2700
TUYA_MAX_KELVIN: Final = 6500

# Options: mirror another light entity onto this entry's lights
CONF_FOLLOW_ENTITY: Final = "follow_entity"
