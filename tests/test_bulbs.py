"""BulbWorker and SyncEngine with a fake tinytuya bulb."""

from __future__ import annotations

import time

from tuyachroma.bulbs import STATUS_READY, STATUS_SYNC, STATUS_UNSUPPORTED, BulbConfig, BulbWorker
from tuyachroma.engine import bulb_color
from tuyachroma.razer import Frame


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
        self._cached = dict(self.current)
        return {"dps": {"20": True, "21": "white", "22": 500, "23": 0, "24": "000003e803e8"}}

    current = {"switch": True, "mode": "white", "brightness": 500, "colourtemp": 0,
               "colour": "000003e803e8"}
    _cached = None

    def state(self):
        # Like tinytuya with a persistent socket: the cache, refreshed by status()
        return dict(self._cached if self._cached is not None else self.current)

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
    worker, bulb = make_worker(method="music")
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
    worker, bulb = make_worker(method="music")
    worker.set_color((0, 0, 255))
    assert wait_for(lambda: ("music", (0, 0, 255)) in bulb.sent)
    worker.stop()
    assert bulb.sent[-1] == ("multi", {"20": True, "21": "white", "22": 500, "23": 0,
                                        "24": "000003e803e8"})


def test_bulb_color_zone() -> None:
    cfg = BulbConfig(name="l", id="x", ip="i", local_key="k", zone="right", min_brightness=0)
    colors = [(255, 0, 0) if x < 11 else (0, 255, 0) for _y in range(6) for x in range(22)]
    assert bulb_color(cfg, Frame("keyboard", 7, colors, 1)) == (0, 255, 0)




def test_restore_uses_state_from_just_before_takeover() -> None:
    worker, bulb = make_worker(method="music")
    assert worker.status == STATUS_READY
    # Meanwhile HA turned the lamp off; then a game starts.
    bulb.current = {"switch": False, "mode": "colour", "brightness": 10, "colourtemp": 0,
                    "colour": "007803e803e8"}
    worker.set_color((0, 255, 0))
    assert wait_for(lambda: worker.status == STATUS_SYNC)
    worker.release()
    assert wait_for(lambda: worker.status == STATUS_READY)
    assert bulb.sent[-1] == ("multi", {"20": False, "21": "colour", "22": 10, "23": 0,
                                        "24": "007803e803e8"})
    worker.stop()


def test_non_rgb_bulb_is_not_retried() -> None:
    bulb = FakeBulb()
    bulb.dpset["colour"] = None
    calls = []

    def factory(_cfg):
        calls.append(1)
        return bulb

    cfg = BulbConfig(name="white", id="x", ip="1.2.3.4", local_key="k")
    worker = BulbWorker(cfg, device_factory=factory)
    worker.start()
    worker.join(2)
    assert not worker.is_alive()
    assert worker.status == STATUS_UNSUPPORTED and len(calls) == 1


def test_default_is_colour_mode() -> None:
    worker, bulb = make_worker()  # bulb claims music support, but default is colour
    worker.set_color((255, 0, 0))
    assert wait_for(lambda: any(k == "multi" and "24" in v for k, v in bulb.sent))
    assert not any(k == "music" for k, _ in bulb.sent)
    worker.stop()


def test_restore_retried_after_connection_error() -> None:
    bulb = FakeBulb()
    fail = {"on": False}
    real_multi = bulb.set_multiple_values

    def flaky(values, nowait=False):
        if fail["on"] and values.get("21") == "white":
            fail["on"] = False
            raise ConnectionError("bulb went away")
        real_multi(values, nowait)

    bulb.set_multiple_values = flaky
    cfg = BulbConfig(name="lamp", id="x", ip="1.2.3.4", local_key="k", min_interval=0.05)
    worker = BulbWorker(cfg, device_factory=lambda _cfg: bulb)
    worker.start()
    assert wait_for(lambda: worker.connected)
    worker.set_color((0, 0, 255))
    assert wait_for(lambda: worker.status == STATUS_SYNC)
    fail["on"] = True
    worker.release()  # first restore fails, worker reconnects (after 5 s) and retries
    assert wait_for(lambda: worker.status == STATUS_READY, timeout=8)
    assert bulb.sent[-1][1].get("21") == "white"
    worker.stop()
