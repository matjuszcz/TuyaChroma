"""Send effects to every Razer Chroma device through the official Chroma SDK.

Uses RzChromaSDK64.dll (installed with Razer Synapse). Effects are sent as
"custom" per-LED frames to all six SDK device categories, so every Chroma
device (keyboards, laptops, mice, mousepads/docks, headsets, keypads,
controllers, speakers and other Chroma Link devices) shows them.
Closing the SDK (UnInit) hands the devices back to the Synapse profile.
"""

from __future__ import annotations

import ctypes as C
import logging
import sys

from .razer import DEVICES, RGB, RazerUnavailable

_LOGGER = logging.getLogger(__name__)

# device -> (DLL function, "custom" effect type for that device, LED count)
# Effect type numbers come from Razer's RzChromaSDKTypes.h.
CUSTOM_EFFECTS = {
    "keyboard": ("CreateKeyboardEffect", 2, 6 * 22),     # Keyboard::CHROMA_CUSTOM
    "mouse": ("CreateMouseEffect", 8, 9 * 7),            # Mouse::CHROMA_CUSTOM2
    "mousepad": ("CreateMousepadEffect", 2, 15),         # Mousepad::CHROMA_CUSTOM
    "headset": ("CreateHeadsetEffect", 4, 5),            # Headset::CHROMA_CUSTOM
    "keypad": ("CreateKeypadEffect", 2, 4 * 5),          # Keypad::CHROMA_CUSTOM
    "chromalink": ("CreateChromaLinkEffect", 1, 5),      # ChromaLink::CHROMA_CUSTOM
}
# Extended layouts of newer devices (Razer's Keyboard::v2 / Mousepad::v2):
# keyboards with extra LEDs (underglow, side strips) use an 8x24 grid plus the
# 6x22 key grid; mousepads/docks with more zones use 20 LEDs.
EXTENDED_EFFECTS = {
    "keyboard": ("CreateKeyboardEffect", 9, "keyboard_ext", 8 * 24),  # Keyboard::CHROMA_CUSTOM2
    "mousepad": ("CreateMousepadEffect", 6, "mousepad_ext", 20),      # Mousepad::CHROMA_CUSTOM2
}
EXTENDED_SIZES = {"keyboard_ext": (24, 8), "mousepad_ext": (20, 1)}
KEY_OVERRIDE = 0x01000000  # Key[] entries with this flag override the color grid
RZRESULT_SUCCESS = 0


def to_colorref(rgb: RGB) -> int:
    """Chroma SDK colors are COLORREF: 0x00BBGGRR."""
    r, g, b = rgb
    return (b & 0xFF) << 16 | (g & 0xFF) << 8 | (r & 0xFF)


class NativeChroma:
    """Thin wrapper around RzChromaSDK64.dll."""

    def __init__(self, dll=None) -> None:
        if dll is None:
            if sys.platform != "win32":
                raise RazerUnavailable("Chroma SDK is only available on Windows")
            name = "RzChromaSDK64.dll" if C.sizeof(C.c_void_p) == 8 else "RzChromaSDK.dll"
            try:
                dll = C.WinDLL(name)
            except OSError as err:
                raise RazerUnavailable(f"{name} not found (is Razer Synapse installed?)") from err
        self._dll = dll
        result = dll.Init()
        if result != RZRESULT_SUCCESS:
            raise RazerUnavailable(f"Chroma SDK Init failed ({result})")
        self._open = True
        self._legacy: set[str] = set()  # devices that rejected the extended layout
        _LOGGER.info("Chroma SDK initialised")

    def send_frames(self, frames: dict[str, list[RGB]]) -> None:
        """Send one frame to every device category (extended layout when possible)."""
        for device in CUSTOM_EFFECTS:
            if device not in frames:
                continue
            ext = EXTENDED_EFFECTS.get(device)
            if ext is not None and device not in self._legacy and ext[2] in frames:
                result = self._set_extended(device, frames[ext[2]], frames[device])
                if result == RZRESULT_SUCCESS:
                    continue
                _LOGGER.info("%s: extended layout not supported (%s), using basic layout",
                             device, result)
                self._legacy.add(device)
            self.set_frame(device, frames[device])

    def _set_extended(self, device: str, ext_colors: list[RGB], base: list[RGB]) -> int:
        func_name, effect_type, _name, count = EXTENDED_EFFECTS[device]
        values = [to_colorref(c) for c in ext_colors[:count]]
        values += [0] * (count - len(values))
        if device == "keyboard":
            # Key[6][22]: the exact key colors, so keys match the basic layout and
            # key effects (reactive, ripple) light the right keys.
            values += [to_colorref(c) | KEY_OVERRIDE for c in base[: 6 * 22]]
        buf = (C.c_uint32 * len(values))(*values)
        return getattr(self._dll, func_name)(effect_type, C.byref(buf), None)

    def set_frame(self, device: str, colors: list[RGB]) -> int:
        func_name, effect_type, count = CUSTOM_EFFECTS[device]
        buf = (C.c_uint32 * count)(*(to_colorref(c) for c in colors[:count]))
        func = getattr(self._dll, func_name)
        return func(effect_type, C.byref(buf), None)

    def close(self) -> None:
        if self._open:
            self._open = False
            try:
                self._dll.UnInit()
            except Exception:  # noqa: BLE001
                pass
            _LOGGER.info("Chroma SDK closed (devices back to the Synapse profile)")


assert set(CUSTOM_EFFECTS) == set(DEVICES)
