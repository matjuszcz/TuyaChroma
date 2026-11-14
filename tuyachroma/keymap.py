"""Windows virtual-key codes -> (row, column) on the Razer keyboard grid (6x22).

Positions come from RZKEY values in Razer's RzChromaSDKTypes.h, where the
high byte is the row and the low byte the column.
"""

from __future__ import annotations

_RZKEY = {
    "ESC": 0x0001, "F1": 0x0003, "F2": 0x0004, "F3": 0x0005, "F4": 0x0006, "F5": 0x0007,
    "F6": 0x0008, "F7": 0x0009, "F8": 0x000A, "F9": 0x000B, "F10": 0x000C, "F11": 0x000D,
    "F12": 0x000E, "1": 0x0102, "2": 0x0103, "3": 0x0104, "4": 0x0105, "5": 0x0106,
    "6": 0x0107, "7": 0x0108, "8": 0x0109, "9": 0x010A, "0": 0x010B,
    "A": 0x0302, "B": 0x0407, "C": 0x0405, "D": 0x0304, "E": 0x0204, "F": 0x0305,
    "G": 0x0306, "H": 0x0307, "I": 0x0209, "J": 0x0308, "K": 0x0309, "L": 0x030A,
    "M": 0x0409, "N": 0x0408, "O": 0x020A, "P": 0x020B, "Q": 0x0202, "R": 0x0205,
    "S": 0x0303, "T": 0x0206, "U": 0x0208, "V": 0x0406, "W": 0x0203, "X": 0x0404,
    "Y": 0x0207, "Z": 0x0403, "NUMLOCK": 0x0112, "NUMPAD0": 0x0513, "NUMPAD1": 0x0412,
    "NUMPAD2": 0x0413, "NUMPAD3": 0x0414, "NUMPAD4": 0x0312, "NUMPAD5": 0x0313,
    "NUMPAD6": 0x0314, "NUMPAD7": 0x0212, "NUMPAD8": 0x0213, "NUMPAD9": 0x0214,
    "NUMPAD_DIVIDE": 0x0113, "NUMPAD_MULTIPLY": 0x0114, "NUMPAD_SUBTRACT": 0x0115,
    "NUMPAD_ADD": 0x0215, "NUMPAD_ENTER": 0x0415, "NUMPAD_DECIMAL": 0x0514,
    "PRINTSCREEN": 0x000F, "SCROLL": 0x0010, "PAUSE": 0x0011, "INSERT": 0x010F,
    "HOME": 0x0110, "PAGEUP": 0x0111, "DELETE": 0x020F, "END": 0x0210, "PAGEDOWN": 0x0211,
    "UP": 0x0410, "LEFT": 0x050F, "DOWN": 0x0510, "RIGHT": 0x0511, "TAB": 0x0201,
    "CAPSLOCK": 0x0301, "BACKSPACE": 0x010E, "ENTER": 0x030E, "LCTRL": 0x0501,
    "LWIN": 0x0502, "LALT": 0x0503, "SPACE": 0x0507, "RALT": 0x050B, "RMENU": 0x050D,
    "RCTRL": 0x050E, "LSHIFT": 0x0401, "RSHIFT": 0x040E,
    "OEM_1": 0x0101, "OEM_2": 0x010C, "OEM_3": 0x010D, "OEM_4": 0x020C, "OEM_5": 0x020D,
    "OEM_6": 0x020E, "OEM_7": 0x030B, "OEM_8": 0x030C, "OEM_9": 0x040A, "OEM_10": 0x040B,
    "OEM_11": 0x040C, "EUR_2": 0x0402,
}

# Windows VK code -> Razer key name
_VK = {
    0x1B: "ESC", 0x09: "TAB", 0x14: "CAPSLOCK", 0x08: "BACKSPACE", 0x0D: "ENTER",
    0x20: "SPACE", 0xA0: "LSHIFT", 0xA1: "RSHIFT", 0xA2: "LCTRL", 0xA3: "RCTRL",
    0xA4: "LALT", 0xA5: "RALT", 0x5B: "LWIN", 0x5D: "RMENU",
    0x2C: "PRINTSCREEN", 0x91: "SCROLL", 0x13: "PAUSE", 0x2D: "INSERT", 0x24: "HOME",
    0x21: "PAGEUP", 0x2E: "DELETE", 0x23: "END", 0x22: "PAGEDOWN",
    0x26: "UP", 0x25: "LEFT", 0x28: "DOWN", 0x27: "RIGHT",
    0x90: "NUMLOCK", 0x6F: "NUMPAD_DIVIDE", 0x6A: "NUMPAD_MULTIPLY",
    0x6D: "NUMPAD_SUBTRACT", 0x6B: "NUMPAD_ADD", 0x6E: "NUMPAD_DECIMAL",
    0xC0: "OEM_1",   # ` ~
    0xBD: "OEM_2",   # - _
    0xBB: "OEM_3",   # = +
    0xDB: "OEM_4",   # [ {
    0xDD: "OEM_5",   # ] }
    0xDC: "OEM_6",   # \\ |
    0xBA: "OEM_7",   # ; :
    0xDE: "OEM_8",   # ' "
    0xBC: "OEM_9",   # , <
    0xBE: "OEM_10",  # . >
    0xBF: "OEM_11",  # / ?
    0xE2: "EUR_2",   # ISO key next to left shift
}
for _i in range(10):
    _VK[0x30 + _i] = str(_i)
    _VK[0x60 + _i] = f"NUMPAD{_i}"
for _i in range(26):
    _VK[0x41 + _i] = chr(0x41 + _i)
for _i in range(12):
    _VK[0x70 + _i] = f"F{_i + 1}"


def key_position(vk: int, extended: bool = False) -> tuple[int, int] | None:
    """(row, column) of a key on the 6x22 grid, or None if unknown."""
    if vk == 0x0D and extended:
        name = "NUMPAD_ENTER"
    else:
        name = _VK.get(vk)
    if name is None:
        return None
    code = _RZKEY[name]
    return code >> 8, code & 0xFF
