"""Windows DPAPI (CryptProtectData, current-user scope) via ctypes. No third-party dependency.

On non-Windows platforms ``protect``/``unprotect`` raise ``DpapiUnavailable``; the macOS agent will
use the Keychain instead.
"""

from __future__ import annotations

import ctypes
import sys

_ENTROPY = b"PcControlDeck.devices.v1"  # binds blobs to this application


class DpapiUnavailable(RuntimeError):
    pass


if sys.platform == "win32":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1

    def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _call(fn, data: bytes) -> bytes:
        inp, _keep1 = _blob(data)
        ent, _keep2 = _blob(_ENTROPY)
        out = _Blob()
        if not fn(ctypes.byref(inp), None, ctypes.byref(ent), None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
            raise OSError(ctypes.GetLastError(), "DPAPI call failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(out.pbData)

    def protect(data: bytes) -> bytes:
        return _call(lambda i, d, e, r, p, f, o: _crypt32.CryptProtectData(i, d, e, r, p, f, o), data)

    def unprotect(data: bytes) -> bytes:
        return _call(lambda i, d, e, r, p, f, o: _crypt32.CryptUnprotectData(i, d, e, r, p, f, o), data)

else:  # pragma: no cover - exercised on macOS later

    def protect(data: bytes) -> bytes:
        raise DpapiUnavailable("DPAPI is Windows-only")

    def unprotect(data: bytes) -> bytes:
        raise DpapiUnavailable("DPAPI is Windows-only")
