"""Pick the best settings for each bulb from what it reports and how it behaves.

Inputs: the data points (DP codes) the Tuya account lists for the device, the
DP numbers the bulb reported over the LAN, and the running worker's state
(connected, measured delay, dropped off Wi-Fi while getting colors).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from .bulbs import STATUS_OFFLINE, STATUS_UNSUPPORTED

_LOGGER = logging.getLogger(__name__)

# Rates the bulb editor offers ("Szybkość zmian"): seconds between colors.
SPEEDS = (0.15, 0.3, 0.5, 1.0)

# Data points of strips with separately controlled LEDs (RGBIC / "Dreamcolor").
SEGMENT_CODES = {
    "paint_colour_data", "dreamlight_scene_mode", "dreamlightmic_music_data",
    "lightpixel_number_set", "light_pixel_number_set", "segment_dimming",
}
MUSIC_CODES = {"music_data", "dreamlightmic_music_data"}
STRIP_CATEGORIES = {"dd", "fwd", "fwl"}  # LED strips, ambient lights


@dataclass
class Change:
    dev_id: str
    name: str
    field: str
    old: Any
    new: Any
    reason: str

    def describe(self) -> str:
        return f"{self.name}: {self.reason}"


@dataclass
class Advice:
    changes: list[Change]
    notes: list[str]
    report: list[str]  # one line per bulb: what it is and what it reported


def _speed_label(interval: float) -> str:
    return {0.15: "szybko", 0.3: "średnio", 0.5: "wolno", 1.0: "bardzo wolno"}.get(interval, f"{interval} s")


def _slowest_needed(interval: float) -> float:
    return next((s for s in SPEEDS if s >= interval - 1e-9), SPEEDS[-1])


def advise(devices: list, details: dict[str, dict], settings: Any = None) -> Advice:
    changes: list[Change] = []
    notes: list[str] = []
    report: list[str] = []
    for dev in devices:
        info = details.get(dev.id, {})
        codes = set(getattr(dev, "codes", []) or [])
        dps = info.get("dps", [])
        latency = info.get("latency_ms")
        report.append(
            f"{dev.name}: category={dev.category or '?'}, product={dev.product_name or '?'}, "
            f"enabled={dev.enabled}, ip={dev.ip or '?'}, version={dev.version or 'auto'}, "
            f"type={info.get('bulb_type') or '?'}, status={info.get('status', 'not running')}, "
            f"latency={'%.0f ms' % latency if latency else '?'}, "
            f"rate={dev.min_interval} s, dropped_off={info.get('dropped_off', 0.0)}, "
            f"local DPs=[{','.join(dps)}], cloud codes=[{','.join(sorted(codes))}]"
        )
        status = info.get("status", "")

        # 1. White-only bulbs can't show colors.
        if status == STATUS_UNSUPPORTED and dev.enabled:
            changes.append(Change(dev.id, dev.name, "enabled", True, False,
                                  "tylko białe światło (brak RGB) – wyłączam sterowanie przez Razer"))
            continue

        # 2. Rate: a bulb that rebooted / dropped off while getting colors, or
        #    one that answers slowly, gets colors less often.
        needed = 0.0
        why = ""
        dropped = float(info.get("dropped_off") or 0.0)
        if dropped:
            needed = max(dropped, 0.5)
            why = "znikała z sieci w trakcie efektów (prawdopodobnie się restartuje)"
        if latency and latency > 250:
            slow = _slowest_needed(min(1.0, latency / 1000 * 1.5))
            if slow > needed:
                needed, why = slow, f"odpowiada wolno (ok. {latency:.0f} ms)"
        if needed:
            target = _slowest_needed(needed)
            if target > dev.min_interval + 1e-9:
                changes.append(Change(
                    dev.id, dev.name, "min_interval", dev.min_interval, target,
                    f"{why} – Szybkość zmian: {_speed_label(dev.min_interval)} → {_speed_label(target)}"))

        # 3. Things to know, no automatic change.
        segmented = bool(codes & SEGMENT_CODES) or (
            dev.category in STRIP_CATEGORIES and bool({"51", "53", "61"} & set(dps)))
        if segmented:
            notes.append(f"{dev.name}: listwa z osobno sterowanymi diodami (RGBIC). Program steruje nią "
                         "jednym kolorem; fala wzdłuż listwy wymaga obsługi segmentów (planowana).")
        elif dev.category in STRIP_CATEGORIES:
            notes.append(f"{dev.name}: zwykła listwa RGB – cała świeci jednym kolorem, w efekcie Wave "
                         "jest jednym punktem fali razem z żarówkami.")
        if codes & MUSIC_CODES and dev.method == "colour":
            notes.append(f"{dev.name}: obsługuje tryb Music (kolor zmienia się od razu, bez płynnego "
                         "przejścia). Możesz go wypróbować w ustawieniach żarówki → Tryb i kliknąć Testuj.")
        if status == STATUS_OFFLINE or status.startswith("błąd"):
            notes.append(f"{dev.name}: teraz nieosiągalna ({status}). Jeśli jest włączona, kliknij "
                         "„Szukaj w sieci”.")
        if not dev.ip:
            notes.append(f"{dev.name}: brak adresu IP – kliknij „Szukaj w sieci”.")
    if settings is not None and getattr(settings, "effect_sync", "precise") != "precise":
        notes.append("Ustawienia: synchronizacja kolorów jest w trybie „Oszczędna”. Dla najdokładniejszych "
                     "kolorów wybierz „Dokładna”.")
    if devices and not any(getattr(d, "codes", None) for d in devices):
        notes.append("Brak danych z konta Tuya o funkcjach urządzeń – kliknij „Odśwież z konta”.")
    for line in report:
        _LOGGER.info("Device report: %s", line)
    for change in changes:
        _LOGGER.info("Advice: %s", change.describe())
    for note in notes:
        _LOGGER.info("Advice (note): %s", note)
    return Advice(changes, notes, report)


def apply(devices: list, changes: list[Change]) -> int:
    by_id = {d.id: d for d in devices}
    applied = 0
    for change in changes:
        dev = by_id.get(change.dev_id)
        if dev is not None:
            setattr(dev, change.field, change.new)
            applied += 1
    return applied
