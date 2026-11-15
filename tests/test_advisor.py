"""Automatic choice of per-bulb settings."""

from __future__ import annotations

from tuyachroma import advisor, bulbs
from tuyachroma.cloud import CloudDevice
from tuyachroma.config import AppConfig, Device
from test_bulbs import make_worker


def dev(i, **kw):
    base = dict(id=i, name=f"L{i}", local_key="k", ip="10.0.0.1", enabled=True, category="dj")
    base.update(kw)
    return Device(**base)


def test_bulb_that_reboots_gets_slower_rate() -> None:
    lamp = dev("lampka", name="Lampka Biurko")
    strip = dev("strip", name="Led Strip", category="dd",
                codes=["colour_data", "switch_led", "work_mode"])
    ok = dev("ok", name="RGB Biuro 1")
    details = {
        "lampka": {"status": bulbs.STATUS_SYNC, "latency_ms": 80, "dps": ["20", "21", "24"],
                   "dropped_off": 0.3},
        "strip": {"status": bulbs.STATUS_SYNC, "latency_ms": 60, "dps": ["20", "21", "24"]},
        "ok": {"status": bulbs.STATUS_SYNC, "latency_ms": 70, "dps": ["20", "21", "24"]},
    }
    advice = advisor.advise([lamp, strip, ok], details)
    assert [(c.dev_id, c.field, c.new) for c in advice.changes] == [("lampka", "min_interval", 0.5)]
    assert "znikała z sieci" in advice.changes[0].describe()
    assert any("Led Strip" in n and "zwykła listwa" in n for n in advice.notes)
    assert len(advice.report) == 3 and "cloud codes=[colour_data" in advice.report[1]
    assert advisor.apply([lamp, strip, ok], advice.changes) == 1 and lamp.min_interval == 0.5
    # applied: nothing more to propose
    assert advisor.advise([lamp, strip, ok], details).changes == []


def test_other_recommendations() -> None:
    white = dev("w", name="Biała")
    slow = dev("s", name="Wolna")
    rgbic = dev("ic", name="Listwa IC", category="dd", codes=["colour_data", "paint_colour_data"])
    music = dev("m", name="Muzyczna", codes=["colour_data", "music_data"])
    gone = dev("g", name="Wyłączona")
    details = {
        "w": {"status": bulbs.STATUS_UNSUPPORTED},
        "s": {"status": bulbs.STATUS_SYNC, "latency_ms": 450},
        "ic": {"status": bulbs.STATUS_SYNC, "latency_ms": 50},
        "m": {"status": bulbs.STATUS_SYNC, "latency_ms": 50},
        "g": {"status": bulbs.STATUS_OFFLINE},
    }
    settings = AppConfig().settings
    settings.effect_sync = "ticks"
    advice = advisor.advise([white, slow, rgbic, music, gone], details, settings)
    changes = {(c.dev_id, c.field): c.new for c in advice.changes}
    assert changes == {("w", "enabled"): False, ("s", "min_interval"): 1.0}
    text = "\n".join(advice.notes)
    assert "Listwa IC" in text and "RGBIC" in text
    assert "Muzyczna" in text and "Music" in text
    assert "Wyłączona" in text and "Szukaj w sieci" in text
    assert "Oszczędna" in text


def test_device_codes_come_from_the_account() -> None:
    config = AppConfig()
    config.merge_cloud([CloudDevice("a", "A", "k", "dd", "Strip", rgb=True,
                                    codes=("colour_data", "paint_colour_data"))])
    assert config.devices[0].codes == ["colour_data", "paint_colour_data"]
    config.merge_cloud([CloudDevice("a", "A", "k", "dd", "Strip", rgb=True, codes=("colour_data",))])
    assert config.devices[0].codes == ["colour_data"]
    assert not any("Brak danych" in n for n in advisor.advise(config.devices, {}).notes)
    config.devices[0].codes = []
    assert any("Odśwież z konta" in n for n in advisor.advise(config.devices, {}).notes)


def test_worker_reports_its_data_points() -> None:
    worker, _bulb = make_worker()
    try:
        assert worker.dps == ["20", "21", "22", "23", "24"]
    finally:
        worker.stop(2)


GU10_CODES = ["bright_value_v2", "colour_data_v2", "control_data", "countdown_1", "music_data",
              "scene_data_v2", "switch_led", "temp_value_v2", "work_mode"]
BAR_CODES = ["colour_data", "countdown", "music_data", "switch_led", "work_mode"]


def test_real_setup_from_a_log() -> None:
    """The user's lights (1.8.0 log): short, readable advice only for lights Razer controls."""
    devices = [
        dev("tv", name="Lampa TV", enabled=False, codes=GU10_CODES),
        dev("lb", name="Lampka Biurko", category="dd", product_name="Battletron Light Bar",
            codes=BAR_CODES, min_interval=0.5),
        dev("ls", name="Led Strip", category="dd", product_name="Battletron Smart desk light Strip",
            codes=BAR_CODES),
        dev("b1", name="RGB Biuro 1", codes=GU10_CODES),
        dev("j1", name="RGB Jadalnia 1", enabled=False, ip="", codes=GU10_CODES),
    ]
    bar = {"status": bulbs.STATUS_SYNC, "latency_ms": 11, "dps": ["20", "21", "24", "26", "47", "53"]}
    details = {"lb": dict(bar, dropped_off=0.3), "ls": dict(bar, dps=bar["dps"] + ["34"]),
               "b1": {"status": bulbs.STATUS_SYNC, "latency_ms": 70,
                      "dps": ["20", "21", "22", "23", "24", "25", "26", "34"]}}
    advice = advisor.advise(devices, details)
    assert advice.changes == []  # Lampka Biurko is already on "wolno"
    text = "\n".join(advice.notes)
    assert "Lampa TV" not in text and "Jadalnia" not in text  # not controlled by Razer
    assert text.count("Tryb Music") == 1 and "Lampka Biurko, Led Strip, RGB Biuro 1" in text
    assert "możliwe, że to RGBIC" in text  # DP 53 without confirmation from the account
    assert len(advice.report) == 5  # the report still lists everything
