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


def test_unknown_effect_type_logged_once(caplog) -> None:
    from tuyachroma import razer

    razer._UNKNOWN_SEEN.discard(("keyboard", 99))
    spec = razer.DEVICES["keyboard"]
    obj = spec.struct()
    obj.write_index = 1
    obj.data[0].effect_type = 99
    raw = bytes(obj)
    with caplog.at_level("INFO", logger="tuyachroma.razer"):
        assert razer.parse("keyboard", raw).active is False
        razer.parse("keyboard", raw)
    assert sum("effect type 99" in r.message for r in caplog.records) == 1


def _with_newest(device: str, slot: int, etype: int) -> bytes:
    obj = razer.DEVICES[device].struct.from_buffer_copy(dump(device))
    obj.write_index = slot + 1
    obj.data[slot].effect_type = etype
    return bytes(obj)


@pytest.mark.parametrize("control", [razer.EFFECT_ACTIVE, razer.EFFECT_RESUME, razer.EFFECT_INIT])
def test_control_message_on_top_keeps_the_last_frame(control: int, caplog) -> None:
    """Synapse writes "Active"/"Resume" between frames: the game's frame still shows.

    Reading only the newest entry made Cyberpunk 2077's effect "end" for a
    moment again and again (the lights flickered to the profile and back).
    """
    raw = _with_newest("keyboard", 9, control)  # slot 8 holds the game's custom frame
    with caplog.at_level("INFO", logger="tuyachroma.razer"):
        frame = razer.parse("keyboard", raw)
    assert frame.active and frame.effect_type == razer.EFFECT_CUSTOM
    assert GAME_ORANGE in frame.colors
    assert not any("not decoded" in r.message for r in caplog.records)  # no noise for these


def test_ending_message_means_no_effect() -> None:
    for ending in (razer.EFFECT_SUSPEND, razer.EFFECT_UNINIT):
        assert not razer.parse("keyboard", _with_newest("keyboard", 9, ending)).active
    # "Active" right after a "Suspend": still nothing shown
    obj = razer.DEVICES["keyboard"].struct.from_buffer_copy(_with_newest("keyboard", 9, razer.EFFECT_ACTIVE))
    obj.data[8].effect_type = razer.EFFECT_SUSPEND
    assert not razer.parse("keyboard", bytes(obj)).active


def test_wrapped_ring_reads_slot_nine() -> None:
    """write_index 0 after 10 writes: the newest frame is in slot 9, not "nothing"."""
    obj = razer.DEVICES["keyboard"].struct.from_buffer_copy(dump("keyboard", write_index=9))
    obj.data[9] = obj.data[8]
    obj.write_index = 0
    frame = razer.parse("keyboard", bytes(obj))
    assert frame is not None and frame.active
    empty = razer.DEVICES["keyboard"].struct()
    assert razer.parse("keyboard", bytes(empty)) is None  # never written


def test_resume_after_suspend_shows_the_effect_again() -> None:
    obj = razer.DEVICES["keyboard"].struct.from_buffer_copy(_with_newest("keyboard", 9, razer.EFFECT_RESUME))
    obj.data[8].effect_type = razer.EFFECT_SUSPEND
    obj.data[7] = razer.DEVICES["keyboard"].struct.from_buffer_copy(dump("keyboard")).data[8]
    frame = razer.parse("keyboard", bytes(obj))
    assert frame.active and frame.effect_type == razer.EFFECT_CUSTOM
