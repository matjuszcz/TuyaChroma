"""Fixes from the agent reviews: crashed games, slow games, SDK back-off,
damaged settings, key effects rendered ahead by the bulbs."""

from __future__ import annotations

import json
import os
import time

from tuyachroma import config as config_mod
from tuyachroma import engine
from tuyachroma.config import AppConfig, Device
from tuyachroma.player import EffectPlayer
from tuyachroma.profiles import Profile
from tuyachroma.razer import DEVICES, ChromaApps, Frame, RazerUnavailable
from tuyachroma.render import Renderer

OWN = os.getpid()


def _frames(active: bool) -> dict:
    return {"keyboard": Frame("keyboard", 6, [(255, 0, 0)] * DEVICES["keyboard"].count, active)}


def test_crashed_game_does_not_count_as_running() -> None:
    apps = ChromaApps(current_pid=999999, apps={OWN: "x.exe", 999999: "game.exe"})
    assert engine.is_game_active(_frames(True), apps, OWN, True, alive=lambda p: True)
    assert not engine.is_game_active(_frames(True), apps, OWN, True, alive=lambda p: False)
    ours = ChromaApps(current_pid=OWN, apps={OWN: "x.exe"})
    assert not engine.is_game_active(_frames(True), ours, OWN, True, alive=lambda p: True)


def test_sdk_send_error_backs_off() -> None:
    made = []

    class Broken:
        def __init__(self):
            made.append(self)

        def send_frames(self, frames):
            raise OSError("RzChromaSDK went away")

        def close(self):
            pass

    player = EffectPlayer(Renderer(Profile()), 1, output_factory=Broken, fps=20)
    player.start()
    time.sleep(1.0)
    player.stop()
    assert 1 <= len(made) <= 2  # not reopened 20 times a second


def test_sdk_unavailable_still_retries() -> None:
    calls = []

    def factory():
        calls.append(1)
        raise RazerUnavailable("not yet")

    player = EffectPlayer(Renderer(Profile()), 1, output_factory=factory, fps=20)
    player.start()
    time.sleep(0.3)
    player.stop()
    assert len(calls) == 1 and "niedostępne" in player.output_status


def _write(tmp_path, data) -> object:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return path


def test_damaged_settings_values_fall_back_to_defaults(tmp_path) -> None:
    path = _write(tmp_path, {
        "settings": {"latency_ms": None, "bulb_effect_rate": None, "fps": "x", "idle": 5,
                     "profiles": [{"name": "Mój", "speed": "fast", "effect": "wave"}],
                     "active_profile": "Mój"},
        "devices": [{"id": "a", "name": "A", "local_key": "", "min_interval": "slow",
                     "enabled": "yes", "zone": ["custom"]}, "junk"],
    })
    cfg = config_mod.load(path)
    s = cfg.settings
    assert s.latency_ms == 300 and s.bulb_effect_rate == 1.0 and s.fps == 20 and s.idle == "effect"
    assert s.profile.name == "Mój" and 1 <= s.profile.speed <= 10
    dev = cfg.devices[0]
    assert dev.min_interval == 0.15 and dev.enabled is False and dev.zone == ["custom"]


def test_non_finite_numbers_keep_defaults(tmp_path) -> None:
    path = _write(tmp_path, '{"settings": {"latency_ms": 1e400, "fps": NaN, "profiles": '
                            '[{"name": "P", "speed": Infinity}]}, "devices": [{"id": "a", '
                            '"name": "A", "local_key": "", "latency_adjust_ms": NaN}]}')
    cfg = config_mod.load(path)
    assert cfg.settings.latency_ms == 300 and cfg.settings.fps == 20
    assert 1 <= cfg.settings.profiles[0].speed <= 10
    assert [d.id for d in cfg.devices] == ["a"] and cfg.devices[0].latency_adjust_ms == 0


def test_settings_that_are_not_a_dict_are_ignored(tmp_path) -> None:
    for data in ([1, 2], {"settings": None, "devices": None, "tuya_session": 5}):
        cfg = config_mod.load(_write(tmp_path, data))
        assert isinstance(cfg, AppConfig) and cfg.settings.profiles


def test_unreadable_settings_file_is_kept(tmp_path) -> None:
    path = _write(tmp_path, '{"devices": [ broken')
    cfg = config_mod.load(path)
    assert cfg.devices == []
    config_mod.save(cfg, path)  # would replace the user's lights and login
    assert (tmp_path / "config.json.bad").read_text(encoding="utf-8") == '{"devices": [ broken'


def test_good_settings_still_load(tmp_path) -> None:
    cfg = AppConfig(devices=[Device(id="a", name="A", local_key="", min_interval=0.3, enabled=True)])
    cfg.settings.latency_ms = 450
    path = tmp_path / "config.json"
    config_mod.save(cfg, path)
    back = config_mod.load(path)
    assert back.settings.latency_ms == 450 and back.devices[0].min_interval == 0.3
    assert back.devices[0].enabled is True


def test_bulbs_rendering_ahead_do_not_cut_the_keyboard_fade() -> None:
    rend = Renderer(Profile(effect="reactive", color1="#ff0000", speed=5))
    now = time.monotonic()
    rend.key_pressed(3, 2, t=now)
    life = rend.profile.period * 0.4
    rend.device_frame("keyboard", now + life - 0.05)       # a bulb, far ahead
    rend.device_frame("keyboard", now + life + 0.5)        # even past the press's end
    kb = rend.device_frame("keyboard", now + 0.1)          # the keyboard, now
    assert kb[3 * 22 + 2] != (0, 0, 0)


def test_slow_loading_game_gets_the_devices_after_probation(monkeypatch) -> None:
    """A game that shows its first effect after the probation must still be a game."""
    from tuyachroma import winsys
    from tuyachroma.razer_out import NativeChroma

    from test_fullsync import FakeDll, Recorder

    monkeypatch.setattr(engine, "GAME_PROBATION", 0.3)
    monkeypatch.setattr(winsys, "process_alive", lambda pid: pid == 55)
    state = {"current": OWN}
    dll = FakeDll()

    class Reader:
        def __init__(self, sources):
            pass

        def wait(self, ms):
            time.sleep(0.005)

        def read(self, src):
            if state["current"] == 55:
                return Frame(src, 9, [(0, 0, 255)] * DEVICES[src].count, 1)
            return Frame(src, 7, [(1, 1, 1)] * DEVICES[src].count, 1)

        def read_apps(self):
            return ChromaApps(current_pid=state["current"], apps={OWN: "x.exe", 55: "game.exe"})

        def close(self):
            pass

    service = engine.SyncService(
        reader_factory=Reader, worker_factory=lambda c: Recorder(c), service_check=lambda: True,
        output_factory=lambda: NativeChroma(dll), own_pid=OWN, screen_factory=lambda: None)
    cfg = AppConfig(devices=[Device(id="a", name="a", local_key="k", ip="1.1.1.1", enabled=True)])
    cfg.settings.idle = "full"

    def wait(cond, t=3):
        end = time.time() + t
        while time.time() < end and not cond():
            time.sleep(0.01)
        return cond()

    service.start(cfg)
    try:
        assert wait(lambda: 55 in service._background_apps)  # loading: no effect yet
        assert wait(lambda: not service.player.yielded)
        state["current"] = 55  # the game shows its first effect
        assert wait(lambda: service.player.yielded and service.game_active)
        assert 55 not in service._background_apps
    finally:
        service.stop()
    state["current"] = OWN
    service._background_apps.add(55)  # learned: kept for the next start
    service.start(cfg)
    try:
        time.sleep(0.3)
        assert not service.player.yielded
    finally:
        service.stop()
