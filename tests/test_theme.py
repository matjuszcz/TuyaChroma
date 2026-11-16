"""The window's look: palettes, the Windows setting, helpers, the saved choice."""

from __future__ import annotations

import sys
import types

import pytest

from tuyachroma import config as config_mod
from tuyachroma import theme


@pytest.fixture(autouse=True)
def _back_to_dark():
    yield
    theme.use("dark")


def test_use_picks_the_palette(monkeypatch) -> None:
    assert theme.use("light") == "light" and not theme.DARK
    assert theme.CANVAS == theme.PALETTES["light"]["CANVAS"]
    assert theme.use("dark") == "dark" and theme.DARK
    assert theme.CANVAS == theme.PALETTES["dark"]["CANVAS"]
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: False)
    assert theme.use("auto") == "light" and theme.use("bogus") == "light"
    monkeypatch.setattr(theme, "system_prefers_dark", lambda: True)
    assert theme.use("auto") == "dark"


@pytest.mark.parametrize("value, dark", [(0, True), (1, False), (None, False)])
def test_windows_app_mode_is_followed(monkeypatch, value, dark) -> None:
    class Key:
        def __enter__(self):
            if value is None:
                raise OSError("no such key")
            return self

        def __exit__(self, *a):
            return False

    fake = types.SimpleNamespace(HKEY_CURRENT_USER=1, OpenKey=lambda *a: Key(),
                                 QueryValueEx=lambda key, name: (value, 4))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    assert theme.system_prefers_dark() is dark


def test_helpers() -> None:
    assert theme.hex_color((-5, 300, 128)) == "#00ff80"
    assert theme.mix((255, 0, 0), "#000000", 0.0) == "#ff0000"
    assert theme.mix((255, 0, 0), "#000000", 1.0) == "#000000"
    for rgb in ((0, 195, 0), (255, 90, 255), (0, 180, 150), (255, 255, 0)):
        assert theme.readable_on(rgb) == "#15181E"   # bright colors: dark names
    for rgb in ((40, 0, 120), (180, 0, 0), (0, 0, 0)):
        assert theme.readable_on(rgb) == "#FFFFFF"


def test_palettes_are_readable() -> None:
    def lum(hex_):
        def ch(c):
            c /= 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        r, g, b = (ch(int(hex_[i:i + 2], 16)) for i in (1, 3, 5))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def ratio(a, b):
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    for name, p in theme.PALETTES.items():
        for bg in ("CANVAS", "SURFACE"):
            for fg in ("INK", "MUTED", "OK", "WARN", "ERROR"):
                assert ratio(p[fg], p[bg]) >= 4.5, (name, fg, bg)
        assert ratio(p["IDLE"], p["SURFACE"]) >= 3, name   # slider / scrollbar thumbs


def test_rounded_rect_survives_a_tiny_box() -> None:
    calls = []
    canvas = types.SimpleNamespace(create_polygon=lambda pts, **kw: calls.append(pts) or 1)
    theme.rounded_rect(canvas, 0, 0, 4, 2, 50)
    assert len(calls[0]) == 56 and all(-1 <= v <= 5 for v in calls[0])


def test_dark_title_bar_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    theme.use("dark")

    class Root:
        def update_idletasks(self):
            raise RuntimeError("no window")

    theme.dark_title_bar(Root())  # swallowed: only the looks


def test_theme_setting_round_trip(tmp_path) -> None:
    path = tmp_path / "config.json"
    cfg = config_mod.AppConfig()
    for value in ("dark", "light", "auto"):
        cfg.settings.theme = value
        config_mod.save(cfg, path)
        assert config_mod.load(path).settings.theme == value
    path.write_text('{"settings": {"theme": "blue"}}', encoding="utf-8")
    assert config_mod.load(path).settings.theme == "auto"


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_window_uses_the_chosen_palette(tmp_path, monkeypatch, mode) -> None:
    from tkinter import ttk

    from test_app import FakeReader, Recorder, _tk_available

    if not _tk_available():
        pytest.skip("no display / tkinter")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from tuyachroma import gui
    from tuyachroma.engine import SyncService

    cfg = config_mod.AppConfig()
    cfg.settings.theme = mode
    service = SyncService(reader_factory=FakeReader, worker_factory=Recorder, service_check=lambda: True)
    app = gui.App(cfg, service, start_hidden=False, use_tray=False)
    try:
        p = theme.PALETTES[mode]
        style = ttk.Style(app.root)
        assert app.root.cget("background").lower() == p["CANVAS"].lower()
        assert app.strip.cget("background").lower() == p["CANVAS"].lower()
        assert style.lookup("TFrame", "background").lower() == p["CANVAS"].lower()
        assert style.lookup("Treeview", "background").lower() == p["SURFACE"].lower()
        assert style.lookup("TCheckbutton", "indicatorforeground").lower() == p["INK"].lower()
        assert style.lookup("TButton", "background").lower() == p["RAISED"].lower()
    finally:
        app.root.destroy()
