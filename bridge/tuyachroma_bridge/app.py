"""Main program: read Razer Chroma colors and push them to Tuya bulbs."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import signal
import sys
import time

from . import __version__
from .bulbs import BulbConfig, BulbWorker, make_device
from .colors import adjust, reduce_colors, zone_indices
from .config import Config, ConfigError, default_path, load, write_example
from .razer import ChromaReader, Frame, RazerUnavailable

_LOGGER = logging.getLogger("tuyachroma_bridge")


def bulb_color(cfg: BulbConfig, frame: Frame) -> tuple[int, int, int]:
    """The color a bulb should show for a device frame."""
    assert frame.colors is not None
    leds = [frame.colors[i] for i in zone_indices(cfg.source, cfg.zone)]
    return adjust(reduce_colors(leds, cfg.mode), cfg.brightness, cfg.min_brightness)


class SyncEngine:
    """Maps device frames to bulb workers (platform independent)."""

    def __init__(self, config: Config, workers: list[BulbWorker]) -> None:
        self.config = config
        self.workers = workers

    def process(self, frames: dict[str, Frame | None]) -> None:
        for worker in self.workers:
            frame = frames.get(worker.cfg.source)
            if frame is not None and frame.active:
                worker.set_color(bulb_color(worker.cfg, frame))
            elif self.config.idle == "restore":
                worker.release()


def run(config: Config) -> int:
    workers = [BulbWorker(b) for b in config.bulbs]
    for w in workers:
        w.start()
    engine = SyncEngine(config, workers)
    stopping = False

    def _stop(*_args) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _stop)

    period = 1.0 / max(1.0, config.fps)
    reader: ChromaReader | None = None
    _LOGGER.info("Running with %d bulb(s). Press Ctrl+C to stop.", len(workers))
    try:
        while not stopping:
            if reader is None:
                try:
                    reader = ChromaReader(config.sources)
                    _LOGGER.info("Connected to Razer Chroma (%s)", ", ".join(config.sources))
                except RazerUnavailable as err:
                    _LOGGER.warning("%s - retrying in 5 s", err)
                    for _ in range(50):
                        if stopping:
                            break
                        time.sleep(0.1)
                    continue
            started = time.monotonic()
            reader.wait(int(period * 1000))
            try:
                frames = {src: reader.read(src) for src in config.sources}
            except OSError as err:
                _LOGGER.warning("Lost Razer Chroma: %s", err)
                reader.close()
                reader = None
                continue
            engine.process(frames)
            spent = time.monotonic() - started
            if spent < period:
                time.sleep(period - spent)
    finally:
        _LOGGER.info("Stopping, restoring bulbs...")
        for w in workers:
            w.stop()
        if reader is not None:
            reader.close()
    return 0


def cmd_scan(_config_path: Path) -> int:
    from .scan import scan  # noqa: PLC0415

    print("Szukam urządzeń Tuya w sieci (10 s)...")
    found = scan(10)
    if not found:
        print("Nic nie znaleziono. Sprawdź, czy komputer jest w tej samej sieci co żarówki.")
        return 1
    for dev in found.values():
        print(f'  id: {dev["id"]}   ip: {dev["ip"]}   version: {dev["version"]}')
    print("\nWpisz id, ip i version do config.json; local_key znajdziesz w Home Assistant "
          "(Tuya Chroma -> urządzenie -> Konfiguruj) lub przez 'python -m tinytuya wizard'.")
    return 0


def cmd_test(config: Config) -> int:
    for cfg in config.bulbs:
        print(f"{cfg.name} ({cfg.ip}): ", end="", flush=True)
        try:
            dev = make_device(cfg)
            status = dev.status()
            if not status or "dps" not in status:
                print(f"BŁĄD: {status}")
                continue
            original = dev.state()
            for rgb in ((255, 0, 0), (0, 255, 0), (0, 0, 255)):
                dev.set_colour(*rgb)
                time.sleep(0.7)
            restore = {dev.dpset[k]: original[k] for k in ("switch", "mode", "brightness", "colourtemp", "colour")
                       if dev.dpset.get(k) and original.get(k) is not None}
            dev.set_multiple_values(restore)
            dev.close()
            print(f"OK (typ {dev.bulb_type}, music: {'tak' if dev.dpset.get('music') else 'nie'})")
        except Exception as err:  # noqa: BLE001
            print(f"BŁĄD: {err}")
    return 0


def cmd_colors(config: Config) -> int:
    try:
        reader = ChromaReader(config.sources)
    except RazerUnavailable as err:
        print(f"Razer Chroma niedostępne: {err}")
        return 1
    try:
        while True:
            reader.wait(500)
            parts = []
            for cfg in config.bulbs:
                frame = reader.read(cfg.source)
                if frame is None or not frame.active:
                    parts.append(f"{cfg.name}: (brak efektu)")
                else:
                    parts.append(f"{cfg.name}: {bulb_color(cfg, frame)}")
            print(" | ".join(parts), flush=True)
    except KeyboardInterrupt:
        return 0
    finally:
        reader.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="TuyaChromaBridge",
        description="Efekty Razer Chroma z gier na żarówkach Tuya.",
    )
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "scan", "test", "colors", "init"],
                        help="run (domyślnie), scan, test, colors, init")
    parser.add_argument("-c", "--config", type=Path, default=None, help="ścieżka do config.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        logging.getLogger("tinytuya").setLevel(logging.WARNING)

    path = args.config or default_path()
    if args.command == "scan":
        return cmd_scan(path)
    if args.command == "init" or (args.command == "run" and not path.exists()):
        if path.exists():
            print(f"{path} już istnieje.")
            return 1
        write_example(path)
        print(f"Utworzono przykładową konfigurację: {path}\n"
              "Uzupełnij dane żarówek (polecenie 'scan' pomoże znaleźć id/ip) i uruchom ponownie.")
        return 0 if args.command == "init" else 1
    try:
        config = load(path)
    except ConfigError as err:
        print(f"Błąd konfiguracji: {err}")
        return 2
    if args.command == "test":
        return cmd_test(config)
    if args.command == "colors":
        return cmd_colors(config)
    return run(config)


if __name__ == "__main__":
    sys.exit(main())
