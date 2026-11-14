"""BulbWorker and SyncEngine with a fake tinytuya bulb."""

from __future__ import annotations

import time

from tuyachroma_bridge.app import SyncEngine, bulb_color
from tuyachroma_bridge.bulbs import BulbConfig, BulbWorker
from tuyachroma_bridge.config import Config, ConfigError, EXAMPLE, parse_config
from tuyachroma_bridge.razer import Frame

import pytest


class FakeBulb:
    """Type B bulb (DP 20-28) recording what was sent."""

    def __init__(self, music: bool = True) -> None:
        self.bulb_configured = True
        self.bulb_type = "B"
        self.dpset = {
            "switch": "20", "mode": "21", "brightness": "22", "colourtemp": "23",
            "colour": "24", "music": "28" if music else None, "value_hexformat": "hsv16",
        }
        self.sent: list = []
        self.closed = False

    def status(self):
        return {"dps": {"20": True, "21": "white", "22": 500, "23": 0, "24": "000003e803e8"}}

    def state(self):
        return {"switch": True, "mode": "white", "brightness": 500, "colourtemp": 0,
                "colour": "000003e803e8"}

    def set_multiple_values(self, values, nowait=False):
        self.sent.append(("multi", dict(values)))

    def set_music_colour(self, transition, r, g, b, nowait=False):
        self.sent.append(("music", (r, g, b)))

    def heartbeat(self, nowait=False):
        pass

    def close(self):
        self.closed = True


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def make_worker(music=True, **kw):
    bulb = FakeBulb(music)
    cfg = BulbConfig(name="lamp", id="x", ip="1.2.3.4", local_key="k", min_interval=0.05, **kw)
    worker = BulbWorker(cfg, device_factory=lambda _cfg: bulb)
    worker.start()
    assert wait_for(lambda: worker.connected)
    return worker, bulb


def test_music_mode_and_restore() -> None:
    worker, bulb = make_worker()
    worker.set_color((255, 0, 0))
    assert wait_for(lambda: ("music", (255, 0, 0)) in bulb.sent)
    assert bulb.sent[0] == ("multi", {"20": True, "21": "music"})

    worker.release()
    assert wait_for(lambda: bulb.sent[-1][0] == "multi" and bulb.sent[-1][1].get("21") == "white")
    assert bulb.sent[-1][1] == {"20": True, "21": "white", "22": 500, "23": 0, "24": "000003e803e8"}
    worker.stop()
    assert bulb.closed


def test_colour_mode_coalesces_updates() -> None:
    worker, bulb = make_worker(music=False)
    for i in range(50):  # a burst of colors: only the newest few are sent
        worker.set_color((i * 5, 0, 255))
    assert wait_for(lambda: any(v.get("24") for k, v in bulb.sent if k == "multi"))
    time.sleep(0.2)
    colours = [v["24"] for k, v in bulb.sent if k == "multi" and "24" in v]
    assert len(colours) < 10
    first = next(v for k, v in bulb.sent if k == "multi" and "24" in v)
    assert first["21"] == "colour" and first["20"] is True
    worker.stop()


def test_stop_restores_state() -> None:
    worker, bulb = make_worker()
    worker.set_color((0, 0, 255))
    assert wait_for(lambda: ("music", (0, 0, 255)) in bulb.sent)
    worker.stop()
    assert bulb.sent[-1] == ("multi", {"20": True, "21": "white", "22": 500, "23": 0,
                                        "24": "000003e803e8"})


def test_sync_engine() -> None:
    config = parse_config(EXAMPLE | {"bulbs": [dict(EXAMPLE["bulbs"][0])]})

    class Recorder:
        def __init__(self, cfg):
            self.cfg = cfg
            self.calls = []

        def set_color(self, rgb):
            self.calls.append(rgb)

        def release(self):
            self.calls.append("release")

    rec = Recorder(config.bulbs[0])
    engine = SyncEngine(config, [rec])
    colors = [(20, 20, 20)] * 120 + [(0, 0, 255)] * 12
    engine.process({"keyboard": Frame("keyboard", 7, colors, 1)})
    assert rec.calls[-1][2] > 200 and rec.calls[-1][0] < 60
    engine.process({"keyboard": Frame("keyboard", 13, None, 2)})
    assert rec.calls[-1] == "release"
    engine.process({"keyboard": None})
    assert rec.calls[-1] == "release"


def test_bulb_color_zone() -> None:
    cfg = BulbConfig(name="l", id="x", ip="i", local_key="k", zone="right", min_brightness=0)
    colors = [(255, 0, 0) if x < 11 else (0, 255, 0) for _y in range(6) for x in range(22)]
    assert bulb_color(cfg, Frame("keyboard", 7, colors, 1)) == (0, 255, 0)


def test_config_validation() -> None:
    with pytest.raises(ConfigError):
        parse_config({"bulbs": []})
    with pytest.raises(ConfigError):
        parse_config({"bulbs": [{"id": "x", "ip": "y"}]})
    with pytest.raises(ConfigError):
        parse_config({"bulbs": [{"id": "x", "ip": "y", "local_key": "k", "source": "toaster"}]})
    cfg = parse_config({"bulbs": [{"id": "x", "ip": "y", "local_key": "k", "version": 3.4}]})
    assert cfg.bulbs[0].version == "3.4" and cfg.bulbs[0].name == "x"
    assert cfg.sources == ["keyboard"]
