"""Program updates (GitHub Releases) and the Razer device database."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from tuyachroma import __version__, devices_db, razer_devices, updater


# ---------------------------------------------------------------- updater


def make_zip(exe_bytes: bytes = b"MZ new exe") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("TuyaChroma.exe", exe_bytes)
        zf.writestr("README.md", "x")
    return buf.getvalue()


def fake_github(zip_bytes: bytes, tag: str = "v99.0.0", digest: str | None = "auto",
                sha_file: bool = False):
    sha = hashlib.sha256(zip_bytes).hexdigest()
    asset = {"name": "TuyaChroma-windows.zip", "browser_download_url": "https://dl/zip",
             "size": len(zip_bytes)}
    if digest == "auto":
        asset["digest"] = f"sha256:{sha}"
    elif digest:
        asset["digest"] = digest
    assets = [asset]
    if sha_file:
        assets.append({"name": "TuyaChroma-windows.zip.sha256", "browser_download_url": "https://dl/sha"})
    release = {"tag_name": tag, "body": "Nowości", "html_url": "https://gh/rel", "assets": assets}

    def get(url, timeout=15, accept=""):
        if url == updater.LATEST_URL:
            return json.dumps(release).encode()
        if url == "https://dl/zip":
            return zip_bytes
        if url == "https://dl/sha":
            return f"{sha}  TuyaChroma-windows.zip\n".encode()
        raise updater.UpdateError("404")

    return get


def test_versions() -> None:
    assert updater.parse_version("v1.10.2") == (1, 10, 2)
    assert updater.is_newer("1.10.0", "1.9.9") and not updater.is_newer("1.4.0", "1.4.0")
    assert updater.is_newer("v99", __version__)


def test_check_latest() -> None:
    z = make_zip()
    rel = updater.check_latest(fake_github(z))
    assert rel.version == "99.0.0" and rel.sha256 == hashlib.sha256(z).hexdigest()
    assert rel.notes == "Nowości"
    # checksum from a .sha256 file when GitHub gives no digest
    rel = updater.check_latest(fake_github(z, digest=None, sha_file=True))
    assert rel.sha256 == hashlib.sha256(z).hexdigest()

    def no_release(url, timeout=15, accept=""):
        raise updater.UpdateError("brak połączenia z GitHub: HTTP Error 404: Not Found")

    assert updater.check_latest(no_release) is None


def test_download_verifies_checksum(tmp_path) -> None:
    z = make_zip(b"MZ good")
    get = fake_github(z)
    exe = updater.download(updater.check_latest(get), tmp_path / "a", get)
    assert exe.read_bytes() == b"MZ good"

    bad = fake_github(z, digest="sha256:" + "0" * 64)
    with pytest.raises(updater.UpdateError, match="suma kontrolna"):
        updater.download(updater.check_latest(bad), tmp_path / "b", bad)
    none = fake_github(z, digest=None)
    with pytest.raises(updater.UpdateError, match="brak sumy"):
        updater.download(updater.check_latest(none), tmp_path / "c", none)


def test_install_swaps_exe_and_cleans_up(tmp_path) -> None:
    current = tmp_path / "TuyaChroma.exe"
    current.write_bytes(b"old version")
    new = tmp_path / "dl" / "TuyaChroma.exe"
    new.parent.mkdir()
    new.write_bytes(b"new version")
    old = updater.install(new, current)
    assert current.read_bytes() == b"new version" and old.read_bytes() == b"old version"
    updater.cleanup_old(current)  # next start removes the previous version
    assert not old.exists()


def test_apply_update_only_for_exe(monkeypatch) -> None:
    monkeypatch.setattr(updater, "can_self_update", lambda: False)
    with pytest.raises(updater.UpdateError, match=".exe"):
        updater.apply_update(updater.Release("9", "v9", "", "", "", 0, "x"))


# ------------------------------------------------------- device database


@pytest.fixture
def db_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    devices_db.reset()
    yield tmp_path
    devices_db.reset()


def test_bundled_database_is_valid(db_dir) -> None:
    db = devices_db.load()
    assert db["version"] >= 1
    assert razer_devices.categorize("Razer Huntsman Mini") == "keyboard"
    assert devices_db.bundled_path().exists()


def test_database_update(db_dir) -> None:
    current = devices_db.load()
    newer = json.loads(json.dumps(current))
    newer["version"] = current["version"] + 1
    newer["rules"].insert(0, {"category": "mouse", "keywords": ["superfuture mouse x"]})
    newer["notes"].append({"match": "superfuture", "note": "nowe urządzenie"})

    updated, version = devices_db.update(fetch=lambda: newer)
    assert updated and version == newer["version"]
    assert (db_dir / "TuyaChroma" / "razer_devices.json").exists()  # cached for next starts
    dev = razer_devices.RazerDevice("Razer Superfuture Mouse X", "mouse", "USB")
    assert razer_devices.categorize(dev.name) == "mouse" and dev.note == "nowe urządzenie"

    devices_db.reset()  # restart: the cached newer copy wins over the built-in one
    assert devices_db.load()["version"] == newer["version"]
    assert devices_db.update(fetch=lambda: current) == (False, newer["version"])  # never downgrade


def test_invalid_database_rejected(db_dir) -> None:
    for bad in ({"version": "x"}, {"version": 9, "rules": []},
                {"version": 9, "rules": [{"category": "toaster", "keywords": ["a"]}]},
                {"version": 9, "rules": [{"category": "mouse", "keywords": ["a"]}], "pids": {"1": {"category": "?"}}}):
        with pytest.raises(devices_db.DatabaseError):
            devices_db.update(fetch=lambda bad=bad: devices_db.validate(bad))
    # a corrupted cached file is ignored, the built-in database is used
    cache = db_dir / "TuyaChroma" / "razer_devices.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{not json", encoding="utf-8")
    devices_db.reset()
    assert devices_db.load()["version"] >= 1


def test_pid_fallback_name(db_dir) -> None:
    devs = razer_devices.group_devices([(r"USB\VID_1532&PID_0257\1", "USB Composite Device")])
    assert devs[0].name == "Razer Huntsman Mini" and devs[0].category == "keyboard"


def test_repository_database_matches_bundled() -> None:
    repo = Path(__file__).resolve().parents[1] / "data" / "razer_devices.json"
    devices_db.validate(json.loads(repo.read_text(encoding="utf-8")))


def test_update_refuses_read_only_folder(tmp_path, monkeypatch) -> None:
    import sys

    exe = tmp_path / "TuyaChroma.exe"
    exe.write_bytes(b"x")
    monkeypatch.setattr(updater, "can_self_update", lambda: True)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(updater.os, "access", lambda path, mode: False)  # e.g. Program Files
    downloads = []
    with pytest.raises(updater.UpdateError, match="uprawnień"):
        updater.apply_update(updater.Release("9", "v9", "", "", "", 0, "x"),
                             get=lambda *a, **k: downloads.append(a))
    assert downloads == []  # refused before downloading anything
