"""Program updates from GitHub Releases.

Checks https://github.com/matjuszcz/TuyaChroma/releases for a newer version,
downloads TuyaChroma-windows.zip, checks its SHA-256 and replaces the running
TuyaChroma.exe (Windows lets a running .exe be renamed, not overwritten).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

from . import __version__

_LOGGER = logging.getLogger(__name__)

REPO = "matjuszcz/TuyaChroma"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET_NAME = "TuyaChroma-windows.zip"
EXE_NAME = "TuyaChroma.exe"
MAX_DOWNLOAD = 200 * 1024 * 1024


class UpdateError(Exception):
    """Update check or installation failed."""


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    page_url: str
    zip_url: str
    zip_size: int
    sha256: str  # hex, may be "" if GitHub gave none and no .sha256 file exists


def parse_version(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+(?:\.\d+)*)", text or "")
    if not m:
        return (0,)
    return tuple(int(x) for x in m.group(1).split("."))


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


def _get(url: str, timeout: float = 15, accept: str = "application/json") -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": f"TuyaChroma/{__version__}",
        "Accept": accept,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URLs
            return resp.read(MAX_DOWNLOAD + 1)
    except OSError as err:
        raise UpdateError(f"brak połączenia z GitHub: {err}") from err


def check_latest(get=_get) -> Release | None:
    """The latest published release, or None if there is none yet."""
    try:
        data = json.loads(get(LATEST_URL))
    except UpdateError as err:
        if "404" in str(err):
            return None  # no release published yet
        raise
    except ValueError as err:
        raise UpdateError(f"nieprawidłowa odpowiedź GitHub: {err}") from err
    assets = {a.get("name"): a for a in data.get("assets", [])}
    asset = assets.get(ASSET_NAME)
    if asset is None:
        return None
    sha = ""
    digest = asset.get("digest") or ""
    if digest.startswith("sha256:"):
        sha = digest.split(":", 1)[1].lower()
    elif f"{ASSET_NAME}.sha256" in assets:
        text = get(assets[f"{ASSET_NAME}.sha256"]["browser_download_url"],
                   accept="application/octet-stream").decode("ascii", "replace")
        sha = text.split()[0].lower() if text.split() else ""
    tag = data.get("tag_name", "")
    return Release(
        version=".".join(str(x) for x in parse_version(tag)),
        tag=tag,
        notes=(data.get("body") or "").strip(),
        page_url=data.get("html_url", f"https://github.com/{REPO}/releases"),
        zip_url=asset["browser_download_url"],
        zip_size=int(asset.get("size") or 0),
        sha256=sha,
    )


def can_self_update() -> bool:
    return getattr(sys, "frozen", False) and sys.platform == "win32"


def download(release: Release, dest_dir: Path, get=_get) -> Path:
    """Download and verify the release; returns the path of the new exe."""
    data = get(release.zip_url, timeout=120, accept="application/octet-stream")
    if len(data) > MAX_DOWNLOAD:
        raise UpdateError("plik aktualizacji jest za duży")
    if release.sha256:
        actual = hashlib.sha256(data).hexdigest()
        if actual != release.sha256:
            raise UpdateError("suma kontrolna się nie zgadza – pobrany plik jest uszkodzony")
    else:
        raise UpdateError("brak sumy kontrolnej w wydaniu – aktualizacja przerwana")
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / ASSET_NAME
    zip_path.write_bytes(data)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [m for m in zf.namelist() if Path(m).name == EXE_NAME]
            if not members:
                raise UpdateError("w paczce nie ma TuyaChroma.exe")
            target = dest_dir / EXE_NAME
            with zf.open(members[0]) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    except zipfile.BadZipFile as err:
        raise UpdateError(f"uszkodzona paczka: {err}") from err
    return target


def install(new_exe: Path, current_exe: Path) -> Path:
    """Swap the exe files. The running exe is renamed to *.old (deleted at next start)."""
    old = current_exe.with_name(current_exe.name + ".old")
    if old.exists():
        old.unlink()
    os.replace(current_exe, old)
    try:
        shutil.move(str(new_exe), str(current_exe))
    except OSError:
        os.replace(old, current_exe)  # roll back
        raise
    return old


def cleanup_old(current_exe: Path | None = None) -> None:
    """Delete the previous version left by an update."""
    exe = current_exe or Path(sys.executable)
    old = exe.with_name(exe.name + ".old")
    try:
        if old.exists():
            old.unlink()
            _LOGGER.info("Removed previous version %s", old)
    except OSError as err:
        _LOGGER.debug("Cannot remove %s yet: %s", old, err)


def apply_update(release: Release, get=_get) -> None:
    """Download, verify and install; then start the new version (caller exits)."""
    if not can_self_update():
        raise UpdateError("aktualizacja działa tylko w wersji .exe – pobierz ją ze strony wydań")
    current = Path(sys.executable)
    work = Path(tempfile.mkdtemp(prefix="TuyaChroma-update-"))
    new_exe = download(release, work, get)
    install(new_exe, current)
    shutil.rmtree(work, ignore_errors=True)
    _LOGGER.info("Updated to %s, restarting", release.version)
    subprocess.Popen([str(current), "--updated"], close_fds=True)  # noqa: S603
