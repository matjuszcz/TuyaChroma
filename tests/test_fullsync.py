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
