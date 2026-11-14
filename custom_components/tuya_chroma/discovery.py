"""Find Tuya devices on the LAN (IP address and protocol version).

Tuya devices broadcast a small UDP packet every few seconds on ports
6666 (protocol 3.1), 6667 (3.3+) and answer discovery requests on 7000.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import tinytuya

_LOGGER = logging.getLogger(__name__)

DISCOVERY_PORTS = (6666, 6667, 7000)
DEFAULT_DISCOVERY_TIME = 8.0


def parse_broadcast(data: bytes) -> dict[str, Any] | None:
    """Decode one UDP broadcast; returns {'id','ip','version'} or None."""
    try:
        payload = json.loads(tinytuya.decrypt_udp(data))
    except Exception:  # noqa: BLE001 - foreign/garbled packets are expected
        return None
    if not isinstance(payload, dict):
        return None
    dev_id = payload.get("gwId") or payload.get("id")
    ip = payload.get("ip")
    if not dev_id or not ip:
        return None
    return {"id": dev_id, "ip": ip, "version": str(payload.get("version") or "")}


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, found: dict[str, dict[str, Any]], wanted: set[str] | None, done: asyncio.Event) -> None:
        self._found = found
        self._wanted = wanted
        self._done = done

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if (info := parse_broadcast(data)) is None:
            return
        self._found[info["id"]] = info
        if self._wanted and self._wanted.issubset(self._found):
            self._done.set()


async def async_discover(
    wanted_ids: set[str] | None = None, timeout: float = DEFAULT_DISCOVERY_TIME
) -> dict[str, dict[str, Any]]:
    """Listen for Tuya broadcasts; returns {device_id: {'ip', 'version'}}."""
    loop = asyncio.get_running_loop()
    found: dict[str, dict[str, Any]] = {}
    done = asyncio.Event()
    transports = []
    for port in DISCOVERY_PORTS:
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _Protocol(found, wanted_ids, done),
                local_addr=("0.0.0.0", port),
                reuse_port=True,
                allow_broadcast=True,
            )
            transports.append(transport)
        except OSError as err:
            _LOGGER.debug("Cannot listen on UDP %s: %s", port, err)
    if not transports:
        _LOGGER.warning("Tuya LAN discovery unavailable: no UDP port could be opened")
        return found

    # Newer (3.5) firmware only answers when asked.
    try:
        from tinytuya import scanner  # noqa: PLC0415

        await loop.run_in_executor(None, scanner.send_discovery_request)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not send discovery request: %s", err)

    try:
        await asyncio.wait_for(done.wait(), timeout)
    except TimeoutError:
        pass
    finally:
        for transport in transports:
            transport.close()
    _LOGGER.debug("Tuya LAN discovery found: %s", found)
    return found
