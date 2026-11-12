"""macOS IOKit through ctypes (no pyobjc): IOHID temperature sensors and the AppleSMC.

- ``HidThermals`` (Apple Silicon): ``IOHIDEventSystemClient`` services matching the Apple vendor
  temperature usage (page 0xff00, usage 5); each service's "Product" name -> temperature (°C).
  These are private-but-exported IOKit symbols used by every Apple Silicon monitoring tool; Apple may
  change them, so every failure simply yields no sensors.
- ``Smc``: the AppleSMC user client (selector 2, read key info / read bytes). Read-only, no root.

Frameworks are loaded lazily: importing this module is safe on any OS (``SMCKeyData`` is also used
by the unit tests to check the 80-byte layout).
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import POINTER, Structure, byref, c_bool, c_char, c_char_p, c_double, c_int, c_int32, c_int64, c_long
from ctypes import c_size_t, c_uint8, c_uint16, c_uint32, c_void_p, sizeof
from typing import Any

from .macos_parsers import decode_smc

log = logging.getLogger(__name__)

_CF_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_IOKIT_PATH = "/System/Library/Frameworks/IOKit.framework/IOKit"
_UTF8 = 0x08000100
_CFNUMBER_SINT32 = 3
_HID_PAGE_APPLE_VENDOR = 0xFF00
_HID_USAGE_TEMPERATURE = 5
_EVENT_TEMPERATURE = 15
_FIELD_TEMPERATURE = _EVENT_TEMPERATURE << 16

_libs: tuple[Any, Any] | None = None


def _load() -> tuple[Any, Any]:
    """(CoreFoundation, IOKit) with argtypes/restypes declared (64-bit pointers must not be truncated)."""
    global _libs
    if _libs is not None:
        return _libs
    cf = ctypes.cdll.LoadLibrary(_CF_PATH)
    io = ctypes.cdll.LoadLibrary(_IOKIT_PATH)
    for fn, args, res in (
        ("CFStringCreateWithCString", [c_void_p, c_char_p, c_uint32], c_void_p),
        ("CFStringGetCString", [c_void_p, c_char_p, c_long, c_uint32], c_bool),
        ("CFNumberCreate", [c_void_p, c_long, c_void_p], c_void_p),
        ("CFDictionaryCreate", [c_void_p, POINTER(c_void_p), POINTER(c_void_p), c_long, c_void_p, c_void_p], c_void_p),
        ("CFArrayGetCount", [c_void_p], c_long),
        ("CFArrayGetValueAtIndex", [c_void_p, c_long], c_void_p),
        ("CFGetTypeID", [c_void_p], c_long),
        ("CFStringGetTypeID", [], c_long),
        ("CFRelease", [c_void_p], None),
    ):
        getattr(cf, fn).argtypes, getattr(cf, fn).restype = args, res
    for fn, args, res in (
        ("IOHIDEventSystemClientCreate", [c_void_p], c_void_p),
        ("IOHIDEventSystemClientSetMatching", [c_void_p, c_void_p], c_int),
        ("IOHIDEventSystemClientCopyServices", [c_void_p], c_void_p),
        ("IOHIDServiceClientCopyProperty", [c_void_p, c_void_p], c_void_p),
        ("IOHIDServiceClientCopyEvent", [c_void_p, c_int64, c_int32, c_int64], c_void_p),
        ("IOHIDEventGetFloatValue", [c_void_p, c_int32], c_double),
        ("IOServiceMatching", [c_char_p], c_void_p),
        ("IOServiceGetMatchingService", [c_uint32, c_void_p], c_uint32),
        ("IOServiceOpen", [c_uint32, c_uint32, c_uint32, POINTER(c_uint32)], c_int),
        ("IOServiceClose", [c_uint32], c_int),
        ("IOObjectRelease", [c_uint32], c_int),
        ("IOConnectCallStructMethod", [c_uint32, c_uint32, c_void_p, c_size_t, c_void_p, POINTER(c_size_t)], c_int),
    ):
        getattr(io, fn).argtypes, getattr(io, fn).restype = args, res
    _libs = (cf, io)
    return _libs


def _cfstr(cf: Any, s: str) -> int:
    return cf.CFStringCreateWithCString(None, s.encode("utf-8"), _UTF8)


def _pystr(cf: Any, ref: int | None) -> str | None:
    if not ref or cf.CFGetTypeID(ref) != cf.CFStringGetTypeID():
        return None
    buf = ctypes.create_string_buffer(256)
    return buf.value.decode("utf-8", "replace") if cf.CFStringGetCString(ref, buf, 256, _UTF8) else None


class HidThermals:
    """Apple Silicon temperature sensors. ``read()`` -> {sensor name: °C} (empty when unavailable)."""

    def __init__(self) -> None:
        self._client: int | None = None
        self._product: int | None = None

    def open(self) -> bool:
        try:
            cf, io = _load()
            keys = (c_void_p * 2)(_cfstr(cf, "PrimaryUsagePage"), _cfstr(cf, "PrimaryUsage"))
            nums = [c_int32(_HID_PAGE_APPLE_VENDOR), c_int32(_HID_USAGE_TEMPERATURE)]
            vals = (c_void_p * 2)(*(cf.CFNumberCreate(None, _CFNUMBER_SINT32, byref(n)) for n in nums))
            callbacks = ctypes.addressof(c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
            vcallbacks = ctypes.addressof(c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
            matching = cf.CFDictionaryCreate(None, keys, vals, 2, callbacks, vcallbacks)
            self._client = io.IOHIDEventSystemClientCreate(None)
            if not self._client:
                return False
            io.IOHIDEventSystemClientSetMatching(self._client, matching)
            for ref in (*keys, *vals, matching):
                cf.CFRelease(ref)
            self._product = _cfstr(cf, "Product")
            return True
        except Exception as e:  # not macOS, symbols missing, sandbox
            log.info("IOHID thermal sensors unavailable: %s", type(e).__name__)
            self._client = None
            return False

    def read(self) -> dict[str, float]:
        if not self._client:
            return {}
        cf, io = _load()
        out: dict[str, float] = {}
        services = io.IOHIDEventSystemClientCopyServices(self._client)
        if not services:
            return out
        try:
            for i in range(cf.CFArrayGetCount(services)):
                svc = cf.CFArrayGetValueAtIndex(services, i)
                name_ref = io.IOHIDServiceClientCopyProperty(svc, self._product)
                name = _pystr(cf, name_ref)
                if name_ref:
                    cf.CFRelease(name_ref)
                event = io.IOHIDServiceClientCopyEvent(svc, _EVENT_TEMPERATURE, 0, 0)
                if not event:
                    continue
                value = io.IOHIDEventGetFloatValue(event, _FIELD_TEMPERATURE)
                cf.CFRelease(event)
                if name:
                    out[name] = max(value, out.get(name, value))  # several services can share a name
        finally:
            cf.CFRelease(services)
        return out

    def close(self) -> None:
        if self._client:
            cf, _ = _load()
            cf.CFRelease(self._client)
            if self._product:
                cf.CFRelease(self._product)
        self._client = None


# ---- AppleSMC -------------------------------------------------------------------------------------
class _Vers(Structure):
    _fields_ = [("major", c_uint8), ("minor", c_uint8), ("build", c_uint8), ("reserved", c_uint8), ("release", c_uint16)]


class _PLimit(Structure):
    _fields_ = [("version", c_uint16), ("length", c_uint16), ("cpuPLimit", c_uint32), ("gpuPLimit", c_uint32), ("memPLimit", c_uint32)]


class _KeyInfo(Structure):
    _fields_ = [("dataSize", c_uint32), ("dataType", c_uint32), ("dataAttributes", c_uint8)]


class SMCKeyData(Structure):
    """SMCKeyData_t from Apple's smc.h; the kernel expects exactly 80 bytes."""

    _fields_ = [("key", c_uint32), ("vers", _Vers), ("pLimitData", _PLimit), ("keyInfo", _KeyInfo), ("result", c_uint8),
                ("status", c_uint8), ("data8", c_uint8), ("data32", c_uint32), ("bytes", c_uint8 * 32)]  # fmt: skip


_SMC_SELECTOR = 2
_CMD_READ_BYTES = 5
_CMD_READ_KEYINFO = 9


def fourcc(key: str) -> int:
    return int.from_bytes(key.encode("ascii")[:4].ljust(4), "big")


def fourcc_str(v: int) -> str:
    return v.to_bytes(4, "big").decode("ascii", "replace")


class Smc:
    """``read(key)`` -> decoded float or None. Key info is cached per key."""

    def __init__(self) -> None:
        self._conn = c_uint32(0)
        self._info: dict[str, tuple[int, str] | None] = {}

    def open(self) -> bool:
        try:
            _, io = _load()
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            task = c_uint32.in_dll(libc, "mach_task_self_").value
            svc = io.IOServiceGetMatchingService(0, io.IOServiceMatching(b"AppleSMC"))
            if not svc:
                return False
            rc = io.IOServiceOpen(svc, task, 0, byref(self._conn))
            io.IOObjectRelease(svc)
            return rc == 0 and bool(self._conn.value)
        except Exception as e:
            log.info("AppleSMC unavailable: %s", type(e).__name__)
            return False

    def _call(self, inp: SMCKeyData) -> SMCKeyData | None:
        _, io = _load()
        out = SMCKeyData()
        size = c_size_t(sizeof(SMCKeyData))
        rc = io.IOConnectCallStructMethod(self._conn.value, _SMC_SELECTOR, byref(inp), sizeof(SMCKeyData), byref(out), byref(size))
        return out if rc == 0 and out.result == 0 else None

    def read(self, key: str) -> float | None:
        if not self._conn.value:
            return None
        try:
            if key not in self._info:
                inp = SMCKeyData(key=fourcc(key), data8=_CMD_READ_KEYINFO)
                out = self._call(inp)
                self._info[key] = (out.keyInfo.dataSize, fourcc_str(out.keyInfo.dataType)) if out and out.keyInfo.dataSize else None
            info = self._info[key]
            if info is None:
                return None
            inp = SMCKeyData(key=fourcc(key), data8=_CMD_READ_BYTES)
            inp.keyInfo.dataSize = info[0]
            out = self._call(inp)
            return decode_smc(info[1], bytes(out.bytes[: info[0]])) if out else None
        except Exception:
            return None

    def close(self) -> None:
        if self._conn.value:
            try:
                _load()[1].IOServiceClose(self._conn.value)
            except Exception:
                pass
        self._conn = c_uint32(0)
