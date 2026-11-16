"""Tests from the test-engineer review: each catches a fix no other test did."""
from __future__ import annotations

import json
import logging
import os
import time

from tuyachroma import config as config_mod
from tuyachroma import engine, main, scan
from tuyachroma.config import AppConfig, Device
from tuyachroma.player import EffectPlayer
from tuyachroma.profiles import Profile
from tuyachroma.razer import DEVICES, ChromaApps, Frame
from tuyachroma.render import Renderer

OWN = os.getpid()


def _wait(cond, t=4.0):
    end = time.time() + t
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_service_ignores_a_crashed_game_still_shown_by_synapse(monkeypatch) -> None:
    from tuyachroma import winsys
    from tuyachroma.razer_out import NativeChroma

    from test_fullsync import FakeDll, Recorder

    monkeypatch.setattr(winsys, "process_alive", lambda pid: False)  # game.exe crashed

    class Reader:
        def __init__(self, sources):
            pass

        def wait(self, ms):
            time.sleep(0.005)

        def read(self, src):  # its last frame stays in Synapse's memory
            return Frame(src, 6, [(255, 0, 0)] * DEVICES[src].count, 1)

        def read_apps(self):
            return ChromaApps(current_pid=77, apps={OWN: "x.exe", 77: "game.exe"})

        def close(self):
            pass

    service = engine.SyncService(
        reader_factory=Reader, worker_factory=lambda c: Recorder(c), service_check=lambda: True,
        output_factory=lambda: NativeChroma(FakeDll()), own_pid=OWN, screen_factory=lambda: None)
    cfg = AppConfig(devices=[Device(id="a", name="a", local_key="k", ip="1.1.1.1", enabled=True)])
    cfg.settings.idle = "full"
    service.start(cfg)
    try:
        assert _wait(lambda: service.player is not None)
        end = time.time() + 1.0
        while time.time() < end:
            assert not service.game_active
            time.sleep(0.02)
    finally:
        service.stop()


def test_sdk_is_reopened_after_the_back_off(monkeypatch) -> None:
    made = []

    class FailsOnce:
        def __init__(self):
            made.append(self)

        def send_frames(self, frames):
            if len(made) == 1:
                raise OSError("went away")

        def close(self):
            pass

    monkeypatch.setattr(EffectPlayer, "SEND_RETRY", 0.2)
    player = EffectPlayer(Renderer(Profile()), 1, output_factory=FailsOnce, fps=20)
    player.start()
    try:
        assert _wait(lambda: len(made) >= 2, 3.0)
    finally:
        player.stop()


def test_presses_kept_while_bulbs_render_far_ahead() -> None:
    rend = Renderer(Profile(effect="reactive", color1="#ff0000", speed=5))
    now = time.monotonic()
    rend.key_pressed(3, 2, t=now)
    rend.device_frame("keyboard", now + 3.0)  # latency up to 3 s
    assert rend.device_frame("keyboard", now + 0.1)[3 * 22 + 2] != (0, 0, 0)


def test_float_saved_for_int_setting_is_rounded(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"settings": {"latency_ms": 450.6}}), encoding="utf-8")
    s = config_mod.load(path).settings
    assert s.latency_ms == 451 and isinstance(s.latency_ms, int)


def test_setup_logging_masks_secrets_and_quiets_cloud_sdk(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(main, "config_dir", lambda: tmp_path)
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    root.handlers = []
    try:
        main.setup_logging(verbose=True)
        logging.getLogger("tuyachroma.x").error("local_key=%s", "SECRETKEY99")
        assert logging.getLogger("tuya_sharing").getEffectiveLevel() >= logging.WARNING
        for h in root.handlers:
            h.flush()
    finally:
        for h in root.handlers:
            h.close()
        root.handlers, _ = saved
        root.setLevel(saved[1])
        logging.getLogger("tuya_sharing").setLevel(logging.NOTSET)
        logging.getLogger("urllib3").setLevel(logging.NOTSET)
        logging.getLogger("tinytuya").setLevel(logging.NOTSET)
    text = (tmp_path / "log.txt").read_text(encoding="utf-8") + capsys.readouterr().err
    assert "local_key" in text and "SECRETKEY99" not in text


def test_scan_drops_spoofed_broadcast(monkeypatch) -> None:
    payload = json.dumps({"gwId": "b1", "ip": "192.168.1.20", "version": "3.3"}).encode()
    sent = [(payload, ("192.168.1.66", 6667)), (payload, ("192.168.1.20", 6667))]

    class Sock:
        def __init__(self, *a):
            pass

        setsockopt = bind = settimeout = close = lambda self, *a: None

        def recvfrom(self, n):
            if sent:
                data, addr = sent.pop(0)
                return data, addr
            raise OSError

    monkeypatch.setattr(scan.socket, "socket", Sock)
    monkeypatch.setattr(scan.tinytuya, "decrypt_udp", lambda d: d.decode())
    from tinytuya import scanner
    monkeypatch.setattr(scanner, "send_discovery_request", lambda *a, **k: None)
    # first datagram comes from .66 speaking for .20: must not be accepted
    found = scan.scan(0.3)
    assert found == {"b1": {"id": "b1", "ip": "192.168.1.20", "version": "3.3"}}
    sent[:] = [(payload, ("192.168.1.66", 6667))]
    assert scan.scan(0.3) == {}


def test_broadcast_with_unknown_version_is_left_to_the_connection(monkeypatch) -> None:
    monkeypatch.setattr(scan.tinytuya, "decrypt_udp", lambda d: d.decode())
    info = scan.parse_broadcast(json.dumps({"gwId": "b", "ip": "10.0.0.2", "version": "9.9"}).encode())
    assert info is not None and info["version"] == ""
