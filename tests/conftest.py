import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_runtime(monkeypatch):
    """Each test starts clean: no real Razer hardware lookup (SetupAPI on the
    Windows CI) and nothing bulbs learned in an earlier test."""
    from tuyachroma import bulbs, engine

    monkeypatch.setattr(engine, "detect_keyboard_layout", lambda: "full")
    for name, empty in (("_learned_slow", dict), ("_music_hangs", set), ("_drop_ages", dict),
                        ("_max_age", dict), ("_rate_innocent", set)):
        monkeypatch.setattr(bulbs, name, empty())
