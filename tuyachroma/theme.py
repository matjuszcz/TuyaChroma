"""The window's look: a quiet, cool grey interface; the only colors on screen
come from the lights themselves (the live strip in the header).

Built on ttk's "clam" theme, which takes custom colors the same way on every
Windows version (the native "vista" theme ignores most of them).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# Palette
CANVAS = "#F4F6F8"    # window background
SURFACE = "#FFFFFF"   # lists, fields
INK = "#1F2630"       # text
MUTED = "#5E6875"     # secondary text
LINE = "#D3D8DE"      # borders, separators
SELECT = "#DCE4EE"    # selected row / pressed button
HOVER = "#EAEEF2"     # hovered button / tab

# Status colors: only for states, never decoration
OK = "#2F7D4F"
WARN = "#B7791F"
ERROR = "#B23A3A"
IDLE = "#8A939E"


def _family(root: tk.Misc, *candidates: str) -> str:
    available = set(tkfont.families(root))
    return next((f for f in candidates if f in available), "TkDefaultFont")


def apply(root: tk.Tk, scale: float = 1.0) -> dict[str, tuple]:
    """Style every ttk widget; returns the fonts used by custom widgets."""
    text = _family(root, "Segoe UI Variable Text", "Segoe UI", "Helvetica", "DejaVu Sans")
    display = _family(root, "Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI", text)
    fonts = {
        "body": (text, 10),
        "small": (text, 9),
        "strong": (text, 10, "bold"),
        "title": (display, 17, "bold"),
    }
    pad = max(1, round(4 * scale))

    root.configure(background=CANVAS)
    # Plain Tk widgets (the profile list) take their look from the option database.
    for option, value in (("*Listbox.background", SURFACE), ("*Listbox.foreground", INK),
                          ("*Listbox.selectBackground", SELECT), ("*Listbox.selectForeground", INK),
                          ("*Listbox.highlightColor", MUTED), ("*Listbox.highlightBackground", LINE),
                          ("*Listbox.borderWidth", 0), ("*Listbox.font", fonts["body"]),
                          ("*TCombobox*Listbox.background", SURFACE)):
        root.option_add(option, value)
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")

    style.configure(".", background=CANVAS, foreground=INK, font=fonts["body"],
                    bordercolor=LINE, lightcolor=CANVAS, darkcolor=CANVAS,
                    troughcolor=LINE, focuscolor=INK, selectbackground=SELECT, selectforeground=INK,
                    fieldbackground=SURFACE, insertcolor=INK)
    style.configure("TFrame", background=CANVAS)
    style.configure("TLabel", background=CANVAS, foreground=INK)
    style.configure("Header.TLabel", font=fonts["title"], foreground=INK)
    style.configure("Muted.TLabel", foreground=MUTED, font=fonts["small"])

    style.configure("TButton", background=SURFACE, foreground=INK, bordercolor=LINE,
                    lightcolor=SURFACE, darkcolor=SURFACE, padding=(pad * 3, pad - 1), relief="flat")
    style.map("TButton",
              background=[("pressed", SELECT), ("active", HOVER), ("disabled", CANVAS)],
              foreground=[("disabled", IDLE)],
              bordercolor=[("focus", MUTED)])

    style.configure("TNotebook", background=CANVAS, borderwidth=0, tabmargins=(0, pad, 0, 0))
    style.configure("TNotebook.Tab", background=CANVAS, foreground=MUTED, bordercolor=CANVAS,
                    lightcolor=CANVAS, padding=(pad * 3, pad + 2))
    style.map("TNotebook.Tab",
              background=[("selected", SURFACE), ("active", HOVER)],
              foreground=[("selected", INK)],
              lightcolor=[("selected", SURFACE)],
              bordercolor=[("selected", LINE)])

    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=INK,
                    bordercolor=LINE, rowheight=max(22, round(26 * scale)))
    style.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", INK)])
    style.configure("Treeview.Heading", background=CANVAS, foreground=MUTED, font=fonts["small"],
                    bordercolor=LINE, lightcolor=CANVAS, darkcolor=CANVAS, relief="flat",
                    padding=(pad, pad))
    style.map("Treeview.Heading", background=[("active", HOVER)])

    style.configure("TLabelframe", background=CANVAS, bordercolor=LINE, lightcolor=CANVAS,
                    darkcolor=CANVAS)
    style.configure("TLabelframe.Label", background=CANVAS, foreground=INK, font=fonts["strong"])
    for widget in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(widget, fieldbackground=SURFACE, bordercolor=LINE, lightcolor=SURFACE,
                        darkcolor=SURFACE, arrowcolor=MUTED, padding=1)
        style.map(widget, bordercolor=[("focus", MUTED)])
    style.map("TCombobox", fieldbackground=[("readonly", SURFACE)])
    for widget in ("TCheckbutton", "TRadiobutton"):
        style.configure(widget, background=CANVAS, foreground=INK, indicatorcolor=SURFACE)
        style.map(widget, background=[("active", CANVAS)],
                  indicatorcolor=[("selected", INK), ("pressed", SELECT)])
    style.configure("Horizontal.TScale", background=CANVAS, troughcolor=LINE)
    style.configure("TScrollbar", background=CANVAS, troughcolor=CANVAS, bordercolor=CANVAS,
                    arrowcolor=MUTED)
    style.configure("TSeparator", background=LINE)
    return fonts


def readable_on(rgb: tuple[int, int, int]) -> str:
    """Black or white text, whichever reads better on this color."""
    r, g, b = rgb
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return INK if luminance > 140 else SURFACE


def hex_color(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)
