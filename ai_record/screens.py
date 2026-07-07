"""Monitor / window / virtual-desktop enumeration + region geometry (video capture).

All OS calls go through ``ctypes``/``user32``/``shcore`` and are imported/resolved
lazily inside guarded functions, so this module is import-safe on any platform with
no display attached. Every enumerator degrades to an empty list (or a documented
fallback) off-Windows or on any failure — it never raises.

Coordinates are PHYSICAL pixels in the Windows *virtual desktop* space: the primary
monitor's top-left is ``(0, 0)`` and monitors positioned to the left of / above the
primary carry NEGATIVE ``x`` / ``y``. This matches ffmpeg ``gdigrab`` desktop offsets,
which are primary-relative and may be negative.
"""

from __future__ import annotations

import ctypes
import logging
import os

log = logging.getLogger("ai_record.screens")

_IS_WINDOWS = os.name == "nt"

# Guard so DPI awareness is only ever set once per process (idempotent + call-safe).
_dpi_aware_done = False

# Virtual-desktop fallback used when the OS query is unavailable.
_VIRTUAL_FALLBACK = {"x": 0, "y": 0, "w": 1920, "h": 1080}


# --------------------------------------------------------------------------- #
# DPI awareness
# --------------------------------------------------------------------------- #
def set_dpi_aware() -> None:
    """Make the process PER-MONITOR-V2 DPI aware so enumeration reports physical px.

    Tries ``user32.SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`` first, then
    falls back to ``shcore.SetProcessDpiAwareness(2)`` and finally the legacy
    ``user32.SetProcessDPIAware()``. Call-once safe (a module flag makes repeat calls
    a no-op) and fully guarded — a no-op off-Windows or on any failure.
    """
    global _dpi_aware_done
    if _dpi_aware_done:
        return
    _dpi_aware_done = True
    if not _IS_WINDOWS:
        return
    try:
        user32 = ctypes.windll.user32
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == handle value (-4).
        try:
            ctx = ctypes.c_void_p(-4)
            if user32.SetProcessDpiAwarenessContext(ctx):
                return
        except Exception:
            pass
        try:  # PROCESS_PER_MONITOR_DPI_AWARE == 2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("set_dpi_aware failed: %s", exc)


# --------------------------------------------------------------------------- #
# ctypes structures / callback types (Windows only; defined lazily-safe)
# --------------------------------------------------------------------------- #
class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
        ("szDevice", ctypes.c_wchar * 32),
    ]


# NOTE: ``ctypes.WINFUNCTYPE`` only EXISTS on Windows — referencing it at module
# scope makes ``import ai_record.screens`` raise on Linux/macOS. Build the callback
# prototypes lazily inside the (Windows-only, guarded) enumerators instead.
def _monitor_enum_proc_type():
    return ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_RECT), ctypes.c_void_p
    )


def _wnd_enum_proc_type():
    return ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


_MONITORINFOF_PRIMARY = 0x1


# --------------------------------------------------------------------------- #
# Monitors
# --------------------------------------------------------------------------- #
def list_monitors() -> list[dict]:
    """Enumerate physical monitors in virtual-desktop coordinates.

    Shape: ``[{"id": str, "name": str, "x": int, "y": int, "w": int, "h": int,
    "dpi": int}, ...]``. Primary monitor at ``(0, 0)``; monitors left/above the
    primary have NEGATIVE ``x`` / ``y``. Guarded → ``[]`` off-Windows / on failure.
    """
    if not _IS_WINDOWS:
        return []
    try:
        set_dpi_aware()
        return _enum_monitors()
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("list_monitors failed: %s", exc)
        return []


def _monitor_dpi(hmon: int) -> int:
    try:
        shcore = ctypes.windll.shcore
        shcore.GetDpiForMonitor.argtypes = [
            ctypes.c_void_p, ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
        ]
        dpix = ctypes.c_uint()
        dpiy = ctypes.c_uint()
        # MDT_EFFECTIVE_DPI == 0
        if shcore.GetDpiForMonitor(ctypes.c_void_p(hmon), 0, ctypes.byref(dpix), ctypes.byref(dpiy)) == 0:
            return int(dpix.value) or 96
    except Exception:
        pass
    return 96


def _enum_monitors() -> list[dict]:
    user32 = ctypes.windll.user32
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetMonitorInfoW.restype = ctypes.c_int
    user32.EnumDisplayMonitors.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, _monitor_enum_proc_type(), ctypes.c_void_p
    ]
    user32.EnumDisplayMonitors.restype = ctypes.c_int

    monitors: list[dict] = []

    def _cb(hmon, hdc, lprc, lparam):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return 1
        r = info.rcMonitor
        name = str(info.szDevice)
        monitors.append({
            "id": name,
            "name": name,
            "x": int(r.left),
            "y": int(r.top),
            "w": int(r.right - r.left),
            "h": int(r.bottom - r.top),
            "dpi": _monitor_dpi(int(hmon) if hmon else 0),
            "_primary": bool(info.dwFlags & _MONITORINFOF_PRIMARY),
        })
        return 1

    cb = _monitor_enum_proc_type()(_cb)
    user32.EnumDisplayMonitors(None, None, cb, None)
    # Sort so the primary monitor comes first (stable, deterministic order).
    monitors.sort(key=lambda m: (not m["_primary"], m["y"], m["x"]))
    for m in monitors:
        m.pop("_primary", None)
    return monitors


# --------------------------------------------------------------------------- #
# Windows (top-level visible titled windows)
# --------------------------------------------------------------------------- #
def list_windows() -> list[dict]:
    """Enumerate visible top-level windows that have a title.

    Shape: ``[{"id": str(hwnd), "name": str, "x": int, "y": int, "w": int,
    "h": int}, ...]``. Guarded → ``[]`` off-Windows / on failure.
    """
    if not _IS_WINDOWS:
        return []
    try:
        set_dpi_aware()
        return _enum_windows()
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("list_windows failed: %s", exc)
        return []


def _is_cloaked(hwnd) -> bool:
    """True if a window is DWM-CLOAKED (hidden virtual-desktop / suspended UWP app).

    Cloaked windows are visible to ``EnumWindows`` but render black / fail to capture,
    so the picker must skip them. Fully guarded — any failure (no dwmapi, bad call)
    is treated as "not cloaked".
    """
    try:
        dwm = ctypes.windll.dwmapi
        DWMWA_CLOAKED = 14
        val = ctypes.c_int(0)
        res = dwm.DwmGetWindowAttribute(
            ctypes.c_void_p(int(hwnd)), DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val)
        )
        return res == 0 and val.value != 0
    except Exception:
        return False


def _window_is_capturable(user32, hwnd) -> bool:
    """A visible, titled window is capturable only if not MINIMIZED and not CLOAKED.

    ``IsIconic`` (minimized) and DWM-cloaked windows produce black/failing captures,
    so the picker must not offer them.
    """
    if user32.IsIconic(hwnd):
        return False
    if _is_cloaked(hwnd):
        return False
    return True


def _enum_windows() -> list[dict]:
    user32 = ctypes.windll.user32
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = ctypes.c_int
    user32.IsIconic.argtypes = [ctypes.c_void_p]
    user32.IsIconic.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
    user32.GetWindowRect.restype = ctypes.c_int
    user32.EnumWindows.argtypes = [_wnd_enum_proc_type(), ctypes.c_void_p]
    user32.EnumWindows.restype = ctypes.c_int

    windows: list[dict] = []

    def _cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return 1
        # Skip minimized (IsIconic) and DWM-cloaked windows — they capture as black.
        if not _window_is_capturable(user32, hwnd):
            return 1
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return 1
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        if not title:
            return 1
        r = _RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return 1
        windows.append({
            "id": str(int(hwnd)),
            "name": title,
            "x": int(r.left),
            "y": int(r.top),
            "w": int(r.right - r.left),
            "h": int(r.bottom - r.top),
        })
        return 1

    cb = _wnd_enum_proc_type()(_cb)
    user32.EnumWindows(cb, None)
    return windows


# --------------------------------------------------------------------------- #
# Virtual desktop bounds
# --------------------------------------------------------------------------- #
def virtual_screen_bounds() -> dict:
    """Return the whole virtual-desktop rectangle ``{"x","y","w","h"}``.

    Uses ``GetSystemMetrics(SM_*VIRTUALSCREEN)``. Guarded fallback
    ``{"x":0,"y":0,"w":1920,"h":1080}`` off-Windows / on failure.
    """
    if not _IS_WINDOWS:
        return dict(_VIRTUAL_FALLBACK)
    try:
        set_dpi_aware()
        user32 = ctypes.windll.user32
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
        x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if w <= 0 or h <= 0:
            return dict(_VIRTUAL_FALLBACK)
        return {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("virtual_screen_bounds failed: %s", exc)
        return dict(_VIRTUAL_FALLBACK)


# --------------------------------------------------------------------------- #
# Pure geometry helpers (no OS calls when ``bounds`` is supplied)
# --------------------------------------------------------------------------- #
def _even_down(v: int) -> int:
    """Force ``v`` even by shrinking by 1 when odd, floored at 2."""
    v = int(v)
    if v % 2:
        v -= 1
    return max(2, v)


def sanitize_region(region: dict, bounds: dict | None = None) -> dict:
    """Clamp a region to the virtual desktop; force even w/h; enforce a 2x2 minimum.

    PURE when ``bounds`` is supplied (``{"x","y","w","h"}``); otherwise it queries
    :func:`virtual_screen_bounds`. Width/height are forced EVEN (shrink by 1 if odd)
    so the frame is valid for ``yuv420p`` encoding.
    """
    if bounds is None:
        bounds = virtual_screen_bounds()
    bx, by = int(bounds["x"]), int(bounds["y"])
    bw, bh = int(bounds["w"]), int(bounds["h"])
    max_x, max_y = bx + bw, by + bh

    x = int(region.get("x", 0))
    y = int(region.get("y", 0))
    w = int(region.get("w", 0))
    h = int(region.get("h", 0))

    # Keep the origin inside the desktop with at least 2px of room to the edge.
    x = max(bx, min(x, max_x - 2))
    y = max(by, min(y, max_y - 2))
    # Clamp the extent so the region stays fully within the desktop.
    w = max(2, min(w, max_x - x))
    h = max(2, min(h, max_y - y))
    return {"x": x, "y": y, "w": _even_down(w), "h": _even_down(h)}


def region_to_ffmpeg(region: dict) -> dict:
    """Convert a region to ffmpeg ``gdigrab`` desktop params (PURE, no OS calls).

    Returns ``{"offset_x": int, "offset_y": int, "w": int, "h": int}``. Offsets are
    PRIMARY-monitor-relative and passed straight through (they MAY be negative for
    monitors left of / above the primary). Width/height are forced EVEN for
    ``yuv420p``.
    """
    return {
        "offset_x": int(region.get("x", 0)),
        "offset_y": int(region.get("y", 0)),
        "w": _even_down(int(region.get("w", 0))),
        "h": _even_down(int(region.get("h", 0))),
    }
