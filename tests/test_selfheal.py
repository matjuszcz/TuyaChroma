"""Self-healing: restart what died, repair lights that keep rejecting connections."""

from __future__ import annotations

import logging
import threading
import time

import pytest

from tuyachroma import bulbs, engine
from tuyachroma.bulbs import BulbWorker
from tuyachroma.config import AppConfig, Device
from test_app import FakeReader, Recorder, _tk_available, dev
from test_bulbs import FakeBulb, wait_for


class Crashing(BulbWorker):
    """A light whose thread dies from a bug (not a network error)."""

    def run(self) -> None:
        raise RuntimeError("bug")


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_watchdog_restarts_a_dead_light() -> None:
    made = []

    def factory(cfg):
        worker = Crashing(cfg, lambda _c: FakeBulb()) if not made else BulbWorker(cfg, lambda _c: FakeBulb())
        made.append(worker)
        return worker

    service = engine.SyncService(reader_factory=FakeReader, worker_factory=factory, service_check=lambda: True,
                                 output_factory=lambda: None, own_pid=1, layout_detector=lambda: "full")
    config = AppConfig(devices=[Device(id="a", name="RGB Biuro 2", local_key="k", ip="1", enabled=True)])
    config.settings.idle = "restore"
    service.start(config)
    try:
        assert wait_for(lambda: not made[0].is_alive())
        assert service.heal(config) == ["RGB Biuro 2"]
        assert service.workers[0] is made[1] and wait_for(lambda: made[1].connected)
        assert service.heal(config) == []  # all fine now
    finally:
        service.stop()


def test_watchdog_leaves_white_only_lights_alone() -> None:
    white = FakeBulb()
    white.dpset["colour"] = None
    service = engine.SyncService(reader_factory=FakeReader, worker_factory=lambda c: BulbWorker(c, lambda _c: white),
                                 service_check=lambda: True, own_pid=1, layout_detector=lambda: "full")
    config = AppConfig(devices=[Device(id="w", name="Biała", local_key="k", ip="1", enabled=True)])
    config.settings.idle = "restore"
    service.start(config)
    try:
        assert wait_for(lambda: service.workers[0].status == bulbs.STATUS_UNSUPPORTED)
        assert wait_for(lambda: not service.workers[0].is_alive())
        assert service.heal(config) == []
    finally:
        service.stop()


def test_watchdog_restarts_a_dead_sync_loop() -> None:
    service = engine.SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True,
                                 own_pid=1, layout_detector=lambda: "full")
    config = AppConfig(devices=[dev("a", enabled=True)])
    config.settings.idle = "restore"
    service.start(config)
    try:
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        service._thread = dead  # as if the loop had died from a bug
        assert service.heal(config) == ["synchronizacja"]
        assert service.running
        assert service.heal(config) == []
    finally:
        service.stop()
    assert service.heal(config) == []  # stopped on purpose: nothing to heal


@pytest.mark.skipif(not _tk_available(), reason="no display / tkinter")
def test_light_rejecting_connections_gets_its_new_key(tmp_path, monkeypatch) -> None:
    """Error 914 for minutes: fetch the light's current key, find its protocol version."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import cloud, gui
    from tuyachroma.cloud import CloudDevice
    from tuyachroma.engine import SyncService

    config = AppConfig(devices=[dev("b2", enabled=True, name="RGB Biuro 2", version="3.5")],
                       tuya_session={"user_code": "u"})
    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    app = gui.App(config, service, start_hidden=False, use_tray=False)
    status = "błąd: no status from bulb ({'Error': 'Check device key or version', 'Err': '914'})"
    monkeypatch.setattr(service, "bulb_statuses", lambda: {"b2": status})
    monkeypatch.setattr(app, "run_bg", lambda func, done=None, error=None: done(func()))
    monkeypatch.setattr(cloud, "fetch_with_session", lambda s, cb=None: (
        [CloudDevice("b2", "RGB Biuro 2", "NEWKEY0123456789", "dj", "GU10", rgb=True)], s))
    monkeypatch.setattr(gui, "probe_version", lambda *a, **k: "3.5")
    applied = []
    monkeypatch.setattr(app, "save_and_apply", lambda bulbs_only=False: applied.append(bulbs_only))
    try:
        app._fail_since["b2"] = 1000.0
        app._heal_rejected(1100.0)          # failing for 100 s: wait
        assert config.devices[0].local_key != "NEWKEY0123456789"
        app._heal_rejected(1200.0)          # 200 s: repair
        assert config.devices[0].local_key == "NEWKEY0123456789" and applied == [True]
        assert "Samonaprawa" in app.busy_var.get()
        app._heal_rejected(1300.0)          # not again so soon
        assert applied == [True]
        # nothing to change: the light itself hangs -> tell what to do
        app.__dict__["_heal_tried"] = {}
        app._heal_rejected(1400.0)
        assert "wyłącz" in app.busy_var.get() and applied == [True]
    finally:
        app.root.destroy()


def test_uncaught_errors_are_logged(caplog) -> None:
    from tuyachroma import main

    old_hook, old_thread_hook = __import__("sys").excepthook, threading.excepthook
    try:
        main.install_error_logging()
        with caplog.at_level(logging.ERROR, logger="tuyachroma"):
            t = threading.Thread(target=lambda: 1 / 0, name="light-x")
            t.start()
            t.join()
            time.sleep(0.05)
        assert any("Unexpected error in thread light-x" in r.message for r in caplog.records)
    finally:
        import sys

        sys.excepthook, threading.excepthook = old_hook, old_thread_hook
