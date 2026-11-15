"""Sync delay: measuring each bulb and sending effect colors ahead of time."""

from __future__ import annotations

import time
from types import SimpleNamespace

from tuyachroma import engine
from tuyachroma.bulbs import BulbConfig, BulbWorker
from tuyachroma.config import AppConfig, Device, load, save
from tuyachroma.profiles import Profile
from tuyachroma.render import Renderer
from test_bulbs import FakeBulb, wait_for


class SlowBulb(FakeBulb):
    """Takes ~80 ms to confirm a color, like a bulb over Wi-Fi."""

    def set_multiple_values(self, values, nowait=False):
        if "24" in values:
            time.sleep(0.08)
        super().set_multiple_values(values, nowait)


def test_worker_measures_latency() -> None:
    bulb = SlowBulb(music=False)
    cfg = BulbConfig(name="l", id="x", ip="1", local_key="k", min_interval=0.01)
    worker = BulbWorker(cfg, device_factory=lambda _c: bulb)
    worker.start()
    try:
        assert wait_for(lambda: worker.connected)
        assert worker.latency_ms is None  # nothing sent yet
        for i in range(6):
            worker.set_color((i * 40, 0, 255))
            time.sleep(0.15)
        assert wait_for(lambda: worker.latency_ms is not None)
        assert 60 <= worker.latency_ms <= 200
    finally:
        worker.stop()


def test_latency_spikes_are_smoothed() -> None:
    w = BulbWorker(BulbConfig(name="l", id="x", ip="1", local_key="k"))
    for _ in range(10):
        w._record_latency(50)
    w._record_latency(3000)  # one Wi-Fi hiccup
    assert w.latency_ms < 100


def _worker(latency, adjust=0):
    return SimpleNamespace(latency_ms=latency,
                           cfg=SimpleNamespace(latency_adjust_ms=adjust, id="x"))


def test_offset_modes() -> None:
    s = AppConfig().settings
    assert s.latency_mode == "auto"
    off = engine.SyncService.latency_offset_ms
    assert off(_worker(120), s) == 120                 # auto: measured
    assert off(_worker(None), s) == 0                  # auto, not measured yet
    assert off(_worker(120, adjust=200), s) == 320     # + per-bulb correction
    s.latency_mode, s.latency_ms = "manual", 400
    assert off(_worker(120), s) == 400
    s.latency_mode = "off"
    assert off(_worker(120, adjust=-50), s) == -50


def test_bulb_gets_color_from_the_future() -> None:
    """With 500 ms delay the bulb is sent now what the keyboard shows in 500 ms."""
    service = engine.SyncService(layout_detector=lambda: "full", own_pid=1)
    renderer = Renderer(Profile(effect="spectrum", speed=5))
    service.player = SimpleNamespace(renderer=renderer)
    service.keyboard_layout = "full"
    settings = AppConfig().settings
    settings.latency_mode, settings.latency_ms = "manual", 500
    worker = SimpleNamespace(latency_ms=None, cfg=BulbConfig(
        name="l", id="x", ip="1", local_key="k", min_brightness=0))
    sent = service._bulb_effect_color(worker, 0, 1, settings, renderer.profile)
    later = Renderer(Profile(effect="spectrum", speed=5)).strip(1, time.monotonic() + 0.5)[0]
    now = Renderer(Profile(effect="spectrum", speed=5)).strip(1, time.monotonic())[0]
    dist = lambda a, b: max(abs(x - y) for x, y in zip(a, b))  # noqa: E731
    assert dist(sent, later) <= dist(sent, now)
    assert dist(sent, later) < 30


def test_latency_settings_saved(tmp_path) -> None:
    config = AppConfig(devices=[Device(id="a", name="a", local_key="k", latency_adjust_ms=150)])
    config.settings.latency_mode, config.settings.latency_ms = "manual", 250
    save(config, tmp_path / "c.json")
    loaded = load(tmp_path / "c.json")
    assert loaded.settings.latency_mode == "manual" and loaded.settings.latency_ms == 250
    assert loaded.devices[0].latency_adjust_ms == 150
    assert loaded.devices[0].bulb_config().latency_adjust_ms == 150


def test_bulbs_connected_at_different_times_show_the_same_color() -> None:
    """Lamps connect one by one: they must still show the same effect color at once."""
    service = engine.SyncService(layout_detector=lambda: "full", own_pid=1)
    renderer = Renderer(Profile(effect="spectrum", speed=5))
    service.player = SimpleNamespace(renderer=renderer)
    settings = AppConfig().settings
    settings.latency_mode = "off"

    class Lamp:
        def __init__(self, i):
            self.latency_ms = None
            self.cfg = BulbConfig(name=f"l{i}", id=str(i), ip="1", local_key="k", min_brightness=0)
            self.colors = []

        def set_color(self, rgb):
            self.colors.append(rgb)

    clock = engine.EffectClock(1.0)
    early, late = Lamp(1), Lamp(2)
    t0 = 1000.0

    def frame(now, lamps):
        clock.update(now)
        color = lambda w, i, n: service._bulb_effect_color(  # noqa: E731
            w, i, n, settings, renderer.profile, at=clock.time)
        engine.dispatch(lamps, {}, "effect", False, profile=renderer.profile,
                        allow_effect=clock.allow, effect_color=color)

    frame(t0 + 0.1, [early])
    frame(t0 + 0.6, [early, late])      # the late lamp connected half a second later
    assert len(early.colors) == 1 and len(late.colors) == 1  # joined at once ...
    assert late.colors[0] == early.colors[0]                 # ... with the same color
    for k in range(1, 5):
        frame(t0 + k + 0.05, [early, late])
        frame(t0 + k + 0.5, [early, late])  # at most one change per tick
    assert len(early.colors) == len(late.colors) == 5
    assert early.colors == late.colors


def test_bulb_shows_the_middle_of_its_tick() -> None:
    """Holding one color per tick, the bulb is never more than half a tick off."""
    clock = engine.EffectClock(1.0)
    clock.update(1000.2)
    assert clock.time == 1000.5
    clock.update(1000.99)
    assert clock.time == 1000.5


def test_profile_change_reaches_bulbs_at_once() -> None:
    clock = engine.EffectClock(1.0)
    lamp = object()
    clock.update(1000.1)
    assert clock.allow(lamp) and not clock.allow(lamp)
    clock.reset()  # new profile: no waiting for the next tick
    assert clock.allow(lamp) and not clock.allow(lamp)


def test_precise_bulb_computes_color_when_sending() -> None:
    """Precise sync: the color is computed right before each send, at the bulb's rate."""
    bulb = FakeBulb(music=False)
    cfg = BulbConfig(name="l", id="p1", ip="1", local_key="k", min_interval=0.05)
    worker = BulbWorker(cfg, device_factory=lambda _cfg: bulb)
    worker.start()
    assert wait_for(lambda: worker.connected)
    asked = []

    def source():
        t = time.monotonic()
        asked.append(t)
        return (int(t * 1000) % 200 + 20, 10, 10)  # changes all the time

    try:
        worker.set_color_source(source, key="spectrum")
        assert wait_for(lambda: len(bulb.sent) >= 6, 3)
        for _ in range(5):
            worker.set_color_source(lambda: (0, 0, 0), key="spectrum")  # same key: ignored
        n = len(bulb.sent)
        assert wait_for(lambda: len(bulb.sent) >= n + 3)
        sent_colors = [v["24"] for kind, v in bulb.sent if kind == "multi" and "24" in v]
        assert len(set(sent_colors)) >= 6  # a fresh color each time
        assert FakeBulb.hex_for((1, 1, 1)) not in sent_colors  # the same-key source was ignored
        worker.set_color((255, 0, 0))  # a game takes over: fixed color again
        assert wait_for(lambda: bulb.sent[-1][1].get("24") == FakeBulb.hex_for((255, 0, 0)))
        calls = len(asked)
        time.sleep(0.3)
        assert len(asked) == calls  # the effect source is no longer used
    finally:
        worker.stop(2)


def test_precise_source_error_does_not_break_the_bulb() -> None:
    bulb = FakeBulb(music=False)
    cfg = BulbConfig(name="l", id="p2", ip="1", local_key="k", min_interval=0.05)
    worker = BulbWorker(cfg, device_factory=lambda _cfg: bulb)
    worker.start()
    assert wait_for(lambda: worker.connected)
    try:
        worker.set_color_source(lambda: 1 / 0, key="broken")
        time.sleep(0.3)
        assert worker.connected and worker.is_alive()
        worker.set_color((0, 255, 0))
        assert wait_for(lambda: len(bulb.sent) >= 1)
    finally:
        worker.stop(2)


def test_precise_bulbs_match_each_other_and_the_keyboard() -> None:
    """Every bulb and the Razer devices draw from one clock: same moment, same color."""
    service = engine.SyncService(layout_detector=lambda: "full", own_pid=1)
    renderer = Renderer(Profile(effect="spectrum", speed=5))
    service.player = SimpleNamespace(renderer=renderer)
    settings = AppConfig().settings
    settings.latency_mode = "off"
    lamps = [SimpleNamespace(latency_ms=None, cfg=BulbConfig(
        name=f"l{i}", id=str(i), ip="1", local_key="k", min_brightness=0)) for i in range(3)]
    sources = [service._bulb_effect_source(w, i, 3, settings, renderer.profile, key="k")[1]
               for i, w in enumerate(lamps)]
    real = time.monotonic
    try:
        time.monotonic = lambda: 1234.5  # one moment
        colors = [src() for src in sources]
        keyboard = Renderer(Profile(effect="spectrum", speed=5)).device_frame("keyboard", 1234.5)[0]
    finally:
        time.monotonic = real
    assert colors[0] == colors[1] == colors[2] == keyboard


def test_auto_mode_uses_instant_changes_where_supported() -> None:
    """Video: the GU10 spots faded behind the mouse; they list control_data (instant)."""
    for codes, expected in ((("colour_data_v2", "control_data", "music_data"), True),
                            (("colour_data", "music_data"), False)):  # Battletron: no control_data
        bulb = FakeBulb(music=True)
        worker = BulbWorker(BulbConfig(name="x", id="x", ip="1", local_key="k", codes=codes),
                            device_factory=lambda _c, b=bulb: b)
        worker.start()
        try:
            assert wait_for(lambda: worker.connected)
            assert worker.use_music is expected
        finally:
            worker.stop(2)


def test_offset_includes_fade_and_hold() -> None:
    s = AppConfig().settings
    fading = SimpleNamespace(latency_ms=60, use_music=False, cfg=SimpleNamespace(latency_adjust_ms=0))
    instant = SimpleNamespace(latency_ms=60, use_music=True, cfg=SimpleNamespace(latency_adjust_ms=0))
    off = engine.SyncService.latency_offset_ms
    assert off(fading, s) == 60 + engine.FADE_MS
    assert off(instant, s) == 60
    s.latency_mode = "manual"
    assert off(fading, s) == s.latency_ms  # manual means exactly what the user set


def test_precise_color_is_for_the_middle_of_the_hold() -> None:
    service = engine.SyncService(layout_detector=lambda: "full", own_pid=1)
    renderer = Renderer(Profile(effect="spectrum", speed=10))  # 1.6 s per cycle: easy to see
    service.player = SimpleNamespace(renderer=renderer)
    settings = AppConfig().settings
    settings.latency_mode = "off"
    lamp = SimpleNamespace(latency_ms=None, use_music=True, _interval=lambda: 0.5,
                           cfg=BulbConfig(name="l", id="x", ip="1", local_key="k", min_brightness=0))
    real = time.monotonic
    try:
        time.monotonic = lambda: 1000.0
        sent = service._bulb_effect_color(lamp, 0, 1, settings, renderer.profile)
    finally:
        time.monotonic = real
    expected = Renderer(Profile(effect="spectrum", speed=10)).strip(1, 1000.25)[0]
    assert sent == expected


def test_old_default_colour_mode_becomes_auto(tmp_path) -> None:
    import json

    from tuyachroma.config import load as load_cfg

    path = tmp_path / "c.json"
    path.write_text(json.dumps({"settings": {}, "devices": [
        {"id": "b1", "name": "RGB Biuro 1", "local_key": "", "method": "colour"}]}), encoding="utf-8")
    assert load_cfg(path).devices[0].method == "auto"   # saved before 1.9.12: was only the default
    path.write_text(json.dumps({"settings": {"config_rev": 2}, "devices": [
        {"id": "b1", "name": "RGB Biuro 1", "local_key": "", "method": "colour"}]}), encoding="utf-8")
    assert load_cfg(path).devices[0].method == "colour"  # chosen by the user later: kept
