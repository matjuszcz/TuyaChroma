"""A light's own effect (e.g. a wave along a strip) captured from the Tuya app."""

from __future__ import annotations

from types import SimpleNamespace

from tuyachroma import bulbs, engine
from tuyachroma.bulbs import BulbConfig, BulbWorker, NativeScene
from tuyachroma.config import AppConfig, Device
from tuyachroma.profiles import Profile
from test_bulbs import FakeBulb, wait_for

WAVE_STATE = {"20": True, "21": "scene", "24": "000003e803e8", "26": 0, "47": 25, "53": 25,
              "51": "AAEAFAEA5wPoA+gB"}


def test_scene_from_state_skips_timer_and_color() -> None:
    scene = NativeScene.from_state(WAVE_STATE)
    assert scene.values() == {"20": True, "21": "scene", "47": 25, "51": "AAEAFAEA5wPoA+gB", "53": 25}
    assert NativeScene.from_state(dict(WAVE_STATE)) == scene  # same effect = same scene


def test_worker_plays_scene_then_colors_again() -> None:
    bulb = FakeBulb(music=False)
    worker = BulbWorker(BulbConfig(name="strip", id="s", ip="1", local_key="k", min_interval=0.02),
                        device_factory=lambda _cfg: bulb)
    worker.start()
    assert wait_for(lambda: worker.connected)
    try:
        scene = NativeScene.from_state(WAVE_STATE)
        worker.set_scene(scene)
        assert wait_for(lambda: worker.status == bulbs.STATUS_SCENE)
        assert bulb.sent[-1] == ("multi", {"20": True, "21": "scene", "47": 25,
                                           "51": "AAEAFAEA5wPoA+gB", "53": 25})
        n = len(bulb.sent)
        for _ in range(5):
            worker.set_scene(NativeScene.from_state(WAVE_STATE))  # every frame: sent only once
        import time
        time.sleep(0.2)
        assert len(bulb.sent) == n
        worker.set_color((255, 0, 0))  # e.g. a game: back to colors, colour mode again
        assert wait_for(lambda: worker.status == bulbs.STATUS_SYNC)
        assert bulb.sent[-1][1].get("21") == "colour" and "24" in bulb.sent[-1][1]
        worker.release()  # restores the state from before the takeover
        assert wait_for(lambda: worker.status == bulbs.STATUS_READY)
        assert bulb.sent[-1][1]["21"] == "white"
    finally:
        worker.stop(2)


def test_hold_and_read_state() -> None:
    bulb = FakeBulb(music=False)
    worker = BulbWorker(BulbConfig(name="strip", id="s2", ip="1", local_key="k", min_interval=0.02),
                        device_factory=lambda _cfg: bulb)
    worker.start()
    assert wait_for(lambda: worker.connected)
    try:
        worker.hold(True)
        worker.set_color((0, 255, 0))
        import time
        time.sleep(0.2)
        assert bulb.sent == []  # nothing sent while the user sets the app
        assert worker.read_state()["21"] == "white"
        worker.hold(False)
        assert wait_for(lambda: len(bulb.sent) == 1)
    finally:
        worker.stop(2)


def test_dispatch_plays_scene_only_in_moving_profiles() -> None:
    calls = []

    class W:
        def __init__(self, scene):
            self.cfg = SimpleNamespace(source="keyboard", zone="all", brightness=1, min_brightness=0,
                                       scene=scene)

        def set_scene(self, scene):
            calls.append(("scene", scene))

        def set_color(self, rgb):
            calls.append(("color", rgb))

    strip, bulb = W(WAVE_STATE), W(None)
    source = lambda w, i, n: (("k", i), lambda: (10, 20, 30))  # noqa: E731
    for effect, expected in (("wave", "scene"), ("spectrum", "scene"), ("wheel", "scene"),
                             ("breathing", "color"), ("static", "color")):
        calls.clear()
        engine.dispatch([strip, bulb], {}, "effect", False, profile=Profile(effect=effect),
                        effect_source=source)
        assert calls[0][0] == expected and calls[1] == ("color", (10, 20, 30))


def test_scene_setting_saved(tmp_path) -> None:
    from tuyachroma.config import load, save

    path = tmp_path / "c.json"
    config = AppConfig(devices=[Device(id="s", name="Led Strip", local_key="k", ip="1",
                                       scene=dict(WAVE_STATE), use_scene=True)])
    save(config, path)
    dev = load(path).devices[0]
    assert dev.scene == WAVE_STATE and dev.use_scene
    assert dev.bulb_config().scene == WAVE_STATE
    dev.use_scene = False
    assert dev.bulb_config().scene is None
