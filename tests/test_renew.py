"""Lights that reboot after a fixed time on one connection (Lampka Biurko: ~183 s)."""

from __future__ import annotations

import time

from tuyachroma import bulbs
from tuyachroma.bulbs import BulbConfig, BulbWorker
from test_bulbs import FakeBulb, wait_for


def _clean(monkeypatch):
    monkeypatch.setattr(bulbs, "_drop_ages", {})
    monkeypatch.setattr(bulbs, "_max_age", {})
    monkeypatch.setattr(bulbs, "_learned_slow", {})


def test_learns_the_connection_age_from_the_log_pattern(monkeypatch) -> None:
    _clean(monkeypatch)
    bulbs._learned_slow["lb"] = 1.0  # what the drops taught before
    bulbs._record_drop_age("lb", "Lampka Biurko", 30.0)   # too short to learn from
    bulbs._record_drop_age("lb", "Lampka Biurko", 183.0)
    assert bulbs.learned_max_age("lb") == 0.0             # once is not a pattern
    bulbs._record_drop_age("lb", "Lampka Biurko", 184.0)
    assert bulbs.learned_max_age("lb") == 153.0           # 30 s before the limit
    assert bulbs.learned_slow_interval("lb") == 0.0       # it was not about too many colors
    bulbs._record_drop_age("x", "Other", 100.0)
    bulbs._record_drop_age("x", "Other", 300.0)           # random drops: nothing learned
    assert bulbs.learned_max_age("x") == 0.0


def test_worker_renews_the_connection_in_time(monkeypatch) -> None:
    _clean(monkeypatch)
    bulbs._max_age["r"] = 0.3
    made = []

    def factory(_cfg):
        made.append(FakeBulb(music=False))
        return made[-1]

    worker = BulbWorker(BulbConfig(name="bar", id="r", ip="1", local_key="k", min_interval=0.02),
                        device_factory=factory)
    worker.start()
    try:
        assert wait_for(lambda: worker.connected)
        worker.set_color((255, 0, 0))
        assert wait_for(lambda: len(made) >= 3, 3)            # renewed again and again
        assert wait_for(lambda: made[-1].sent and worker.status == bulbs.STATUS_SYNC)
        assert made[-1].sent[-1][1].get("24")                  # color sent on the new connection
        assert all(v.get("21") != "white" for b in made for _k, v in b.sent)  # no restore between
        assert worker.slow_interval == 0.0                     # renewing is not a problem
    finally:
        worker.stop(2)


def test_heartbeat_also_while_colors_flow(monkeypatch) -> None:
    _clean(monkeypatch)
    monkeypatch.setattr(bulbs, "HEARTBEAT_EVERY", 0.1)
    beats = []

    class Bar(FakeBulb):
        def heartbeat(self, nowait=False):
            beats.append(time.monotonic())

    bar = Bar(music=False)
    worker = BulbWorker(BulbConfig(name="bar", id="h", ip="1", local_key="k", min_interval=0.02),
                        device_factory=lambda _cfg: bar)
    worker.start()
    try:
        assert wait_for(lambda: worker.connected)
        worker.set_color_source(lambda: (int(time.monotonic() * 500) % 200 + 20, 0, 0), key="fx")
        time.sleep(0.6)
        assert len(bar.sent) > 10 and len(beats) >= 3
    finally:
        worker.stop(2)
