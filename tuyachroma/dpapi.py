"""Protect secrets (Tuya local keys) at rest with Windows DPAPI.

Encrypted values can only be decrypted by the same Windows user. On other
platforms (tests) values are stored as-is.
"""

from __future__ import annotations

import base64
import ctypes
import sys

PREFIX = "dpapi:"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _call(func_name: str, data: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32")
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    func = getattr(crypt32, func_name)
    if func_name == "CryptProtectData":
        ok = func(ctypes.byref(blob_in), "TuyaChroma", None, None, None, 0, ctypes.byref(blob_out))
    else:
        ok = func(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"{func_name} failed ({ctypes.get_last_error()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect(secret: str) -> str:
    if sys.platform != "win32" or not secret:
        return secret
    return PREFIX + base64.b64encode(_call("CryptProtectData", secret.encode())).decode()


def unprotect(value: str) -> str:
    if not value.startswith(PREFIX):
        return value
    if sys.platform != "win32":
        raise OSError("DPAPI-protected value can only be read on Windows")
    return _call("CryptUnprotectData", base64.b64decode(value[len(PREFIX):])).decode()
