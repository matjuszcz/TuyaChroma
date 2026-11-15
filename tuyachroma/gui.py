"""Main window and tray icon."""

from __future__ import annotations

from collections.abc import Callable
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import colorchooser, messagebox, ttk
from typing import Any

from . import __version__, devices_db, razer_devices, updater, winsys
from .bulbs import STATUS_OFFLINE, probe_version
from .config import IDLE_MODES, SOURCES, ZONES, AppConfig, Device, config_dir, save
from .engine import SyncService
from .profiles import COLOR_MODES, DIRECTIONS, EFFECTS, Profile
from .razer import DEVICES
from .render import KEYBOARD_AREAS, Renderer
from .scan import scan

_LOGGER = logging.getLogger(__name__)

SOURCE_LABELS = {
    "keyboard": "Klawiatura", "mouse": "Mysz", "mousepad": "Podkładka",
    "headset": "Słuchawki", "keypad": "Keypad", "chromalink": "Chroma Link",
}
ZONE_LABELS = {
    "all": "Całość", "left": "Lewa strona", "center": "Środek", "right": "Prawa strona",
    "top": "Góra", "bottom": "Dół",
}
IDLE_LABELS = {
    "full": "Pełna synchronizacja: profil na wszystkich urządzeniach Razer i żarówkach (równo)",
    "effect": "Profil tylko na żarówkach (Razer zostaje przy profilu z Synapse)",
    "restore": "Oddaj żarówki do HA (stan sprzed przejęcia)",
    "keep": "Zostaw ostatni kolor z gry",
}
EFFECT_LABELS = {
    "wave": "Wave – fala tęczy",
    "spectrum": "Spectrum Cycling – wszystko zmienia kolory razem",
    "breathing": "Breathing – oddychanie",
    "static": "Static – stały kolor",
    "wheel": "Wheel – tęczowe koło",
    "starlight": "Starlight – migające gwiazdy",
    "fire": "Fire – ogień",
    "reactive": "Reactive – podświetla naciśnięte klawisze",
    "ripple": "Ripple – fala od naciśniętego klawisza",
    "ambient": "Ambient Awareness – kolory z ekranu",
}
COLOR_MODE_LABELS = {"single": "Jeden kolor", "dual": "Dwa kolory", "random": "Losowe kolory"}
DIRECTION_LABELS = {"right": "W prawo / zgodnie z zegarem", "left": "W lewo / przeciwnie",
                    "down": "W dół", "up": "W górę"}
assert set(EFFECT_LABELS) == set(EFFECTS) and set(COLOR_MODE_LABELS) == set(COLOR_MODES)
assert set(DIRECTION_LABELS) == set(DIRECTIONS)
LAYOUT_LABELS = {
    "auto": "Automatycznie (z wykrytej klawiatury)",
    "full": "Pełnowymiarowa (z klawiaturą numeryczną)",
    "tkl": "Bez klawiatury numerycznej (TKL)",
    "mini": "60% (np. Huntsman Mini)",
}
METHOD_LABELS = {
    "colour": "Kolor (zalecany)",
    "music": "Music (szybszy, nie każda żarówka)",
}
# Device.min_interval (s between colors) -> label
SPEED_LABELS = {
    0.15: "szybko – do 7 zmian/s (domyślnie)",
    0.3: "średnio – ok. 3 zmiany/s",
    0.5: "wolno – 2 zmiany/s",
    1.0: "bardzo wolno – 1 zmiana/s",
}
RAZER_GREEN = "#44d62c"


def _label_to_key(labels: dict[str, str], label: str) -> str:
    return next((k for k, v in labels.items() if v == label), label)


class App:
    def __init__(self, config: AppConfig, service: SyncService, start_hidden: bool,
                 use_tray: bool = True) -> None:
        self.config = config
        self.service = service
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._tray = None
        self._tray_ready = threading.Event()
        self._account = None
        self._login_deadline = 0.0
        self._quitting = False
        self._tray_hint_shown = False
        self.available_release: updater.Release | None = None
        self._notified_version = ""

        self.root = tk.Tk()
        self.root.title("TuyaChroma")
        # Sizes below are for 100% scaling; grow them on high-DPI screens.
        self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96)
        width = min(self.px(900), self.root.winfo_screenwidth() - 40)
        height = min(self.px(600), self.root.winfo_screenheight() - 80)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(self.px(760), self.px(480))
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        # Windows is shutting down / logging off: give the bulbs back right away
        # (Tk may end the process before the system event thread gets its turn).
        self.root.protocol("WM_SAVE_YOURSELF", self.on_session_end)
        self.root.bind("<Unmap>", self._on_unmap)
        self._build()
        self.refresh_devices()
        if use_tray:
            self._start_tray()
        if start_hidden and self._tray is not None:
            # autostart: hide only once the icon is really in the tray
            self._tray_hint_shown = True
            self.root.after(300, self._hide_when_tray_ready, 0)
        self.root.after(200, self._pump)
        self.root.after(1000, self._tick)
        self.root.after(20_000, self._auto_update_check)  # shortly after start, then daily
        self._fail_since: dict[str, float] = {}
        self._last_rescan = -1e9
        self._rescanning = False
        self._rescan_every = self.RESCAN_EVERY
        self.root.after(30_000, self._watch_unreachable)
        self.root.after(90_000, self._log_device_report)

    # ----------------------------------------------------------- utilities

    def px(self, value: int) -> int:
        return int(value * self.scale)

    def run_bg(self, func: Callable[[], Any], done: Callable[[Any], None] | None = None,
               error: Callable[[Exception], None] | None = None) -> None:
        """Run func in a thread; deliver the result on the Tk thread."""

        def worker() -> None:
            try:
                result = func()
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Background task failed")
                if error:
                    # bind now: Python deletes `err` when the except block ends
                    self._queue.put(lambda e=err: error(e))
                return
            if done:
                self._queue.put(lambda r=result: done(r))

        threading.Thread(target=worker, daemon=True).start()

    def _pump(self) -> None:
        try:
            while True:
                try:
                    callback = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    callback()
                except Exception:  # noqa: BLE001 - keep the window alive
                    _LOGGER.exception("UI callback failed")
        finally:
            if not self._quitting:
                self.root.after(100, self._pump)

    def save_and_apply(self, bulbs_only: bool = False) -> None:
        """Save and apply changes; quick successive changes are applied together.

        ``bulbs_only``: only the bulb selection/settings changed, so running
        bulbs and the Razer effect continue without a restart (no flicker).
        """
        save(self.config)
        if not self.config.settings.sync_enabled:
            return
        self._apply_full = getattr(self, "_apply_full", False) or not bulbs_only
        if getattr(self, "_apply_pending", None) is not None:
            self.root.after_cancel(self._apply_pending)
        self.set_busy("Stosuję zmiany…")
        self._apply_pending = self.root.after(800, self._apply_now)

    def _apply_now(self) -> None:
        self._apply_pending = None
        full, self._apply_full = self._apply_full, False

        def apply() -> None:
            if full or not self.service.update_bulbs(self.config):
                self.service.restart(self.config)

        self.run_bg(apply, lambda _r: self.set_busy(""))

    def set_busy(self, text: str) -> None:
        self.busy_var.set(text)

    # ---------------------------------------------------------------- layout

    def _build(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"))

        top = ttk.Frame(self.root, padding=(12, 10))
        top.pack(fill="x")
        ttk.Label(top, text="TuyaChroma", style="Header.TLabel").pack(side="left")
        self.sync_button = ttk.Button(top, command=self.toggle_sync)
        self.sync_button.pack(side="right")
        self.update_button = ttk.Button(top, command=self.install_update)  # shown when available
        self.dot = tk.Canvas(top, width=14, height=14, highlightthickness=0)
        self.dot.pack(side="left", padx=(14, 6))
        self.status_var = tk.StringVar()
        ttk.Label(top, textvariable=self.status_var).pack(side="left")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self._build_bulbs_tab()
        self._build_account_tab()
        self._build_razer_tab()
        self._build_profiles_tab()
        self._build_settings_tab()

        bottom = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        bottom.pack(fill="x")
        self.busy_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.busy_var, foreground="#666").pack(side="left")
        ttk.Label(bottom, text=f"v{__version__}", foreground="#999").pack(side="right")

    def _build_bulbs_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Światła")

        ttk.Label(tab, text="Światła RGB z Twojego konta. Zaznacz te, którymi ma sterować Razer (kolumna ✓)."
                  ).pack(anchor="w", pady=(0, 4))
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="Odśwież z konta", command=self.refresh_from_account).pack(side="left")
        ttk.Button(bar, text="Szukaj w sieci", command=self.scan_lan).pack(side="left", padx=6)
        ttk.Button(bar, text="Dobierz ustawienia automatycznie", command=self.auto_tune).pack(side="left")

        cols = ("on", "name", "ip", "version", "source", "zone", "latency", "status")
        self.tree = ttk.Treeview(tab, columns=cols, show="headings", height=10, selectmode="browse")
        for col, title, width in (
            ("on", "✓", 36), ("name", "Nazwa", 180), ("ip", "IP", 110), ("version", "Wersja", 55),
            ("source", "Źródło", 100), ("zone", "Strefa", 90), ("latency", "Opóźnienie", 80),
            ("status", "Stan", 200),
        ):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=self.px(width), anchor="center" if col in ("on", "version") else "w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._load_editor())

        self.editor = ttk.LabelFrame(tab, text="Ustawienia światła", padding=10)
        self.editor.pack(fill="x", pady=(8, 0))
        self.ed_enabled = tk.BooleanVar()
        self.ed_source = tk.StringVar()
        self.ed_zone = tk.StringVar()
        self.ed_ip = tk.StringVar()
        self.ed_version = tk.StringVar()
        self.ed_brightness = tk.DoubleVar(value=100)
        self.ed_method = tk.StringVar()
        self.ed_latency = tk.IntVar(value=0)
        self.ed_speed = tk.StringVar(value=SPEED_LABELS[0.15])
        self.ed_use_scene = tk.BooleanVar(value=False)
        self.ed_scene_info = tk.StringVar(value="")
        ttk.Checkbutton(self.editor, text="Steruj przez Razer", variable=self.ed_enabled).grid(row=0, column=0, sticky="w")
        ttk.Label(self.editor, text="Źródło koloru:").grid(row=0, column=1, sticky="e", padx=(16, 4))
        ttk.Combobox(self.editor, textvariable=self.ed_source, state="readonly", width=14,
                     values=[SOURCE_LABELS[s] for s in SOURCES]).grid(row=0, column=2, sticky="w")
        ttk.Label(self.editor, text="Strefa:").grid(row=0, column=3, sticky="e", padx=(16, 4))
        ttk.Combobox(self.editor, textvariable=self.ed_zone, state="readonly", width=12,
                     values=[ZONE_LABELS[z] for z in ZONES]).grid(row=0, column=4, sticky="w")
        ttk.Label(self.editor, text="IP:").grid(row=1, column=1, sticky="e", padx=(16, 4), pady=(8, 0))
        ttk.Entry(self.editor, textvariable=self.ed_ip, width=16).grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Label(self.editor, text="Wersja:").grid(row=1, column=3, sticky="e", padx=(16, 4), pady=(8, 0))
        ttk.Combobox(self.editor, textvariable=self.ed_version, width=6,
                     values=["", "3.1", "3.3", "3.4", "3.5"]).grid(row=1, column=4, sticky="w", pady=(8, 0))
        ttk.Label(self.editor, text="Jasność %:").grid(row=2, column=1, sticky="e", padx=(16, 4), pady=(8, 0))
        ttk.Scale(self.editor, from_=10, to=100, variable=self.ed_brightness, length=140).grid(row=2, column=2, sticky="w", pady=(8, 0))
        ttk.Label(self.editor, text="Tryb:").grid(row=2, column=3, sticky="e", padx=(16, 4), pady=(8, 0))
        ttk.Combobox(self.editor, textvariable=self.ed_method, state="readonly", width=30,
                     values=list(METHOD_LABELS.values())).grid(row=2, column=4, sticky="w", pady=(8, 0))
        ttk.Label(self.editor, text="Korekta opóźnienia (ms):").grid(row=3, column=1, sticky="e", padx=(16, 4), pady=(8, 0))
        ttk.Spinbox(self.editor, from_=-1000, to=1000, increment=50, textvariable=self.ed_latency,
                    width=7).grid(row=3, column=2, sticky="w", pady=(8, 0))
        ttk.Label(self.editor, text="(+ gdy żarówka spóźnia się względem klawiatury)",
                  foreground="#666").grid(row=3, column=3, columnspan=2, sticky="w", padx=(16, 0), pady=(8, 0))
        ttk.Label(self.editor, text="Szybkość zmian:").grid(row=4, column=1, sticky="e", padx=(16, 4), pady=(8, 0))
        ttk.Combobox(self.editor, textvariable=self.ed_speed, state="readonly", width=30,
                     values=list(SPEED_LABELS.values())).grid(row=4, column=2, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(self.editor, text="(wolniej, jeśli żarówka się zawiesza)",
                  foreground="#666").grid(row=4, column=4, sticky="w", padx=(8, 0), pady=(8, 0))
        btns = ttk.Frame(self.editor)
        btns.grid(row=5, column=0, columnspan=5, sticky="w", pady=(10, 0))
        ttk.Button(btns, text="Zapisz", command=self.save_editor).pack(side="left")
        ttk.Button(btns, text="Testuj (mignij)", command=self.test_bulb).pack(side="left", padx=8)
        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Checkbutton(btns, variable=self.ed_use_scene,
                        text="Własna fala listwy w Wave/Spectrum/Koło").pack(side="left")
        ttk.Button(btns, text="Zapamiętaj efekt z aplikacji…",
                   command=self.capture_scene).pack(side="left", padx=6)
        ttk.Label(btns, textvariable=self.ed_scene_info, foreground="#666").pack(side="left")
        self.editor.columnconfigure(5, weight=1)

    def _build_account_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(tab, text="Konto Tuya")
        self.account_var = tk.StringVar()
        ttk.Label(tab, textvariable=self.account_var, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(
            tab, justify="left", wraplength=780,
            text=(
                "Zaloguj się kontem z aplikacji Smart Life / Tuya Smart, żeby pobrać listę żarówek "
                "i ich klucze. Konto służy tylko do odczytu kluczy, a żarówki są potem sterowane lokalnie.\n\n"
                "1. W aplikacji: Ja → ⚙ Ustawienia → Konto i bezpieczeństwo → Kod użytkownika (User Code).\n"
                "2. Wpisz kod poniżej i kliknij „Pokaż kod QR”.\n"
                "3. Zeskanuj kod QR aplikacją (ikona skanera w prawym górnym rogu) i potwierdź logowanie.\n"
                "Aplikacja pokaże to jako autoryzację „Home Assistant” – korzystamy z tego samego "
                "mechanizmu logowania co Home Assistant.\n"
                "Logowanie jest zapamiętywane (zaszyfrowane dla Twojego konta Windows), żeby przycisk "
                "„Odśwież z konta” w zakładce Światła działał bez ponownego skanowania kodu QR."
            ),
        ).pack(anchor="w", pady=(8, 12))
        row = ttk.Frame(tab)
        row.pack(anchor="w")
        ttk.Label(row, text="Kod użytkownika:").pack(side="left")
        self.user_code_var = tk.StringVar(value=self.config.user_code)
        ttk.Entry(row, textvariable=self.user_code_var, width=18).pack(side="left", padx=6)
        ttk.Button(row, text="Pokaż kod QR", command=self.start_login).pack(side="left")
        ttk.Button(row, text="Wyloguj (usuń zapisane logowanie)", command=self.logout).pack(side="left", padx=12)
        self.login_var = tk.StringVar()
        ttk.Label(tab, textvariable=self.login_var, foreground="#555").pack(anchor="w", pady=(10, 6))
        self.qr_label = ttk.Label(tab)
        self.qr_label.pack(anchor="w")
        self._update_account_label()

    def _scrollable(self, parent):
        """A frame that scrolls vertically when its content is taller than the window."""
        canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=(16, 12, 16, 12))
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

        def wheel(event) -> None:
            if canvas.winfo_ismapped() and inner.winfo_height() > canvas.winfo_height():
                canvas.yview_scroll(-1 * (event.delta // 120 or (1 if event.num == 4 else -1)), "units")

        canvas.bind("<Enter>", lambda _e: (canvas.bind_all("<MouseWheel>", wheel),
                                           canvas.bind_all("<Button-4>", wheel),
                                           canvas.bind_all("<Button-5>", wheel)))
        canvas.bind("<Leave>", lambda _e: (canvas.unbind_all("<MouseWheel>"),
                                           canvas.unbind_all("<Button-4>"),
                                           canvas.unbind_all("<Button-5>")))
        self.settings_canvas = canvas
        return inner

    def _build_settings_tab(self) -> None:
        outer = ttk.Frame(self.notebook)
        self.notebook.add(outer, text="Ustawienia")
        bottom = ttk.Frame(outer, padding=(16, 6, 16, 8))
        bottom.pack(side="bottom", fill="x")  # always visible, whatever the window height
        ttk.Separator(outer, orient="horizontal").pack(side="bottom", fill="x")
        body = ttk.Frame(outer)
        body.pack(side="top", fill="both", expand=True)
        tab = self._scrollable(body)
        s = self.config.settings
        self.set_autostart = tk.BooleanVar(value=s.autostart)
        self.set_minimized = tk.BooleanVar(value=s.start_minimized)
        self.set_idle = tk.StringVar(value=s.idle)
        self.set_fps = tk.IntVar(value=int(s.fps))
        self.set_bulb_rate = tk.DoubleVar(value=s.bulb_effect_rate)
        self.set_effect_sync = tk.StringVar(value=s.effect_sync)
        self.set_layout = tk.StringVar(value=LAYOUT_LABELS[s.keyboard_layout])
        ttk.Checkbutton(tab, text="Uruchamiaj razem z Windows (przejmuj żarówki po włączeniu komputera)",
                        variable=self.set_autostart).pack(anchor="w")
        ttk.Checkbutton(tab, text="Startuj zminimalizowany do zasobnika (obok zegara)",
                        variable=self.set_minimized).pack(anchor="w", pady=(4, 12))
        ttk.Label(tab, text="Gdy Razer działa, ale żadna gra nie wysyła efektów Chroma:").pack(anchor="w")
        for mode in IDLE_MODES:
            ttk.Radiobutton(tab, text=IDLE_LABELS[mode], value=mode, variable=self.set_idle).pack(anchor="w", padx=12)
        ttk.Label(tab, foreground="#666", wraplength=self.px(780), justify="left",
                  text="Efekt wybierasz w zakładce „Profile efektów”.").pack(anchor="w", padx=12, pady=(2, 0))
        ttk.Label(tab, text="Synchronizacja kolorów żarówek w efektach:").pack(anchor="w", pady=(12, 0))
        ttk.Radiobutton(
            tab, value="precise", variable=self.set_effect_sync,
            text="Dokładna – kolor liczony w chwili wysłania, tak często, jak pozwala „Szybkość zmian” żarówki",
        ).pack(anchor="w", padx=12)
        row = ttk.Frame(tab)
        row.pack(anchor="w", padx=12)
        ttk.Radiobutton(row, value="ticks", variable=self.set_effect_sync,
                        text="Oszczędna – wszystkie żarówki razem, zmian na sekundę:").pack(side="left")
        ttk.Spinbox(row, from_=0.5, to=5, increment=0.5, textvariable=self.set_bulb_rate, width=5).pack(side="left", padx=6)
        ttk.Label(row, text="(1 = oszczędnie dla żarówek, 3–5 = płynniej)", foreground="#666").pack(side="left")
        row = ttk.Frame(tab)
        row.pack(anchor="w", pady=(8, 0))
        ttk.Label(row, text="Układ klawiatury Razer:").pack(side="left")
        ttk.Combobox(row, textvariable=self.set_layout, state="readonly", width=40,
                     values=list(LAYOUT_LABELS.values())).pack(side="left", padx=6)
        row = ttk.Frame(tab)
        row.pack(anchor="w", pady=(8, 0))
        ttk.Label(row, text="Odczyty Razer i klatki efektów na sekundę:").pack(side="left")
        ttk.Spinbox(row, from_=5, to=60, textvariable=self.set_fps, width=5).pack(side="left", padx=6)
        ttk.Label(
            tab, foreground="#666", wraplength=self.px(780), justify="left",
            text=("Kolory z gry zawsze mają pierwszeństwo. Efektów z profilu Synapse nie da się odczytać – "
                  "w trybie pełnej synchronizacji program sam odtwarza wybrany profil na wszystkich "
                  "urządzeniach Razer Chroma i żarówkach, a po zamknięciu programu urządzenia Razer "
                  "wracają do profilu z Synapse.\n"
                  "Po wyłączeniu komputera, uśpieniu, zamknięciu Synapse lub programu albo wstrzymaniu "
                  "sterowania żarówki wracają do stanu sprzed przejęcia i dalej działają z Home Assistant."),
        ).pack(anchor="w", pady=(16, 0))
        lat = ttk.LabelFrame(tab, text="Opóźnienie żarówek w efektach", padding=8)
        lat.pack(anchor="w", fill="x", pady=(12, 0))
        self.set_latency_mode = tk.StringVar(value=s.latency_mode)
        self.set_latency_ms = tk.IntVar(value=s.latency_ms)
        lrow = ttk.Frame(lat)
        lrow.pack(anchor="w")
        for mode, label in (("auto", "Automatyczna (z pomiaru każdej żarówki)"),
                            ("manual", "Ręczna:"), ("off", "Wyłączona")):
            ttk.Radiobutton(lrow, text=label, value=mode, variable=self.set_latency_mode).pack(side="left", padx=(0, 6))
            if mode == "manual":
                ttk.Spinbox(lrow, from_=0, to=3000, increment=50, textvariable=self.set_latency_ms,
                            width=6).pack(side="left")
                ttk.Label(lrow, text="ms").pack(side="left", padx=(2, 14))
        ttk.Label(
            lat, foreground="#666", wraplength=self.px(780), justify="left",
            text=("Efekty liczy program, więc wysyła żarówkom kolor z wyprzedzeniem równym ich opóźnieniu – "
                  "wtedy świecą równo z klawiaturą. Zmierzone opóźnienie każdej żarówki widać w kolumnie "
                  "„Opóźnienie” w zakładce Światła, a korektę dla jednej żarówki ustawisz w jej ustawieniach. "
                  "Kolorów z gry nie da się wyprzedzić (gra steruje klawiaturą bezpośrednio)."),
        ).pack(anchor="w", pady=(4, 0))
        upd = ttk.LabelFrame(tab, text="Aktualizacje", padding=8)
        upd.pack(anchor="w", fill="x", pady=(12, 0))
        self.set_auto_update = tk.BooleanVar(value=s.auto_update)
        ttk.Checkbutton(upd, text="Sprawdzaj codziennie aktualizacje programu i bazy urządzeń Razer",
                        variable=self.set_auto_update).pack(anchor="w")
        urow = ttk.Frame(upd)
        urow.pack(anchor="w", pady=(6, 0))
        ttk.Button(urow, text="Sprawdź teraz", command=lambda: self.check_updates(manual=True)).pack(side="left")
        self.update_status = tk.StringVar(value=f"Zainstalowana wersja: {__version__}")
        ttk.Label(urow, textvariable=self.update_status).pack(side="left", padx=10)
        ttk.Button(bottom, text="Zapisz ustawienia", command=self.save_settings).pack(side="left")
        ttk.Button(bottom, text="Otwórz folder z logami", command=self.open_logs).pack(side="left", padx=8)

    def _build_profiles_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Profile efektów")
        self._profiles_tab_index = self.notebook.index("end") - 1
        left = ttk.Frame(tab)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Profile:").pack(anchor="w")
        self.profile_list = tk.Listbox(left, height=14, width=24, exportselection=False)
        self.profile_list.pack(fill="y", expand=True)
        self.profile_list.bind("<<ListboxSelect>>", lambda _e: self._load_profile())
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="Nowy", command=self.new_profile).pack(side="left")
        ttk.Button(btns, text="Usuń", command=self.delete_profile).pack(side="left", padx=4)
        ttk.Button(left, text="Ustaw jako aktywny", command=self.activate_profile).pack(fill="x", pady=(6, 0))

        right = ttk.Frame(tab, padding=(14, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)
        self.pf_name = tk.StringVar()
        self.pf_effect = tk.StringVar()
        self.pf_mode = tk.StringVar()
        self.pf_direction = tk.StringVar()
        self.pf_speed = tk.IntVar(value=5)
        self.pf_density = tk.IntVar(value=5)
        self.pf_brightness = tk.DoubleVar(value=100)
        self.pf_colors = ["#44d62c", "#0080ff"]
        grid = ttk.Frame(right)
        grid.pack(anchor="w")
        rows = [
            ("Nazwa:", ttk.Entry(grid, textvariable=self.pf_name, width=28)),
            ("Efekt:", ttk.Combobox(grid, textvariable=self.pf_effect, state="readonly", width=40,
                                    values=[EFFECT_LABELS[e] for e in EFFECTS])),
            ("Kolory:", None),
            ("Tryb kolorów:", ttk.Combobox(grid, textvariable=self.pf_mode, state="readonly", width=24,
                                           values=list(COLOR_MODE_LABELS.values()))),
            ("Kierunek:", ttk.Combobox(grid, textvariable=self.pf_direction, state="readonly", width=24,
                                       values=list(DIRECTION_LABELS.values()))),
            ("Prędkość (1–10):", ttk.Scale(grid, from_=1, to=10, variable=self.pf_speed, length=180)),
            ("Gęstość gwiazd:", ttk.Scale(grid, from_=1, to=10, variable=self.pf_density, length=180)),
            ("Jasność %:", ttk.Scale(grid, from_=5, to=100, variable=self.pf_brightness, length=180)),
        ]
        for r, (label, widget) in enumerate(rows):
            ttk.Label(grid, text=label).grid(row=r, column=0, sticky="e", pady=3)
            if widget is not None:
                widget.grid(row=r, column=1, sticky="w", padx=6, pady=3)
        colors = ttk.Frame(grid)
        colors.grid(row=2, column=1, sticky="w", padx=6)
        self.pf_swatches = []
        for i in range(2):
            sw = tk.Label(colors, width=4, background=self.pf_colors[i], relief="groove")
            sw.pack(side="left")
            ttk.Button(colors, text=f"Kolor {i + 1}…", command=lambda i=i: self.pick_profile_color(i)).pack(side="left", padx=(4, 10))
            self.pf_swatches.append(sw)
        ttk.Button(right, text="Zapisz profil", command=self.save_profile).pack(anchor="w", pady=(6, 8))
        ttk.Label(right, text="Podgląd (klawiatura, mysz, podkładka, słuchawki, keypad, Chroma Link, żarówki):",
                  foreground="#555").pack(anchor="w")
        self.preview = tk.Canvas(right, width=self.px(470), height=self.px(150), background="#111",
                                 highlightthickness=0)
        self.preview.pack(anchor="w", pady=(4, 0))
        self._preview_renderer = Renderer(self.config.settings.profile, seed=1)
        self._preview_next_key = 0.0
        self.refresh_profiles()
        for var in (self.pf_effect, self.pf_mode, self.pf_direction, self.pf_speed,
                    self.pf_density, self.pf_brightness):
            var.trace_add("write", self._form_changed)
        self.root.after(100, self._draw_preview)

    # ------------------------------------------------------------- profiles

    def refresh_profiles(self, select: str | None = None) -> None:
        active = self.config.settings.active_profile
        select = select or active
        self.profile_list.delete(0, "end")
        for i, p in enumerate(self.config.settings.profiles):
            self.profile_list.insert("end", ("★ " if p.name == active else "   ") + p.name)
            if p.name == select:
                self.profile_list.selection_set(i)
        self._load_profile()
        self._update_tray_menu()

    def _selected_profile(self) -> Profile | None:
        sel = self.profile_list.curselection()
        profiles = self.config.settings.profiles
        return profiles[sel[0]] if sel and sel[0] < len(profiles) else None

    def _load_profile(self) -> None:
        p = self._selected_profile()
        if p is None:
            return
        self.pf_name.set(p.name)
        self.pf_effect.set(EFFECT_LABELS[p.effect])
        self.pf_mode.set(COLOR_MODE_LABELS[p.color_mode])
        self.pf_direction.set(DIRECTION_LABELS[p.direction])
        self.pf_speed.set(p.speed)
        self.pf_density.set(p.density)
        self.pf_brightness.set(round(p.brightness * 100))
        self.pf_colors = [p.color1, p.color2]
        for sw, c in zip(self.pf_swatches, self.pf_colors):
            sw.configure(background=c)
        self._preview_renderer.set_profile(self._profile_from_form(p.name))

    def _profile_from_form(self, fallback_name: str) -> Profile:
        p = Profile(
            name=self.pf_name.get().strip() or fallback_name,
            effect=_label_to_key(EFFECT_LABELS, self.pf_effect.get()),
            color1=self.pf_colors[0], color2=self.pf_colors[1],
            color_mode=_label_to_key(COLOR_MODE_LABELS, self.pf_mode.get()),
            speed=round(self.pf_speed.get()), direction=_label_to_key(DIRECTION_LABELS, self.pf_direction.get()),
            density=round(self.pf_density.get()), brightness=round(self.pf_brightness.get()) / 100,
        )
        p.sanitize()
        return p

    def pick_profile_color(self, index: int) -> None:
        _rgb, hex_color = colorchooser.askcolor(color=self.pf_colors[index], parent=self.root,
                                                title=f"Kolor {index + 1}")
        if hex_color:
            self.pf_colors[index] = hex_color
            self.pf_swatches[index].configure(background=hex_color)
            self._preview_renderer.set_profile(self._profile_from_form("podgląd"))

    def save_profile(self) -> None:
        old = self._selected_profile()
        if old is None:
            return
        new = self._profile_from_form(old.name)
        names = [p.name for p in self.config.settings.profiles if p is not old]
        if new.name in names:
            messagebox.showinfo("TuyaChroma", f"Profil „{new.name}” już istnieje.")
            return
        settings = self.config.settings
        if settings.active_profile == old.name:
            settings.active_profile = new.name
        settings.profiles[settings.profiles.index(old)] = new
        self.refresh_profiles(select=new.name)
        if settings.active_profile == new.name:
            self.apply_profile_change()
        else:
            save(self.config)
        self.set_busy(f"Zapisano profil „{new.name}”.")

    def new_profile(self) -> None:
        names = {p.name for p in self.config.settings.profiles}
        n = 1
        while f"Mój profil {n}" in names:
            n += 1
        self.config.settings.profiles.append(Profile(name=f"Mój profil {n}"))
        self.refresh_profiles(select=f"Mój profil {n}")
        save(self.config)

    def delete_profile(self) -> None:
        p = self._selected_profile()
        settings = self.config.settings
        if p is None or len(settings.profiles) <= 1:
            return
        settings.profiles.remove(p)
        was_active = settings.active_profile == p.name
        if was_active:
            settings.active_profile = settings.profiles[0].name
        self.refresh_profiles()
        if was_active:
            self.apply_profile_change()
        else:
            save(self.config)

    def activate_profile(self, name: str | None = None) -> None:
        p = self._selected_profile() if name is None else next(
            (x for x in self.config.settings.profiles if x.name == name), None)
        if p is None:
            return
        self.config.settings.active_profile = p.name
        self.refresh_profiles(select=p.name)
        self.apply_profile_change()
        self.set_busy(f"Aktywny profil: {p.name}")

    def apply_profile_change(self) -> None:
        """Save; switch the playing profile live (no flicker) or restart if needed."""
        save(self.config)
        if not self.config.settings.sync_enabled:
            return
        if not self.service.apply_profile(self.config):
            self.save_and_apply()

    def _preview_layout(self) -> str:
        setting = self.config.settings.keyboard_layout
        if setting in KEYBOARD_AREAS:
            return setting
        return razer_devices.keyboard_layout(getattr(self, "razer_devices", []) or [])

    def _preview_visible(self) -> bool:
        try:
            return (self.root.state() == "normal"
                    and self.notebook.index(self.notebook.select()) == self._profiles_tab_index)
        except tk.TclError:
            return False

    def _draw_preview(self) -> None:
        if self._quitting:
            return
        if self._preview_visible():  # no drawing while hidden in the tray / other tab
            try:
                self._render_preview()
            except tk.TclError:
                return
            self.root.after(70, self._draw_preview)
        else:
            self.root.after(500, self._draw_preview)

    def _form_changed(self, *_args) -> None:
        p = self._selected_profile()
        if p is not None:
            self._preview_renderer.set_profile(self._profile_from_form(p.name))

    def _render_preview(self) -> None:
        r = self._preview_renderer
        t = time.monotonic()
        if r.profile.effect in ("reactive", "ripple") and t >= self._preview_next_key:
            r.key_pressed(r._rng.randrange(6), r._rng.randrange(22), t)  # simulated typing
            self._preview_next_key = t + 0.6
        c = self.preview
        c.delete("all")
        u = self.px(14)

        r.keyboard_area = KEYBOARD_AREAS[self._preview_layout()]
        kc0, kc1, kr0, kr1 = r.keyboard_area

        def grid(device, x0, y0, cell):
            spec = DEVICES[device]
            colors = r.device_frame(device, t)
            for i, rgb in enumerate(colors):
                x, y = i % spec.width, i // spec.width
                if device == "keyboard" and not (kc0 <= x <= kc1 and kr0 <= y <= kr1):
                    fill = "#222222"  # no key there on this keyboard
                else:
                    fill = "#%02x%02x%02x" % rgb
                c.create_rectangle(x0 + x * cell, y0 + y * cell, x0 + (x + 1) * cell - 2,
                                   y0 + (y + 1) * cell - 2, fill=fill, width=0)

        grid("keyboard", self.px(8), self.px(8), u)
        grid("mouse", self.px(330), self.px(8), self.px(10))
        grid("keypad", self.px(410), self.px(8), self.px(11))
        grid("mousepad", self.px(8), self.px(100), self.px(13))
        grid("headset", self.px(215), self.px(100), self.px(13))
        grid("chromalink", self.px(300), self.px(100), self.px(13))
        bulbs = r.strip(max(1, len(self.config.enabled_devices) or 4), t)
        for i, rgb in enumerate(bulbs):
            x = self.px(8) + i * self.px(22)
            c.create_oval(x, self.px(125), x + self.px(18), self.px(143), fill="#%02x%02x%02x" % rgb, width=0)

    # ---------------------------------------------------------------- bulbs

    def refresh_devices(self) -> None:
        selected = self.tree.selection()
        statuses = self.service.bulb_statuses()
        latencies = self.service.bulb_latencies()
        existing = set(self.tree.get_children())
        for dev in self.config.devices:
            if dev.enabled and not dev.ready:
                status = "brak IP – kliknij „Szukaj w sieci”"
            elif dev.id in statuses:
                status = statuses[dev.id]
            elif dev.enabled:
                status = "wstrzymana" if not self.config.settings.sync_enabled else "uruchamianie…"
            else:
                status = "sterowana z HA" if dev.ip else "nie znaleziono w sieci"
            ms = latencies.get(dev.id)
            values = (
                "✔" if dev.enabled else "", dev.name, dev.ip or "?", dev.version or "auto",
                SOURCE_LABELS.get(dev.source, dev.source), ZONE_LABELS.get(str(dev.zone), str(dev.zone)),
                f"{ms:.0f} ms" if ms is not None else "–",
                status,
            )
            if dev.id in existing:
                self.tree.item(dev.id, values=values)
            else:
                self.tree.insert("", "end", iid=dev.id, values=values)
        for iid in existing - {d.id for d in self.config.devices}:
            self.tree.delete(iid)
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected)

    def _selected_device(self) -> Device | None:
        sel = self.tree.selection()
        return self.config.device(sel[0]) if sel else None

    def _on_tree_click(self, event: tk.Event) -> None:
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if not iid or (dev := self.config.device(iid)) is None:
            return
        dev.enabled = not dev.enabled
        self.refresh_devices()
        self.tree.selection_set(iid)
        self._load_editor()
        self.save_and_apply(bulbs_only=True)

    def _load_editor(self) -> None:
        dev = self._selected_device()
        if dev is None:
            return
        self.editor.configure(text=f"Ustawienia światła: {dev.name}")
        self.ed_enabled.set(dev.enabled)
        self.ed_source.set(SOURCE_LABELS.get(dev.source, dev.source))
        self.ed_zone.set(ZONE_LABELS.get(str(dev.zone), str(dev.zone)))
        self.ed_ip.set(dev.ip)
        self.ed_version.set(dev.version)
        self.ed_brightness.set(round(dev.brightness * 100))
        self.ed_method.set(METHOD_LABELS.get(dev.method, METHOD_LABELS["colour"]))
        self.ed_latency.set(int(dev.latency_adjust_ms))
        self.ed_use_scene.set(dev.use_scene and bool(dev.scene))
        self.ed_scene_info.set("(zapamiętany)" if dev.scene else "(brak zapamiętanego efektu)")
        speed = min(SPEED_LABELS, key=lambda v: abs(v - dev.min_interval))
        self.ed_speed.set(SPEED_LABELS[speed])

    def save_editor(self) -> None:
        dev = self._selected_device()
        if dev is None:
            messagebox.showinfo("TuyaChroma", "Najpierw wybierz żarówkę z listy.")
            return
        dev.enabled = self.ed_enabled.get()
        dev.source = _label_to_key(SOURCE_LABELS, self.ed_source.get())
        dev.zone = _label_to_key(ZONE_LABELS, self.ed_zone.get())
        dev.ip = self.ed_ip.get().strip()
        version = self.ed_version.get().strip().replace(",", ".")
        dev.version = version if version in ("3.1", "3.2", "3.3", "3.4", "3.5") else ""
        dev.brightness = round(self.ed_brightness.get()) / 100
        dev.method = _label_to_key(METHOD_LABELS, self.ed_method.get()) or "colour"
        try:
            dev.latency_adjust_ms = max(-1000, min(1000, int(self.ed_latency.get())))
        except (tk.TclError, ValueError):
            dev.latency_adjust_ms = 0
        dev.use_scene = self.ed_use_scene.get() and bool(dev.scene)
        dev.min_interval = next((k for k, v in SPEED_LABELS.items() if v == self.ed_speed.get()), 0.15)
        if dev.method not in METHOD_LABELS:
            dev.method = "colour"
        self.refresh_devices()
        self.save_and_apply(bulbs_only=True)

    def capture_scene(self) -> None:
        """Remember the effect the user set on this light in the Tuya app (e.g. a wave)."""
        dev = self._selected_device()
        if dev is None:
            messagebox.showinfo("TuyaChroma", "Najpierw wybierz listwę z listy.")
            return
        if not dev.ip:
            messagebox.showinfo("TuyaChroma", "Ta listwa nie ma adresu IP – kliknij „Szukaj w sieci”.")
            return
        worker = next((w for w in self.service.workers if w.cfg.id == dev.id), None)
        if worker is not None:
            worker.hold(True)  # stop sending colors while the user sets the effect
        try:
            if not messagebox.askokcancel(
                    "TuyaChroma – efekt listwy",
                    f"1. Otwórz aplikację Tuya / Smart Life i ustaw na „{dev.name}” efekt, który ma "
                    "się włączać w profilach Wave, Spectrum i Koło (np. falę).\n"
                    "2. Gdy listwa go pokazuje, kliknij OK.\n\n"
                    "Program na ten czas nie wysyła do niej kolorów.\n\n"
                    "Uwaga: taki efekt listwa animuje sama, w swoim tempie – w tych profilach "
                    "nie będzie zsynchronizowana z urządzeniami Razer (w grach i pozostałych "
                    "profilach nadal będzie)."):
                if worker is not None:
                    worker.hold(False)
                return
        except Exception:
            if worker is not None:
                worker.hold(False)
            raise
        self.set_busy(f"Odczytuję efekt z „{dev.name}”…")

        def read():
            if worker is not None and worker.is_alive() and worker.connected:
                return worker.read_state()
            from .bulbs import make_device  # noqa: PLC0415

            reply = make_device(dev.bulb_config()).status()
            return reply.get("dps") if isinstance(reply, dict) else None

        def done(dps) -> None:
            if worker is not None:
                worker.hold(False)
            mode = str((dps or {}).get("21", ""))
            if not dps or mode in ("", "colour", "white"):
                self.set_busy("Nie odczytano efektu.")
                messagebox.showwarning(
                    "TuyaChroma", f"„{dev.name}” nie pokazuje teraz własnego efektu (tryb: {mode or '?'}). "
                    "Ustaw efekt w aplikacji Tuya i spróbuj jeszcze raz.")
                return
            dev.scene = {str(k): v for k, v in dps.items()}
            dev.use_scene = True
            _LOGGER.info("%s: captured its own effect %s", dev.name, dev.scene)
            self._load_editor()
            self.set_busy(f"Zapamiętano efekt „{dev.name}” (tryb {mode}).")
            self.save_and_apply(bulbs_only=True)

        def failed(err: Exception) -> None:
            if worker is not None:
                worker.hold(False)
            self.set_busy(f"Nie udało się odczytać efektu: {err}")

        self.run_bg(read, done, failed)

    def scan_lan(self) -> None:
        if not self.config.devices:
            messagebox.showinfo("TuyaChroma", "Najpierw zaloguj się w zakładce „Konto Tuya”.")
            return
        self.set_busy("Szukam żarówek w sieci (10 s)…")
        self.run_bg(lambda: self._scan_and_probe(), self._scan_done,
                    lambda err: self.set_busy(f"Błąd wyszukiwania: {err}"))

    def _scan_and_probe(self) -> tuple[int, int]:
        found = scan(10)
        matched = self.config.apply_scan(found)
        for dev in self.config.devices:
            if dev.ip and not dev.version:
                dev.version = probe_version(dev.id, dev.ip, dev.local_key) or ""
        return matched, len(self.config.devices)

    def _scan_done(self, result: tuple[int, int]) -> None:
        matched, total = result
        msg = f"Znaleziono w sieci {matched} z {total} urządzeń."
        if matched < total:
            msg += " Brakujące: czy są włączone? Czy Zapora Windows nie blokuje programu?"
        self.set_busy(msg)
        self.refresh_devices()
        self.save_and_apply(bulbs_only=True)

    def test_bulb(self) -> None:
        dev = self._selected_device()
        if dev is None or not dev.ip:
            messagebox.showinfo("TuyaChroma", "Wybierz żarówkę z adresem IP.")
            return
        self.set_busy(f"Testuję {dev.name}…")
        self.run_bg(lambda: self._test(dev), lambda msg: self.set_busy(msg),
                    lambda err: self.set_busy(f"{dev.name}: błąd – {err}"))

    def _test(self, dev: Device) -> str:
        import tinytuya  # noqa: PLC0415

        was_running = self.service.running
        self.service.stop()  # free the bulb's local connection
        try:
            version = probe_version(dev.id, dev.ip, dev.local_key, dev.version)
            if version is None:
                return f"{dev.name}: brak odpowiedzi (sprawdź IP, wyłącz inne lokalne integracje)"
            dev.version = version
            bulb = tinytuya.BulbDevice(dev.id, dev.ip, dev.local_key, version=float(version))
            bulb.status()
            if not bulb.dpset.get("colour"):
                return f"{dev.name}: ta żarówka nie obsługuje kolorów RGB"
            original = bulb.state()
            for rgb in ((255, 0, 0), (0, 255, 0), (0, 0, 255)):
                bulb.set_colour(*rgb)
                time.sleep(0.8)
            bulb.set_multiple_values({
                bulb.dpset[k]: original[k]
                for k in ("switch", "mode", "brightness", "colourtemp", "colour")
                if bulb.dpset.get(k) and original.get(k) is not None
            })
            bulb.close()
            return f"{dev.name}: OK (protokół {version})"
        finally:
            save(self.config)
            if was_running:
                self.service.start(self.config)

    # -------------------------------------------------------- razer devices

    def _build_razer_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Urządzenia Razer")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 6))
        self.razer_info = tk.StringVar(value="Wyszukuję urządzenia Razer…")
        ttk.Label(bar, textvariable=self.razer_info).pack(side="left")
        ttk.Button(bar, text="Odśwież listę", command=self.refresh_razer_devices).pack(side="right")
        ttk.Button(bar, text="Aktualizuj bazę urządzeń", command=lambda: self.update_device_db(manual=True)).pack(side="right", padx=6)
        self.db_info = tk.StringVar()
        ttk.Label(tab, textvariable=self.db_info, foreground="#555").pack(anchor="w")
        self._show_db_info()
        cols = ("name", "type", "conn", "sdk", "note")
        self.razer_tree = ttk.Treeview(tab, columns=cols, show="headings", height=10)
        for col, title, width in (("name", "Urządzenie", 250), ("type", "Typ", 115),
                                  ("conn", "Połączenie", 120), ("sdk", "Efekty przez", 95),
                                  ("note", "Uwagi", 290)):
            self.razer_tree.heading(col, text=title)
            self.razer_tree.column(col, width=self.px(width), anchor="w")
        self.razer_tree.pack(fill="both", expand=True)
        ttk.Label(
            tab, foreground="#666", wraplength=self.px(820), justify="left",
            text=("Lista odświeża się sama po podłączeniu lub odłączeniu urządzenia. Efekty trafiają do "
                  "wszystkich urządzeń obsługiwanych przez Synapse – także tych, których nie widać na "
                  "liście (np. urządzenia partnerów z Chroma Connect)."),
        ).pack(anchor="w", pady=(6, 0))
        self._razer_refresh_pending = False
        self.razer_devices: list = []
        self.refresh_razer_devices()

    def request_razer_refresh(self) -> None:
        """Thread-safe: called by Windows when a device is plugged in or removed."""
        self._queue.put(self._schedule_razer_refresh)

    def _schedule_razer_refresh(self) -> None:
        if not self._razer_refresh_pending:  # a plug-in fires many events: wait for them
            self._razer_refresh_pending = True
            self.root.after(1500, self.refresh_razer_devices)

    def refresh_razer_devices(self) -> None:
        self._razer_refresh_pending = False
        self.run_bg(razer_devices.detect, self._show_razer_devices,
                    lambda err: self.razer_info.set(f"Błąd wykrywania: {err}"))

    def _show_razer_devices(self, devices: list) -> None:
        self.razer_devices = devices
        for iid in self.razer_tree.get_children():
            self.razer_tree.delete(iid)
        for dev in devices:
            self.razer_tree.insert("", "end", values=(
                dev.name, razer_devices.CATEGORY_LABELS[dev.category], dev.connection,
                SOURCE_LABELS.get(dev.sdk_category, dev.sdk_category), dev.note,
            ))
        stamp = time.strftime("%H:%M:%S")
        if devices:
            layout = LAYOUT_LABELS[razer_devices.keyboard_layout(devices)].split(" (")[0]
            self.razer_info.set(f"Wykryto {len(devices)} urządzeń Razer, układ klawiatury: "
                                f"{layout} (odświeżono {stamp}).")
        else:
            self.razer_info.set(f"Nie wykryto urządzeń Razer (odświeżono {stamp}).")

    # -------------------------------------------- bulbs that changed their IP

    RESCAN_AFTER = 60.0     # a bulb unreachable this long ...
    RESCAN_EVERY = 300.0    # ... triggers a network search, at most this often
    RESCAN_IDLE = 600.0     # not found = switched off: search every 10 min

    def _watch_unreachable(self) -> None:
        if self._quitting:
            return
        try:
            self._check_unreachable(time.monotonic())
        finally:
            self.root.after(30_000, self._watch_unreachable)

    def _check_unreachable(self, now: float) -> None:
        """Routers often give bulbs a new IP (e.g. after a restart): find them again."""
        statuses = self.service.bulb_statuses()
        failing = {d.id for d in self.config.enabled_devices
                   if statuses.get(d.id, "") == STATUS_OFFLINE
                   or statuses.get(d.id, "").startswith("błąd")}
        for dev_id in failing:
            self._fail_since.setdefault(dev_id, now)
        for dev_id in list(self._fail_since):
            if dev_id not in failing:
                del self._fail_since[dev_id]
        if not self._fail_since:
            self._rescan_every = self.RESCAN_EVERY
        long_failing = [i for i, t in self._fail_since.items() if now - t >= self.RESCAN_AFTER]
        if not long_failing or self._rescanning or now - self._last_rescan < self._rescan_every:
            return
        self._rescanning = True
        self._last_rescan = now
        _LOGGER.info("Unreachable bulb(s) %s: searching the network for new IPs", long_failing)

        def done(found: dict) -> None:
            self._rescanning = False
            changed = []
            for dev in self.config.devices:
                info = found.get(dev.id)
                if info and info.get("ip") and info["ip"] != dev.ip:
                    changed.append(f"{dev.name}: {dev.ip or '?'} → {info['ip']}")
                    dev.ip = info["ip"]
                    if info.get("version"):
                        dev.version = info["version"]
            if not any(i in found for i in long_failing):
                # They don't announce themselves: switched off, not a new IP.
                self._rescan_every = self.RESCAN_IDLE
            if changed:
                _LOGGER.info("New bulb IPs: %s", "; ".join(changed))
                self.set_busy("Znaleziono żarówki pod nowym adresem IP: " + "; ".join(changed))
                self.refresh_devices()
                self.save_and_apply(bulbs_only=True)

        def failed(_err: Exception) -> None:
            self._rescanning = False

        self.run_bg(lambda: scan(10), done, failed)

    # -------------------------------------------------------------- updates

    def _show_db_info(self, extra: str = "") -> None:
        try:
            db = devices_db.load()
            text = f"Baza urządzeń Razer: wersja {db['version']} ({db.get('updated', '?')})"
        except devices_db.DatabaseError as err:
            text = f"Baza urządzeń Razer: {err}"
        self.db_info.set(text + (f" – {extra}" if extra else ""))

    def _auto_update_check(self) -> None:
        if self._quitting:
            return
        if self.config.settings.auto_update:
            self.check_updates(manual=False)
            self.update_device_db(manual=False)
        self.root.after(24 * 3600 * 1000, self._auto_update_check)

    def check_updates(self, manual: bool = True) -> None:
        if manual:
            self.update_status.set("Sprawdzam…")

        def done(release) -> None:
            if release is None:
                self.update_status.set(f"Wersja {__version__} – brak opublikowanych wydań.")
                return
            if not updater.is_newer(release.version):
                self.available_release = None
                self.update_status.set(f"Masz najnowszą wersję ({__version__}).")
                return
            self.available_release = release
            self.update_status.set(f"Dostępna nowa wersja {release.version} (masz {__version__}).")
            self.update_button.configure(text=f"Zaktualizuj do {release.version}")
            self.update_button.pack(side="right", padx=(0, 8), before=self.sync_button)
            if self._tray is not None and self._notified_version != release.version:
                self._notified_version = release.version
                try:
                    self._tray.notify(f"Dostępna nowa wersja {release.version}. Otwórz program, "
                                      "żeby zaktualizować.", "TuyaChroma")
                except Exception:  # noqa: BLE001
                    pass

        def failed(err: Exception) -> None:
            if manual:
                self.update_status.set(f"Nie udało się sprawdzić: {err}")

        self.run_bg(updater.check_latest, done, failed)

    def install_update(self) -> None:
        release = self.available_release
        if release is None:
            return
        if not updater.can_self_update():
            webbrowser.open(release.page_url)
            return
        notes = f"\n\nCo nowego:\n{release.notes[:600]}" if release.notes else ""
        if not messagebox.askyesno(
                "TuyaChroma",
                f"Zaktualizować do wersji {release.version}?\n"
                f"Program pobierze ją z GitHub, sprawdzi sumę kontrolną i uruchomi się ponownie. "
                f"Żarówki na chwilę wrócą do HA.{notes}", parent=self.root):
            return
        self.set_busy("Pobieram aktualizację…")
        self.update_button.configure(state="disabled")

        def run():
            self.service.stop()  # give the bulbs back before the swap
            updater.apply_update(release)

        def failed(err: Exception) -> None:
            self.update_button.configure(state="normal")
            self.set_busy(f"Aktualizacja nie powiodła się: {err}")
            if self.config.settings.sync_enabled:
                self.run_bg(lambda: self.service.start(self.config))

        self.run_bg(run, lambda _r: self.quit(), failed)

    def update_device_db(self, manual: bool = True) -> None:
        if manual:
            self._show_db_info("sprawdzam…")

        def done(result) -> None:
            updated, version = result
            if updated:
                self._show_db_info("zaktualizowano")
                self.refresh_razer_devices()
            else:
                self._show_db_info("aktualna" if manual else "")

        def failed(err: Exception) -> None:
            if manual:
                self._show_db_info(f"nie udało się: {err}")

        self.run_bg(devices_db.update, done, failed)

    # -------------------------------------------------------------- account

    def auto_tune(self) -> None:
        """Check what each bulb is and how it behaves; propose the best settings."""
        if not self.config.devices:
            messagebox.showinfo("TuyaChroma", "Najpierw zaloguj się w zakładce „Konto Tuya”.")
            return
        self.set_busy("Sprawdzam urządzenia…")

        def fetch():
            # Fresh data points from the account (what each device supports).
            if not self.config.tuya_session:
                return None
            from .cloud import fetch_with_session  # noqa: PLC0415

            return fetch_with_session(self.config.tuya_session)

        def done(result) -> None:
            if result is not None:
                devices, session = result
                self.config.tuya_session = session
                self.config.merge_cloud(devices)
                save(self.config)
                self.refresh_devices()
            self._show_advice()

        def failed(err: Exception) -> None:
            _LOGGER.warning("Auto settings: account data unavailable: %s", err)
            self._show_advice()

        self.run_bg(fetch, done, failed)

    def _log_device_report(self) -> None:
        """Once after start: write what each bulb is and supports to the log (no changes)."""
        if self._quitting or not self.config.devices:
            return
        try:
            from . import advisor  # noqa: PLC0415

            advisor.advise(self.config.devices, self.service.bulb_details(), self.config.settings)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Device report failed")

    def _show_advice(self) -> None:
        from . import advisor  # noqa: PLC0415

        advice = advisor.advise(self.config.devices, self.service.bulb_details(), self.config.settings)
        lines = []
        if advice.changes:
            lines.append("Proponowane zmiany:")
            lines += [f"• {c.describe()}" for c in advice.changes]
        if advice.notes:
            lines.append("")
            lines.append("Warto wiedzieć:")
            lines += [f"• {n}" for n in advice.notes]
        if not lines:
            lines = ["Wszystkie żarówki mają już najlepsze ustawienia."]
        text = "\n".join(lines)
        self.set_busy("Sprawdzono urządzenia (szczegóły w logu: „Device report”).")
        if advice.changes:
            if messagebox.askyesno("TuyaChroma – dobór ustawień", text + "\n\nZastosować zmiany?"):
                n = advisor.apply(self.config.devices, advice.changes)
                _LOGGER.info("Auto settings: applied %d change(s)", n)
                self.refresh_devices()
                self.save_and_apply(bulbs_only=True)
                self.set_busy(f"Zastosowano {n} zmian(y).")
        else:
            messagebox.showinfo("TuyaChroma – dobór ustawień", text)

    def refresh_from_account(self) -> None:
        if not self.config.tuya_session:
            messagebox.showinfo("TuyaChroma", "Zaloguj się w zakładce „Konto Tuya” (kod QR). "
                                              "Później odświeżanie działa bez kodu QR.")
            self.notebook.select(1)
            return
        self.set_busy("Pobieram listę urządzeń z konta Tuya…")

        def fetch():
            from .cloud import fetch_with_session  # noqa: PLC0415

            return fetch_with_session(self.config.tuya_session)

        def done(result) -> None:
            devices, session = result
            self.config.tuya_session = session  # token may have been refreshed
            before = {d.id for d in self.config.devices}
            new = self.config.merge_cloud(devices)
            removed = len(before - {d.id for d in self.config.devices})
            save(self.config)
            self._update_account_label()
            self.refresh_devices()
            self.set_busy(f"Lista z konta: {len(self.config.devices)} świateł RGB "
                          f"(nowe: {new}, usunięte: {removed}). Szukam ich w sieci…")
            self.scan_lan()

        def failed(err: Exception) -> None:
            self.set_busy(f"{err}")
            if "QR" in str(err) or "wygas" in str(err):
                self.notebook.select(1)

        self.run_bg(fetch, done, failed)

    def logout(self) -> None:
        self.config.tuya_session = {}
        self.config.username = ""
        save(self.config)
        self._update_account_label()
        self.login_var.set("Usunięto zapisane logowanie. Żarówki i ich klucze zostają – "
                           "program dalej nimi steruje.")


    def _update_account_label(self) -> None:
        if self.config.username or self.config.devices:
            who = self.config.username or "konto Tuya"
            self.account_var.set(f"Zalogowano: {who} – {len(self.config.devices)} świateł RGB")
        else:
            self.account_var.set("Nie zalogowano")

    def start_login(self) -> None:
        code = self.user_code_var.get().strip()
        if not code:
            messagebox.showinfo("TuyaChroma", "Wpisz kod użytkownika z aplikacji Smart Life.")
            return
        self.login_var.set("Pobieram kod QR…")

        def request():
            from .cloud import TuyaAccount  # noqa: PLC0415

            account = TuyaAccount()
            return account, account.request_qr(code)

        self.run_bg(request, self._show_qr, lambda err: self.login_var.set(f"Błąd: {err}"))

    def _show_qr(self, result) -> None:
        import qrcode  # noqa: PLC0415
        from PIL import ImageTk  # noqa: PLC0415

        self._account, data = result
        img = qrcode.make(data, box_size=6, border=2).get_image()
        self._qr_image = ImageTk.PhotoImage(img)
        self.qr_label.configure(image=self._qr_image)
        self.login_var.set("Zeskanuj kod aplikacją Smart Life i potwierdź logowanie…")
        self._login_deadline = time.monotonic() + 180
        self.root.after(2000, self._poll_login)

    def _poll_login(self) -> None:
        if self._account is None:
            return
        if time.monotonic() > self._login_deadline:
            self.login_var.set("Kod QR wygasł – kliknij „Pokaż kod QR” ponownie.")
            self.qr_label.configure(image="")
            return
        account = self._account

        def check():
            info = account.poll_login()
            if info is None:
                return None
            return info, account.fetch_devices(info)

        def done(result):
            if result is None:
                self.root.after(2000, self._poll_login)
                return
            self._account = None
            self.qr_label.configure(image="")
            info, devices = result
            self.config.username = info.get("username") or ""
            self.config.user_code = account.user_code or ""
            try:
                self.config.tuya_session = account.session(info)
            except (KeyError, AttributeError):
                self.config.tuya_session = {}
            new = self.config.merge_cloud(devices)
            rgb = sum(1 for d in devices if d.rgb)
            self.login_var.set(
                f"Zalogowano. Na koncie jest {len(devices)} urządzeń, w tym {rgb} świateł RGB "
                f"(nowych: {new}). Inne urządzenia są pomijane."
            )
            save(self.config)
            self._update_account_label()
            self.refresh_devices()
            self.notebook.select(0)
            self.scan_lan()

        self.run_bg(check, done, lambda err: (self.login_var.set(f"Błąd logowania: {err}"),
                                              self.root.after(3000, self._poll_login)))

    # ------------------------------------------------------------- settings

    def save_settings(self) -> None:
        s = self.config.settings
        s.autostart = self.set_autostart.get()
        s.start_minimized = self.set_minimized.get()
        s.idle = self.set_idle.get()
        try:
            s.fps = max(5, min(60, int(self.set_fps.get())))
        except (tk.TclError, ValueError):
            s.fps = 20
        try:
            s.bulb_effect_rate = max(0.5, min(5.0, float(self.set_bulb_rate.get())))
        except (tk.TclError, ValueError):
            s.bulb_effect_rate = 1.0
        s.effect_sync = self.set_effect_sync.get() if self.set_effect_sync.get() in ("precise", "ticks") else "precise"
        s.auto_update = self.set_auto_update.get()
        s.latency_mode = self.set_latency_mode.get()
        try:
            s.latency_ms = max(0, min(3000, int(self.set_latency_ms.get())))
        except (tk.TclError, ValueError):
            s.latency_ms = 300
        s.keyboard_layout = _label_to_key(LAYOUT_LABELS, self.set_layout.get())
        if s.keyboard_layout not in LAYOUT_LABELS:
            s.keyboard_layout = "auto"
        try:
            winsys.set_autostart(s.autostart, s.start_minimized)
        except OSError as err:
            messagebox.showwarning("TuyaChroma", f"Nie udało się ustawić autostartu: {err}")
        self.save_and_apply()
        self.set_busy("Zapisano ustawienia.")

    def open_logs(self) -> None:
        path = config_dir()
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603, S607

    # ------------------------------------------------------- sync & status

    def toggle_sync(self) -> None:
        s = self.config.settings
        s.sync_enabled = not s.sync_enabled
        save(self.config)
        if s.sync_enabled:
            self.run_bg(lambda: self.service.start(self.config))
        else:
            self.set_busy("Oddaję żarówki do HA…")
            self.run_bg(self.service.stop, lambda _r: self.set_busy("Sterowanie wstrzymane."))
        self._update_tray_menu()

    def _tick(self) -> None:
        s = self.config.settings
        if not s.sync_enabled:
            text, color = "Sterowanie wstrzymane – żarówki działają z HA", "#999"
        elif not self.config.enabled_devices:
            text, color = "Wybierz światła w zakładce „Światła”", "#e0a000"
        else:
            status = self.service.razer_status
            text = f"Razer: {status}"
            if self.service.game_active:
                color = RAZER_GREEN
            elif "efekt" in status or "połączono" in status:
                color = "#2b7de9"
            else:
                color = "#e0a000"
        self.status_var.set(text)
        self.dot.delete("all")
        self.dot.create_oval(2, 2, 12, 12, fill=color, outline=color)
        self.sync_button.configure(text="Wstrzymaj sterowanie" if s.sync_enabled else "Włącz sterowanie")
        self.refresh_devices()
        self.root.after(1000, self._tick)

    # ------------------------------------------------------------- tray/exit

    def _start_tray(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import pystray  # noqa: PLC0415
            from PIL import Image, ImageDraw  # noqa: PLC0415
        except ImportError:
            _LOGGER.exception("Tray icon unavailable")
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((6, 6, 58, 58), fill=RAZER_GREEN)
        draw.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 255))
        try:
            menu = self._tray_menu(pystray)
            list(self._profile_items(pystray))  # build the profile submenu now: fail loudly here
            self._tray = pystray.Icon("TuyaChroma", img, "TuyaChroma", menu=menu)
        except Exception:  # noqa: BLE001 - without a tray the window just stays a window
            _LOGGER.exception("Tray icon could not be created")
            self._tray = None
            return

        def setup(icon) -> None:
            # Runs in pystray's thread. Errors here used to make the icon silently
            # invisible, so log them and only rely on the tray once it is shown.
            try:
                icon.visible = True
                self._tray_ready.set()
                _LOGGER.info("Tray icon shown")
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Tray icon could not be shown")

        self._tray.run_detached(setup)

    @property
    def tray_ok(self) -> bool:
        """Only hide the window when the tray icon is really visible."""
        return self._tray is not None and self._tray_ready.is_set()

    def _profile_items(self, pystray):
        def action_for(name: str):
            # exactly (icon, item): pystray rejects callables with other arities
            def action(icon, item) -> None:
                self._queue.put(lambda: self.activate_profile(name))
            return action

        def checked_for(name: str):
            def checked(item) -> bool:
                return self.config.settings.active_profile == name
            return checked

        for p in self.config.settings.profiles:
            yield pystray.MenuItem(p.name, action_for(p.name), checked=checked_for(p.name), radio=True)

    def _tray_menu(self, pystray=None):
        if pystray is None:
            import pystray  # noqa: PLC0415

        def sync_text(item) -> str:
            return "Wstrzymaj sterowanie" if self.config.settings.sync_enabled else "Włącz sterowanie"

        return pystray.Menu(
            pystray.MenuItem("Otwórz", lambda: self._queue.put(self.show), default=True),
            pystray.MenuItem(sync_text, lambda: self._queue.put(self.toggle_sync)),
            pystray.MenuItem("Profil efektu", pystray.Menu(lambda: self._profile_items(pystray))),
            pystray.MenuItem("Zakończ", lambda: self._queue.put(self.quit)),
        )

    def _update_tray_menu(self) -> None:
        if self._tray is not None:
            self._tray.update_menu()

    def _hide_when_tray_ready(self, waited: int) -> None:
        if self.tray_ok:
            self.root.withdraw()
        elif waited < 10_000:
            self.root.after(300, self._hide_when_tray_ready, waited + 300)
        else:
            _LOGGER.warning("Tray icon did not appear; keeping the window visible")

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_to_tray(self) -> None:
        self.root.withdraw()
        if not self._tray_hint_shown and self._tray is not None:
            self._tray_hint_shown = True
            try:
                self._tray.notify(
                    "TuyaChroma działa dalej w zasobniku obok zegara. "
                    "Kliknij ikonę, żeby otworzyć; „Zakończ” oddaje żarówki do HA.",
                    "TuyaChroma",
                )
            except Exception:  # noqa: BLE001 - notifications are optional
                pass

    def _on_unmap(self, event: tk.Event) -> None:
        # Minimize button (_) -> hide to the tray instead of the taskbar.
        if event.widget is self.root and self.tray_ok and self.root.state() == "iconic":
            self.root.after(0, self.hide_to_tray)

    def on_close(self) -> None:
        if self.tray_ok:
            self.hide_to_tray()  # keep running in the tray
        else:
            self.quit()

    def on_session_end(self) -> None:
        _LOGGER.info("Windows session ending: releasing bulbs")
        self.service.stop(timeout=4)
        # If the shutdown gets cancelled, we are still alive: resume after a while.
        self.root.after(20000, self._resume_after_cancelled_shutdown)

    def _resume_after_cancelled_shutdown(self) -> None:
        if self.config.settings.sync_enabled and not self.service.running:
            self.run_bg(lambda: self.service.start(self.config))

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self.set_busy("Oddaję żarówki do HA…")
        self.root.update_idletasks()
        self.service.stop()
        if self._tray is not None:
            self._tray.stop()
        self.root.destroy()

    def mainloop(self) -> None:
        self.root.mainloop()
