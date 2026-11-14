"""Full sync: profile on all Razer devices, games take over, bulbs follow."""

from __future__ import annotations

import os
import time

from tuyachroma import engine
from tuyachroma.config import AppConfig, Device
from tuyachroma.player import EffectPlayer
from tuyachroma.profiles import Profile
from tuyachroma.razer import ChromaApps, DEVICES, Frame, RazerUnavailable, parse_app_data
from tuyachroma.razer_out import CUSTOM_EFFECTS, NativeChroma, to_colorref
from tuyachroma.render import Renderer
from pathlib import Path

OWN = os.getpid()


class FakeDll:
    def __init__(self, init_result=0, reject_extended=False):
        self.calls = []
        self.init_result = init_result
        self.reject_extended = reject_extended

    def Init(self):
        self.calls.append(("Init",))
        return self.init_result

    def UnInit(self):
        self.calls.append(("UnInit",))
        return 0

    def __getattr__(self, name):
        if name.startswith("Create"):
            def create(effect_type, param, effect_id):
                buf = param._obj  # ctypes byref -> the array
                self.calls.append((name, effect_type, list(buf), effect_id))
                extended = (name, effect_type) in {("CreateKeyboardEffect", 9), ("CreateMousepadEffect", 6)}
                return 50 if (extended and self.reject_extended) else 0  # 50 = NOT_SUPPORTED
            return create
        raise AttributeError(name)


def test_native_chroma_sends_custom_frames_to_all_categories() -> None:
    dll = FakeDll()
    out = NativeChroma(dll)
    for device, (func, etype, count) in CUSTOM_EFFECTS.items():
        assert DEVICES[device].count == count
        out.set_frame(device, [(255, 0, 0)] * count)
        name, effect_type, colors, eid = dll.calls[-1]
        assert (name, effect_type, eid) == (func, etype, None)
        assert colors == [0x0000FF] * count  # COLORREF 0x00BBGGRR
    out.close()
    out.close()
    assert dll.calls.count(("UnInit",)) == 1
    assert to_colorref((1, 2, 3)) == 0x030201


def test_native_chroma_init_failure() -> None:
    try:
        NativeChroma(FakeDll(init_result=1062))  # RZRESULT_SERVICE_NOT_ACTIVE
    except RazerUnavailable as err:
        assert "1062" in str(err)
    else:
        raise AssertionError("expected RazerUnavailable")


def test_app_data_from_synapse_dump() -> None:
    raw = (Path(__file__).parent / "data" / "app-data.bin").read_bytes()
    apps = parse_app_data(raw)
    assert apps.current_pid == 1536 and apps.current_name == "asd.exe" and len(apps.apps) == 5


def test_game_detection() -> None:
    active = {"keyboard": Frame("keyboard", 7, [(1, 2, 3)] * 132, 1)}
    idle = {"keyboard": Frame("keyboard", 13, None, 1)}
    ours = ChromaApps(current_pid=OWN, apps={OWN: "TuyaChroma.exe", 99: "game.exe"})
    game = ChromaApps(current_pid=99, apps={OWN: "TuyaChroma.exe", 99: "game.exe"})
    assert not engine.is_game_active(active, ours, OWN, full_sync=True)  # our own effect
    assert engine.is_game_active(active, game, OWN, full_sync=True)
    assert not engine.is_game_active(idle, game, OWN, full_sync=True)
    assert engine.is_game_active(active, None, OWN, full_sync=False)
    assert not engine.is_game_active(active, None, OWN, full_sync=True)


def test_player_pauses_for_games() -> None:
    dll = FakeDll()
    player = EffectPlayer(Renderer(Profile(effect="spectrum")), bulb_count=3,
                          output_factory=lambda: NativeChroma(dll), fps=50)
    player.start()
    try:
        deadline = time.time() + 2
        while time.time() < deadline and len(dll.calls) < 20:
            time.sleep(0.01)
        sent = [c[0] for c in dll.calls if c[0].startswith("Create")]
        assert {"CreateKeyboardEffect", "CreateMouseEffect", "CreateMousepadEffect",
                "CreateHeadsetEffect", "CreateKeypadEffect", "CreateChromaLinkEffect"} <= set(sent)
        kb = next(c for c in dll.calls if c[0] == "CreateKeyboardEffect")
        assert kb[1] == 9 and len(kb[2]) == 8 * 24 + 6 * 22  # extended keyboard layout
        pad = next(c for c in dll.calls if c[0] == "CreateMousepadEffect")
        assert pad[1] == 6 and len(pad[2]) == 20  # extended mousepad layout
        frames, strip = player.latest()
        assert set(frames) == set(DEVICES) and len(strip) == 3
        player.paused = True
        time.sleep(0.1)
        n = len(dll.calls)
        time.sleep(0.2)
        assert len(dll.calls) == n  # nothing sent while a game owns the lighting
        assert "steruje gra" in player.output_status
    finally:
        player.stop()
    assert dll.calls[-1] == ("UnInit",)  # devices handed back to Synapse


class Recorder:
    def __init__(self, cfg):
        self.cfg, self.calls, self.status = cfg, [], "ready"

    def set_color(self, rgb):
        self.calls.append(rgb)

    def release(self):
        self.calls.append("release")

    def start(self):
        pass

    def request_stop(self):
        self.calls.append("stop")

    def join(self, timeout=None):
        pass


def test_full_sync_service(monkeypatch) -> None:
    monkeypatch.setattr(engine, "SERVICE_CHECK_INTERVAL", 0.05)
    state = {"current": OWN, "alive": True}

    class Reader:
        def __init__(self, sources):
            if not state["alive"]:
                raise RazerUnavailable("down")
            self.closed = False

        def wait(self, ms):
            time.sleep(0.005)

        def read(self, src):
            if state["current"] == OWN:  # Synapse shows our own effect
                return Frame(src, 7, [(1, 1, 1)] * DEVICES[src].count, 1)
            return Frame(src, 6, [(255, 0, 0)] * DEVICES[src].count, 1)  # the game: red

        def read_apps(self):
            return ChromaApps(current_pid=state["current"], apps={OWN: "x.exe", 77: "game.exe"})

        def close(self):
            self.closed = True

    dll = FakeDll()
    workers = []
    service = engine.SyncService(
        reader_factory=Reader, worker_factory=lambda c: workers.append(Recorder(c)) or workers[-1],
        service_check=lambda: state["alive"], output_factory=lambda: NativeChroma(dll),
        own_pid=OWN, screen_factory=lambda: None,
    )
    config = AppConfig(devices=[Device(id=i, name=i, local_key="k", ip="1.1.1.1", enabled=True)
                                for i in ("a", "b")])
    config.settings.idle = "full"
    config.settings.active_profile = "Wave"
    config.settings.bulb_effect_rate = 5
    service.start(config)
    try:
        deadline = time.time() + 3
        while time.time() < deadline and not (workers[0].calls and workers[1].calls
                                               and any(c[0] == "CreateKeyboardEffect" for c in dll.calls)):
            time.sleep(0.01)
        assert "pełna synchronizacja" in service.razer_status
        assert workers[0].calls[-1] != workers[1].calls[-1]  # wave across the two bulbs
        assert any(c[0] == "CreateMouseEffect" for c in dll.calls)  # all devices get the profile

        state["current"] = 77  # a game takes over
        deadline = time.time() + 2
        while time.time() < deadline and workers[0].calls[-1] != (255, 0, 0):
            time.sleep(0.01)
        assert workers[0].calls[-1] == (255, 0, 0) and service.game_active
        assert "game.exe" in service.razer_status and service.player.paused

        state["current"] = OWN  # game closed
        deadline = time.time() + 2
        while time.time() < deadline and service.game_active:
            time.sleep(0.01)
        assert not service.player.paused

        state["alive"] = False  # Synapse / PC shutting down
        deadline = time.time() + 2
        while time.time() < deadline and workers[0].calls[-1] != "release":
            time.sleep(0.01)
        assert workers[0].calls[-1] == "release"
    finally:
        service.stop()
    assert dll.calls[-1] == ("UnInit",)


def test_live_profile_switch_without_restart(monkeypatch) -> None:
    class Reader:
        def __init__(self, sources):
            pass

        def wait(self, ms):
            time.sleep(0.005)

        def read(self, src):
            return Frame(src, 13, None, 1)

        def read_apps(self):
            return ChromaApps(current_pid=OWN, apps={OWN: "x.exe"})

        def close(self):
            pass

    hooks = []

    class Hook:
        def __init__(self, cb):
            self.cb, self.running = cb, False
            hooks.append(self)

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

    dll = FakeDll()
    service = engine.SyncService(
        reader_factory=Reader, worker_factory=lambda c: Recorder(c), service_check=lambda: True,
        output_factory=lambda: NativeChroma(dll), keyhook_factory=Hook, screen_factory=lambda: None,
        own_pid=OWN,
    )
    config = AppConfig(devices=[Device(id="a", name="a", local_key="k", ip="1.1.1.1", enabled=True)])
    config.settings.idle = "full"
    config.settings.active_profile = "Wave"
    service.start(config)
    try:
        deadline = time.time() + 2
        while time.time() < deadline and not any(c[0] == "CreateKeyboardEffect" for c in dll.calls):
            time.sleep(0.01)
        player = service.player
        config.settings.active_profile = "Reaktywny"
        assert service.apply_profile(config)
        assert service.player is player  # same player: no restart
        assert player.renderer.profile.effect == "reactive"
        assert hooks and hooks[-1].running  # key presses are read only for key effects
        config.settings.active_profile = "Ogień"
        assert service.apply_profile(config)
        assert not hooks[-1].running
        time.sleep(0.1)
        assert ("UnInit",) not in dll.calls  # Razer devices never fell back to Synapse
        assert "Ogień" in service.razer_status
    finally:
        service.stop()
    assert not service.apply_profile(config)  # not running -> caller restarts instead


def test_full_sync_reports_sdk_problem(monkeypatch) -> None:
    class Reader:
        def __init__(self, sources):
            pass

        def wait(self, ms):
            time.sleep(0.005)

        def read(self, src):
            return Frame(src, 13, None, 1)

        def read_apps(self):
            return ChromaApps(current_pid=0, apps={})

        def close(self):
            pass

    service = engine.SyncService(
        reader_factory=Reader, worker_factory=lambda c: Recorder(c), service_check=lambda: True,
        output_factory=lambda: NativeChroma(FakeDll(init_result=1062)), screen_factory=lambda: None,
        own_pid=OWN,
    )
    config = AppConfig(devices=[Device(id="a", name="a", local_key="k", ip="1.1.1.1", enabled=True)])
    config.settings.idle = "full"
    service.start(config)
    try:
        deadline = time.time() + 2
        while time.time() < deadline and "niedostępne" not in service.razer_status:
            time.sleep(0.01)
        assert "Chroma SDK niedostępne" in service.razer_status and "1062" in service.razer_status
    finally:
        service.stop()


def test_extended_layouts_and_fallback() -> None:
    from tuyachroma.razer_out import KEY_OVERRIDE

    frames = {d: [(255, 0, 0)] * DEVICES[d].count for d in DEVICES}
    frames["keyboard_ext"] = [(0, 0, 255)] * (8 * 24)
    frames["mousepad_ext"] = [(0, 255, 0)] * 20
    dll = FakeDll()
    out = NativeChroma(dll)
    out.send_frames(frames)
    kb = next(c for c in dll.calls if c[0] == "CreateKeyboardEffect")
    assert kb[1] == 9
    assert kb[2][:192] == [0xFF0000] * 192  # extra LEDs: blue (COLORREF BGR)
    assert kb[2][192:] == [0x0000FF | KEY_OVERRIDE] * 132  # keys: red, override flag
    assert len([c for c in dll.calls if c[0].startswith("Create")]) == 6  # all categories

    old = FakeDll(reject_extended=True)  # older device / Synapse
    out = NativeChroma(old)
    out.send_frames(frames)
    out.send_frames(frames)
    kb_calls = [c for c in old.calls if c[0] == "CreateKeyboardEffect"]
    assert [c[1] for c in kb_calls] == [9, 2, 2]  # tried extended once, then basic 6x22
    pad_calls = [c for c in old.calls if c[0] == "CreateMousepadEffect"]
    assert [c[1] for c in pad_calls] == [6, 2, 2] and len(pad_calls[-1][2]) == 15
