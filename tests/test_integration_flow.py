"""Whole chain: SyncService + real BulbWorker threads + simulated bulbs."""

from __future__ import annotations

import time

from tuyachroma import engine
from tuyachroma.bulbs import STATUS_READY, STATUS_SYNC, BulbWorker
from tuyachroma.config import AppConfig, Device
from tuyachroma.razer import Frame, RazerUnavailable
from test_bulbs import FakeBulb, wait_for

HA_STATE = {"20": True, "21": "white", "22": 500, "23": 0, "24": "000003e803e8"}


class World:
    """Simulated Synapse: alive or not, game effect or not."""

    def __init__(self) -> None:
        self.alive = True
        self.game_color = None  # None = no game

    def reader(self, sources):
        if not self.alive:
            raise RazerUnavailable("RzSDKService nie działa")
        world = self

        class Reader:
            closed = False

            def wait(self, ms):
                time.sleep(ms / 1000)

            def read(self, src):
                if world.game_color is None:
                    return Frame(src, 13, None, 1)
                return Frame(src, 6, [world.game_color] * 132, 2)

            def close(self):
                self.closed = True

        return Reader()


def make(monkeypatch, idle="effect"):
    monkeypatch.setattr(engine, "SERVICE_CHECK_INTERVAL", 0.05)
    world = World()
    bulbs: dict[str, FakeBulb] = {}
    workers: list[BulbWorker] = []

    def worker_factory(cfg):
        cfg.min_interval = 0.02
        bulb = bulbs.setdefault(cfg.id, FakeBulb(music=False))
        w = BulbWorker(cfg, device_factory=lambda _c: bulb)
        workers.append(w)
        return w

    service = engine.SyncService(reader_factory=world.reader, worker_factory=worker_factory,
                                 service_check=lambda: world.alive, screen_factory=lambda: None)
    config = AppConfig(devices=[
        Device(id=i, name=i, local_key="k" * 16, ip="10.0.0.1", enabled=True) for i in ("a", "b")
    ])
    config.settings.idle = idle
    config.settings.active_profile = "Wave"
    config.settings.bulb_effect_rate = 5
    return world, bulbs, workers, service, config


def colours(bulb):
    return [v["24"] for k, v in bulb.sent if k == "multi" and "24" in v]


def test_full_day(monkeypatch) -> None:
    world, bulbs, workers, service, config = make(monkeypatch)
    service.start(config)
    try:
        # PC on, Synapse running, no game: both bulbs play the wave, differently
        assert wait_for(lambda: all(w.status == STATUS_SYNC for w in workers), 3)
        time.sleep(0.6)
        assert colours(bulbs["a"])[-1] != colours(bulbs["b"])[-1]

        # a game starts: both bulbs show the game color
        world.game_color = (255, 0, 0)
        expected = "0000" + "03e8" * 2  # red in hsv16
        assert wait_for(lambda: all(colours(b) and colours(b)[-1] == expected
                                    for b in bulbs.values()), 3)

        # game ends: back to the effect
        world.game_color = None
        assert wait_for(lambda: colours(bulbs["a"])[-1] != expected, 3)

        # Synapse closes / PC shuts down: bulbs go back to Home Assistant's state
        world.alive = False
        assert wait_for(lambda: all(w.status == STATUS_READY for w in workers), 3)
        for bulb in bulbs.values():
            assert bulb.sent[-1] == ("multi", HA_STATE)
    finally:
        service.stop()


def test_stop_returns_bulbs_to_ha(monkeypatch) -> None:
    world, bulbs, workers, service, config = make(monkeypatch)
    service.start(config)
    assert wait_for(lambda: all(w.status == STATUS_SYNC for w in workers), 3)
    service.stop()  # program closed / sync paused / Windows sleep
    for bulb in bulbs.values():
        assert bulb.sent[-1] == ("multi", HA_STATE)
        assert bulb.closed


def test_restore_mode_leaves_bulbs_alone_without_game(monkeypatch) -> None:
    world, bulbs, workers, service, config = make(monkeypatch, idle="restore")
    service.start(config)
    try:
        assert wait_for(lambda: all(w.connected for w in workers), 3)
        time.sleep(0.3)
        assert not any(colours(b) for b in bulbs.values())  # HA keeps control
        world.game_color = (0, 255, 0)
        assert wait_for(lambda: all(colours(b) for b in bulbs.values()), 3)
        world.game_color = None
        assert wait_for(lambda: all(b.sent[-1] == ("multi", HA_STATE) for b in bulbs.values()), 3)
    finally:
        service.stop()
