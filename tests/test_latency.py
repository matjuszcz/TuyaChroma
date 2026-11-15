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
