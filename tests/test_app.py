"""Config, Tuya account, sync engine and GUI flows."""

from __future__ import annotations

import functools
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


def test_sync_survives_an_unexpected_error(monkeypatch) -> None:
    """The core loop must never stop syncing for good because of one error."""
    from tuyachroma import engine

    FakeReader.instances.clear()
    workers = []

    def worker_factory(cfg):
        workers.append(Recorder(cfg))
        return workers[-1]

    real_dispatch = engine.dispatch
    calls = {"n": 0}

    def flaky_dispatch(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        return real_dispatch(*args, **kwargs)

    monkeypatch.setattr(engine, "dispatch", flaky_dispatch)
    real_wait = engine.threading.Event.wait
    service = SyncService(reader_factory=FakeReader, worker_factory=worker_factory,
                          service_check=lambda: True)
    monkeypatch.setattr(service._stop, "wait", lambda t=None: real_wait(service._stop, min(t or 0, 0.05)))
    service.start(AppConfig(devices=[dev("a", enabled=True)]))
    try:
        deadline = time.time() + 3
        while time.time() < deadline and calls["n"] < 10:
            time.sleep(0.01)
        assert calls["n"] >= 10  # kept going after the error
        assert "release" in workers[0].calls  # the bulb was given back meanwhile
        assert len(FakeReader.instances) >= 2  # the Razer reader was reopened
        assert service.razer_status == "efekt z gry aktywny"
    finally:
        service.stop()


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


@functools.lru_cache(maxsize=1)
def _tk_available() -> bool:
    # checked once: on Windows, creating many Tk interpreters in one process can fail
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
    app._tray_ready.set()  # the icon is really in the tray
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
        assert app.ed_speed.get() == gui.SPEED_LABELS[0.15]
        app.ed_speed.set(gui.SPEED_LABELS[0.5])  # a bulb that hangs when flooded
        app.save_editor()
        assert config.devices[0].min_interval == 0.5
        assert config.devices[0].bulb_config().min_interval == 0.5
        app._load_editor()
        assert app.ed_speed.get() == gui.SPEED_LABELS[0.5]
    finally:
        service.stop()
        app.root.destroy()


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
    monkeypatch.setattr(mainmod.winsys, "acquire_single_instance", lambda **kw: True)
    monkeypatch.setattr(mainmod.winsys, "set_autostart", lambda *a: None)
    monkeypatch.setattr(mainmod.winsys, "start_system_event_listener", lambda **kw: None)
    import tuyachroma.gui as guimod

    monkeypatch.setattr(guimod, "App", FakeApp)
    assert mainmod.main(["--minimized"]) == 0
    assert started == {"sync_enabled": True}


def test_migrations(tmp_path) -> None:
    import json

    path = tmp_path / "c.json"
    path.write_text(json.dumps({"settings": {"idle": "restore", "fps": 20}, "devices": []}))
    assert load(path).settings.idle == "effect"  # 1.0 default -> new default
    # 1.1: one effect -> becomes the active profile "Mój efekt"
    path.write_text(json.dumps({"settings": {"idle": "restore", "effect": {
        "effect": "breathing", "color": "#ff0000", "period": 4.0, "brightness": 0.5}}, "devices": []}))
    s = load(path).settings
    assert s.idle == "restore" and s.active_profile == "Mój efekt"
    p = s.profile
    assert (p.effect, p.color1, p.speed, p.brightness) == ("breathing", "#ff0000", 4, 0.5)
    assert len(s.profiles) > 5  # plus the built-in profiles


def test_profiles_roundtrip(tmp_path) -> None:
    from tuyachroma.profiles import Profile

    config = AppConfig()
    config.settings.idle = "full"
    config.settings.profiles.append(Profile("Mój", "ripple", color_mode="random", speed=9))
    config.settings.active_profile = "Mój"
    save(config, tmp_path / "c.json")
    loaded = load(tmp_path / "c.json")
    assert loaded.settings == config.settings
    assert loaded.settings.profile.effect == "ripple"


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_gui_profiles_tab(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import gui

    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    applied = []
    monkeypatch.setattr(service, "apply_profile", lambda cfg: applied.append(cfg.settings.active_profile) or True)
    config = AppConfig()
    app = gui.App(config, service, start_hidden=False, use_tray=False)
    try:
        app.notebook.select(app._profiles_tab_index)
        for _ in range(10):
            app.root.update()
            time.sleep(0.02)
        assert app._preview_visible() and app.preview.find_all()  # preview drawn

        app.new_profile()
        assert config.settings.profiles[-1].name == "Mój profil 1"
        app.pf_name.set("Czerwone gwiazdy")
        app.pf_effect.set(gui.EFFECT_LABELS["starlight"])
        assert app._preview_renderer.profile.effect == "starlight"  # live preview
        app.pf_colors[0] = "#ff0000"
        app.save_profile()
        assert config.settings.profiles[-1].name == "Czerwone gwiazdy"
        assert config.settings.profiles[-1].effect == "starlight"
        app.activate_profile()
        assert config.settings.active_profile == "Czerwone gwiazdy" and applied == ["Czerwone gwiazdy"]
        assert load(cfgmod.default_path()).settings.profile.color1 == "#ff0000"

        app.notebook.select(0)
        app.root.update()
        assert not app._preview_visible()  # no drawing on other tabs / in the tray
    finally:
        app._quitting = True
        app.root.destroy()


def test_fetch_with_session_refreshes_token() -> None:
    from types import SimpleNamespace

    from tuyachroma.cloud import fetch_with_session

    session = {"user_code": "C", "terminal_id": "t", "endpoint": "e",
               "token_info": {"t": 1, "uid": "u", "expire_time": 1, "access_token": "old",
                              "refresh_token": "r"}}

    class FakeManager:
        def __init__(self, client_id, user_code, terminal_id, endpoint, token, listener):
            self.listener = listener
            self.user_homes = ["home"]
            self.device_map = {"1": SimpleNamespace(
                id="1", name="Lamp", local_key="k", category="dj", product_name="", sub=False,
                function={"colour_data_v2": 1}, status_range={}, status={})}

        def update_device_cache(self):
            self.listener.update_token({"t": 2, "uid": "u", "expire_time": 7200,
                                        "access_token": "new", "refresh_token": "r2"})

    with patch("tuya_sharing.Manager", FakeManager):
        devices, updated = fetch_with_session(session)
    assert [d.name for d in devices] == ["Lamp"] and devices[0].rgb
    assert updated["token_info"]["access_token"] == "new"  # saved for next time
    assert session["token_info"]["access_token"] == "old"  # input not modified

    class Expired(FakeManager):
        def __init__(self, *a):
            super().__init__(*a)
            self.user_homes, self.device_map = [], {}

        def update_device_cache(self):
            pass

    with patch("tuya_sharing.Manager", Expired), pytest.raises(TuyaCloudError, match="QR"):
        fetch_with_session(session)
    with pytest.raises(TuyaCloudError):
        fetch_with_session({})


def test_tuya_session_saved(tmp_path) -> None:
    config = AppConfig(tuya_session={"user_code": "C", "token_info": {"access_token": "secret-token"}})
    path = tmp_path / "c.json"
    save(config, path)
    assert load(path).tuya_session == config.tuya_session
    if sys.platform == "win32":
        assert "secret-token" not in path.read_text(encoding="utf-8")  # DPAPI-encrypted


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_gui_razer_devices_and_account_refresh(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import cloud, gui, razer_devices

    detected = [razer_devices.RazerDevice("Razer BlackWidow V4", "keyboard", "USB", "0287")]
    calls = []
    monkeypatch.setattr(razer_devices, "detect", lambda: calls.append(1) or list(detected))
    monkeypatch.setattr(gui, "scan", lambda _s: {"n1": {"id": "n1", "ip": "10.0.0.7", "version": "3.5"}})
    monkeypatch.setattr(cloud, "fetch_with_session", lambda s: (
        [CloudDevice("n1", "New strip", "k" * 16, "dd", "", rgb=True)], {**s, "refreshed": True}))
    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    config = AppConfig(devices=[dev("old")], tuya_session={"user_code": "C"})
    app = gui.App(config, service, start_hidden=False, use_tray=False)

    def pump(cond, timeout=5):
        end = time.time() + timeout
        while time.time() < end and not cond():
            app.root.update()
            time.sleep(0.02)

    try:
        pump(lambda: app.razer_tree.get_children())
        row = app.razer_tree.item(app.razer_tree.get_children()[0], "values")
        assert row[0] == "Razer BlackWidow V4" and row[1] == "Klawiatura"
        # a mouse gets plugged in: Windows fires several events, one refresh follows
        detected.append(razer_devices.RazerDevice("Razer Basilisk V3", "mouse", "USB", "0099"))
        n = len(calls)
        for _ in range(5):
            app.request_razer_refresh()
        pump(lambda: len(app.razer_tree.get_children()) == 2)
        assert len(app.razer_tree.get_children()) == 2 and len(calls) == n + 1

        app.refresh_from_account()
        pump(lambda: config.device("n1") is not None and config.device("n1").ip)
        assert [d.id for d in config.devices] == ["n1"]  # removed "old", added the new strip
        assert config.device("n1").ip == "10.0.0.7" and config.tuya_session.get("refreshed")
        app.logout()
        assert config.tuya_session == {} and load(cfgmod.default_path()).tuya_session == {}
    finally:
        service.stop()
        app._quitting = True
        app.root.destroy()


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_gui_update_available(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import gui, updater

    rel = updater.Release("99.0.0", "v99.0.0", "Nowości", "https://gh/rel", "https://dl", 1, "ab")
    monkeypatch.setattr(updater, "check_latest", lambda: rel)
    monkeypatch.setattr(updater, "can_self_update", lambda: False)
    opened = []
    monkeypatch.setattr(gui.webbrowser, "open", lambda url: opened.append(url))
    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    app = gui.App(AppConfig(), service, start_hidden=False, use_tray=False)
    try:
        app.check_updates(manual=True)
        deadline = time.time() + 5
        while time.time() < deadline and app.available_release is None:
            app.root.update()
            time.sleep(0.02)
        assert "99.0.0" in app.update_status.get()
        assert app.update_button.winfo_manager() == "pack"  # "Zaktualizuj do 99.0.0" visible
        app.install_update()  # running from source: opens the release page instead
        assert opened == ["https://gh/rel"]
    finally:
        service.stop()
        app._quitting = True
        app.root.destroy()


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_quick_clicks_applied_once(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import gui

    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    calls = []
    monkeypatch.setattr(service, "update_bulbs", lambda cfg: calls.append("update") or True)
    monkeypatch.setattr(service, "restart", lambda cfg: calls.append("restart"))
    config = AppConfig(devices=[dev("a"), dev("b"), dev("c")])
    app = gui.App(config, service, start_hidden=False, use_tray=False)
    try:
        for d in config.devices:  # three quick ticks
            d.enabled = True
            app.save_and_apply(bulbs_only=True)
        deadline = time.time() + 3
        while time.time() < deadline and not calls:
            app.root.update()
            time.sleep(0.02)
        time.sleep(0.2)
        app.root.update()
        assert calls == ["update"]  # one live update, no restart
        app.save_and_apply()  # e.g. settings changed -> full restart
        deadline = time.time() + 3
        while time.time() < deadline and len(calls) < 2:
            app.root.update()
            time.sleep(0.02)
        assert calls == ["update", "restart"]
    finally:
        service.stop()
        app._quitting = True
        app.root.destroy()



@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_tray_menu_builds_with_real_pystray(tmp_path, monkeypatch) -> None:
    """Regression: profile items had a callable pystray rejects -> invisible tray icon."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    pystray = pytest.importorskip("pystray")
    from tuyachroma import gui

    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    app = gui.App(AppConfig(), service, start_hidden=False, use_tray=False)
    activated = []
    monkeypatch.setattr(app, "activate_profile", lambda name=None: activated.append(name))
    try:
        menu = app._tray_menu(pystray)
        top = list(menu.items)
        assert [str(i.text) for i in top][0] == "Otwórz"
        submenu = top[2].submenu
        items = list(submenu.items)  # builds the profile items like the tray thread does
        assert [i.text for i in items] == [p.name for p in app.config.settings.profiles]
        assert items[0].checked  # active profile marked
        items[3](None)  # click a profile in the tray
        app._pump()
        assert activated == [app.config.settings.profiles[3].name]
        assert not app.tray_ok  # no icon -> closing the window must not hide the app
        app.on_close()
        assert app._quitting
    finally:
        service.stop()
        if not app._quitting:
            app._quitting = True
            app.root.destroy()


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_buttons_visible_in_every_tab(tmp_path, monkeypatch) -> None:
    """Regression: new settings pushed 'Zapisz ustawienia' below the window."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tkinter import ttk

    from tuyachroma import gui

    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    config = AppConfig(devices=[dev("a")])
    app = gui.App(config, service, start_hidden=False, use_tray=False)
    app.root.geometry("900x600")
    try:
        app.tree.selection_set("a")
        app._load_editor()

        def buttons(widget):
            for child in widget.winfo_children():
                if isinstance(child, ttk.Button):
                    yield child
                yield from buttons(child)

        must_see = {"Zapisz ustawienia", "Otwórz folder z logami", "Zapisz", "Testuj (mignij)",
                    "Szukaj w sieci", "Odśwież z konta", "Zapisz profil", "Odśwież listę"}
        seen = set()
        for index in range(len(app.notebook.tabs())):
            app.notebook.select(index)
            for _ in range(5):
                app.root.update()
            win_bottom = app.root.winfo_rooty() + app.root.winfo_height()
            win_right = app.root.winfo_rootx() + app.root.winfo_width()
            for b in buttons(app.notebook.nametowidget(app.notebook.select())):
                text = str(b.cget("text"))
                if text not in must_see or not b.winfo_ismapped():
                    continue
                if b.winfo_toplevel() is not app.root:
                    continue
                if str(b.winfo_parent()).startswith(str(getattr(app, "settings_canvas", ""))):
                    continue  # inside the scrolling area: reachable by scrolling
                assert b.winfo_rooty() + b.winfo_height() <= win_bottom, f"{text} below the window"
                assert b.winfo_rootx() + b.winfo_width() <= win_right, f"{text} cut off on the right"
                assert b.winfo_width() >= 40, f"{text} squeezed"
                seen.add(text)
        assert must_see <= seen
    finally:
        service.stop()
        app._quitting = True
        app.root.destroy()


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_unreachable_bulbs_found_under_new_ip(tmp_path, monkeypatch) -> None:
    """After a PC/router restart the bulbs often get new IPs: search and update them."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import gui

    config = AppConfig(devices=[dev("a", enabled=True, ip="10.0.0.5"),
                                dev("b", enabled=True, ip="10.0.0.6")])
    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    app = gui.App(config, service, start_hidden=False, use_tray=False)
    statuses = {"a": "błąd: Device Unreachable", "b": "sterowana przez Razer"}
    monkeypatch.setattr(service, "bulb_statuses", lambda: dict(statuses))
    scans = []

    on_network = {"a": {"id": "a", "ip": "10.0.0.50", "version": "3.5"},
                  "b": {"id": "b", "ip": "10.0.0.6", "version": "3.3"}}

    def fake_scan(seconds):
        scans.append(seconds)
        return dict(on_network)

    monkeypatch.setattr(gui, "scan", fake_scan)
    monkeypatch.setattr(app, "run_bg", lambda func, done=None, error=None: done(func()))
    applied = []
    monkeypatch.setattr(app, "save_and_apply", lambda bulbs_only=False: applied.append(bulbs_only))
    try:
        app._check_unreachable(1000.0)        # just failed: wait
        app._check_unreachable(1030.0)
        assert scans == []
        app._check_unreachable(1061.0)        # failing for a minute: search
        assert scans == [10]
        assert config.device("a").ip == "10.0.0.50" and config.device("a").version == "3.5"
        assert config.device("b").ip == "10.0.0.6"
        assert applied == [True]
        app._check_unreachable(1100.0)        # still failing, but not more than every 5 min
        assert scans == [10]
        statuses["a"] = "sterowana przez Razer"
        app._check_unreachable(1500.0)
        assert scans == [10] and app._fail_since == {}
        # Switched off (not found by the search): search again only every 30 min.
        statuses["b"] = "wyłączona / poza siecią"
        del on_network["b"]
        app._check_unreachable(2000.0)
        app._check_unreachable(2061.0)
        assert scans == [10, 10] and app._rescan_every == app.RESCAN_IDLE
        app._check_unreachable(2061.0 + 400)
        assert scans == [10, 10]
        app._check_unreachable(2061.0 + 1801)
        assert scans == [10, 10, 10]
    finally:
        app.root.destroy()


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_auto_tune_button(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import bulbs, gui

    config = AppConfig(devices=[dev("a", enabled=True, name="Lampka Biurko")])
    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    app = gui.App(config, service, start_hidden=False, use_tray=False)
    monkeypatch.setattr(service, "bulb_details", lambda: {"a": {
        "status": bulbs.STATUS_SYNC, "latency_ms": 90, "dps": ["20", "24"], "dropped_off": 0.3}})
    monkeypatch.setattr(app, "run_bg", lambda func, done=None, error=None: done(func()))
    asked = []
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda title, text, **kw: asked.append(text) or True)
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, text, **kw: asked.append(text))
    applied = []
    monkeypatch.setattr(app, "save_and_apply", lambda bulbs_only=False: applied.append(bulbs_only))
    try:
        app.auto_tune()
        assert "Lampka Biurko" in asked[0] and "wolno" in asked[0]
        assert config.devices[0].min_interval == 0.5 and applied == [True]
        app.auto_tune()  # nothing left to change: just information
        assert len(asked) == 2 and "Zastosować" not in asked[1]
    finally:
        app.root.destroy()


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_capture_scene_from_the_app(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import gui

    config = AppConfig(devices=[dev("s", enabled=True, name="Led Strip")])
    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    app = gui.App(config, service, start_hidden=False, use_tray=False)
    held = []
    worker = SimpleNamespace(cfg=SimpleNamespace(id="s"), connected=True, is_alive=lambda: True,
                             hold=held.append, read_state=lambda: {"20": True, "21": "scene", "51": "AA"})
    service.workers = [worker]
    monkeypatch.setattr(app, "run_bg", lambda func, done=None, error=None: done(func()))
    monkeypatch.setattr(gui.messagebox, "askokcancel", lambda *a, **kw: True)
    warnings = []
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **kw: warnings.append(a))
    monkeypatch.setattr(app, "save_and_apply", lambda bulbs_only=False: None)
    try:
        app.tree.selection_set("s")
        app._load_editor()
        app.capture_scene()
        assert held == [True, False]  # colors paused while the user used the app
        assert config.devices[0].scene == {"20": True, "21": "scene", "51": "AA"}
        assert config.devices[0].use_scene and app.ed_use_scene.get()
        worker.read_state = lambda: {"20": True, "21": "colour"}  # no effect set in the app
        app.capture_scene()
        assert warnings and config.devices[0].scene["21"] == "scene"  # kept the old one
    finally:
        service.workers = []
        app.root.destroy()


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_editor_choice_survives_the_status_refresh(tmp_path, monkeypatch) -> None:
    """User report: a new color source jumped back to "Klawiatura" by itself.

    The status refresh (every second) re-selected the row, which reloaded the
    editor from the saved settings before the change was saved.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import gui

    config = AppConfig(devices=[dev("lb", enabled=True, name="Lampka Biurko"), dev("ls", name="Led Strip")])
    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    app = gui.App(config, service, start_hidden=False, use_tray=False)
    monkeypatch.setattr(app, "save_and_apply", lambda bulbs_only=False: None)
    try:
        app.tree.selection_set("lb")
        app.root.update()
        app.ed_source.set(gui.SOURCE_LABELS["mouse"])  # picked, not saved yet
        for _ in range(3):
            app.refresh_devices()  # what the 1 s status tick does
            app.root.update()
        assert app.ed_source.get() == gui.SOURCE_LABELS["mouse"]
        time.sleep(0.6)  # saved by itself shortly after the change
        app.root.update()
        assert config.devices[0].source == "mouse"
        app.tree.selection_set("ls")  # another light: its own settings
        app.root.update()
        assert app.ed_source.get() == gui.SOURCE_LABELS["keyboard"]
        app.ed_source.set(gui.SOURCE_LABELS["chromalink"])
        app.tree.selection_set("lb")  # switching right away still saves the change
        app.root.update()
        assert config.devices[1].source == "chromalink" and config.devices[0].source == "mouse"
        assert app.ed_source.get() == gui.SOURCE_LABELS["mouse"]
    finally:
        app.root.destroy()


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_editor_does_not_undo_changes_made_elsewhere(tmp_path, monkeypatch) -> None:
    """A new IP found by the program, or the ✓ column, must survive an editor save."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import gui

    config = AppConfig(devices=[dev("lb", enabled=True, name="Lampka Biurko", ip="192.168.0.71")])
    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    app = gui.App(config, service, start_hidden=False, use_tray=False)
    monkeypatch.setattr(app, "save_and_apply", lambda bulbs_only=False: None)
    try:
        app.tree.selection_set("lb")
        app.root.update()
        config.devices[0].ip = "192.168.0.99"   # found again after a router restart
        config.devices[0].enabled = False       # ✓ unticked
        app.ed_source.set(gui.SOURCE_LABELS["chromalink"])
        app.save_editor()
        d = config.devices[0]
        assert (d.source, d.ip, d.enabled) == ("chromalink", "192.168.0.99", False)
        app._reload_editor_if_idle()  # the editor now shows the current values
        assert app.ed_ip.get() == "192.168.0.99" and app.ed_enabled.get() is False
        app.ed_brightness.set(50)
        app.save_editor()
        assert d.brightness == 0.5 and d.ip == "192.168.0.99"
    finally:
        app.root.destroy()


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_saving_settings_restarts_only_when_needed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import gui

    config = AppConfig(devices=[dev("a", enabled=True)])
    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    app = gui.App(config, service, start_hidden=False, use_tray=False)
    applied = []
    monkeypatch.setattr(app, "save_and_apply", lambda bulbs_only=False: applied.append(bulbs_only))
    monkeypatch.setattr(gui.winsys, "set_autostart", lambda *a: None)
    try:
        app.set_latency_mode.set("manual")   # read live by the running sync
        app.set_effect_sync.set("ticks")
        app.save_settings()
        assert applied == [] and config.settings.latency_mode == "manual"
        assert load(cfgmod.default_path()).settings.effect_sync == "ticks"  # saved
        app.set_idle.set("full")             # needs the sync to start again
        app.save_settings()
        assert applied == [False]
    finally:
        app.root.destroy()
