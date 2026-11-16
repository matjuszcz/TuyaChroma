"""BulbWorker and SyncEngine with a fake tinytuya bulb."""

from __future__ import annotations

import time

import pytest

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

    @staticmethod
    def hex_for(rgb):
        import tinytuya

        return tinytuya.BulbDevice.rgb_to_hexvalue(*rgb, "hsv16")


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


def test_switched_off_bulb_is_quiet(monkeypatch, caplog) -> None:
    """A bulb without power: calm status, one log line, ever longer pauses, then back."""
    from tuyachroma import bulbs

    # long enough for Windows' ~15 ms timer: 50, 100, 200 ms (10/40 ms both came out 31 ms)
    monkeypatch.setattr(bulbs, "RETRY_FIRST", 0.05)
    monkeypatch.setattr(bulbs, "RETRY_MAX", 0.4)
    bulb = FakeBulb()
    offline = {"Error": "Network Error: Device Unreachable", "Err": "905", "Payload": None}
    calls = []
    power = {"on": False}

    def status():
        calls.append(time.monotonic())
        return offline if not power["on"] else FakeBulb.status(bulb)

    bulb.status = status
    cfg = BulbConfig(name="biuro", id="x", ip="1.2.3.4", local_key="k")
    worker = BulbWorker(cfg, device_factory=lambda _cfg: bulb)
    with caplog.at_level("DEBUG", logger="tuyachroma.bulbs"):
        worker.start()
        try:
            assert wait_for(lambda: len(calls) >= 5, 4)
            assert worker.status == bulbs.STATUS_OFFLINE
            warnings = [r for r in caplog.records if r.levelname == "WARNING"]
            assert len(warnings) == 1
            gaps = [b - a for a, b in zip(calls, calls[1:])]
            assert gaps[2] > gaps[0]  # backing off
            power["on"] = True  # switched on again
            assert wait_for(lambda: worker.connected)
            assert worker.status == STATUS_READY
            assert any("reachable again" in r.message for r in caplog.records)
        finally:
            worker.stop(2)


def test_unconfirmed_color_reconnects_and_resends(monkeypatch) -> None:
    """tinytuya returns errors instead of raising: the lamp froze without notice."""
    from tuyachroma import bulbs as _b

    monkeypatch.setattr(_b, "CONFIRM_EVERY", 0.0)  # every color confirmed: errors show at once
    worker, bulb = make_worker(music=False)
    try:
        worker.set_color((255, 0, 0))
        assert wait_for(lambda: len(bulb.sent) >= 1)
        fail = {"left": 1}
        real = bulb.set_multiple_values

        def flaky(values, nowait=False):
            real(values, nowait)
            if fail["left"]:
                fail["left"] -= 1
                return {"Error": "Check device key or version", "Err": "914", "Payload": None}
            return None

        bulb.set_multiple_values = flaky
        worker.set_color((0, 0, 255))
        # reconnected (quickly) and sent the color again, now more slowly
        assert wait_for(lambda: fail["left"] == 0 and worker.connected
                        and worker.status == STATUS_SYNC and worker.slow_interval >= 0.3, 3)
        n = len(bulb.sent)
        worker.set_color((0, 255, 0))
        assert wait_for(lambda: len(bulb.sent) > n)
        assert worker.status == STATUS_SYNC
    finally:
        worker.stop(2)


def test_unchanged_color_is_refreshed(monkeypatch) -> None:
    """A bulb that rebooted by itself gets the current color again."""
    from tuyachroma import bulbs

    monkeypatch.setattr(bulbs, "REFRESH_EVERY", 0.3)
    worker, bulb = make_worker(music=False)
    try:
        worker.set_color((255, 0, 0))
        assert wait_for(lambda: len(bulb.sent) >= 1)
        n = len(bulb.sent)
        for _ in range(10):
            worker.set_color((255, 0, 0))  # the engine keeps sending the same color
            time.sleep(0.1)
        assert len(bulb.sent) >= n + 2
    finally:
        worker.stop(2)


def test_bulb_that_dropped_off_stays_slower(monkeypatch) -> None:
    """Lampka Biurko vanished from Wi-Fi under a fast effect: remember to go easy on it."""
    from tuyachroma import bulbs as _b

    monkeypatch.setattr(_b, "CONFIRM_EVERY", 0.0)  # every color confirmed: errors show at once
    from tuyachroma import bulbs

    monkeypatch.setattr(bulbs, "_learned_slow", {})
    monkeypatch.setattr(bulbs, "RETRY_FIRST", 0.01)
    monkeypatch.setattr(bulbs, "SLOW_RECOVER", 0.0)  # would normally speed up again
    worker, bulb = make_worker(music=False)
    try:
        worker.set_color((255, 0, 0))
        assert wait_for(lambda: len(bulb.sent) >= 1)
        gone = {"left": 1}
        real = bulb.set_multiple_values

        def drop(values, nowait=False):
            real(values, nowait)
            if gone["left"]:
                gone["left"] -= 1
                return {"Error": "Network Error: Device Unreachable", "Err": "905", "Payload": None}
            return None

        bulb.set_multiple_values = drop
        worker.set_color((0, 0, 255))
        assert wait_for(lambda: gone["left"] == 0 and worker.connected and worker.status == STATUS_SYNC, 3)
        for color in ((1, 200, 1), (200, 1, 1), (1, 1, 200)):
            worker.set_color(color)
            time.sleep(0.1)
        assert worker.slow_interval >= 0.3  # did not recover below what it learned
        assert bulbs._learned_slow["x"] >= 0.3
    finally:
        worker.stop(2)
    again = BulbWorker(worker.cfg, device_factory=lambda _cfg: FakeBulb())
    assert again.slow_interval >= 0.3  # a new worker (e.g. after changing settings) remembers


def test_light_that_came_back_in_white_mode_gets_colors_again() -> None:
    """Switched off and on at the wall: it comes back white; colors alone wouldn't show."""
    bulb = FakeBulb(music=False)
    cfg = BulbConfig(name="lamp", id="x", ip="1.2.3.4", local_key="k", min_interval=0.05)
    worker = BulbWorker(cfg, device_factory=lambda _cfg: bulb)
    worker._in_sync_mode, worker._target = True, (1, 2, 3)  # showing our colors before
    worker._connect()            # FakeBulb reports mode "white"
    worker._send((255, 0, 0))
    assert bulb.sent[-1][1].get("20") is True and bulb.sent[-1][1].get("21") == "colour"
    worker._send((0, 255, 0))   # back to plain colors
    assert "20" not in bulb.sent[-1][1]

    class InColour(FakeBulb):
        def status(self):
            return {"dps": {"20": True, "21": "colour", "22": 500, "23": 0, "24": "000003e803e8"}}

    bulb2 = InColour(music=False)
    worker2 = BulbWorker(cfg, device_factory=lambda _cfg: bulb2)
    worker2._in_sync_mode, worker2._target = True, (1, 2, 3)
    worker2._connect()
    worker2._send((255, 0, 0))
    assert "20" not in bulb2.sent[-1][1]  # still in our mode: just the color


def test_light_switched_off_on_purpose_stays_off_after_reconnect() -> None:
    class Off(FakeBulb):
        def status(self):
            return {"dps": {"20": False, "21": "colour", "22": 500, "23": 0, "24": "000003e803e8"}}

    bulb = Off(music=False)
    cfg = BulbConfig(name="lamp", id="x", ip="1.2.3.4", local_key="k", min_interval=0.05)
    worker = BulbWorker(cfg, device_factory=lambda _cfg: bulb)
    worker._in_sync_mode, worker._target = True, (1, 2, 3)
    worker._connect()
    worker._send((255, 0, 0))
    assert "20" not in bulb.sent[-1][1]


def test_unconfirmed_restore_is_tried_again() -> None:
    """A light that skipped the answer to the restore must not stay on the Razer color."""
    worker, bulb = make_worker(music=False)
    try:
        worker.set_color((255, 0, 0))
        assert wait_for(lambda: worker.status == STATUS_SYNC)
        real = bulb.set_multiple_values
        skipped = {"left": 1}

        def flaky(values, nowait=False):
            real(values, nowait)
            if values.get("21") == "white" and skipped["left"]:
                skipped["left"] -= 1
                return {"Error": "Timeout", "Err": "902", "Payload": None}
            return None

        bulb.set_multiple_values = flaky
        worker.release()
        assert wait_for(lambda: skipped["left"] == 0 and worker.status == STATUS_READY, 12)
        restores = [v for k, v in bulb.sent if k == "multi" and v.get("21") == "white"]
        assert len(restores) == 2  # tried again after reconnecting
    finally:
        worker.stop(2)


def test_takeover_uses_the_connection_state_if_the_read_is_skipped() -> None:
    bulb = FakeBulb(music=False)
    cfg = BulbConfig(name="lamp", id="x", ip="1.2.3.4", local_key="k", min_interval=0.05)
    worker = BulbWorker(cfg, device_factory=lambda _cfg: bulb)
    worker._connect()
    bulb.status = lambda: {"Error": "Timeout", "Err": "902", "Payload": None}
    worker._send((255, 0, 0))
    assert worker._original.get("mode") == "white" and worker._original.get("switch") is True


def test_full_send_repeated_until_confirmed() -> None:
    """A lost switch+mode send must not be followed by colour-only sends."""
    bulb = FakeBulb(music=False)
    cfg = BulbConfig(name="lamp", id="x", ip="1.2.3.4", local_key="k", min_interval=0.05)
    worker = BulbWorker(cfg, device_factory=lambda _cfg: bulb)
    worker._connect()
    real = bulb.set_multiple_values
    bulb.set_multiple_values = lambda values, nowait=False: (real(values, nowait),
                                                             {"Err": "902", "Error": "Timeout"})[1]
    with pytest.raises(ConnectionError):
        worker._send((255, 0, 0))
    bulb.set_multiple_values = real
    worker._send((0, 255, 0))
    assert bulb.sent[-1][1].get("20") is True and bulb.sent[-1][1].get("21") == "colour"


def test_heartbeat_waits_only_in_music_mode(monkeypatch) -> None:
    from tuyachroma import bulbs as _b

    monkeypatch.setattr(_b, "HEARTBEAT_EVERY", 0.1)
    for music, expected in ((False, True), (True, False)):
        seen = []

        class Beat(FakeBulb):
            def heartbeat(self, nowait=False):
                seen.append(nowait)

        bulb = Beat(music=music)
        cfg = BulbConfig(name="l", id="hb", ip="1", local_key="k", min_interval=0.05,
                         method="music" if music else "colour")
        worker = BulbWorker(cfg, device_factory=lambda _c: bulb)
        worker.start()
        try:
            assert wait_for(lambda: len(seen) >= 2, 3)
        finally:
            worker.stop(2)
        assert set(seen) == {expected}, (music, seen)


def test_scene_mode_after_reconnect_is_switched_back_to_colors() -> None:
    class InScene(FakeBulb):
        def status(self):
            return {"dps": {"20": True, "21": "scene", "22": 500, "23": 0, "24": "000003e803e8"}}

    bulb = InScene(music=False)
    cfg = BulbConfig(name="lamp", id="x", ip="1.2.3.4", local_key="k", min_interval=0.05)
    worker = BulbWorker(cfg, device_factory=lambda _cfg: bulb)
    worker._in_sync_mode, worker._target = True, (1, 2, 3)
    worker._connect()
    assert worker._resend_full
    from tuyachroma.bulbs import NativeScene

    worker2 = BulbWorker(cfg, device_factory=lambda _cfg: InScene(music=False))
    worker2._in_sync_mode, worker2._target = True, NativeScene.from_state({"21": "scene"})
    worker2._connect()
    assert not worker2._resend_full  # it plays its own effect: leave it
