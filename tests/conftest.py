"""Fixtures for Tuya Chroma tests."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

import pytest

from custom_components.tuya_chroma.tuya_device import TuyaLightState

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations in every test."""
    yield


@pytest.fixture(autouse=True)
def no_settle_delay():
    with patch("custom_components.tuya_chroma.chroma.SESSION_SETTLE_DELAY", 0):
        yield


class FakeTuyaLight:
    """In-memory stand-in for TuyaLight (an RGB+CCT bulb)."""

    instances: list[FakeTuyaLight] = []

    def __init__(self, *args) -> None:
        self.args = args
        self.calls: list[tuple] = []
        self.state = TuyaLightState(
            is_on=False,
            mode="white",
            brightness=1.0,
            colour_temp=0.0,
            hs_color=(0.0, 0.0),
            colour_value=1.0,
            supports_colour=True,
            supports_colour_temp=True,
            supports_brightness=True,
        )
        FakeTuyaLight.instances.append(self)

    def get_state(self) -> TuyaLightState:
        return self.state

    def turn_on(self) -> None:
        self.calls.append(("turn_on",))
        self.state = replace(self.state, is_on=True)

    def turn_off(self) -> None:
        self.calls.append(("turn_off",))
        self.state = replace(self.state, is_on=False)

    def set_hs(self, hue, sat, value) -> None:
        self.calls.append(("set_hs", hue, sat, value))
        self.state = replace(
            self.state, is_on=True, mode="colour", hs_color=(hue, sat), colour_value=value
        )

    def set_white(self, brightness, colour_temp) -> None:
        self.calls.append(("set_white", brightness, colour_temp))
        self.state = replace(
            self.state,
            is_on=True,
            mode="white",
            brightness=brightness,
            colour_temp=self.state.colour_temp if colour_temp is None else colour_temp,
        )


@pytest.fixture
def fake_tuya():
    FakeTuyaLight.instances.clear()
    with (
        patch("custom_components.tuya_chroma.TuyaLight", FakeTuyaLight),
        patch("custom_components.tuya_chroma.config_flow.TuyaLight", FakeTuyaLight),
    ):
        yield FakeTuyaLight
