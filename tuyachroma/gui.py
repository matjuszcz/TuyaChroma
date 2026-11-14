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
from tkinter import messagebox, ttk
from typing import Any

from . import __version__, winsys
from .bulbs import probe_version
from .config import IDLE_MODES, SOURCES, ZONES, AppConfig, Device, config_dir, save
from .engine import SyncService
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
    "restore": "Wróć do stanu sprzed przejęcia (sterowanie z HA)",
    "keep": "Zostaw ostatni kolor z gry",
}
RAZER_GREEN = "#44d62c"


def _label_to_key(labels: dict[str, str], label: str) -> str:
    return next((k for k, v in labels.items() if v == label), label)


class App:
    def __init__(self, config: AppConfig, service: SyncService, start_hidden: bool) -> None:
        self.config = config
        self.service = service
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._tray = None
        self._account = None
        self._login_deadline = 0.0
        self._quitting = False

        self.root = tk.Tk()
        self.root.title("TuyaChroma")
        self.root.geometry("900x600")
        self.root.minsize(760, 480)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self.refresh_devices()
        self._start_tray()
        if start_hidden and self._tray is not None:
            self.root.withdraw()
        self.root.after(200, self._pump)
        self.root.after(1000, self._tick)

    # ----------------------------------------------------------- utilities

    def run_bg(self, func: Callable[[], Any], done: Callable[[Any], None] | None = None,
               error: Callable[[Exception], None] | None = None) -> None:
        """Run func in a thread; deliver the result on the Tk thread."""

        def worker() -> None:
            try:
                result = func()
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Background task failed")
                if error:
                    self._queue.put(lambda: error(err))
                return
            if done:
                self._queue.put(lambda: done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _pump(self) -> None:
        while True:
            try:
                callback = self._queue.get_nowait()
            except queue.Empty:
                break
            callback()
        self.root.after(100, self._pump)

    def save_and_apply(self) -> None:
        save(self.config)
        if self.config.settings.sync_enabled:
            self.set_busy("Stosuję zmiany…")
            self.run_bg(lambda: self.service.restart(self.config), lambda _r: self.set_busy(""))

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
        self.dot = tk.Canvas(top, width=14, height=14, highlightthickness=0)
        self.dot.pack(side="left", padx=(14, 6))
        self.status_var = tk.StringVar()
        ttk.Label(top, textvariable=self.status_var).pack(side="left")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self._build_bulbs_tab()
        self._build_account_tab()
        self._build_settings_tab()

        bottom = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        bottom.pack(fill="x")
        self.busy_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.busy_var, foreground="#666").pack(side="left")
        ttk.Label(bottom, text=f"v{__version__}", foreground="#999").pack(side="right")

    def _build_bulbs_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="Żarówki")

        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="Zaznacz żarówki, którymi ma sterować Razer (kliknij w kolumnę ✓).").pack(side="left")
        ttk.Button(bar, text="Szukaj w sieci", command=self.scan_lan).pack(side="right")

        cols = ("on", "name", "ip", "version", "source", "zone", "status")
        self.tree = ttk.Treeview(tab, columns=cols, show="headings", height=10, selectmode="browse")
        for col, title, width in (
            ("on", "✓", 40), ("name", "Nazwa", 200), ("ip", "IP", 120), ("version", "Wersja", 60),
            ("source", "Źródło", 110), ("zone", "Strefa", 100), ("status", "Stan", 220),
        ):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor="center" if col in ("on", "version") else "w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._load_editor())

        self.editor = ttk.LabelFrame(tab, text="Ustawienia żarówki", padding=10)
        self.editor.pack(fill="x", pady=(8, 0))
        self.ed_enabled = tk.BooleanVar()
        self.ed_source = tk.StringVar()
        self.ed_zone = tk.StringVar()
        self.ed_ip = tk.StringVar()
        self.ed_version = tk.StringVar()
        self.ed_brightness = tk.DoubleVar(value=100)
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
        btns = ttk.Frame(self.editor)
        btns.grid(row=1, column=5, rowspan=2, sticky="e", padx=(24, 0))
        ttk.Button(btns, text="Zapisz", command=self.save_editor).pack(fill="x")
        ttk.Button(btns, text="Testuj (mignij)", command=self.test_bulb).pack(fill="x", pady=(6, 0))
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
                "mechanizmu logowania co Home Assistant."
            ),
        ).pack(anchor="w", pady=(8, 12))
        row = ttk.Frame(tab)
        row.pack(anchor="w")
        ttk.Label(row, text="Kod użytkownika:").pack(side="left")
        self.user_code_var = tk.StringVar(value=self.config.user_code)
        ttk.Entry(row, textvariable=self.user_code_var, width=18).pack(side="left", padx=6)
        ttk.Button(row, text="Pokaż kod QR", command=self.start_login).pack(side="left")
        self.login_var = tk.StringVar()
        ttk.Label(tab, textvariable=self.login_var, foreground="#555").pack(anchor="w", pady=(10, 6))
        self.qr_label = ttk.Label(tab)
        self.qr_label.pack(anchor="w")
        self._update_account_label()

    def _build_settings_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(tab, text="Ustawienia")
        s = self.config.settings
        self.set_autostart = tk.BooleanVar(value=s.autostart)
        self.set_minimized = tk.BooleanVar(value=s.start_minimized)
        self.set_idle = tk.StringVar(value=s.idle)
        self.set_fps = tk.IntVar(value=int(s.fps))
        ttk.Checkbutton(tab, text="Uruchamiaj razem z Windows (przejmuj żarówki po włączeniu komputera)",
                        variable=self.set_autostart).pack(anchor="w")
        ttk.Checkbutton(tab, text="Startuj zminimalizowany do zasobnika (obok zegara)",
                        variable=self.set_minimized).pack(anchor="w", pady=(4, 12))
        ttk.Label(tab, text="Gdy żadna gra nie wysyła efektów Razer Chroma:").pack(anchor="w")
        for mode in IDLE_MODES:
            ttk.Radiobutton(tab, text=IDLE_LABELS[mode], value=mode, variable=self.set_idle).pack(anchor="w", padx=12)
        row = ttk.Frame(tab)
        row.pack(anchor="w", pady=(12, 0))
        ttk.Label(row, text="Odczyty Razer na sekundę:").pack(side="left")
        ttk.Spinbox(row, from_=5, to=60, textvariable=self.set_fps, width=5).pack(side="left", padx=6)
        ttk.Label(
            tab, foreground="#666", wraplength=780, justify="left",
            text=("Po wyłączeniu komputera, uśpieniu, wstrzymaniu sterowania lub zamknięciu programu "
                  "żarówki wracają do stanu sprzed przejęcia i dalej działają z Home Assistant."),
        ).pack(anchor="w", pady=(16, 0))
        row = ttk.Frame(tab)
        row.pack(anchor="w", pady=(16, 0))
        ttk.Button(row, text="Zapisz ustawienia", command=self.save_settings).pack(side="left")
        ttk.Button(row, text="Otwórz folder z logami", command=self.open_logs).pack(side="left", padx=8)

    # ---------------------------------------------------------------- bulbs

    def refresh_devices(self) -> None:
        selected = self.tree.selection()
        statuses = self.service.bulb_statuses()
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
            values = (
                "✔" if dev.enabled else "", dev.name, dev.ip or "?", dev.version or "auto",
                SOURCE_LABELS.get(dev.source, dev.source), ZONE_LABELS.get(str(dev.zone), str(dev.zone)),
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
        self.save_and_apply()

    def _load_editor(self) -> None:
        dev = self._selected_device()
        if dev is None:
            return
        self.editor.configure(text=f"Ustawienia żarówki: {dev.name}")
        self.ed_enabled.set(dev.enabled)
        self.ed_source.set(SOURCE_LABELS.get(dev.source, dev.source))
        self.ed_zone.set(ZONE_LABELS.get(str(dev.zone), str(dev.zone)))
        self.ed_ip.set(dev.ip)
        self.ed_version.set(dev.version)
        self.ed_brightness.set(round(dev.brightness * 100))

    def save_editor(self) -> None:
        dev = self._selected_device()
        if dev is None:
            messagebox.showinfo("TuyaChroma", "Najpierw wybierz żarówkę z listy.")
            return
        dev.enabled = self.ed_enabled.get()
        dev.source = _label_to_key(SOURCE_LABELS, self.ed_source.get())
        dev.zone = _label_to_key(ZONE_LABELS, self.ed_zone.get())
        dev.ip = self.ed_ip.get().strip()
        dev.version = self.ed_version.get().strip()
        dev.brightness = round(self.ed_brightness.get()) / 100
        self.refresh_devices()
        self.save_and_apply()

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
        self.set_busy(f"Znaleziono w sieci {matched} z {total} urządzeń.")
        self.refresh_devices()
        self.save_and_apply()

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
            return f"{dev.name}: OK (protokół {version}, tryb music: {'tak' if bulb.dpset.get('music') else 'nie'})"
        finally:
            save(self.config)
            if was_running:
                self.service.start(self.config)

    # -------------------------------------------------------------- account

    def _update_account_label(self) -> None:
        if self.config.username or self.config.devices:
            who = self.config.username or "konto Tuya"
            self.account_var.set(f"Zalogowano: {who} – {len(self.config.devices)} urządzeń")
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
            new = self.config.merge_cloud(devices)
            lights = sum(1 for d in devices if d.is_light)
            self.login_var.set(f"Zalogowano. Pobrano {len(devices)} urządzeń ({lights} świateł, nowych: {new}).")
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
            text, color = "Wybierz żarówki w zakładce „Żarówki”", "#e0a000"
        else:
            text = f"Razer: {self.service.razer_status}"
            color = RAZER_GREEN if self.service.game_active else (
                "#2b7de9" if "połączono" in self.service.razer_status else "#e0a000")
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
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((6, 6, 58, 58), fill=RAZER_GREEN)
        draw.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 255))
        self._tray = pystray.Icon("TuyaChroma", img, "TuyaChroma", menu=self._tray_menu())
        self._tray.run_detached()

    def _tray_menu(self):
        import pystray  # noqa: PLC0415

        return pystray.Menu(
            pystray.MenuItem("Otwórz", lambda: self._queue.put(self.show), default=True),
            pystray.MenuItem(
                lambda _i: "Wstrzymaj sterowanie" if self.config.settings.sync_enabled else "Włącz sterowanie",
                lambda: self._queue.put(self.toggle_sync),
            ),
            pystray.MenuItem("Zakończ", lambda: self._queue.put(self.quit)),
        )

    def _update_tray_menu(self) -> None:
        if self._tray is not None:
            self._tray.update_menu()

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self) -> None:
        if self._tray is not None:
            self.root.withdraw()  # keep running in the tray
        else:
            self.quit()

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
