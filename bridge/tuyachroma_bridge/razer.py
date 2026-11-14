"""Read the LED colors that games and apps send to Razer Chroma devices.

Razer's SDK service (RzSDKService, part of Synapse) mirrors every Chroma SDK
effect into shared memory used by its "Chroma Emulator". This module opens
that shared memory read-only and decodes the per-LED colors.

Layouts, object names and the color de-obfuscation key are ported from
RazerSdkReader by diogotr7 (MIT License): https://github.com/diogotr7/RazerSdkReader
Copyright (c) 2023 Diogo Trindade. See NOTICE for the full license text.
"""

from __future__ import annotations

import ctypes as C
from dataclasses import dataclass
import logging
import sys

_LOGGER = logging.getLogger(__name__)

RGB = tuple[int, int, int]

# --------------------------------------------------------------------------
# Structures (all packed, little endian)
# --------------------------------------------------------------------------

u32 = C.c_uint32
i32 = C.c_int32


class _S(C.LittleEndianStructure):
    _pack_ = 1


class Breathing(_S):
    _fields_ = [("param", u32), ("type", i32), ("color1", u32), ("color2", u32)]


class Static(_S):
    _fields_ = [("param", u32), ("color", u32)]


class Blinking(_S):
    _fields_ = [("param", u32), ("color", u32)]


class Reactive(_S):
    _fields_ = [("param", u32), ("color", u32), ("duration", i32)]


class Starlight(_S):
    _fields_ = [("param", u32), ("type", i32), ("duration", i32), ("color1", u32), ("color2", u32)]


class Wave(_S):
    _fields_ = [("param", u32), ("direction", i32), ("speed", i32)]


class Timestamp(_S):
    _fields_ = [("ticks", C.c_uint64)]


class ChromaString(_S):
    _fields_ = [("chars", C.c_uint16 * 260)]  # UTF-16, NUL terminated


class ChromaDevice(_S):
    _fields_ = [("instance", ChromaString), ("instance_name", ChromaString)]


class KeyboardCustom2(_S):
    _fields_ = [("color", u32 * 132), ("key", u32 * 132)]


class KeyboardCustom3(_S):
    _fields_ = [("color", u32 * 192), ("key", u32 * 132)]


class KeyboardEffect(_S):
    _fields_ = [
        ("wave", Wave), ("breathing", Breathing), ("reactive", Reactive),
        ("starlight", Starlight), ("static", Static), ("custom", u32 * 132),
        ("custom2", KeyboardCustom2), ("custom3", KeyboardCustom3),
    ]


class KeyboardData(_S):
    _fields_ = [("flag", u32), ("effect_type", i32), ("effect", KeyboardEffect),
                ("padding", u32), ("timestamp", Timestamp)]


class MouseEffect(_S):
    _fields_ = [
        ("breathing", Breathing), ("blinking", Blinking), ("custom", u32 * 30),
        ("custom2", u32 * 63), ("reactive", Reactive), ("static", Static), ("wave", Wave),
    ]


class MouseData(_S):
    _fields_ = [("flag", u32), ("led", i32), ("effect_type", i32), ("effect", MouseEffect),
                ("timestamp", Timestamp)]


class MousepadEffect(_S):
    _fields_ = [("breathing", Breathing), ("wave", Wave), ("static", Static),
                ("custom", u32 * 15), ("custom2", u32 * 20)]


class MousepadData(_S):
    _fields_ = [("flag", u32), ("effect_type", i32), ("effect", MousepadEffect),
                ("timestamp", Timestamp)]


class HeadsetEffect(_S):
    _fields_ = [("breathing", Breathing), ("custom", u32 * 5), ("static", Static)]


class HeadsetData(_S):
    _fields_ = [("flag", u32), ("effect_type", i32), ("effect", HeadsetEffect),
                ("padding", u32), ("timestamp", Timestamp)]


class KeypadEffect(_S):
    _fields_ = [("breathing", Breathing), ("custom", u32 * 20), ("reactive", Reactive),
                ("static", Static), ("wave", Wave)]


class KeypadData(_S):
    _fields_ = [("flag", u32), ("effect_type", i32), ("effect", KeypadEffect),
                ("timestamp", Timestamp)]


class LinkEffect(_S):
    _fields_ = [("breathing", Breathing), ("custom", u32 * 50), ("static", Static)]


class LinkData(_S):
    _fields_ = [("flag", u32), ("effect_type", i32), ("effect", LinkEffect),
                ("timestamp", Timestamp)]


def _device_struct(name: str, data_type: type[_S]) -> type[_S]:
    return type(name, (_S,), {"_fields_": [
        ("write_index", u32), ("padding", u32),
        ("data", data_type * 10), ("device", ChromaDevice * 10),
    ]})


Keyboard = _device_struct("Keyboard", KeyboardData)
Mouse = _device_struct("Mouse", MouseData)
Mousepad = _device_struct("Mousepad", MousepadData)
Headset = _device_struct("Headset", HeadsetData)
Keypad = _device_struct("Keypad", KeypadData)
Link = _device_struct("Link", LinkData)

# Effect types
EFFECT_NONE = 0
EFFECT_STATIC = 6
EFFECT_CUSTOM = 7
EFFECT_CUSTOM_KEY = 8


@dataclass(frozen=True)
class DeviceSpec:
    """Shared-memory objects and LED grid of one Chroma device class."""

    name: str
    struct: type[_S]
    file_name: str
    event_name: str
    width: int
    height: int

    @property
    def count(self) -> int:
        return self.width * self.height


DEVICES: dict[str, DeviceSpec] = {
    spec.name: spec
    for spec in (
        DeviceSpec("keyboard", Keyboard, r"Global\{74164FAD-E73C-4FA1-A9AA-70813315ED9C}",
                   r"Global\{45C97C2C-2D50-4F30-B50E-AFBB1CE22E93}", 22, 6),
        DeviceSpec("mouse", Mouse, r"Global\{0DBE78AC-AC93-408F-A27E-8F61EA067B05}",
                   r"Global\{0DB0CEFA-C51E-4255-87FB-2D36A0159896}", 7, 9),
        DeviceSpec("mousepad", Mousepad, r"Global\{17EFA16B-E476-4E43-A98A-3AA837681741}",
                   r"Global\{A966C3C0-231A-4BE5-9C90-5E0C80349891}", 15, 1),
        DeviceSpec("headset", Headset, r"Global\{CDB274E2-C50A-4425-8076-1E71550CBE8A}",
                   r"Global\{9FE422BE-A752-4F67-9EC6-11ED6135478E}", 5, 1),
        DeviceSpec("keypad", Keypad, r"Global\{0FFE5A62-387E-4360-95A3-5D8D4075780D}",
                   r"Global\{5C49A446-0B97-46CA-BD60-EE5CAF8DDD59}", 5, 4),
        DeviceSpec("chromalink", Link, r"Global\{8AE08F8C-BE3E-4248-AB01-0B595960EC3E}",
                   r"Global\{4D006319-9569-4E38-B0DF-811AA2DF115F}", 5, 1),
    )
}

# Mutexes/event that make RzSDKService believe Synapse and the Chroma Emulator
# are running, so that it keeps the shared memory above up to date.
SYNAPSE_ONLINE_MUTEX = r"Global\{08B4F43A-DA51-4120-B388-CE0F8CE6F61A}"
SYNAPSE_EVENT = r"Global\{89811F96-91C2-4C19-8E0A-54469F491550}"
OLD_SYNAPSE_ONLINE_MUTEX = r"Global\{B1570C3F-8B14-45B0-BCEB-C57ED1F5C589}"
OLD_SYNAPSE_VERSION_MUTEX = r"Global\{D7B0F094-74E7-4055-BE84-F447B722DEB7}"
CHROMA_EMULATOR_MUTEX = r"Global\{5606D98C-C0DC-43E1-9A14-D992B52750F7}"

# --------------------------------------------------------------------------
# Color de-obfuscation
# --------------------------------------------------------------------------

_ORIGINAL_KEY = bytes([
    0x45, 0x86, 0x56, 0x02, 0x34, 0x86, 0x27, 0xF6, 0xD6, 0x16, 0x02, 0x75,
    0xF6, 0x27, 0xB6, 0x37, 0x86, 0xF6, 0x07, 0x02, 0x96, 0x37, 0x02, 0x97,
    0xF6, 0x57, 0x27, 0x02, 0xF6, 0xE6, 0x56, 0xD2, 0x37, 0x47, 0xF6, 0x07,
    0xD2, 0x37, 0x86, 0xF6, 0x07, 0x02, 0x47, 0xF6, 0x02, 0x76, 0x56, 0x47,
    0x02, 0x47, 0x86, 0x56, 0x02, 0xD6, 0xF6, 0x37, 0x47, 0x02, 0xF6, 0x57,
    0x47, 0x02, 0xF6, 0x66, 0x02, 0x97, 0xF6, 0x57, 0x27, 0x02, 0x34, 0x86,
    0x27, 0xF6, 0xD6, 0x16, 0x02, 0x46, 0x56, 0x67, 0x96, 0x36, 0x56, 0x37,
    0xE2, 0x02, 0x02, 0x75, 0x86, 0x56, 0x47, 0x86, 0x56, 0x27, 0x02, 0x96,
    0x47, 0x72, 0x37, 0x02, 0x37, 0xD6, 0x16, 0x27, 0x47, 0x02, 0xC6, 0x96,
    0x76, 0x86, 0x47, 0x96, 0xE6, 0x76, 0x02, 0x26, 0x16, 0x37, 0x56, 0x46,
    0x02, 0xF6, 0xE6, 0x02, 0x96, 0xE6, 0xD2, 0xF6, 0x75, 0x56, 0x29, 0x27,
    0x56, 0x02, 0x07, 0x57, 0x37, 0x86, 0x96, 0xE6, 0x76, 0x02, 0x47, 0x86,
    0x56, 0x02, 0xC6, 0x96, 0xD6, 0x96, 0x47, 0x37, 0x02, 0x47, 0xF6, 0x02,
    0x77, 0x86, 0x16, 0x47, 0x02, 0x16, 0x02, 0x76, 0x16, 0xD6, 0x96, 0xE6,
    0x76, 0x02, 0xC6, 0x16, 0x07, 0x47, 0xF6, 0x07, 0x02, 0x36, 0x16, 0xE6,
    0x02, 0x46, 0xF6, 0xE2, 0x02, 0x45, 0x86, 0x56, 0x02, 0x25, 0x16, 0xA7,
    0x56, 0x27, 0x02, 0x24, 0xC6, 0x16, 0x46, 0x56, 0x02, 0x05, 0x27, 0xF6,
    0x02, 0x13, 0x73, 0x02, 0x96, 0x37, 0x02, 0x26, 0x57, 0x96, 0xC6, 0x47,
    0x02, 0x77, 0x96, 0x47, 0x86, 0x02, 0x16, 0x02, 0x07, 0xF6, 0x77, 0x56,
    0x27, 0x66, 0x57, 0xC6, 0x02, 0x07, 0x27, 0xF6, 0x36, 0x56, 0x37, 0x37,
    0xF6, 0x27, 0x02, 0x47, 0xF6, 0x02, 0x27, 0x57, 0xE6, 0x02, 0x07, 0x56,
    0x27, 0x66, 0xF6, 0x12, 0x24, 0x57, 0x96, 0xC6, 0x47, 0x02, 0x96, 0xE6,
    0x47, 0xF6, 0x02, 0xF6, 0x57, 0x27, 0x02, 0x44, 0xE4, 0x14, 0xC2, 0x02,
    0x47, 0x86, 0x56, 0x02, 0x25, 0x16, 0xA7, 0x56, 0x27, 0x02, 0x05, 0x86,
    0xF6, 0xE6, 0x56, 0x02, 0x23, 0x02, 0x66, 0x56, 0x16, 0x47, 0x57, 0x27,
    0x56, 0x37, 0x02, 0x16, 0xE6, 0x02, 0x96, 0xC6, 0xC6, 0x57, 0xD6, 0x96,
    0xE6, 0x16, 0x47, 0x56, 0x46, 0x02, 0x25, 0x16, 0xA7, 0x56, 0x27, 0x02,
    0xC6, 0xF6, 0x76, 0xF6, 0x02, 0x77, 0x96, 0x47, 0x86, 0x02, 0x36, 0x57,
    0x37, 0x47, 0xF6, 0xD6, 0x96, 0xA7, 0x16, 0x26, 0xC6, 0x56, 0x02, 0x25,
    0x16, 0xA7, 0x56, 0x27, 0x02, 0x34, 0x86, 0x27, 0xF6, 0xD6, 0x16, 0x99,
    0x02, 0x25, 0x74, 0x24, 0x02, 0xC6, 0x96, 0x76, 0x86, 0x47, 0x96, 0xE6,
    0x76, 0xE2, 0x02, 0x95, 0xF6, 0x57, 0x27, 0x02, 0xD6, 0xF6, 0x26, 0xBB,
    0x45, 0x86, 0x56, 0x02, 0x25, 0x16, 0xA7, 0x56, 0x27, 0x02, 0x34, 0x86,
    0x27, 0xF6, 0xD6, 0x16, 0x02, 0x34, 0xF6, 0xE6, 0xE6, 0x56, 0x36, 0x47,
    0x56, 0x46, 0x02, 0x44, 0x56, 0x67, 0x96, 0x36, 0x56, 0x37, 0x02, 0x05,
    0x27, 0xF6, 0x76, 0x27, 0x16, 0xD6, 0x02, 0x77, 0x96, 0xC6, 0xC6, 0x02,
    0x56, 0xE6, 0x16, 0x26, 0xC6, 0x56, 0x02, 0x07, 0x16, 0x27, 0x47, 0xE6,
    0x56, 0x27, 0x37, 0x02, 0x47, 0xF6, 0x02, 0x56, 0xE6, 0x16, 0x26, 0xC6,
    0x56, 0x02, 0x34, 0x86, 0x27, 0xF6, 0xD6, 0x16, 0x02, 0xC6, 0x96, 0x76,
    0x86, 0x47, 0x96, 0xE6, 0x76, 0x02, 0x47, 0x86, 0x27, 0xF6, 0x57, 0x76,
    0x86, 0x02, 0x16, 0x02, 0x37, 0x96, 0xE6, 0x76, 0xC6, 0x56, 0x02, 0x36,
    0xC6, 0x96, 0x36, 0xB6, 0x02, 0xF6, 0xE6, 0x02, 0x47, 0x86, 0x56, 0x96,
    0x27, 0x02, 0x37, 0xF6, 0x66, 0x47, 0x77, 0xA0,
])


def _build_key() -> list[int]:
    key = []
    for i in range(len(_ORIGINAL_KEY) // 4):
        j = i - 3 if i > 124 else i
        k = _ORIGINAL_KEY
        key.append(int.from_bytes(bytes([k[j], k[129 + j], k[258 + j], k[387 + j]]), "little"))
    return key


_KEY = _build_key()


def decrypt(color: int, ticks: int) -> RGB:
    """Decode one obfuscated SDK color (RGBA little endian) to (r, g, b)."""
    value = color ^ _KEY[ticks % 128]
    return value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


# Effects that carry per-LED colors. Older SDK built-in effects (wave,
# breathing, ...) are approximated by their main color; control messages
# (init, suspend, active, ...) mean "no game effect right now".
EFFECT_WAVE = 1
EFFECT_SPECTRUM = 2
EFFECT_BREATHING = 3
EFFECT_BLINKING = 4
EFFECT_REACTIVE = 5
EFFECT_STARLIGHT = 12


@dataclass
class Frame:
    """Decoded colors of one device (row-major, width*height LEDs).

    ``colors`` is None when the device currently shows no SDK effect
    (no game/app is driving it, or the app released it).
    """

    device: str
    effect_type: int
    colors: list[RGB] | None
    ticks: int

    @property
    def active(self) -> bool:
        return self.colors is not None


def _effect_main_color(effect: object, etype: int, ticks: int) -> RGB | None:
    for attr, field in (
        ("breathing", "color1") if etype == EFFECT_BREATHING else (None, None),
        ("blinking", "color") if etype == EFFECT_BLINKING else (None, None),
        ("reactive", "color") if etype == EFFECT_REACTIVE else (None, None),
        ("starlight", "color1") if etype == EFFECT_STARLIGHT else (None, None),
    ):
        if attr and hasattr(effect, attr):
            return decrypt(getattr(getattr(effect, attr), field), ticks)
    return None


def parse(device: str, raw: bytes) -> Frame | None:
    """Decode a raw shared-memory snapshot. Returns None if nothing was written yet."""
    spec = DEVICES[device]
    if len(raw) < C.sizeof(spec.struct):
        raise ValueError(f"{device}: need {C.sizeof(spec.struct)} bytes, got {len(raw)}")
    obj = spec.struct.from_buffer_copy(raw[: C.sizeof(spec.struct)])
    if obj.write_index == 0:
        return None
    data = obj.data[min(obj.write_index - 1, 9)]
    ticks = data.timestamp.ticks
    etype = data.effect_type
    effect = data.effect

    colors: list[RGB] | None
    if etype == EFFECT_NONE:
        colors = [(0, 0, 0)] * spec.count
    elif etype == EFFECT_STATIC:
        colors = [decrypt(effect.static.color, ticks)] * spec.count
    elif etype in (EFFECT_CUSTOM, EFFECT_CUSTOM_KEY):
        if device == "keyboard":
            source = effect.custom2.color if etype == EFFECT_CUSTOM_KEY else effect.custom
        elif device == "mouse":
            source = effect.custom2
        else:
            source = effect.custom
        colors = [decrypt(source[i], ticks) for i in range(spec.count)]
    elif (main := _effect_main_color(effect, etype, ticks)) is not None:
        colors = [main] * spec.count
    else:
        colors = None
    return Frame(device=device, effect_type=etype, colors=colors, ticks=ticks)


# --------------------------------------------------------------------------
# Windows shared-memory access
# --------------------------------------------------------------------------


class RazerUnavailable(Exception):
    """Razer SDK service is not running or shared memory is not available."""


class ChromaReader:
    """Holds the emulator mutexes and reads device snapshots (Windows only)."""

    FILE_MAP_READ = 0x0004
    SYNCHRONIZE = 0x00100000
    EVENT_MODIFY_STATE = 0x0002
    WAIT_TIMEOUT = 0x102

    def __init__(self, devices: list[str]) -> None:
        if sys.platform != "win32":
            raise RazerUnavailable("Razer Chroma can only be read on Windows")
        from ctypes import wintypes as W  # noqa: PLC0415

        self._W = W
        k32 = C.WinDLL("kernel32", use_last_error=True)
        k32.OpenFileMappingW.restype = W.HANDLE
        k32.OpenFileMappingW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]
        k32.MapViewOfFile.restype = C.c_void_p
        k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, C.c_size_t]
        k32.UnmapViewOfFile.argtypes = [C.c_void_p]
        k32.CreateMutexW.restype = W.HANDLE
        k32.CreateMutexW.argtypes = [C.c_void_p, W.BOOL, W.LPCWSTR]
        k32.CreateEventW.restype = W.HANDLE
        k32.CreateEventW.argtypes = [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR]
        k32.OpenEventW.restype = W.HANDLE
        k32.OpenEventW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]
        k32.WaitForMultipleObjects.argtypes = [W.DWORD, C.POINTER(W.HANDLE), W.BOOL, W.DWORD]
        k32.CloseHandle.argtypes = [W.HANDLE]
        k32.ReleaseMutex.argtypes = [W.HANDLE]
        k32.SetEvent.argtypes = [W.HANDLE]
        k32.ResetEvent.argtypes = [W.HANDLE]
        self._k32 = k32

        # Security attributes with a NULL DACL (= everyone full access), like the
        # original library, so that the Razer service can open our objects.
        adv = C.WinDLL("advapi32", use_last_error=True)
        self._sd = C.create_string_buffer(64)
        adv.InitializeSecurityDescriptor(self._sd, 1)
        adv.SetSecurityDescriptorDacl(self._sd, True, None, False)

        class SECURITY_ATTRIBUTES(C.Structure):
            _fields_ = [("nLength", W.DWORD), ("lpSecurityDescriptor", C.c_void_p),
                        ("bInheritHandle", W.BOOL)]

        self._sa = SECURITY_ATTRIBUTES(C.sizeof(SECURITY_ATTRIBUTES), C.addressof(self._sd), False)

        self._mutexes: list[int] = []
        self._maps: dict[str, tuple[int, int]] = {}
        self._events: dict[str, int] = {}
        try:
            self._acquire_mutexes()
            for name in devices:
                self._open_device(DEVICES[name])
        except Exception:
            self.close()
            raise

    def _acquire_mutexes(self) -> None:
        for name, pulse in (
            (SYNAPSE_ONLINE_MUTEX, True),
            (OLD_SYNAPSE_ONLINE_MUTEX, True),
            (OLD_SYNAPSE_VERSION_MUTEX, True),
            (CHROMA_EMULATOR_MUTEX, False),
        ):
            handle = self._k32.CreateMutexW(C.byref(self._sa), True, name)
            if not handle:
                raise RazerUnavailable(f"CreateMutex failed ({C.get_last_error()})")
            self._mutexes.append(handle)
            if pulse:
                self._pulse(SYNAPSE_EVENT)

    def _pulse(self, name: str) -> None:
        handle = self._k32.OpenEventW(self.EVENT_MODIFY_STATE | self.SYNCHRONIZE, False, name)
        if not handle:
            _LOGGER.debug("Event %s not found (%s)", name, C.get_last_error())
            return
        self._k32.SetEvent(handle)
        self._k32.ResetEvent(handle)
        self._k32.CloseHandle(handle)

    def _open_device(self, spec: DeviceSpec) -> None:
        mapping = self._k32.OpenFileMappingW(self.FILE_MAP_READ, False, spec.file_name)
        if not mapping:
            raise RazerUnavailable(
                f"Shared memory for {spec.name} not found (is Razer Synapse running?)"
            )
        view = self._k32.MapViewOfFile(mapping, self.FILE_MAP_READ, 0, 0, C.sizeof(spec.struct))
        if not view:
            self._k32.CloseHandle(mapping)
            raise RazerUnavailable(f"MapViewOfFile failed for {spec.name} ({C.get_last_error()})")
        self._maps[spec.name] = (mapping, view)
        event = self._k32.CreateEventW(C.byref(self._sa), True, False, spec.event_name)
        if event:
            self._events[spec.name] = event

    def read(self, device: str) -> Frame | None:
        _, view = self._maps[device]
        raw = C.string_at(view, C.sizeof(DEVICES[device].struct))
        return parse(device, raw)

    def wait(self, timeout_ms: int) -> None:
        """Block until any device signals an update, or the timeout passes."""
        handles = list(self._events.values())
        if not handles:
            return
        arr = (self._W.HANDLE * len(handles))(*handles)
        self._k32.WaitForMultipleObjects(len(handles), arr, False, timeout_ms)
        for handle in handles:
            self._k32.ResetEvent(handle)

    def close(self) -> None:
        for mapping, view in self._maps.values():
            self._k32.UnmapViewOfFile(view)
            self._k32.CloseHandle(mapping)
        self._maps.clear()
        for event in self._events.values():
            self._k32.CloseHandle(event)
        self._events.clear()
        for mutex in reversed(self._mutexes):
            self._k32.ReleaseMutex(mutex)
            self._k32.CloseHandle(mutex)
        self._mutexes.clear()
