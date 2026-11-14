"""Synchronous wrapper around tinytuya for local Tuya bulbs/LED strips.

All methods here block on network I/O and must be run in an executor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import functools
import logging
from typing import Any, TypeVar

import tinytuya

_LOGGER = logging.getLogger(__name__)


class TuyaError(Exception):
    """Raised when the Tuya device cannot be reached or returns an error."""


@dataclass
class TuyaLightState:
    """Normalized light state."""

    is_on: bool
    mode: str | None  # "white", "colour", "scene", "music" ...
    brightness: float | None  # 0..1 (white mode)
    colour_temp: float | None  # 0..1, 0 = warm, 1 = cold
    hs_color: tuple[float, float] | None  # hue 0..360, sat 0..100
    colour_value: float | None  # 0..1 (colour mode brightness)
    supports_colour: bool
    supports_colour_temp: bool
    supports_brightness: bool


_T = TypeVar("_T")


def _wrap_errors(func: Callable[..., _T]) -> Callable[..., _T]:
    """Turn low-level tinytuya/socket exceptions into TuyaError."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        try:
            return func(*args, **kwargs)
        except TuyaError:
            raise
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as err:
            raise TuyaError(f"{type(err).__name__}: {err}") from err

    return wrapper


def _check(result: Any) -> Any:
    if isinstance(result, dict) and "Error" in result:
        raise TuyaError(f"{result.get('Error')} (code {result.get('Err')})")
    return result


class TuyaLight:
    """A local Tuya light controlled with tinytuya.BulbDevice."""

    def __init__(self, device_id: str, host: str, local_key: str, version: str) -> None:
        self._dev = tinytuya.BulbDevice(
            dev_id=device_id,
            address=host,
            local_key=local_key,
            version=float(version),
            connection_timeout=3,
            connection_retry_limit=2,
            connection_retry_delay=1,
        )

    def _value_range(self) -> tuple[int, int]:
        dpset = self._dev.dpset
        vmin = dpset.get("value_min") or 0
        vmax = dpset.get("value_max") or 255
        if vmin < 0 or vmax <= 0:
            return 0, 255
        return vmin, vmax

    def _to_fraction(self, raw: Any, use_min: bool = True) -> float | None:
        if raw is None:
            return None
        vmin, vmax = self._value_range()
        if not use_min:
            vmin = 0
        return max(0.0, min(1.0, (int(raw) - vmin) / (vmax - vmin)))

    def _from_fraction(self, value: float, use_min: bool = True) -> int:
        vmin, vmax = self._value_range()
        if not use_min:
            vmin = 0
        value = max(0.0, min(1.0, value))
        return int(round(vmin + value * (vmax - vmin)))

    @_wrap_errors
    def get_state(self) -> TuyaLightState:
        """Read the current state from the device."""
        status = _check(self._dev.status())
        if not status or "dps" not in status:
            raise TuyaError(f"No data points in status response: {status}")
        if not self._dev.bulb_configured:
            raise TuyaError("Could not detect bulb type (unsupported device?)")
        state = _check(self._dev.state())

        hs_color = None
        colour_value = None
        hexformat = self._dev.dpset.get("value_hexformat")
        if state.get("colour"):
            try:
                h, s, v = tinytuya.BulbDevice.hexvalue_to_hsv(state["colour"], hexformat)
                hs_color = (h * 360.0, s * 100.0)
                colour_value = v
            except ValueError:
                _LOGGER.debug("Unparsable colour value: %s", state["colour"])

        return TuyaLightState(
            is_on=bool(state.get("is_on")),
            mode=state.get("mode"),
            brightness=self._to_fraction(state.get("brightness")),
            colour_temp=self._to_fraction(state.get("colourtemp"), use_min=False),
            hs_color=hs_color,
            colour_value=colour_value,
            supports_colour=bool(self._dev.dpset.get("colour")),
            supports_colour_temp=bool(self._dev.dpset.get("colourtemp")),
            supports_brightness=bool(self._dev.dpset.get("brightness")),
        )

    @_wrap_errors
    def turn_on(self) -> None:
        _check(self._dev.turn_on())

    @_wrap_errors
    def turn_off(self) -> None:
        _check(self._dev.turn_off())

    @_wrap_errors
    def set_hs(self, hue: float, saturation: float, value: float) -> None:
        """Set colour mode; hue 0..360, saturation 0..100, value 0..1."""
        _check(
            self._dev.set_hsv(
                max(0.0, min(1.0, hue / 360.0)),
                max(0.0, min(1.0, saturation / 100.0)),
                max(0.01, min(1.0, value)),
            )
        )

    @_wrap_errors
    def set_white(self, brightness: float, colour_temp: float | None) -> None:
        """Set white mode; brightness and colour_temp as 0..1 fractions."""
        dps: dict[str, Any] = {}
        dpset = self._dev.dpset
        if dpset.get("switch"):
            dps[dpset["switch"]] = True
        if dpset.get("mode"):
            dps[dpset["mode"]] = tinytuya.BulbDevice.DPS_MODE_WHITE
        if dpset.get("brightness"):
            dps[dpset["brightness"]] = self._from_fraction(brightness)
        if colour_temp is not None and dpset.get("colourtemp"):
            dps[dpset["colourtemp"]] = self._from_fraction(colour_temp, use_min=False)
        _check(self._dev.set_multiple_values(dps))
