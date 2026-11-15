"""Full sync: profile on all Razer devices, games take over, bulbs follow."""

from __future__ import annotations

import os
import time

import pytest

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
        deadline = time.time() + 2 + engine.GAME_HOLD
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


def test_service_uses_detected_layout() -> None:
    from tuyachroma.render import KEYBOARD_AREAS

    service = engine.SyncService(
        reader_factory=lambda s: (_ for _ in ()).throw(RazerUnavailable("x")),
        worker_factory=lambda c: Recorder(c), service_check=lambda: True,
        output_factory=lambda: NativeChroma(FakeDll()), screen_factory=lambda: None,
        own_pid=OWN, layout_detector=lambda: "mini",
    )
    config = AppConfig(devices=[Device(id="a", name="a", local_key="k", ip="1.1.1.1", enabled=True)])
    config.settings.idle = "full"
    service.start(config)
    try:
        assert service.keyboard_layout == "mini"
        assert service.player.renderer.keyboard_area == KEYBOARD_AREAS["mini"]
    finally:
        service.stop()
    config.settings.keyboard_layout = "full"  # manual setting wins over detection
    service.start(config)
    try:
        assert service.keyboard_layout == "full"
    finally:
        service.stop()


class SlowWorker(Recorder):
    """Like a real bulb worker: needs a moment to give the bulb back."""

    log: list = []

    def __init__(self, cfg, restore_time=0.4):
        super().__init__(cfg)
        self._done = time.monotonic() + 3600
        self._restore_time = restore_time
        SlowWorker.log.append(("start", cfg.id, time.monotonic()))

    def request_stop(self):
        super().request_stop()
        self._done = time.monotonic() + self._restore_time

    def is_alive(self):
        return time.monotonic() < self._done

    def join(self, timeout=None):
        end = time.monotonic() + (timeout or 0)
        while self.is_alive() and time.monotonic() < end:
            time.sleep(0.01)
        if not self.is_alive():
            SlowWorker.log.append(("stopped", self.cfg.id, time.monotonic()))


def _idle_reader(sources):
    class R:
        def wait(self, ms):
            time.sleep(0.005)

        def read(self, src):
            return Frame(src, 13, None, 1)

        def read_apps(self):
            return ChromaApps(current_pid=OWN, apps={OWN: "x.exe"})

        def close(self):
            pass

    return R()


def _service(dll, worker_factory):
    return engine.SyncService(
        reader_factory=_idle_reader, worker_factory=worker_factory, service_check=lambda: True,
        output_factory=lambda: NativeChroma(dll), screen_factory=lambda: None, own_pid=OWN,
        layout_detector=lambda: "full",
    )


def _dev(i, **kw):
    return Device(id=i, name=i, local_key="k", ip="1.1.1.1", enabled=True, **kw)


def test_update_bulbs_without_restart() -> None:
    dll = FakeDll()
    created = []
    service = _service(dll, lambda c: created.append(Recorder(c)) or created[-1])
    config = AppConfig(devices=[_dev("a"), _dev("b")])
    config.settings.idle = "full"
    service.start(config)
    try:
        deadline = time.time() + 2
        while time.time() < deadline and not any(c[0] == "CreateKeyboardEffect" for c in dll.calls):
            time.sleep(0.01)
        a, b = service.workers
        config.devices.append(_dev("c"))          # tick a new bulb
        config.device("b").enabled = False         # untick another
        assert service.update_bulbs(config)
        assert service.workers[0] is a             # untouched bulb keeps running
        assert [w.cfg.id for w in service.workers] == ["a", "c"]
        assert b.calls[-1] == "stop"               # removed bulb given back
        assert service.player.bulb_count == 2
        config.device("a").zone = "left"           # changed settings: only that bulb restarts
        assert service.update_bulbs(config)
        assert service.workers[0] is not a and service.workers[1].cfg.id == "c"
        assert ("UnInit",) not in dll.calls        # Razer devices never fell back to Synapse
        opened = []
        service._reader_factory = lambda srcs: opened.append(list(srcs)) or _idle_reader(srcs)
        lights = list(service.workers)
        config.device("c").source = "mouse"        # follows another Razer device now
        assert service.update_bulbs(config)        # no full restart (it used to flicker)
        assert service.workers[0] is lights[0]     # the other light kept running
        deadline = time.time() + 2
        while time.time() < deadline and not opened:
            time.sleep(0.01)
        assert opened and opened[0] == ["keyboard", "mouse"]  # only the reader reopened
        assert ("UnInit",) not in dll.calls        # Razer devices kept the profile
    finally:
        service.stop()


def test_new_connection_waits_for_old_one() -> None:
    SlowWorker.log = []
    service = _service(FakeDll(), lambda c: SlowWorker(c, restore_time=0.4))
    config = AppConfig(devices=[_dev("lamp")])
    config.settings.idle = "effect"
    service.start(config)
    service.stop(timeout=0.05)  # the lamp is slow to give back (like in the log)
    service.start(config)       # restart right away
    try:
        events = [e for e in SlowWorker.log if e[1] == "lamp"]
        stopped = next(t for kind, _id, t in events if kind == "stopped")
        second_start = [t for kind, _id, t in events if kind == "start"][1]
        assert second_start >= stopped  # reconnects only after the old connection closed
    finally:
        service.stop()


def test_other_chroma_apps() -> None:
    apps = ChromaApps(current_pid=OWN, apps={OWN: "TuyaChroma.exe", 5: "Razer Synapse.exe",
                                            6: "RazerAppEngine.exe", 7: "game.exe", 8: "old.exe",
                                            9: "TuyaChroma.exe"})
    alive = lambda pid: pid != 8  # noqa: E731 - old.exe has exited, its entry stayed
    assert engine.other_chroma_apps(apps, OWN, alive) == {7: "game.exe"}
    assert engine.other_chroma_apps(None, OWN, alive) == {}


def test_full_sync_hands_razer_devices_to_a_game(monkeypatch) -> None:
    """Synapse may keep our effect on top: when a game starts, let go of the SDK."""
    from tuyachroma import winsys

    monkeypatch.setattr(engine, "SERVICE_CHECK_INTERVAL", 0.05)
    state = {"running": set(), "current": OWN}
    monkeypatch.setattr(winsys, "process_alive", lambda pid: pid in state["running"])
    dll = FakeDll()

    class Reader:
        def __init__(self, sources):
            pass

        def wait(self, ms):
            time.sleep(0.005)

        def read(self, src):
            if state["current"] == OWN:
                return Frame(src, 7, [(1, 1, 1)] * DEVICES[src].count, 1)
            return Frame(src, 6, [(0, 255, 0)] * DEVICES[src].count, 1)

        def read_apps(self):
            apps = {OWN: "TuyaChroma.exe"}
            apps.update({pid: "game.exe" for pid in state["running"]})
            return ChromaApps(current_pid=state["current"], apps=apps)

        def close(self):
            pass

    workers = []
    service = engine.SyncService(
        reader_factory=Reader, worker_factory=lambda c: workers.append(Recorder(c)) or workers[-1],
        service_check=lambda: True, output_factory=lambda: NativeChroma(dll),
        own_pid=OWN, screen_factory=lambda: None,
    )
    config = AppConfig(devices=[Device(id="a", name="a", local_key="k", ip="1.1.1.1", enabled=True)])
    config.settings.idle = "full"
    service.start(config)

    def wait(cond, t=3):
        end = time.time() + t
        while time.time() < end and not cond():
            time.sleep(0.01)
        return cond()

    try:
        assert wait(lambda: any(c[0] == "CreateKeyboardEffect" for c in dll.calls))
        state["running"].add(77)  # the game starts; Synapse still shows our effect
        assert wait(lambda: ("UnInit",) in dll.calls)  # we let go of the Razer devices
        assert service.player.yielded and "gra" in service.player.output_status
        state["current"] = 77  # now the game's effect is shown
        assert wait(lambda: workers[0].calls and workers[0].calls[-1] == (0, 255, 0))
        assert service.game_active
        inits = dll.calls.count(("Init",))
        state["running"].clear()  # game closed
        state["current"] = OWN
        assert wait(lambda: dll.calls.count(("Init",)) > inits)  # our profile is back
        assert not service.player.yielded
        assert wait(lambda: not service.game_active)  # after the short hold
    finally:
        service.stop()


def test_background_chroma_app_does_not_block_full_sync(monkeypatch) -> None:
    """A Chroma app that never shows an effect (e.g. a wallpaper program) is not a game."""
    from tuyachroma import winsys

    monkeypatch.setattr(engine, "GAME_PROBATION", 0.3)
    monkeypatch.setattr(winsys, "process_alive", lambda pid: pid == 55)
    dll = FakeDll()

    class Reader:
        def __init__(self, sources):
            pass

        def wait(self, ms):
            time.sleep(0.005)

        def read(self, src):
            return Frame(src, 7, [(1, 1, 1)] * DEVICES[src].count, 1)

        def read_apps(self):  # the background app is listed but Synapse shows us
            return ChromaApps(current_pid=OWN, apps={OWN: "TuyaChroma.exe", 55: "wallpaper.exe"})

        def close(self):
            pass

    service = engine.SyncService(
        reader_factory=Reader, worker_factory=lambda c: Recorder(c), service_check=lambda: True,
        output_factory=lambda: NativeChroma(dll), own_pid=OWN, screen_factory=lambda: None)
    config = AppConfig(devices=[Device(id="a", name="a", local_key="k", ip="1.1.1.1", enabled=True)])
    config.settings.idle = "full"
    service.start(config)
    try:
        end = time.time() + 2
        while time.time() < end and not service.player.yielded:
            time.sleep(0.01)
        assert service.player.yielded and "oddane grze" in service.razer_status  # on trial
        end = time.time() + 3
        while time.time() < end and (service.player.yielded or dll.calls.count(("Init",)) < 2):
            time.sleep(0.01)
        assert not service.player.yielded and dll.calls.count(("Init",)) >= 2  # devices taken back
        time.sleep(0.5)
        assert not service.player.yielded  # and it stays that way
        assert "pełna synchronizacja" in service.razer_status
    finally:
        service.stop()


def _game_service(monkeypatch, read, sources_cfg=("keyboard",)):
    from tuyachroma import winsys

    monkeypatch.setattr(winsys, "process_alive", lambda pid: pid == 34092)

    class Reader:
        def __init__(self, sources):
            self.sources = sources

        def wait(self, ms):
            time.sleep(0.005)

        def read(self, src):
            return read(src)

        def read_apps(self):
            return ChromaApps(current_pid=34092, apps={OWN: "TuyaChroma.exe", 34092: "Cyberpunk2077.exe"})

        def close(self):
            pass

    workers = []
    service = engine.SyncService(
        reader_factory=Reader, worker_factory=lambda c: workers.append(Recorder(c)) or workers[-1],
        service_check=lambda: True, output_factory=lambda: NativeChroma(FakeDll()),
        own_pid=OWN, screen_factory=lambda: None)
    config = AppConfig(devices=[Device(id=f"b{i}", name=f"b{i}", local_key="k", ip="1.1.1.1",
                                       enabled=True, source=s) for i, s in enumerate(sources_cfg)])
    config.settings.idle = "full"
    return service, config, workers


def test_game_effect_pauses_do_not_flicker(monkeypatch, caplog) -> None:
    """Cyberpunk 2077 (log): ~0.6 s effect, ~70 ms of nothing, again and again."""
    start = time.monotonic()

    def read(src):
        phase = (time.monotonic() - start) % 0.68
        if phase > 0.61:  # the short gap
            return Frame(src, 99, None, 1)
        return Frame(src, 7, [(255, 200, 0)] * DEVICES[src].count, 1)  # Cyberpunk yellow

    service, config, workers = _game_service(monkeypatch, read)
    with caplog.at_level("INFO", logger="tuyachroma.engine"):
        service.start(config)
        try:
            time.sleep(2.2)
        finally:
            service.stop()
    colors = [c for c in workers[0].calls if isinstance(c, tuple)]
    assert colors and set(colors) == {colors[0]}  # only the game's color, never the profile
    starts = [r for r in caplog.records if r.message.startswith("Game effect active")]
    assert len(starts) == 1 and not any(r.message == "Game effect ended" for r in caplog.records
                                        if r.created - caplog.records[0].created < 2.0)


def test_light_on_another_device_follows_the_game(monkeypatch) -> None:
    """The game drives only the keyboard: a light set to the mouse follows it too."""
    def read(src):
        if src == "keyboard":
            return Frame(src, 6, [(0, 0, 255)] * DEVICES[src].count, 1)
        return Frame(src, 99, None, 1)

    service, config, workers = _game_service(monkeypatch, read, sources_cfg=("mouse",))
    service.start(config)
    try:
        end = time.time() + 2
        while time.time() < end and not any(c == (0, 0, 255) for c in workers[0].calls):
            time.sleep(0.01)
        assert (0, 0, 255) in workers[0].calls
        assert service._sources == ["keyboard", "mouse"]  # the keyboard is read for that
    finally:
        service.stop()


def test_light_settings_apply_without_reconnecting() -> None:
    """1.9.10 log: each source change gave the light back to HA for ~3 s and reconnected."""
    from tuyachroma.bulbs import BulbWorker
    from test_bulbs import FakeBulb

    bulbs_made = []

    def factory(cfg):
        bulbs_made.append(FakeBulb(music=False))
        return BulbWorker(cfg, device_factory=lambda _c, b=bulbs_made[-1]: b)

    dll = FakeDll()
    service = _service(dll, factory)
    config = AppConfig(devices=[_dev("a")])
    config.settings.idle = "full"
    service.start(config)
    try:
        deadline = time.time() + 3
        while time.time() < deadline and not (bulbs_made and bulbs_made[0].sent):
            time.sleep(0.01)
        worker = service.workers[0]
        for change in (dict(source="chromalink"), dict(zone="left", brightness=0.5),
                       dict(min_interval=0.3, latency_adjust_ms=100)):
            for k, v in change.items():
                setattr(config.device("a"), k, v)
            assert service.update_bulbs(config)
            assert service.workers[0] is worker  # same connection
            for k, v in change.items():
                assert getattr(worker.cfg, k) == v  # applied
        assert len(bulbs_made) == 1  # never reconnected
        assert all(v.get("21") != "white" for _k, v in bulbs_made[0].sent)  # never given back
        config.device("a").ip = "1.1.1.2"  # a new IP does need a new connection
        assert service.update_bulbs(config)
        assert service.workers[0] is not worker
    finally:
        service.stop()


class _Light:
    def __init__(self, i, source="chromalink", zone="all"):
        from tuyachroma.bulbs import BulbConfig

        self.cfg = BulbConfig(name=f"l{i}", id=f"l{i}", ip="1", local_key="k", source=source,
                              zone=zone, min_brightness=0)
        self.colors = []
        self.latency_ms = None
        self.use_music = True

    def set_color(self, rgb):
        self.colors.append(rgb)


LINK = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255)]


def test_lights_on_chroma_link_get_one_link_led_each() -> None:
    """All lights on Chroma Link: each shows its own LED, not one mixed color."""
    lights = [_Light(i) for i in range(5)]
    frames = {"chromalink": Frame("chromalink", 7, list(LINK), 1)}
    engine.dispatch(lights, frames, "full", True)
    assert [lt.colors[-1] for lt in lights] == LINK
    # a game that sets only the first LED: nobody goes dark
    only_first = [(200, 0, 50)] + [(0, 0, 0)] * 4
    engine.dispatch(lights, {"chromalink": Frame("chromalink", 7, only_first, 1)}, "full", True)
    assert {lt.colors[-1] for lt in lights} == {(200, 0, 50)}
    # a light on a part of Chroma Link or another device is not counted
    other = _Light(9, source="keyboard")
    mixed = [lights[0], other, lights[1]]
    engine.dispatch(mixed, {**frames, "keyboard": Frame("keyboard", 6, [(9, 9, 9)] * 132, 1)}, "full", True)
    assert lights[0].colors[-1] == LINK[0] and lights[1].colors[-1] == LINK[1]


def test_wave_runs_across_lights_on_chroma_link() -> None:
    service = engine.SyncService(layout_detector=lambda: "full", own_pid=OWN)
    renderer = Renderer(Profile(effect="wave", speed=5))
    service.player = type("P", (), {"renderer": renderer})()
    lights = [_Light(i) for i in range(5)]
    service.workers = lights
    settings = AppConfig().settings
    settings.latency_mode = "off"
    real = time.monotonic
    try:
        time.monotonic = lambda: 500.0
        colors = [service._bulb_effect_source(lt, i, 5, settings, renderer.profile, "k")[1]()
                  for i, lt in enumerate(lights)]
        expected = Renderer(Profile(effect="wave", speed=5)).frame("chromalink", 5, 1, 500.0)
    finally:
        time.monotonic = real
    assert len(set(colors)) == 5          # a wave, not one mixed color
    assert colors == expected              # exactly the Chroma Link LEDs (e.g. the ARGB controller)


def _saturation(rgb):
    return max(rgb) - min(rgb)


@pytest.mark.parametrize("source", ["mouse", "mousepad", "headset", "keypad", "chromalink"])
def test_every_source_follows_the_wave(source) -> None:
    """Averaging a whole device made a wave grey (128,128,128) on every light."""
    renderer = Renderer(Profile(effect="wave", speed=5))
    spec = DEVICES[source]
    frame = renderer.frame(source, spec.width, spec.height, 100.0)
    one = _Light(0, source=source)
    single = engine.link_color(one.cfg, frame, engine.link_slot([one], one))
    assert _saturation(single) > 150  # a real color, not grey
    lights = [_Light(i, source=source) for i in range(3)]
    colors = [engine.link_color(lt.cfg, frame, engine.link_slot(lights, lt)) for lt in lights]
    assert len(set(colors)) == 3 and all(_saturation(c) > 150 for c in colors)  # the wave runs on


@pytest.mark.parametrize("source", ["mouse", "mousepad", "headset", "keypad", "chromalink"])
def test_every_source_follows_a_game(source) -> None:
    spec = DEVICES[source]
    lit = [(0, 0, 0)] * spec.count
    lit[0] = (255, 40, 0)  # the game lights only one LED (e.g. a mouse's side)
    lights = [_Light(i, source=source) for i in range(2)]
    engine.dispatch(lights, {source: Frame(source, 7, lit, 1)}, "full", True)
    for lt in lights:
        assert lt.colors[-1] == (255, 40, 0)  # nobody dark, the game's color


def _fake_screen(w, h):
    """Left third red, middle green, right blue; a dark taskbar at the bottom."""
    out = []
    for y in range(h):
        for x in range(w):
            if y >= h - 2:
                out.append((20, 20, 20))
            elif x < w // 3:
                out.append((220, 30, 30))
            elif x < w - w // 3:
                out.append((30, 220, 30))
            else:
                out.append((30, 30, 220))
    return out


def test_ambient_lights_show_their_part_of_the_screen() -> None:
    """Ambient: each light shows its part of the desktop, not one average of all of it."""
    service = engine.SyncService(layout_detector=lambda: "full", own_pid=OWN)
    renderer = Renderer(Profile(effect="ambient"), screen=_fake_screen)
    service.player = type("P", (), {"renderer": renderer})()
    lights = [_Light(i, source="keyboard") for i in range(3)]
    service.workers = lights
    settings = AppConfig().settings
    settings.latency_mode = "off"
    colors = [service._bulb_effect_color(lt, i, 3, settings, renderer.profile) for i, lt in enumerate(lights)]
    dominant = [max(range(3), key=lambda c, rgb=rgb: rgb[c]) for rgb in colors]
    assert dominant == [0, 1, 2]  # left red, middle green, right blue
    assert all(max(c) > 180 for c in colors)  # the dark taskbar doesn't dim them
    # a light with a zone shows that part, wherever it is in the list
    right = _Light(9, source="keyboard", zone="right")
    rgb = service._bulb_effect_color(right, 0, 3, settings, renderer.profile)
    assert max(range(3), key=lambda c: rgb[c]) == 2
