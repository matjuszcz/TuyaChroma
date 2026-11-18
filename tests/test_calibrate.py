"""Camera calibration against a simulated camera, keyboard and lights."""

from __future__ import annotations

import time
from types import SimpleNamespace

from tuyachroma import calibrate, engine
from tuyachroma.config import AppConfig

FPS = 30.0


class World:
    """When things were commanded, and what a camera would see because of it."""

    def __init__(self, keyboard_delay: float, lights: dict[str, tuple[float, float]]) -> None:
        self.keyboard_delay = keyboard_delay
        self.lights = lights  # id -> (delay s, fade s)
        self.events: dict[str, list[tuple[float, tuple]]] = {"kb": []}
        self.camera_lag = 0.12  # the same for everything: must cancel out

    def command(self, who: str, color: tuple) -> None:
        self.events.setdefault(who, []).append((time.monotonic(), tuple(color)))

    def level(self, who: str, t: float, delay: float, fade: float) -> float:
        """0..1 brightness of one thing at time t."""
        value = 0.0
        for at, color in self.events.get(who, []):
            start = at + delay
            if t < start:
                break
            target = 1.0 if max(color) > 0 else 0.0
            ramp = 1.0 if fade <= 0 else min(1.0, (t - start) / fade)
            value = value + (target - value) * ramp
        return value

    def grid(self, t: float) -> list[float]:
        seen = t - self.camera_lag
        cells = [30.0] * (calibrate.GRID_W * calibrate.GRID_H)
        kb = self.level("kb", seen, self.keyboard_delay, 0.0)
        for i in range(0, 20):           # the keyboard: a few areas at the top
            cells[i] += 150 * kb
        for n, (dev_id, (delay, fade)) in enumerate(self.lights.items()):
            lit = self.level(dev_id, seen, delay, fade)
            for i in range(60 + 30 * n, 80 + 30 * n):   # each light lights its part of the wall
                cells[i] += 120 * lit
        return cells


class FakeCamera:
    def __init__(self, world: World) -> None:
        self.world = world

    def samples(self, since: float) -> list:
        now = time.monotonic()
        out, t = [], since
        while t <= now:
            out.append((t, self.world.grid(t)))
            t += 1 / FPS
        return out


class FakePlayer:
    def __init__(self, world: World) -> None:
        self.world = world
        self.output_ready = True
        self.override_sent = None
        self._override = None

    @property
    def override(self):
        return self._override

    @override.setter
    def override(self, color) -> None:
        self._override = color
        if color is not None:
            self.world.command("kb", color)
            self.override_sent = (time.monotonic(), tuple(color))


class FakeLight:
    def __init__(self, world: World, dev_id: str, latency_ms: float) -> None:
        self.world = world
        self.cfg = SimpleNamespace(id=dev_id, name=dev_id, latency_adjust_ms=0)
        self.latency_ms = latency_ms
        self.fade_ms = 250.0
        self.last_sent = None

    def set_color(self, color) -> None:
        self.world.command(self.cfg.id, color)
        self.last_sent = (time.monotonic(), tuple(color))


def _service(world: World, lights: list[FakeLight]):
    service = SimpleNamespace(player=FakePlayer(world), workers=lights, game_active=False,
                              calibrating=False, _settings=AppConfig().settings,
                              latency_offset_ms=engine.SyncService.latency_offset_ms)
    return service


def test_calibration_finds_how_much_earlier_each_light_must_get_its_colors() -> None:
    # keyboard: 40 ms; GU10: 180 ms + a 250 ms fade (middle at 305 ms); slow strip: 420 ms + 250
    world = World(0.04, {"gu": (0.18, 0.25), "strip": (0.42, 0.25)})
    lights = [FakeLight(world, "gu", latency_ms=120), FakeLight(world, "strip", latency_ms=300)]
    service = _service(world, lights)
    result = calibrate.run(service, FakeCamera(world), trials=2, step=1.2)
    assert not result.message, result.message
    assert abs(result.keyboard_ms - (40 + 120)) < 25  # (with the camera's own delay)
    gu, strip = result.lights
    assert abs(gu.relative_ms - 265) < 40          # (180 + 125) - 40
    assert abs(strip.relative_ms - 505) < 40       # (420 + 125) - 40
    # the correction makes it be sent exactly that much earlier than the keyboard
    assert abs(gu.current_ms + gu.adjust_ms - gu.relative_ms) <= 1
    assert gu.current_ms == 60 + 250                # half the round trip + its fade, now
    assert service.calibrating is False and service.player.override is None


def test_camera_that_does_not_see_the_keyboard_says_so() -> None:
    world = World(0.04, {"gu": (0.18, 0.25)})

    class Blind(FakeCamera):
        def samples(self, since):
            return [(t, [30.0] * (calibrate.GRID_W * calibrate.GRID_H)) for t, _g in super().samples(since)]

    service = _service(world, [FakeLight(world, "gu", 100)])
    result = calibrate.run(service, Blind(world), trials=2, step=0.4)
    assert "nie widzi" in result.message and result.lights == []
    assert service.calibrating is False


def test_no_calibration_while_a_game_owns_the_devices() -> None:
    world = World(0.04, {})
    service = _service(world, [])
    service.game_active = True
    assert "Gra" in calibrate.run(service, FakeCamera(world)).message


def test_response_time_is_the_middle_of_the_change() -> None:
    samples = [(i / 30, [10.0] if i < 30 else [10.0 + min(1.0, (i - 30) / 6) * 100]) for i in range(80)]
    seen = calibrate.response_time(samples, 1.0)
    assert abs(seen - 0.1) < 0.02  # half of a 0.2 s ramp



def test_calibration_window_measures_and_applies(tmp_path, monkeypatch) -> None:
    import pytest

    from test_app import FakeReader, Recorder, _tk_available, dev

    if not _tk_available():
        pytest.skip("no display / tkinter")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from PIL import Image

    from tuyachroma import calibrate_gui, gui

    class Cam:
        def __init__(self, index):
            self.closed = False

        def preview(self, width):
            return Image.new("RGB", (width, width * 3 // 4), (40, 40, 60))

        def close(self):
            self.closed = True

    result = calibrate.Result(keyboard_ms=150.0, lights=[
        calibrate.LightResult("a", "RGB Biuro 1", 420.0, 270.0, 310.0, -40),
        calibrate.LightResult("b", "Light Bar", None)])
    monkeypatch.setattr(calibrate, "available", lambda: True)
    monkeypatch.setattr(calibrate, "find_cameras", lambda: [0])
    monkeypatch.setattr(calibrate, "Camera", Cam)
    monkeypatch.setattr(calibrate, "run", lambda service, camera, progress, stop=None: result)
    config = AppConfig(devices=[dev("a", enabled=True), dev("b", enabled=True)])
    service = engine.SyncService(reader_factory=FakeReader, worker_factory=Recorder,
                                 service_check=lambda: True)
    app = gui.App(config, service, start_hidden=False, use_tray=False)
    monkeypatch.setattr(type(service), "running", property(lambda self: True))
    try:
        win = calibrate_gui.open_window(app)
        end = time.time() + 5
        while time.time() < end and str(win.start_button.cget("state")) == "disabled":
            app.root.update()
            time.sleep(0.02)
        win.start()
        end = time.time() + 5
        while time.time() < end and not win.table.get_children():
            app.root.update()
            time.sleep(0.02)
        rows = [win.table.item(i, "values") for i in win.table.get_children()]
        assert rows[0][3] == "-40 ms" and rows[1][1] == "nie widać"
        win.apply()
        assert config.device("a").latency_adjust_ms == -40
        assert config.device("b").latency_adjust_ms == 0
        win.close()
    finally:
        app.root.destroy()
