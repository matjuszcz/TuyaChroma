"""Find Tuya devices on the LAN (id, IP, protocol version)."""

from __future__ import annotations

import ipaddress
import json
import socket
import time

import tinytuya

PORTS = (6666, 6667, 7000)
VERSIONS = ("", "3.1", "3.2", "3.3", "3.4", "3.5")


def parse_broadcast(data: bytes) -> dict | None:
    try:
        payload = json.loads(tinytuya.decrypt_udp(data))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    dev_id = payload.get("gwId") or payload.get("id")
    if not dev_id or not payload.get("ip"):
        return None
    version = str(payload.get("version") or "")
    if version not in VERSIONS:
        version = ""  # unknown: let the connection find it
    return {"id": dev_id, "ip": str(payload["ip"]), "version": version}


def trusted(info: dict, sender: str) -> bool:
    """Anyone on the LAN can send a broadcast (its key is public): only accept a
    device announcing its own private address."""
    try:
        return info["ip"] == sender and ipaddress.ip_address(sender).is_private
    except ValueError:
        return False


def scan(seconds: float = 10.0) -> dict[str, dict]:
    socks = []
    for port in PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.bind(("", port))
        except OSError:
            s.close()
            continue
        s.settimeout(0.2)
        socks.append(s)
    try:
        from tinytuya import scanner  # noqa: PLC0415

        scanner.send_discovery_request()
    except Exception:  # noqa: BLE001
        pass
    found: dict[str, dict] = {}
    end = time.monotonic() + seconds
    while time.monotonic() < end and socks:
        for s in socks:
            try:
                data, (sender, _port) = s.recvfrom(4096)
            except (TimeoutError, socket.timeout, OSError):
                continue
            if (info := parse_broadcast(data)) is not None and trusted(info, sender):
                found[info["id"]] = info
    for s in socks:
        s.close()
    return found
