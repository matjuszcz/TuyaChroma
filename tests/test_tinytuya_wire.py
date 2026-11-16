"""BulbWorker against a fake light speaking the real Tuya protocol (real tinytuya)."""

from __future__ import annotations

import time

import tinytuya

from fake_tuya import KEY, FakeBulb
from tuyachroma import bulbs


def _worker(fake: FakeBulb) -> bulbs.BulbWorker:
    def factory(_cfg):
        d = tinytuya.BulbDevice(dev_id="x", address="127.0.0.1", local_key=KEY.decode(), version=3.3,
                                persist=True, connection_timeout=3, connection_retry_limit=1,
                                connection_retry_delay=1, port=fake.port)
        d.set_socketPersistent(True)
        return d

    cfg = bulbs.BulbConfig(name="b", id="x", ip="127.0.0.1", local_key=KEY.decode())
    return bulbs.BulbWorker(cfg, device_factory=factory)


def _wait(cond, t=5.0):
    end = time.monotonic() + t
    while time.monotonic() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_measured_delay_is_the_lights_own_after_unconfirmed_refreshes() -> None:
    """A refresh sent without waiting left its answer queued: the next color read it
    as its own answer and the measured delay collapsed (lights ran behind)."""
    fake = FakeBulb(delay=0.08)
    w = _worker(fake)
    w.start()
    try:
        w.set_color((10, 10, 10))
        time.sleep(0.6)
        w.set_color((10, 10, 10))
        w.update_config(w.cfg)  # a refresh of the same value: sent without waiting
        time.sleep(0.6)
        t0 = time.monotonic()
        w.set_color_source(lambda: (int((time.monotonic() - t0) * 200) % 256, 50, 0), key=1)
        time.sleep(2.5)
        assert w.latency_ms is not None and w.latency_ms >= 60
    finally:
        w.stop(5)


def test_lost_restore_is_not_reported_as_done() -> None:
    lost = {"on": False}
    fake = FakeBulb(delay=0.05, skip=lambda cmd, n, obj: lost["on"] and cmd == 7
                    and (obj.get("dps") or {}).get("21") == "white")
    w = _worker(fake)
    w.start()
    try:
        w.set_color((200, 10, 10))
        assert _wait(lambda: fake.dps["21"] == "colour")
        lost["on"] = True
        w.release()
        time.sleep(1.5)
        assert w._in_sync_mode and w.status != bulbs.STATUS_READY  # still to be given back
        lost["on"] = False
        assert _wait(lambda: fake.dps["21"] == "white" and w.status == bulbs.STATUS_READY, 20)
    finally:
        w.stop(5)


def test_restore_after_a_lost_command_is_one_command() -> None:
    """After one failed multi-value write tinytuya sent each value alone, 1 s apart."""
    drop = {"n": 0}

    def skip(cmd, n, obj):
        if cmd == 7 and len(obj.get("dps") or {}) > 1 and drop["n"] < 2:
            drop["n"] += 1
            return True
        return False

    fake = FakeBulb(delay=0.05, skip=skip)
    w = _worker(fake)
    w.start()
    w.set_color((0, 255, 0))
    assert _wait(lambda: fake.dps["21"] == "colour", 8)
    started = time.monotonic()
    w.stop(timeout=10)
    assert time.monotonic() - started < 2.0
    restores = [d for _t, c, d in fake.log if c == 7 and d and d.get("21") == "white"]
    assert restores and len(restores[-1]) >= 4  # all values in one command


def test_lost_takeover_answer_still_gives_the_light_back() -> None:
    """The light switched to our color but its answer was lost: it must be given back."""
    lost = {"n": 0}

    def skip(cmd, n, obj):
        if cmd == 7 and (obj.get("dps") or {}).get("21") == "colour" and lost["n"] < 1:
            lost["n"] += 1
            return "apply"  # applied, but no answer
        return False

    fake = FakeBulb(delay=0.05, skip=skip)
    w = _worker(fake)
    w.start()
    try:
        w.set_color((200, 10, 10))
        assert _wait(lambda: fake.dps["21"] == "colour" and lost["n"] == 1, 8)
        assert _wait(lambda: w._in_sync_mode, 8)
        w.release()
        assert _wait(lambda: fake.dps["21"] == "white" and w.status == bulbs.STATUS_READY, 20)
    finally:
        w.stop(5)


def test_color_after_a_heartbeat_is_not_held_back(monkeypatch) -> None:
    """Draining the heartbeat's empty answer must not wait for the reply timeout."""
    monkeypatch.setattr(bulbs, "HEARTBEAT_EVERY", 0.3)
    fake = FakeBulb(delay=0.01)
    w = _worker(fake)
    w.start()
    try:
        assert _wait(lambda: w.connected)
        t0 = time.monotonic()
        w.set_color_source(lambda: (int((time.monotonic() - t0) * 300) % 256, 40, 0), key=1)
        time.sleep(2.5)
    finally:
        w.stop(5)
    sends = [t for t, c, d in fake.log if c == 7 and d and "24" in d]
    gaps = [b - a for a, b in zip(sends[5:], sends[6:])]
    assert gaps and max(gaps) < 0.33, max(gaps)  # (min interval 0.15 s + a little)
