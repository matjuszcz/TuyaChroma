"""Config, Tuya account, sync engine and GUI flows."""

from __future__ import annotations

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


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_minimize_and_close_go_to_tray(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import gui

    notes = []

    class FakeTray:
        def notify(self, msg, title):
            notes.append(msg)

        def stop(self):
            pass

    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    app = gui.App(AppConfig(), service, start_hidden=False, use_tray=False)
    app._tray = FakeTray()
    try:
        app.root.update()
        if sys.platform == "win32":
            app.root.iconify()  # real minimize button
        else:  # no window manager under Xvfb: simulate the minimize event
            real_state = app.root.state
            monkeypatch.setattr(app.root, "state", lambda *a: "iconic" if not a else real_state(*a))
            app._on_unmap(SimpleNamespace(widget=app.root))
            monkeypatch.setattr(app.root, "state", real_state)
        for _ in range(50):
            app.root.update()
            time.sleep(0.01)
        assert app.root.state() == "withdrawn"
        assert len(notes) == 1  # hint shown once
        app.show()
        app.root.update()
        app.on_close()  # X button
        app.root.update()
        assert app.root.state() == "withdrawn" and len(notes) == 1
        assert not app._quitting  # still running
    finally:
        app.root.destroy()


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_gui_shows_login_error_and_validates_version(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import cloud, gui

    class BadAccount:
        def request_qr(self, code):
            raise TuyaCloudError("USER_CODE_INVALID (kod 1106)")

    monkeypatch.setattr(cloud, "TuyaAccount", BadAccount)
    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    config = AppConfig(devices=[dev("a")])
    app = gui.App(config, service, start_hidden=False, use_tray=False)
    try:
        app.user_code_var.set("WRONG")
        app.start_login()
        deadline = time.time() + 5
        while time.time() < deadline and "Błąd" not in app.login_var.get():
            app.root.update()
            time.sleep(0.02)
        assert "USER_CODE_INVALID" in app.login_var.get()

        # a callback that raises must not stop the UI loop
        app._queue.put(lambda: 1 / 0)
        app._queue.put(lambda: app.set_busy("still alive"))
        deadline = time.time() + 3
        while time.time() < deadline and app.busy_var.get() != "still alive":
            app.root.update()
            time.sleep(0.02)
        assert app.busy_var.get() == "still alive"

        app.tree.selection_set("a")
        app._load_editor()
        app.ed_version.set("3,4")
        app.save_editor()
        assert config.devices[0].version == "3.4"
        app.ed_version.set("abc")
        app.save_editor()
        assert config.devices[0].version == ""
    finally:
        service.stop()
        app.root.destroy()


def test_effect_when_razer_runs_without_game() -> None:
    from tuyachroma.effects import EffectSettings

    a = Recorder(dev("a", min_brightness=0).bulb_config())
    b = Recorder(dev("b", min_brightness=0).bulb_config())
    idle_frame = {"keyboard": Frame("keyboard", 13, None, 1)}  # Synapse running, no game
    fx = EffectSettings(effect="wave", period=8)
    assert not dispatch([a, b], idle_frame, "effect", fx, now=0.0)
    assert a.calls[-1] != b.calls[-1] and max(a.calls[-1]) > 200  # wave across bulbs
    # a game starts: its colors win over the effect
    game = {"keyboard": Frame("keyboard", 6, [(0, 0, 255)] * 132, 2)}
    assert dispatch([a, b], game, "effect", fx, now=1.0)
    assert a.calls[-1] == (0, 0, 255) == b.calls[-1]


def test_effect_mode_service_and_return_to_ha(monkeypatch) -> None:
    from tuyachroma import engine

    monkeypatch.setattr(engine, "SERVICE_CHECK_INTERVAL", 0.05)

    class IdleReader(FakeReader):
        def read(self, src):
            return Frame(src, 13, None, 5)  # no game

    alive = {"v": True}

    def reader_factory(sources):
        if not alive["v"]:
            raise RazerUnavailable("RzSDKService nie działa")
        return IdleReader(sources)

    workers = []

    def worker_factory(cfg):
        workers.append(Recorder(cfg))
        return workers[-1]

    service = SyncService(reader_factory=reader_factory, worker_factory=worker_factory,
                          service_check=lambda: alive["v"])
    config = AppConfig(devices=[dev("a", enabled=True)])
    assert config.settings.idle == "effect" and config.settings.effect.effect == "wave"
    service.start(config)
    deadline = time.time() + 2
    while time.time() < deadline and not workers[0].calls:
        time.sleep(0.01)
    assert isinstance(workers[0].calls[0], tuple)  # bulb shows the wave
    assert "efekt" in service.razer_status and not service.game_active
    alive["v"] = False  # computer shutting down / Synapse closed
    deadline = time.time() + 2
    while time.time() < deadline and workers[0].calls[-1] != "release":
        time.sleep(0.01)
    assert workers[0].calls[-1] == "release"  # back to Home Assistant
    service.stop()


def test_effect_settings_roundtrip(tmp_path) -> None:
    config = AppConfig()
    config.settings.effect.effect = "breathing"
    config.settings.effect.color = "#ff0000"
    config.settings.effect.period = 3.0
    save(config, tmp_path / "c.json")
    assert load(tmp_path / "c.json").settings.effect == config.settings.effect


def test_migrates_v1_0_default_idle(tmp_path) -> None:
    import json

    path = tmp_path / "c.json"
    path.write_text(json.dumps({"settings": {"idle": "restore", "fps": 20}, "devices": []}))
    assert load(path).settings.idle == "effect"  # 1.0 default -> new default
    path.write_text(json.dumps({"settings": {"idle": "restore", "effect": {"effect": "static"}},
                                "devices": []}))
    loaded = load(path)
    assert loaded.settings.idle == "restore" and loaded.settings.effect.effect == "static"


def test_pause_is_not_kept_after_restart(tmp_path, monkeypatch) -> None:
    from tuyachroma import main as mainmod

    config = AppConfig(devices=[dev("a", enabled=True)])
    config.settings.sync_enabled = False  # user paused, then restarted the PC
    started = {}

    class FakeService:
        def start(self, cfg):
            started["sync_enabled"] = cfg.settings.sync_enabled

        def stop(self, timeout=5):
            pass

    class FakeApp:
        def __init__(self, *a, **kw):
            pass

        def mainloop(self):
            pass

    monkeypatch.setattr(mainmod, "load", lambda: config)
    monkeypatch.setattr(mainmod, "SyncService", FakeService)
    monkeypatch.setattr(mainmod, "setup_logging", lambda v: None)
    monkeypatch.setattr(mainmod.winsys, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(mainmod.winsys, "set_autostart", lambda *a: None)
    monkeypatch.setattr(mainmod.winsys, "start_system_event_listener", lambda **kw: None)
    import tuyachroma.gui as guimod

    monkeypatch.setattr(guimod, "App", FakeApp)
    assert mainmod.main(["--minimized"]) == 0
    assert started == {"sync_enabled": True}
