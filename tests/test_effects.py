import colorsys

from tuyachroma.effects import EffectSettings, effect_color, parse_hex


def hue(rgb):
    return colorsys.rgb_to_hsv(*(c / 255 for c in rgb))[0]


def test_wave_moves_across_bulbs() -> None:
    fx = EffectSettings(effect="wave", period=8)
    a, b = effect_color(fx, 0, 0, 4), effect_color(fx, 0, 1, 4)
    assert a != b  # bulbs are at different points of the wave
    # bulb 1 shows now what bulb 0 showed a quarter cycle earlier
    assert effect_color(fx, 2, 1, 4) == effect_color(fx, 0, 0, 4)
    assert max(effect_color(fx, 1.3, 2, 4)) == 255  # fully saturated, full brightness


def test_spectrum_all_bulbs_equal_and_cycling() -> None:
    fx = EffectSettings(effect="spectrum", period=10)
    assert effect_color(fx, 3, 0, 3) == effect_color(fx, 3, 2, 3)
    assert abs(hue(effect_color(fx, 5, 0, 1)) - 0.5) < 0.01
    assert effect_color(fx, 0, 0, 1) == effect_color(fx, 10, 0, 1)


def test_breathing_and_static() -> None:
    fx = EffectSettings(effect="breathing", color="#ff0000", period=4)
    low, high = effect_color(fx, 0), effect_color(fx, 2)
    assert high == (255, 0, 0) and 0 < low[0] < 40
    st = EffectSettings(effect="static", color="#0080ff", brightness=0.5)
    assert effect_color(st, 123) == (0, 64, 128)
    assert parse_hex("bad") == (68, 214, 44)


def test_effect_changes_at_most_once_per_second() -> None:
    fx = EffectSettings(effect="wave", period=8)
    # 20 frames per second for 10 s -> about 10 different colors, not 200
    colors = [effect_color(fx, i / 20, 0, 1) for i in range(200)]
    changes = sum(1 for a, b in zip(colors, colors[1:]) if a != b)
    assert changes <= 10
