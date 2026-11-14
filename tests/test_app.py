"""Config, Tuya account, sync engine and GUI flows."""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tuyachroma import config as cfgmod
from tuyachroma.cloud import CloudDevice, TuyaAccount, TuyaCloudError
from tuyachroma.config import AppConfig, Device, load, save
from tuyachroma.engine import SyncService, dispatch
from tuyachroma.razer import Frame, RazerUnavailable


def dev(i: str, **kw) -> Device:
    base = dict(id=i, name=f"Lamp {i}", local_key="0123456789abcdef", ip=f"10.0.0.{len(i)}")
    base.update(kw)
    return Device(**base)


# ------------------------------------------------------------------ config


def test_config_roundtrip(tmp_path) -> None:
    config = AppConfig(devices=[dev("a", enabled=True, zone="left"), dev("b")], username="me")
    config.settings.idle = "keep"
    path = tmp_path / "config.json"
    save(config, path)
    loaded = load(path)
    assert loaded == config
    assert load(tmp_path / "missing.json") == AppConfig()


def test_merge_cloud_keeps_settings_and_apply_scan() -> None:
    config = AppConfig(devices=[dev("a", enabled=True, source="mouse", local_key="old")])
    config.devices.append(dev("gone"))
    new = config.merge_cloud([
        CloudDevice("a", "Renamed", "newkey", "dj", "Bulb", rgb=True),
        CloudDevice("b", "Strip", "key2", "dd", "Strip", rgb=True),
        CloudDevice("w", "White bulb", "key3", "dj", "Bulb", rgb=False),
        CloudDevice("p", "Plug", "key4", "cz", "Plug", rgb=False),
    ])
    assert new == 1
    assert [d.id for d in config.devices] == ["a", "b"]  # only RGB lights, stale removed
    a = config.device("a")
    assert (a.name, a.local_key, a.enabled, a.source) == ("Renamed", "newkey", True, "mouse")
    b = config.device("b")
    assert not b.enabled and b.ip == ""
    assert config.apply_scan({"b": {"ip": "10.0.0.9", "version": "3.5"}}) == 1
    assert (b.ip, b.version) == ("10.0.0.9", "3.5")
    assert [d.id for d in config.enabled_devices] == ["a"]


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows only")
def test_local_keys_encrypted_on_disk(tmp_path) -> None:
    path = tmp_path / "c.json"
    save(AppConfig(devices=[dev("a")]), path)
    text = path.read_text(encoding="utf-8")
    assert "0123456789abcdef" not in text and "dpapi:" in text
    assert load(path).devices[0].local_key == "0123456789abcdef"


# ----------------------------------------------------------------- account


def test_account_login_flow() -> None:
    control = MagicMock()
    control.qr_code.return_value = {"success": True, "result": {"qrcode": "tok"}}
    control.login_result.side_effect = [
        (False, {"msg": "not yet"}),
        (True, {"t": 1, "uid": "u", "expire_time": 1, "access_token": "a", "refresh_token": "r",
                "terminal_id": "t", "endpoint": "https://apigw.tuyaeu.com", "username": "me@x"}),
    ]
    manager = MagicMock()
    def cust(i, name, cat, codes, key="k", sub=False):
        return SimpleNamespace(id=i, name=name, local_key=key, category=cat, product_name="",
                               sub=sub, function={c: None for c in codes}, status_range={}, status={})

    manager.device_map = {
        "1": cust("1", "Lamp RGB", "dj", ["switch_led", "work_mode", "colour_data_v2"]),
        "2": cust("2", "Plug", "cz", ["switch_1"]),
        "3": cust("3", "Zigbee", "dj", ["colour_data"], key="", sub=True),
        "4": cust("4", "White lamp", "dj", ["switch_led", "bright_value_v2", "temp_value_v2"]),
        "5": cust("5", "LED strip", "dd", ["switch_led", "colour_data"]),
        "6": cust("6", "Old bulb", "dj", []),  # no data points: trust the category
    }
    with patch("tuya_sharing.LoginControl", return_value=control), \
         patch("tuya_sharing.Manager", return_value=manager) as mgr:
        account = TuyaAccount()
        assert account.request_qr(" CODE ") == "tuyaSmart--qrLogin?token=tok"
        assert account.poll_login() is None
        info = account.poll_login()
        devices = account.fetch_devices(info)
    assert account.username == "me@x" and account.user_code == "CODE"
    rgb = {d.name: d.rgb for d in devices}
    assert rgb == {"Lamp RGB": True, "LED strip": True, "Old bulb": True,
                   "Plug": False, "White lamp": False}  # no sub-devices
    assert mgr.call_args.args[2:4] == ("t", "https://apigw.tuyaeu.com")


def test_account_bad_user_code() -> None:
    control = MagicMock()
    control.qr_code.return_value = {"success": False, "msg": "user code invalid", "code": 1}
    with patch("tuya_sharing.LoginControl", return_value=control):
        with pytest.raises(TuyaCloudError, match="user code invalid"):
            TuyaAccount().request_qr("bad")


# ------------------------------------------------------------------ engine


class Recorder:
    def __init__(self, cfg):
        self.cfg = cfg
        self.calls = []
        self.status = "ready"
        self.started = self.stopped = False

    def set_color(self, rgb):
        self.calls.append(rgb)

    def release(self):
        self.calls.append("release")

    def start(self):
        self.started = True

    def request_stop(self):
        self.calls.append("stop")
        self.stopped = True

    def join(self, timeout=None):
        pass


def test_dispatch() -> None:
    w = Recorder(dev("a", source="keyboard").bulb_config())
    colors = [(20, 20, 20)] * 120 + [(0, 0, 255)] * 12
    assert dispatch([w], {"keyboard": Frame("keyboard", 7, colors, 1)}, "restore")
    assert w.calls[-1][2] > 200
    assert not dispatch([w], {"keyboard": Frame("keyboard", 13, None, 2)}, "restore")
    assert w.calls[-1] == "release"
    n = len(w.calls)
    dispatch([w], {"keyboard": None}, "keep")
    assert len(w.calls) == n  # keep: nothing sent


class FakeReader:
    instances = []

    def __init__(self, sources):
        self.sources = sources
        self.closed = False
        FakeReader.instances.append(self)

    def wait(self, ms):
        time.sleep(0.01)

    def read(self, src):
        return Frame(src, 6, [(255, 0, 0)] * 132, 5)

    def close(self):
        self.closed = True


def test_sync_service_lifecycle() -> None:
    FakeReader.instances.clear()
    workers = []

    def worker_factory(cfg):
        w = Recorder(cfg)
        workers.append(w)
        return w

    service = SyncService(reader_factory=FakeReader, worker_factory=worker_factory,
                          service_check=lambda: True)
    config = AppConfig(devices=[dev("a", enabled=True), dev("b", enabled=True, source="mouse"),
                                dev("c"), dev("d", enabled=True, ip="")])
    service.start(config)
    assert [w.cfg.id for w in workers] == ["a", "b"]  # only enabled bulbs with an IP
    deadline = time.time() + 2
    while time.time() < deadline and not service.game_active:
        time.sleep(0.01)
    assert service.game_active and service.razer_status == "efekt z gry aktywny"
    assert FakeReader.instances[0].sources == ["keyboard", "mouse"]
    assert workers[0].calls and workers[0].calls[0][0] > 200
    service.stop()
    assert all(w.stopped for w in workers)
    assert FakeReader.instances[0].closed and not service.running


def test_sync_service_waits_for_razer() -> None:
    def no_razer(_sources):
        raise RazerUnavailable("Synapse not running")

    service = SyncService(reader_factory=no_razer, worker_factory=Recorder, service_check=lambda: False)
    service.start(AppConfig(devices=[dev("a", enabled=True)]))
    time.sleep(0.1)
    assert "Synapse not running" in service.razer_status
    service.stop()
    assert service.razer_status == "zatrzymane"


# --------------------------------------------------------------------- GUI


def _tk_available() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_gui_login_and_select(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import cloud, gui

    class FakeAccount:
        user_code = None

        def __init__(self):
            self.polls = 0

        def request_qr(self, code):
            self.user_code = code
            return "tuyaSmart--qrLogin?token=x"

        def poll_login(self):
            self.polls += 1
            return {"username": "me@x"} if self.polls > 1 else None

        def fetch_devices(self, info):
            return [CloudDevice("bf1", "Lamp", "k" * 16, "dj", "Bulb", rgb=True),
                    CloudDevice("bf2", "White", "k" * 16, "dj", "Bulb", rgb=False)]

    monkeypatch.setattr(cloud, "TuyaAccount", FakeAccount)
    monkeypatch.setattr(gui, "scan", lambda _s: {"bf1": {"id": "bf1", "ip": "10.0.0.5", "version": "3.3"}})
    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    app = gui.App(AppConfig(), service, start_hidden=False, use_tray=False)
    try:
        app.user_code_var.set("CODE")
        app.start_login()
        deadline = time.time() + 15
        while time.time() < deadline and not app.config.devices or (
            app.config.devices and not app.config.devices[0].ip and time.time() < deadline
        ):
            app.root.update()
            time.sleep(0.02)
        assert app.config.username == "me@x"
        assert app.config.devices[0].ip == "10.0.0.5"
        assert app.tree.exists("bf1") and not app.tree.exists("bf2")

        # Click the ✓ column to hand the lamp to Razer
        app.config.devices[0].enabled = False
        app.tree.selection_set("bf1")
        app._load_editor()
        app.ed_enabled.set(True)
        app.ed_zone.set(gui.ZONE_LABELS["right"])
        app.save_editor()
        deadline = time.time() + 5
        while time.time() < deadline and not service.workers:
            app.root.update()
            time.sleep(0.02)
        assert [w.cfg.zone for w in service.workers] == ["right"]
        saved = load(cfgmod.default_path())
        assert saved.devices[0].enabled and saved.devices[0].zone == "right"
    finally:
        service.stop()
        app.root.destroy()


def test_bulbs_return_to_ha_when_synapse_dies(monkeypatch) -> None:
    from tuyachroma import engine

    monkeypatch.setattr(engine, "SERVICE_CHECK_INTERVAL", 0.05)
    FakeReader.instances.clear()
    alive = {"v": True}
    workers = []

    def worker_factory(cfg):
        w = Recorder(cfg)
        workers.append(w)
        return w

    def reader_factory(sources):
        if not alive["v"]:
            raise RazerUnavailable("RzSDKService nie działa")
        return FakeReader(sources)

    service = SyncService(reader_factory=reader_factory, worker_factory=worker_factory,
                          service_check=lambda: alive["v"])
    service.start(AppConfig(devices=[dev("a", enabled=True)]))
    deadline = time.time() + 2
    while time.time() < deadline and not service.game_active:
        time.sleep(0.01)
    assert service.game_active
    alive["v"] = False  # Synapse crashes; shared memory still holds the last game color
    deadline = time.time() + 2
    while time.time() < deadline and service.game_active:
        time.sleep(0.01)
    assert not service.game_active
    assert workers[0].calls[-1] == "release"
    assert FakeReader.instances[0].closed
    time.sleep(0.2)
    assert not service.game_active and "nie działa" in service.razer_status
    service.stop()
