"""End-to-end test of the Windows shared-memory path (runs only on Windows).

It publishes a real Synapse dump under the same Global\\ name RzSDKService
uses, then reads it back through ChromaReader.
"""

from __future__ import annotations

import ctypes
import sys

import pytest

from tuyachroma import razer
from test_razer import dump

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def publish(name: str, data: bytes):
    from ctypes import wintypes as W

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileMappingW.restype = W.HANDLE
    k32.CreateFileMappingW.argtypes = [W.HANDLE, ctypes.c_void_p, W.DWORD, W.DWORD, W.DWORD, W.LPCWSTR]
    k32.MapViewOfFile.restype = ctypes.c_void_p
    k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, ctypes.c_size_t]
    invalid = W.HANDLE(-1)
    mapping = k32.CreateFileMappingW(invalid, None, 0x04, 0, len(data), name)  # PAGE_READWRITE
    if not mapping:
        pytest.skip(f"cannot create {name} (error {ctypes.get_last_error()}), needs admin")
    view = k32.MapViewOfFile(mapping, 0x0002 | 0x0004, 0, 0, len(data))  # FILE_MAP_WRITE|READ
    ctypes.memmove(view, data, len(data))
    return k32, mapping, view


def test_reader_without_synapse_reports_unavailable() -> None:
    with pytest.raises(razer.RazerUnavailable):
        razer.ChromaReader(["headset"])


def test_process_running() -> None:
    import os

    exe = os.path.basename(sys.executable)
    assert razer.process_running(exe)
    assert not razer.process_running("definitely-not-running-123.exe")
    assert razer.razer_service_running() is False  # no Synapse on the build machine


def test_reader_reads_published_memory(monkeypatch) -> None:
    monkeypatch.setattr(razer, "razer_service_running", lambda: True)
    spec = razer.DEVICES["keyboard"]
    k32, mapping, view = publish(spec.file_name, dump("keyboard", write_index=9))
    try:
        reader = razer.ChromaReader(["keyboard"])
        try:
            reader.wait(10)
            frame = reader.read("keyboard")
        finally:
            reader.close()
    finally:
        k32.UnmapViewOfFile(ctypes.c_void_p(view))
        k32.CloseHandle(mapping)
    assert frame is not None and frame.active
    assert (221, 121, 0) in frame.colors


def test_autostart_registry(monkeypatch) -> None:
    import winreg

    from tuyachroma import winsys

    monkeypatch.setattr(winsys, "APP_NAME", "TuyaChromaTest")
    try:
        winsys.set_autostart(True, minimized=True)
        assert winsys.get_autostart()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, winsys.RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "TuyaChromaTest")
        assert value.endswith("--minimized") and sys.executable in value
    finally:
        winsys.set_autostart(False)
    assert not winsys.get_autostart()


def test_single_instance() -> None:
    from tuyachroma import winsys

    assert winsys.acquire_single_instance()
    first = winsys._instance_handle
    assert not winsys.acquire_single_instance()  # a second copy is detected
    winsys._instance_handle = first
