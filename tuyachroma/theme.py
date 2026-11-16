"""The window's look: a quiet graphite interface (or a light one, following
Windows), where the only colors on screen come from the lights themselves -
the glowing strip in the header.

Built on ttk's "clam" theme, which takes custom colors the same way on every
Windows version (the native "vista" theme ignores most of them).
"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

PALETTES = {
    "dark": {
        "CANVAS": "#1C1F26",   # window background
        "SURFACE": "#242832",  # lists, fields
        "RAISED": "#2D323D",   # buttons, the selected tab
        "HOVER": "#363C49",    # hovered button / tab
        "SELECT": "#3A4560",   # selected row
        "LINE": "#373D49",     # borders, separators
        "INK": "#ECEEF2",      # text
        "MUTED": "#9AA2AF",    # secondary text
        "IDLE": "#6B7380",
        "OK": "#5BCB93",
        "WARN": "#E8B65A",
        "ERROR": "#F07474",
        "PREVIEW": "#14161B",  # behind the keyboard preview
    },
    "light": {
        "CANVAS": "#F3F5F8",
        "SURFACE": "#FFFFFF",
        "RAISED": "#FFFFFF",
        "HOVER": "#E8ECF1",
        "SELECT": "#DAE3EF",
        "LINE": "#D5DAE1",
        "INK": "#1D232C",
        "MUTED": "#5C6673",
        "IDLE": "#8A939E",
        "OK": "#2F7D4F",
        "WARN": "#A86E12",
        "ERROR": "#B23A3A",
        "PREVIEW": "#16181D",
    },
}
MODES = ("auto", "dark", "light")

# The active palette (set by use(); dark until then)
CANVAS = SURFACE = RAISED = HOVER = SELECT = LINE = INK = MUTED = IDLE = OK = WARN = ERROR = PREVIEW = ""
DARK = True


def system_prefers_dark() -> bool:
    """Windows' app mode (Settings > Personalization > Colors)."""
    if sys.platform != "win32":
        return True
    try:
        import winreg  # noqa: PLC0415

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def use(mode: str = "auto") -> str:
    """Pick the palette ("auto" follows Windows); returns "dark" or "light"."""
    global DARK
    name = mode if mode in ("dark", "light") else ("dark" if system_prefers_dark() else "light")
    globals().update(PALETTES[name])
    DARK = name == "dark"
    return name


use("dark")


def _family(root: tk.Misc, *candidates: str) -> str:
    available = set(tkfont.families(root))
    return next((f for f in candidates if f in available), "TkDefaultFont")


def apply(root: tk.Tk, scale: float = 1.0) -> dict[str, tuple]:
    """Style every ttk widget; returns the fonts used by custom widgets."""
    text = _family(root, "Segoe UI Variable Text", "Segoe UI", "Helvetica", "DejaVu Sans")
    display = _family(root, "Segoe UI Variable Display Semib", "Segoe UI Variable Display",
                      "Segoe UI Semibold", "Segoe UI", text)
    fonts = {
        "body": (text, 10),
        "small": (text, 9),
        "strong": (text, 10, "bold"),
        "title": (display, 18, "bold"),
        "pill": (text, 9, "bold"),  # names on the light pills
    }
    pad = max(1, round(4 * scale))

    root.configure(background=CANVAS)
    # Plain Tk widgets (the profile list, the combobox drop-downs) take their
    # look from the option database.
    for option, value in (("*Listbox.background", SURFACE), ("*Listbox.foreground", INK),
                          ("*Listbox.selectBackground", SELECT), ("*Listbox.selectForeground", INK),
                          ("*Listbox.highlightColor", LINE), ("*Listbox.highlightBackground", LINE),
                          ("*Listbox.borderWidth", 0), ("*Listbox.font", fonts["body"]),
                          ("*Listbox.activeStyle", "none"),
                          ("*TCombobox*Listbox.background", SURFACE),
                          ("*TCombobox*Listbox.foreground", INK),
                          ("*TCombobox*Listbox.selectBackground", SELECT),
                          ("*TCombobox*Listbox.selectForeground", INK),
                          ("*Canvas.background", CANVAS), ("*Toplevel.background", CANVAS)):
        root.option_add(option, value)
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    style.configure(".", background=CANVAS, foreground=INK, font=fonts["body"],
                    bordercolor=LINE, lightcolor=CANVAS, darkcolor=CANVAS,
                    troughcolor=SURFACE, focuscolor=MUTED, selectbackground=SELECT, selectforeground=INK,
                    fieldbackground=SURFACE, insertcolor=INK)
    style.map(".", foreground=[("disabled", IDLE)])
    style.configure("TFrame", background=CANVAS)
    style.configure("TLabel", background=CANVAS, foreground=INK)
    style.configure("Header.TLabel", font=fonts["title"], foreground=INK)
    style.configure("Muted.TLabel", foreground=MUTED, font=fonts["small"])

    # Buttons: flat, a shade above the background; the main action is inverted.
    style.configure("TButton", background=RAISED, foreground=INK, bordercolor=RAISED,
                    lightcolor=RAISED, darkcolor=RAISED, padding=(pad * 3, pad - 1), relief="flat")
    style.map("TButton",
              background=[("disabled", CANVAS), ("pressed", SELECT), ("active", HOVER)],
              lightcolor=[("pressed", SELECT), ("active", HOVER)],
              darkcolor=[("pressed", SELECT), ("active", HOVER)],
              bordercolor=[("focus", MUTED), ("active", HOVER)],
              foreground=[("disabled", IDLE)])
    style.configure("Accent.TButton", background=INK, foreground=CANVAS, bordercolor=INK,
                    lightcolor=INK, darkcolor=INK, font=fonts["strong"])
    style.map("Accent.TButton",
              background=[("pressed", MUTED), ("active", MUTED)],
              lightcolor=[("pressed", MUTED), ("active", MUTED)],
              darkcolor=[("pressed", MUTED), ("active", MUTED)],
              bordercolor=[("focus", MUTED), ("active", MUTED)])

    # Tabs: borderless pills; the open one is raised.
    style.configure("TNotebook", background=CANVAS, borderwidth=0, bordercolor=CANVAS,
                    lightcolor=CANVAS, darkcolor=CANVAS, tabmargins=(0, 0, 0, pad))
    style.configure("TNotebook.Tab", background=CANVAS, foreground=MUTED, bordercolor=CANVAS,
                    lightcolor=CANVAS, darkcolor=CANVAS, padding=(pad * 4, pad + 2), borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", RAISED), ("active", HOVER)],
              foreground=[("selected", INK), ("active", INK)],
              lightcolor=[("selected", RAISED), ("active", HOVER)],
              darkcolor=[("selected", RAISED), ("active", HOVER)],
              bordercolor=[("selected", RAISED), ("active", HOVER)],
              expand=[("selected", (0, 0, 0, 0))])

    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=INK,
                    bordercolor=SURFACE, lightcolor=SURFACE, darkcolor=SURFACE,
                    rowheight=max(24, round(26 * scale)))
    style.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", INK)])
    style.configure("Treeview.Heading", background=SURFACE, foreground=MUTED, font=fonts["small"],
                    bordercolor=SURFACE, lightcolor=SURFACE, darkcolor=SURFACE, relief="flat",
                    padding=(pad, pad + 1))
    style.map("Treeview.Heading", background=[("active", HOVER)])

    style.configure("TLabelframe", background=CANVAS, bordercolor=LINE, lightcolor=CANVAS,
                    darkcolor=CANVAS)
    style.configure("TLabelframe.Label", background=CANVAS, foreground=INK, font=fonts["strong"])
    for widget in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(widget, fieldbackground=SURFACE, background=RAISED, foreground=INK,
                        bordercolor=LINE, lightcolor=SURFACE, darkcolor=SURFACE, arrowcolor=MUTED,
                        padding=pad // 2)
        style.map(widget, bordercolor=[("focus", MUTED)], lightcolor=[("focus", SURFACE)],
                  fieldbackground=[("readonly", SURFACE), ("disabled", CANVAS)],
                  foreground=[("disabled", IDLE)], background=[("active", HOVER)])
    for widget in ("TCheckbutton", "TRadiobutton"):
        style.configure(widget, background=CANVAS, foreground=INK, indicatorcolor=SURFACE,
                        indicatorbackground=SURFACE, upperbordercolor=LINE, lowerbordercolor=LINE)
        style.map(widget, background=[("active", CANVAS)],
                  indicatorcolor=[("selected", INK), ("pressed", SELECT)])
    style.configure("Horizontal.TScale", background=RAISED, troughcolor=SURFACE, bordercolor=LINE,
                    lightcolor=RAISED, darkcolor=RAISED)
    style.map("Horizontal.TScale", background=[("active", HOVER)])
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background=RAISED, troughcolor=CANVAS,
                        bordercolor=CANVAS, lightcolor=RAISED, darkcolor=RAISED, arrowcolor=MUTED,
                        gripcount=0)
        style.map(f"{orient}.TScrollbar", background=[("active", HOVER)])
    style.configure("TSeparator", background=LINE)
    return fonts


def readable_on(rgb: tuple[int, int, int]) -> str:
    """Dark or light text, whichever reads better on this color."""
    r, g, b = rgb
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#15181E" if luminance > 140 else "#FFFFFF"


def hex_color(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def mix(rgb: tuple[int, int, int], other: str, amount: float) -> str:
    """``rgb`` blended into the color ``other`` ("#rrggbb"): 0 = rgb, 1 = other."""
    base = tuple(int(other[i:i + 2], 16) for i in (1, 3, 5))
    return hex_color(tuple(c + (o - c) * amount for c, o in zip(rgb, base)))


def rounded_rect(canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float, r: float,
                 **kw) -> int:
    """A rectangle with round corners (Tk has none)."""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    points = (x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
              x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0)
    return canvas.create_polygon(points, smooth=True, **kw)


def dark_title_bar(root: tk.Tk) -> None:
    """Windows 10/11: a title bar matching the dark window."""
    if sys.platform != "win32" or not DARK:
        return
    try:
        import ctypes  # noqa: PLC0415

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (newer, older builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                                         ctypes.sizeof(value)) == 0:
                break
    except Exception:  # noqa: BLE001 - only the looks
        pass
