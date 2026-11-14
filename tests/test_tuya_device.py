"""Tests for the tinytuya wrapper using a type B (DP 20-26) bulb."""

from __future__ import annotations

from unittest.mock import patch

from custom_components.tuya_chroma.tuya_device import TuyaLight

STATUS_B = {
    "dps": {"20": True, "21": "colour", "22": 500, "23": 1000, "24": "00f003e801f4", "26": 0}
}


def make_light() -> TuyaLight:
    light = TuyaLight("bf123", "192.168.1.60", "0123456789abcdef", "3.3")
    light._dev.status = lambda nowait=False: _status(light)
    light._dev.cached_status = lambda historic=False, nowait=False: STATUS_B
    return light


def _status(light: TuyaLight) -> dict:
    light._dev.detect_bulb(STATUS_B)
    return STATUS_B


def test_get_state_type_b() -> None:
    state = make_light().get_state()
    assert state.is_on and state.mode == "colour"
    assert state.supports_colour and state.supports_colour_temp
    assert state.hs_color == (240.0, 100.0)
    assert state.colour_value == 0.5
    assert state.colour_temp == 1.0
    assert abs(state.brightness - (500 - 10) / 990) < 1e-9


def test_set_white_type_b() -> None:
    light = make_light()
    light.get_state()
    with patch.object(light._dev, "set_multiple_values", return_value={}) as setter:
        light.set_white(1.0, 0.0)
    setter.assert_called_once_with({"20": True, "21": "white", "22": 1000, "23": 0})
