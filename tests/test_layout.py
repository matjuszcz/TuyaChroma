"""Keyboard layouts: e.g. Huntsman Mini (60%) inside Razer's 6x22 grid."""

from __future__ import annotations

import colorsys

from tuyachroma.bulbs import BulbConfig
from tuyachroma.colors import zone_indices
from tuyachroma.engine import zone_color
from tuyachroma.profiles import Profile
from tuyachroma.razer_devices import RazerDevice, keyboard_layout
from tuyachroma.render import KEYBOARD_AREAS, Renderer

MINI = KEYBOARD_AREAS["mini"]
USER_SETUP = [
    RazerDevice("Razer Huntsman Mini", "keyboard", "USB"),
    RazerDevice("Razer Basilisk V3", "mouse", "USB"),
    RazerDevice("Razer Mouse Bungee V3 Chroma", "mousepad", "USB"),
    RazerDevice("Razer Chroma Addressable RGB Controller", "chromalink", "USB"),
]


def hue(rgb):
    return colorsys.rgb_to_hsv(*(c / 255 for c in rgb))[0]


def test_layout_detection() -> None:
    assert keyboard_layout(USER_SETUP) == "mini"
    assert keyboard_layout([RazerDevice("Razer Huntsman V2 TKL", "keyboard", "USB")]) == "tkl"
    assert keyboard_layout([RazerDevice("Razer BlackWidow V4", "keyboard", "USB")]) == "full"
    assert keyboard_layout([]) == "full"


def test_wave_spans_the_mini_keys() -> None:
    r = Renderer(Profile(effect="wave", speed=5))
    r.keyboard_area = MINI
    kb = r.device_frame("keyboard", 0.0)
    row = [kb[3 * 22 + c] for c in range(MINI[0], MINI[1] + 1)]
    hues = [hue(c) for c in row]
    # the whole rainbow is spread over the 14 keys of the Mini, evenly
    steps = [(hues[i] - hues[i + 1]) % 1 for i in range(len(hues) - 1)]
    assert all(abs(s - 1 / 14) < 0.02 for s in steps)
    full = Renderer(Profile(effect="wave", speed=5)).device_frame("keyboard", 0.0)
    assert full[3 * 22 + 14] != kb[3 * 22 + 14]  # differs from the full-size wave


def test_bulb_zones_on_the_mini() -> None:
    right = zone_indices("keyboard", "right", MINI)
    cols = {i % 22 for i in right}
    assert cols == {11, 12, 13, 14}  # right side of the Mini (14 columns), not the numpad
    assert {i % 22 for i in zone_indices("keyboard", "left", MINI)} == {1, 2, 3, 4}
    # game paints the Mini's right keys blue, numpad (absent) red
    colors = [(255, 0, 0)] * 132
    for i in right:
        colors[i] = (0, 0, 255)
    cfg = BulbConfig(name="l", id="x", ip="i", local_key="k", zone="right", min_brightness=0)
    assert zone_color(cfg, colors, MINI) == (0, 0, 255)
    assert zone_color(cfg, colors) == (255, 0, 0)  # full layout would pick the numpad


def test_stars_only_on_existing_keys() -> None:
    r = Renderer(Profile(effect="starlight", density=10, color1="#ffffff"), seed=3)
    r.keyboard_area = MINI
    for i in range(1, 60):
        kb = r.device_frame("keyboard", i * 0.05)
        for idx, c in enumerate(kb):
            if c != (0, 0, 0):
                assert MINI[0] <= idx % 22 <= MINI[1]
