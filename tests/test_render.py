"""Effect rendering for every Razer device category and the bulb strip."""

from __future__ import annotations

import pytest

from tuyachroma.keymap import key_position
from tuyachroma.profiles import EFFECTS, Profile, default_profiles
from tuyachroma.razer import DEVICES
from tuyachroma.render import Renderer


def r(effect: str, **kw) -> Renderer:
    return Renderer(Profile(name="t", effect=effect, **kw), screen=lambda w, h: [(10, 20, 30)] * (w * h),
                    seed=42)


@pytest.mark.parametrize("effect", EFFECTS)
@pytest.mark.parametrize("device", list(DEVICES))
def test_every_effect_on_every_device(effect: str, device: str) -> None:
    rend = r(effect)
    rend.key_pressed(3, 2, t=0.9)
    for t in (1.0, 1.1, 1.5):
        colors = rend.device_frame(device, t)
        assert len(colors) == DEVICES[device].count
        assert all(len(c) == 3 and all(0 <= v <= 255 for v in c) for c in colors)


def test_wave_runs_across_keyboard_and_bulbs() -> None:
    rend = r("wave", speed=5)
    kb = rend.device_frame("keyboard", 1.0)
    assert kb[0] != kb[21] and kb[0] == kb[22]  # columns differ, rows equal (left/right wave)
    later = rend.device_frame("keyboard", 1.0 + Profile(speed=5).period / 22)
    assert later[1] == kb[0] or later[0] != kb[0]  # the wave moves
    strip = rend.strip(4, 1.0)
    assert len(set(strip)) == 4
    down = r("wave", direction="down").device_frame("keyboard", 1.0)
    assert down[0] == down[21] and down[0] != down[5 * 22]


def test_static_breathing_spectrum() -> None:
    assert set(r("static", color1="#ff0000").device_frame("mouse", 3)) == {(255, 0, 0)}
    b = r("breathing", color1="#ff0000", speed=5)
    period = Profile(speed=5).period
    assert b.device_frame("headset", 0)[0] == (0, 0, 0)
    assert b.device_frame("headset", period / 2)[0] == (255, 0, 0)
    dual = r("breathing", color1="#ff0000", color2="#0000ff", color_mode="dual", speed=5)
    assert dual.device_frame("headset", period * 1.5)[0] == (0, 0, 255)
    spec = r("spectrum").device_frame("keyboard", 2.0)
    assert len(set(spec)) == 1


def test_brightness() -> None:
    assert r("static", color1="#ff8000", brightness=0.5).device_frame("chromalink", 0)[0] == (128, 64, 0)


def test_starlight_twinkles() -> None:
    rend = r("starlight", density=10, color_mode="single", color1="#ffffff")
    lit = 0
    for i in range(1, 40):
        lit = max(lit, sum(1 for c in rend.device_frame("keyboard", i * 0.05) if c != (0, 0, 0)))
    assert 0 < lit < 132  # some stars, not all keys


def test_fire_hotter_at_bottom() -> None:
    rend = r("fire")
    for i in range(1, 30):
        kb = rend.device_frame("keyboard", i * 0.05)
    bottom = sum(sum(c) for c in kb[5 * 22:])
    top = sum(sum(c) for c in kb[:22])
    assert bottom > top


def test_reactive_lights_pressed_key_and_fades() -> None:
    rend = r("reactive", color1="#ff0000", speed=5)
    row, col = key_position(0x41)  # A
    rend.key_pressed(row, col, t=10.0)
    kb = rend.device_frame("keyboard", 10.01)
    assert kb[row * 22 + col][0] > 240 and sum(1 for c in kb if c != (0, 0, 0)) == 1
    assert rend.device_frame("mouse", 10.01)[0][0] > 240  # other devices flash
    assert rend.device_frame("keyboard", 20.0)[row * 22 + col] == (0, 0, 0)


def test_ripple_ring_expands() -> None:
    rend = r("ripple", color1="#00ff00", speed=5)
    rend.key_pressed(3, 10, t=5.0)
    near = rend.device_frame("keyboard", 5.0)
    later = rend.device_frame("keyboard", 5.3)
    assert near[3 * 22 + 10][1] > 200
    assert later[3 * 22 + 10][1] < near[3 * 22 + 10][1]  # center fades as the ring moves out
    assert any(later[3 * 22 + x][1] > 50 for x in (13, 14, 15, 16, 17))


def test_ambient_uses_screen() -> None:
    assert set(r("ambient").device_frame("keypad", 1)) == {(10, 20, 30)}
    assert set(Renderer(Profile(effect="ambient")).device_frame("keypad", 1)) == {(0, 0, 0)}


def test_keymap_positions() -> None:
    assert key_position(0x1B) == (0, 1)       # Esc
    assert key_position(0x41) == (3, 2)       # A
    assert key_position(0x20) == (5, 7)       # Space
    assert key_position(0x0D) == (3, 14)      # Enter
    assert key_position(0x0D, extended=True) == (4, 21)  # numpad Enter
    assert key_position(0x70) == (0, 3)       # F1
    assert key_position(0xFF) is None


def test_default_profiles_cover_all_effects() -> None:
    assert {p.effect for p in default_profiles()} == set(EFFECTS)
    assert len({p.name for p in default_profiles()}) == len(default_profiles())
