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


def test_renewing_is_quiet(monkeypatch, caplog) -> None:
    _clean(monkeypatch)
    bulbs._max_age["q"] = 0.2
    statuses = []
    worker = BulbWorker(BulbConfig(name="bar", id="q", ip="1", local_key="k", min_interval=0.02),
                        device_factory=lambda _cfg: FakeBulb(music=False))
    with caplog.at_level("INFO", logger="tuyachroma.bulbs"):
        worker.start()
        try:
            assert wait_for(lambda: worker.connected)
            worker.set_color((255, 0, 0))
            end = time.monotonic() + 1.0
            while time.monotonic() < end:
                statuses.append(worker.status)
                time.sleep(0.005)
        finally:
            worker.stop(2)
    connects = [r for r in caplog.records if "connected (type" in r.message and r.levelname == "INFO"]
    assert len(connects) == 1  # only the first connection is logged
    assert bulbs.STATUS_CONNECTING not in statuses[statuses.index(bulbs.STATUS_SYNC):]


def test_rate_is_not_blamed_when_slowing_down_does_not_help(monkeypatch) -> None:
    """1.9.5 log: Lampka Biurko dropped off at random times even at 1 color per second."""
    _clean(monkeypatch)
    monkeypatch.setattr(bulbs, "_rate_innocent", set())
    worker = BulbWorker(BulbConfig(name="Lampka Biurko", id="lb", ip="1", local_key="k"),
                        device_factory=lambda _cfg: FakeBulb())
    for _ in range(4):  # 0.3 -> 0.6 -> 1.0 s, then again at 1.0 s
        worker._slow_down(dropped_off=True)
    assert bulbs.rate_not_the_cause("lb")
    assert worker.slow_interval == 0.0 and bulbs.learned_slow_interval("lb") == 0.0
    worker._slow_down(dropped_off=True)  # later drops: no slowing down any more
    assert worker.slow_interval == 0.0


def test_advisor_gives_the_speed_back() -> None:
    from tuyachroma import advisor
    from tuyachroma.config import Device

    lamp = Device(id="lb", name="Lampka Biurko", local_key="k", ip="1", enabled=True, category="dd",
                  product_name="Battletron Light Bar", min_interval=1.0)
    advice = advisor.advise([lamp], {"lb": {"status": bulbs.STATUS_SYNC, "latency_ms": 11,
                                            "rate_not_the_cause": True, "dropped_off": 0.0}})
    assert [(c.field, c.new) for c in advice.changes] == [("min_interval", 0.3)]
    assert any("zasilacz" in n for n in advice.notes)


def test_renewing_keeps_the_lights_own_effect_playing(monkeypatch) -> None:
    """Renewal before the light resets itself must not restart its scene (a jump)."""
    _clean(monkeypatch)
    bulbs._max_age["s"] = 0.2
    bulb = FakeBulb(music=False)
    connects = []
    worker = BulbWorker(BulbConfig(name="bar", id="s", ip="1", local_key="k", min_interval=0.02),
                        device_factory=lambda _cfg: connects.append(1) or bulb)
    scene = bulbs.NativeScene.from_state({"21": "scene", "25": "abcdef"})
    worker.start()
    try:
        assert wait_for(lambda: worker.connected)
        worker.set_scene(scene)
        time.sleep(1.0)  # several renewals
    finally:
        worker.stop(2)
    scenes = [v for k, v in bulb.sent if k == "multi" and v.get("21") == "scene"]
    assert len(connects) >= 2  # it did renew
    assert len(scenes) == 1


def test_learned_behaviour_is_remembered_between_runs(tmp_path, monkeypatch) -> None:
    path = tmp_path / "learned.json"
    bulbs.load_learned(path)
    for age in (183.0, 185.0):  # dropped off twice after the same time
        bulbs._record_drop_age("lb", "Light Bar", age)
    bulbs._learned_slow["gu"] = 0.6
    bulbs._remember("gu")
    bulbs._music_hangs.add("b2")
    bulbs._remember("b2")
    # the next run
    for name in ("_learned_slow", "_max_age", "_drop_ages", "_learned_at"):
        monkeypatch.setattr(bulbs, name, {})
    monkeypatch.setattr(bulbs, "_music_hangs", set())
    bulbs.load_learned(path)
    assert bulbs.learned_max_age("lb") == 183.0 - bulbs.RENEW_EARLY
    assert bulbs.learned_slow_interval("gu") == 0.6 and "b2" in bulbs._music_hangs
    bulbs.forget_learned("lb")
    monkeypatch.setattr(bulbs, "_max_age", {})
    bulbs.load_learned(path)
    assert bulbs.learned_max_age("lb") == 0.0  # forgotten for good


def test_old_lessons_are_forgotten(tmp_path) -> None:
    import json

    path = tmp_path / "learned.json"
    old = time.time() - (bulbs.LEARNED_KEEP_DAYS + 1) * 86400
    path.write_text(json.dumps({"x": {"at": old, "slow": 1.0}, "y": {"at": time.time(), "slow": 0.6},
                                "z": "junk", "w": {"at": "now", "slow": 1.0}}))
    bulbs.load_learned(path)
    assert bulbs.learned_slow_interval("x") == 0.0 and bulbs.learned_slow_interval("y") == 0.6
    assert bulbs.learned_slow_interval("w") == 0.0


def test_a_light_switched_off_teaches_nothing(tmp_path, monkeypatch) -> None:
    """Power cut by hand while syncing: not a reboot - no slower rate, no renewal time."""
    bulbs.load_learned(tmp_path / "learned.json")
    w = BulbWorker(BulbConfig(name="RGB Biuro 1", id="gu", ip="1", local_key="k", min_interval=0.1),
                   device_factory=lambda _cfg: FakeBulb())
    for away in (400.0, 410.0):  # two power cuts after a similar connection age
        w._pending_drop = (time.monotonic() - 600.0, away)
        w._slow_down(dropped_off=True)
        w._judge_drop()  # back after 10 minutes
    assert bulbs.learned_max_age("gu") == 0.0 and bulbs.learned_slow_interval("gu") == 0.0
    assert w.slow_interval == 0.0
    assert not (tmp_path / "learned.json").exists() or "gu" not in (tmp_path / "learned.json").read_text()
    # a real reboot (back within seconds) is learned and kept
    for age in (183.0, 185.0):
        w._pending_drop = (time.monotonic() - 20.0, age)
        w._judge_drop()
    assert bulbs.learned_max_age("gu") == 183.0 - bulbs.RENEW_EARLY
    assert "gu" in (tmp_path / "learned.json").read_text()


def test_speed_change_forgets_only_the_rate(tmp_path) -> None:
    bulbs.load_learned(tmp_path / "learned.json")
    bulbs._learned_slow["lb"] = 0.6
    bulbs._max_age["lb"] = 150.0
    bulbs._music_hangs.add("lb")
    bulbs.forget_learned("lb", rate_only=True)
    assert bulbs.learned_slow_interval("lb") == 0.0
    assert bulbs.learned_max_age("lb") == 150.0 and "lb" in bulbs._music_hangs


def test_damaged_learned_file_never_stops_the_program(tmp_path) -> None:
    path = tmp_path / "learned.json"
    for text in ('{"x": {"at": 1e999999, "slow": 0.6}}', '{"x": {"at": 10' + "0" * 400 + '}}',
                 "[" * 100000, '{"x": {"at": Infinity, "max_age": Infinity}}', "junk"):
        path.write_text(text)
        bulbs.load_learned(path)  # no exception
    assert bulbs.learned_max_age("x") == 0.0
