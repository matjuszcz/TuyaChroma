"""Minimal async client for the Razer Chroma SDK REST API.

The Chroma SDK REST server is provided by Razer Synapse on Windows:
``POST http://<host>:54235/razer/chromasdk`` creates a session and returns a
session URI (on its own port), which then accepts per-device effects.
A session has to be kept alive with a heartbeat at least every 15 seconds,
otherwise Synapse drops it and returns control to the profile lighting.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=5)
# Synapse needs a moment before a new session accepts effects.
SESSION_SETTLE_DELAY = 1.5


class ChromaError(Exception):
    """Raised when the Chroma SDK returns an error or cannot be reached."""


def rgb_to_bgr_int(red: int, green: int, blue: int) -> int:
    """Convert RGB to the BGR integer used by the Chroma SDK."""
    return (blue & 0xFF) << 16 | (green & 0xFF) << 8 | (red & 0xFF)


class ChromaClient:
    """Keeps one Chroma SDK session alive and applies static colors."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        devices: list[str],
    ) -> None:
        self._http = session
        self._host = host
        self._port = port
        self._devices = devices
        self._uri: str | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        """Return True if a session is currently open."""
        return self._uri is not None

    @property
    def base_url(self) -> str:
        """Return the URL of the SDK init endpoint."""
        return f"http://{self._host}:{self._port}/razer/chromasdk"

    async def _request(
        self, method: str, url: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            async with self._http.request(
                method, url, json=payload, timeout=REQUEST_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise ChromaError(f"{method} {url} failed: {err}") from err
        if not isinstance(data, dict):
            return {}
        result = data.get("result")
        if result not in (None, 0):
            raise ChromaError(f"{method} {url} returned result={result}")
        return data

    async def async_probe(self) -> None:
        """Check that the SDK server answers (used by the config flow)."""
        await self._request("GET", self.base_url)

    async def async_connect(self) -> None:
        """Create a new SDK session."""
        async with self._lock:
            await self._async_connect_locked()

    async def _async_connect_locked(self) -> None:
        payload = {
            "title": "Home Assistant",
            "description": "Tuya Chroma integration for Home Assistant",
            "author": {
                "name": "Home Assistant",
                "contact": "https://www.home-assistant.io",
            },
            "device_supported": self._devices,
            "category": "application",
        }
        data = await self._request("POST", self.base_url, payload)
        uri = data.get("uri")
        if not uri:
            raise ChromaError(f"Chroma SDK did not return a session uri: {data}")
        # Synapse returns "http://localhost:<port>/chromasdk"; when Home
        # Assistant runs on another machine, keep the port/path but use our host.
        parts = urlsplit(uri)
        netloc = f"{self._host}:{parts.port}" if parts.port else self._host
        self._uri = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
        _LOGGER.debug("Chroma session created: %s", self._uri)
        await asyncio.sleep(SESSION_SETTLE_DELAY)

    async def async_heartbeat(self) -> None:
        """Keep the session alive, reconnecting if it was lost."""
        async with self._lock:
            if self._uri is None:
                await self._async_connect_locked()
                return
            try:
                await self._request("PUT", f"{self._uri}/heartbeat")
            except ChromaError:
                self._uri = None
                raise

    async def async_set_color(
        self, device: str, rgb: tuple[int, int, int] | None
    ) -> None:
        """Set a static color on one device, or turn it off with None."""
        if rgb is None:
            payload: dict[str, Any] = {"effect": "CHROMA_NONE"}
        else:
            payload = {
                "effect": "CHROMA_STATIC",
                "param": {"color": rgb_to_bgr_int(*rgb)},
            }
        async with self._lock:
            if self._uri is None:
                await self._async_connect_locked()
            assert self._uri is not None
            try:
                await self._request("PUT", f"{self._uri}/{device}", payload)
            except ChromaError:
                self._uri = None
                raise

    async def async_disconnect(self) -> None:
        """Close the session; Synapse then restores the profile lighting."""
        async with self._lock:
            if self._uri is None:
                return
            uri, self._uri = self._uri, None
            try:
                await self._request("DELETE", uri)
            except ChromaError as err:
                _LOGGER.debug("Error closing Chroma session: %s", err)
