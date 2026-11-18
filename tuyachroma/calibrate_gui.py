"""The "Kalibracja kamerą" window: aim the camera, measure, apply the corrections."""

from __future__ import annotations

import gc
import logging
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import calibrate, theme

_LOGGER = logging.getLogger(__name__)

INTRO = ("Program po kolei rozjaśni klawiaturę i każde światło (po kilka razy), a kamera "
         "zmierzy, kiedy naprawdę się zmieniają. Potem ustawi każdemu światłu korektę, żeby "
         "zmieniało kolor razem z klawiaturą.\n"
         "Kamera (w komputerze albo telefon) ma widzieć klawiaturę (albo jej poświatę) i pokój, "
         "w którym świecą światła – same światła nie muszą być w kadrze. Najlepiej po zmroku, "
         "bez ruchu przed kamerą. Pomiar trwa ok. 10 s na każde światło.")

PHONE_STEPS = ("Telefonem: postaw telefon tak, żeby widział klawiaturę i pokój, włącz nagrywanie "
               "filmu i kliknij „Rozpocznij pomiar”. Gdy pojawi się „Gotowe”, zatrzymaj nagrywanie, "
               "przenieś film na komputer (kabel, iCloud, e-mail…) i kliknij „Wczytaj film…”.")


class CalibrationWindow:
    def __init__(self, app) -> None:
        self.app = app
        self.camera: calibrate.Camera | None = None
        self.result: calibrate.Result | None = None
        self._stop = threading.Event()
        self._running = False
        self._preview_image = None

        win = self.win = tk.Toplevel(app.root, background=theme.CANVAS)
        win.title("Kalibracja kamerą")
        win.transient(app.root)
        win.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=INTRO, wraplength=app.px(560), justify="left").pack(anchor="w")

        modes = ttk.Frame(body)
        modes.pack(anchor="w", pady=(12, 0))
        self.mode = tk.StringVar(value="camera")
        ttk.Radiobutton(modes, text="Kamera w komputerze (na żywo)", value="camera",
                        variable=self.mode, command=self._mode_changed).pack(side="left")
        ttk.Radiobutton(modes, text="Telefon (nagranie filmu)", value="phone",
                        variable=self.mode, command=self._mode_changed).pack(side="left", padx=(16, 0))
        row = ttk.Frame(body)
        row.pack(anchor="w", pady=(8, 6))
        ttk.Label(row, text="Kamera:").pack(side="left")
        self.camera_var = tk.StringVar(value="szukam…")
        self.camera_box = ttk.Combobox(row, textvariable=self.camera_var, state="readonly", width=14)
        self.camera_box.pack(side="left", padx=6)
        self.camera_box.bind("<<ComboboxSelected>>", lambda _e: self._open_selected())
        self.start_button = ttk.Button(row, text="Rozpocznij pomiar", style="Accent.TButton",
                                       command=self.start, state="disabled")
        self.start_button.pack(side="left", padx=(12, 0))
        self.load_button = ttk.Button(row, text="Wczytaj film…", command=self.load_video,
                                      state="disabled")
        self.load_button.pack(side="left", padx=(6, 0))

        self.preview = tk.Label(body, background=theme.PREVIEW, width=app.px(40), height=app.px(10))
        self.preview.pack(anchor="w", pady=(4, 8))
        self.status_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.status_var, foreground=theme.MUTED).pack(anchor="w")

        cols = ("light", "response", "relative", "adjust")
        self.table = ttk.Treeview(body, columns=cols, show="headings", height=5)
        for col, title, width in (("light", "Światło", 170), ("response", "Reakcja", 90),
                                  ("relative", "Później niż klawiatura", 160),
                                  ("adjust", "Korekta", 90)):
            self.table.heading(col, text=title)
            self.table.column(col, width=app.px(width), anchor="w")
        self.table.pack(fill="x", pady=(8, 8))
        bottom = ttk.Frame(body)
        bottom.pack(fill="x")
        self.apply_button = ttk.Button(bottom, text="Zastosuj korekty", command=self.apply,
                                       state="disabled")
        self.apply_button.pack(side="left")
        ttk.Button(bottom, text="Zamknij", command=self.close).pack(side="right")

        app.run_bg(lambda: calibrate.find_cameras(), self._cameras_found,
                   lambda err: self.status_var.set(f"Nie udało się wyszukać kamer: {err}"))
        self._tick()

    # ------------------------------------------------------------ camera

    def _mode_changed(self) -> None:
        if self.mode.get() == "phone":
            if self.camera is not None:
                self.camera.close()
                self.camera = None
            self.camera_box.configure(state="disabled")
            self.start_button.configure(state="normal")
            self.status_var.set(PHONE_STEPS)
        else:
            self.camera_box.configure(state="readonly")
            if getattr(self, "_indexes", None):
                self._open_selected()
            else:
                self.start_button.configure(state="disabled")
                self.status_var.set("Nie znaleziono kamery w komputerze – użyj telefonu.")

    def _cameras_found(self, found: list[int]) -> None:
        if not self.win.winfo_exists() or self.mode.get() == "phone":
            if self.win.winfo_exists():
                self._indexes = found
            return
        if not found:
            self.camera_var.set("brak")
            self.mode.set("phone")
            self._mode_changed()
            self.status_var.set("Nie znaleziono kamery w komputerze. " + PHONE_STEPS)
            return
        self.camera_box.configure(values=[f"Kamera {i + 1}" for i in found])
        self._indexes = found
        self.camera_box.current(0)
        self._open_selected()

    def _open_selected(self) -> None:
        index = self._indexes[self.camera_box.current()]
        if self.camera is not None:
            self.camera.close()
            self.camera = None
        try:
            self.camera = calibrate.Camera(index)
        except OSError as err:
            self.status_var.set(f"Nie udało się otworzyć kamery: {err}")
            return
        self.start_button.configure(state="normal")
        self.status_var.set("Ustaw kamerę, a potem kliknij „Rozpocznij pomiar”.")

    def _tick(self) -> None:
        if not self.win.winfo_exists():
            return
        if self.camera is not None:
            img = self.camera.preview(self.app.px(320))
            if img is not None:
                from PIL import ImageTk  # noqa: PLC0415

                self._preview_image = ImageTk.PhotoImage(img)
                self.preview.configure(image=self._preview_image, width=img.width, height=img.height)
        self.win.after(100, self._tick)

    # ------------------------------------------------------------ measuring

    def start(self) -> None:
        phone = self.mode.get() == "phone"
        if self._running or (self.camera is None and not phone):
            return
        if phone:
            self._running = True
            self.start_button.configure(state="disabled")
            self.load_button.configure(state="disabled")
            self._countdown(5)
            return
        self._running = True
        self._stop.clear()
        self.start_button.configure(state="disabled")
        self.apply_button.configure(state="disabled")
        for iid in self.table.get_children():
            self.table.delete(iid)
        self._run(self.camera)

    def _countdown(self, seconds: int) -> None:
        if not self.win.winfo_exists():
            return
        if seconds > 0:
            self.status_var.set(f"Nagrywa telefon? Start za {seconds} s…")
            self.win.after(1000, lambda: self._countdown(seconds - 1))
            return
        self._run(None)

    def _run(self, camera) -> None:
        for iid in self.table.get_children():
            self.table.delete(iid)

        def progress(text: str) -> None:
            self.app._queue.put(lambda: self.status_var.set(f"Mierzę: {text}"))

        self.app.run_bg(lambda: calibrate.run(self.app.service, camera, progress, stop=self._stop),
                        self._done, self._failed)

    def load_video(self) -> None:
        if self.result is None or not self.result.commands:
            return
        path = filedialog.askopenfilename(
            parent=self.win, title="Film z pomiarem",
            filetypes=[("Filmy", "*.mov *.mp4 *.m4v *.avi *.mkv *.3gp"), ("Wszystkie pliki", "*.*")])
        if not path:
            return
        self.status_var.set("Analizuję film…")
        self.load_button.configure(state="disabled")
        result = self.result
        self.app.run_bg(lambda: calibrate.analyze_video(calibrate.read_video(path), result),
                        self._done, self._failed)

    def _failed(self, err: Exception) -> None:
        self._running = False
        if self.win.winfo_exists():
            self.start_button.configure(state="normal")
            if self.result is not None and self.result.commands:
                self.load_button.configure(state="normal")  # e.g. try another video
            self.status_var.set(f"Pomiar nie powiódł się: {err}")

    def _done(self, result: calibrate.Result) -> None:
        self._running = False
        if not self.win.winfo_exists():
            return
        self.start_button.configure(state="normal")
        self.result = result
        if result.message:
            self.status_var.set(result.message)
            if result.commands:
                self.load_button.configure(state="normal")
            return
        if result.keyboard_ms is None and result.commands:
            # filmed with a phone: the recording is analyzed next
            self.load_button.configure(state="normal")
            self.status_var.set("Gotowe. Zatrzymaj nagrywanie, przenieś film na komputer i kliknij "
                                "„Wczytaj film…”.")
            return
        usable = 0
        for iid in self.table.get_children():
            self.table.delete(iid)
        for light in result.lights:
            if light.relative_ms is None:
                values = (light.name, "nie widać", "–", "–")
            else:
                usable += 1
                values = (light.name, f"{light.response_ms:.0f} ms", f"{light.relative_ms:+.0f} ms",
                          f"{light.adjust_ms:+d} ms")
            self.table.insert("", "end", values=values)
        if usable:
            self.apply_button.configure(state="normal")
            self.status_var.set("Gotowe. „Zastosuj korekty” ustawi je światłom (pole „Korekta (ms)”).")
        else:
            self.status_var.set("Kamera nie zobaczyła zmian świateł – skieruj ją na pokój, "
                                "w którym świecą, i spróbuj ponownie.")

    def apply(self) -> None:
        if self.result is None:
            return
        changed = []
        for light in self.result.lights:
            dev = self.app.config.device(light.dev_id)
            if dev is not None and light.adjust_ms is not None:
                dev.latency_adjust_ms = light.adjust_ms
                changed.append(f"{dev.name} {light.adjust_ms:+d} ms")
        if not changed:
            return
        _LOGGER.info("Camera calibration applied: %s", "; ".join(changed))
        self.app.refresh_devices()
        self.app._reload_editor_if_idle()
        self.app.save_and_apply(bulbs_only=True)
        self.app.set_busy("Kalibracja zastosowana: " + "; ".join(changed))
        self.apply_button.configure(state="disabled")
        self.status_var.set("Zastosowano. Możesz zamknąć okno.")

    def close(self) -> None:
        self._stop.set()
        if self.camera is not None:
            self.camera.close()
            self.camera = None
        self.win.destroy()
        # Free the window's Tk variables here, on the window's thread: collected
        # later by another thread (a light's worker), Tk makes that thread wait
        # ~1 s for the main loop - a light stalled for a second.
        self.camera_var = self.status_var = self.mode = None
        gc.collect()


def open_window(app) -> CalibrationWindow | None:
    if not calibrate.available():
        messagebox.showinfo("TuyaChroma", "Ta wersja programu nie ma obsługi kamery.")
        return None
    if not app.config.settings.sync_enabled or not app.service.running:
        messagebox.showinfo("TuyaChroma", "Włącz sterowanie, żeby skalibrować światła.")
        return None
    return CalibrationWindow(app)
