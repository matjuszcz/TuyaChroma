"""Program entry point."""

from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import sys
import threading

from . import __version__, updater, winsys
from .config import config_dir, load
from .engine import SyncService

_LOGGER = logging.getLogger("tuyachroma")


def setup_logging(verbose: bool) -> None:
    handlers: list[logging.Handler] = []
    try:
        config_dir().mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(config_dir() / "log.txt", maxBytes=1_000_000,
                                            backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    if sys.stderr is not None:  # None in a windowed exe
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    if not verbose:
        logging.getLogger("tinytuya").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="TuyaChroma")
    parser.add_argument("--minimized", action="store_true", help="start in the tray")
    parser.add_argument("--updated", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    _LOGGER.info("TuyaChroma %s starting", __version__)

    if sys.platform == "win32":
        try:
            import ctypes  # noqa: PLC0415

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass

    if args.updated:
        _LOGGER.info("Started after an update")
    if not winsys.acquire_single_instance(wait_seconds=30 if args.updated else 0):
        import tkinter as tk  # noqa: PLC0415
        from tkinter import messagebox  # noqa: PLC0415

        root = tk.Tk()
        root.withdraw()  # no empty window behind the message
        messagebox.showinfo("TuyaChroma", "TuyaChroma już działa – ikona jest w zasobniku obok zegara.",
                            parent=root)
        root.destroy()
        return 0

    if updater.can_self_update():
        updater.cleanup_old()
    try:
        from . import devices_db  # noqa: PLC0415

        _LOGGER.info("Razer device database version %s", devices_db.load()["version"])
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Razer device database: %s", err)
    config = load()
    # "Pause" is temporary: every start (e.g. turning the PC on) takes the bulbs over again.
    config.settings.sync_enabled = True
    service = SyncService()
    try:
        winsys.set_autostart(config.settings.autostart, config.settings.start_minimized)
    except OSError as err:
        _LOGGER.warning("Autostart: %s", err)

    if config.settings.sync_enabled:
        service.start(config)

    resume_timer: list[threading.Timer] = []

    def on_resume() -> None:
        # Give Wi-Fi a moment to come back before reconnecting to the bulbs.
        if config.settings.sync_enabled:
            timer = threading.Timer(8, lambda: service.start(config))
            timer.daemon = True
            resume_timer.append(timer)
            timer.start()

    app_holder: list = []

    def on_devices_changed() -> None:
        if app_holder:
            app_holder[0].request_razer_refresh()

    winsys.start_system_event_listener(
        on_shutdown=lambda: service.stop(timeout=4),
        on_suspend=lambda: service.stop(timeout=4),
        on_resume=on_resume,
        on_devices_changed=on_devices_changed,
    )

    from .gui import App  # noqa: PLC0415

    start_hidden = args.minimized and config.settings.start_minimized and bool(config.devices)
    app = App(config, service, start_hidden=start_hidden)
    app_holder.append(app)
    try:
        app.mainloop()
    finally:
        service.stop()
        _LOGGER.info("TuyaChroma stopped")
    return 0
