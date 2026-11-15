"""Program entry point."""

from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time

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


def running_from_temp(exe: Path) -> bool:
    """Started straight from an archive (WinRAR/7-Zip/Explorer unpack to Temp)?"""
    path = str(exe).lower()
    temp = str(Path(tempfile.gettempdir())).lower()
    return path.startswith(temp) or "rar$ex" in path or "\\appdata\\local\\temp\\" in path


def install_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "TuyaChroma"


def offer_install(exe: Path) -> bool:
    """Copy the program out of a temporary folder. True = new copy started, exit."""
    import tkinter as tk  # noqa: PLC0415
    from tkinter import messagebox  # noqa: PLC0415

    target_dir = install_dir()
    root = tk.Tk()
    root.withdraw()
    try:
        if not messagebox.askyesno(
                "TuyaChroma",
                "Program został uruchomiony prosto z archiwum (folder tymczasowy, który zniknie "
                "po zamknięciu archiwum). Autostart i aktualizacje nie będą wtedy działać.\n\n"
                f"Zainstalować go w folderze:\n{target_dir}\n\n"
                "(Wystarczy raz. Ustawienia i żarówki zostają.)", parent=root):
            return False
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / exe.name
        shutil.copy2(exe, target)
        _LOGGER.info("Installed to %s", target)
        updater.start_detached(target, "--relaunched")
        return True
    except OSError as err:
        _LOGGER.exception("Install failed")
        messagebox.showerror("TuyaChroma", f"Nie udało się skopiować programu: {err}", parent=root)
        return False
    finally:
        root.destroy()


def selftest_child() -> int:
    """CI check: a copy started by the program must have its own libraries."""
    time.sleep(4)  # the parent has exited by now and its temporary files are gone
    try:
        import importlib  # noqa: PLC0415

        import certifi  # noqa: PLC0415

        importlib.import_module("PIL._imaging")  # the module missing in the log
        importlib.import_module("pystray")
        if not Path(certifi.where()).exists():
            raise FileNotFoundError(certifi.where())
    except Exception:  # noqa: BLE001
        _LOGGER.exception("SELFTEST-CHILD FAILED")
        return 1
    _LOGGER.info("SELFTEST-CHILD OK")
    return 0


def relaunch_args(args: argparse.Namespace) -> list[str]:
    extra = ["--updated", "--relaunched"]
    if args.minimized:
        extra.append("--minimized")
    if args.verbose:
        extra.append("--verbose")
    return extra


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="TuyaChroma")
    parser.add_argument("--minimized", action="store_true", help="start in the tray")
    parser.add_argument("--updated", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--relaunched", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--selftest-spawn", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--selftest-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    _LOGGER.info("TuyaChroma %s starting", __version__)
    if args.selftest_child:
        return selftest_child()
    if args.selftest_spawn:
        updater.start_detached(Path(sys.executable), "--selftest-child")
        return 0
    if args.updated and not args.relaunched and updater.can_self_update():
        # Versions up to 1.6.0 started the new exe so that it shared the old
        # one's temporary library folder (deleted when the old one quits).
        # Start once more as a fully independent process.
        updater.start_detached(Path(sys.executable), *relaunch_args(args))
        return 0

    if sys.platform == "win32":
        try:
            import ctypes  # noqa: PLC0415

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass

    if args.updated:
        _LOGGER.info("Started after an update")
    wait = 30 if (args.updated or args.relaunched) else 0
    if not winsys.acquire_single_instance(wait_seconds=wait):
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
        exe = Path(sys.executable)
        if running_from_temp(exe) and offer_install(exe):
            return 0
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
