"""Decoding tests against real shared-memory dumps from Razer Synapse.

The dumps come from RazerSdkReader's test data (MIT License, see NOTICE).
"""

from __future__ import annotations

import ctypes
from pathlib import Path
import struct

import pytest

from tuyachroma import razer

DATA = Path(__file__).parent / "data"
FILES = {"keyboard": "keyboard", "mouse": "mouse", "mousepad": "mousepad",
         "headset": "headset", "keypad": "keypad", "chromalink": "link"}
SIZES = {"keyboard": 40088, "mouse": 14888, "mousepad": 12328,
         "headset": 11048, "keypad": 11848, "chromalink": 12808}

GAME_ORANGE = (221, 121, 0)


def dump(device: str, write_index: int | None = None) -> bytes:
    raw = bytearray((DATA / f"{FILES[device]}.bin").read_bytes())
    if write_index is not None:
        raw[0:4] = struct.pack("<I", write_index)
    return bytes(raw)


@pytest.mark.parametrize("device", list(SIZES))
def test_struct_sizes(device: str) -> None:
    assert ctypes.sizeof(razer.DEVICES[device].struct) == SIZES[device]
    assert len(dump(device)) == SIZES[device]


@pytest.mark.parametrize("device", list(SIZES))
def test_latest_frame_is_suspended(device: str) -> None:
    frame = razer.parse(device, dump(device))
    assert frame is not None
    assert frame.effect_type == 13  # Suspend: the game released the device
    assert not frame.active


def test_keyboard_custom_frame() -> None:
    frame = razer.parse("keyboard", dump("keyboard", write_index=9))  # ring slot 8
    assert frame.effect_type == razer.EFFECT_CUSTOM
    assert len(frame.colors) == 132
    assert frame.colors.count((55, 30, 0)) == 131  # dim background
    assert GAME_ORANGE in frame.colors  # one highlighted key


def test_same_game_color_on_all_devices() -> None:
    mouse = razer.parse("mouse", dump("mouse", write_index=10))  # slot 9: static
    pad = razer.parse("mousepad", dump("mousepad", write_index=7))  # slot 6: static
    link = razer.parse("chromalink", dump("chromalink", write_index=9))  # slot 8: custom
    assert mouse.effect_type == razer.EFFECT_STATIC and set(mouse.colors) == {GAME_ORANGE}
    assert set(pad.colors) == {GAME_ORANGE}
    assert link.colors[0] == GAME_ORANGE and link.colors[1] == (110, 60, 0)


def test_decrypted_alpha_is_opaque() -> None:
    raw = razer.Keyboard.from_buffer_copy(dump("keyboard"))
    data = raw.data[8]
    value = data.effect.custom[0] ^ razer._KEY[data.timestamp.ticks % 128]
    assert value >> 24 == 0xFF


def test_empty_memory() -> None:
    assert razer.parse("headset", bytes(SIZES["headset"])) is None


def test_reader_requires_windows(monkeypatch) -> None:
    monkeypatch.setattr(razer.sys, "platform", "linux")
    with pytest.raises(razer.RazerUnavailable):
        razer.ChromaReader(["keyboard"])
