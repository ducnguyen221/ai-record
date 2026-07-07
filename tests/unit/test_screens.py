"""Hardware-free tests for ai_record.screens.

Covers the PURE geometry helpers (``sanitize_region`` / ``region_to_ffmpeg``) with
synthetic layouts including NEGATIVE-coordinate monitors and ODD dimensions, and the
guarded shape of the ctypes enumerators (no user32 / no display).
"""

from __future__ import annotations

import inspect

import ai_record.screens as screens


def test_enum_functions_have_no_dangling_winfunctype_names():
    # Regression: moving WINFUNCTYPE prototypes into lazy factories left `_enum_monitors`
    # referencing a deleted `_MONITORENUMPROC` name → NameError → list_monitors() silently
    # returned [] (no displays in the picker). Guard every enum against dangling refs.
    for fn in (screens._enum_monitors, screens._enum_windows):
        src = inspect.getsource(fn)
        assert "_MONITORENUMPROC" not in src, f"{fn.__name__} refs deleted _MONITORENUMPROC"
        assert "_WNDENUMPROC" not in src, f"{fn.__name__} refs deleted _WNDENUMPROC"
        # the lazy factory it DOES use must exist + be callable
    assert callable(screens._monitor_enum_proc_type)
    assert callable(screens._wnd_enum_proc_type)


# --------------------------------------------------------------------------- #
# sanitize_region (PURE with explicit bounds)
# --------------------------------------------------------------------------- #
def test_sanitize_region_forces_even_dims():
    b = {"x": 0, "y": 0, "w": 1920, "h": 1080}
    out = screens.sanitize_region({"x": 10, "y": 20, "w": 101, "h": 201}, bounds=b)
    # odd → even by shrinking 1
    assert out == {"x": 10, "y": 20, "w": 100, "h": 200}


def test_sanitize_region_clamps_into_bounds():
    b = {"x": 0, "y": 0, "w": 1920, "h": 1080}
    # origin + size overflow the desktop → width/height clamped so it stays inside.
    out = screens.sanitize_region({"x": 1900, "y": 1000, "w": 800, "h": 800}, bounds=b)
    assert out["x"] == 1900 and out["y"] == 1000
    assert out["x"] + out["w"] <= b["x"] + b["w"]
    assert out["y"] + out["h"] <= b["y"] + b["h"]
    assert out["w"] % 2 == 0 and out["h"] % 2 == 0


def test_sanitize_region_negative_bounds_in_range():
    # A monitor to the LEFT / ABOVE the primary → negative origin is valid & preserved.
    b = {"x": -1920, "y": -200, "w": 1920, "h": 1080}
    out = screens.sanitize_region({"x": -1900, "y": -100, "w": 641, "h": 481}, bounds=b)
    assert out["x"] == -1900 and out["y"] == -100
    assert out["w"] == 640 and out["h"] == 480  # odd → even
    assert out["x"] + out["w"] <= b["x"] + b["w"]


def test_sanitize_region_out_of_bounds_origin_clamped():
    b = {"x": 0, "y": 0, "w": 1000, "h": 1000}
    out = screens.sanitize_region({"x": -50, "y": 5000, "w": 200, "h": 200}, bounds=b)
    # x clamped up to bounds.x; y clamped down to leave >=2px room
    assert out["x"] == 0
    assert out["y"] <= b["h"] - 2
    assert out["w"] >= 2 and out["h"] >= 2


def test_sanitize_region_enforces_min_2x2():
    b = {"x": 0, "y": 0, "w": 1920, "h": 1080}
    out = screens.sanitize_region({"x": 5, "y": 5, "w": 1, "h": 0}, bounds=b)
    assert out["w"] == 2 and out["h"] == 2


# --------------------------------------------------------------------------- #
# region_to_ffmpeg (PURE passthrough incl. negatives)
# --------------------------------------------------------------------------- #
def test_region_to_ffmpeg_passthrough_positive():
    out = screens.region_to_ffmpeg({"x": 100, "y": 200, "w": 640, "h": 480})
    assert out == {"offset_x": 100, "offset_y": 200, "w": 640, "h": 480}


def test_region_to_ffmpeg_passthrough_negative_offsets():
    # Offsets for a monitor left of / above the primary pass straight through negative.
    out = screens.region_to_ffmpeg({"x": -1920, "y": -300, "w": 800, "h": 600})
    assert out["offset_x"] == -1920 and out["offset_y"] == -300


def test_region_to_ffmpeg_forces_even_dims():
    out = screens.region_to_ffmpeg({"x": -5, "y": -5, "w": 101, "h": 51})
    assert out["offset_x"] == -5 and out["offset_y"] == -5
    assert out["w"] == 100 and out["h"] == 50


# --------------------------------------------------------------------------- #
# Enumerators — guarded shape when the user32/ctypes layer is absent
# --------------------------------------------------------------------------- #
def test_list_monitors_guarded_empty_off_windows(monkeypatch):
    monkeypatch.setattr(screens, "_IS_WINDOWS", False)
    assert screens.list_monitors() == []


def test_list_windows_guarded_empty_off_windows(monkeypatch):
    monkeypatch.setattr(screens, "_IS_WINDOWS", False)
    assert screens.list_windows() == []


def test_virtual_screen_bounds_fallback_off_windows(monkeypatch):
    monkeypatch.setattr(screens, "_IS_WINDOWS", False)
    assert screens.virtual_screen_bounds() == {"x": 0, "y": 0, "w": 1920, "h": 1080}


def test_list_monitors_passthrough_shape(monkeypatch):
    # Force the Windows branch but stub the raw enumerator with a synthetic multi-mon
    # layout (primary at 0,0 + a monitor to the left with negative x).
    synthetic = [
        {"id": r"\\.\DISPLAY1", "name": r"\\.\DISPLAY1", "x": 0, "y": 0,
         "w": 1920, "h": 1080, "dpi": 96},
        {"id": r"\\.\DISPLAY2", "name": r"\\.\DISPLAY2", "x": -1920, "y": 0,
         "w": 1920, "h": 1080, "dpi": 144},
    ]
    monkeypatch.setattr(screens, "_IS_WINDOWS", True)
    monkeypatch.setattr(screens, "_enum_monitors", lambda: synthetic)
    out = screens.list_monitors()
    assert out == synthetic
    for m in out:
        assert set(m) == {"id", "name", "x", "y", "w", "h", "dpi"}


def test_list_monitors_guarded_when_enum_raises(monkeypatch):
    def boom():
        raise OSError("no user32")

    monkeypatch.setattr(screens, "_IS_WINDOWS", True)
    monkeypatch.setattr(screens, "_enum_monitors", boom)
    assert screens.list_monitors() == []


def test_list_windows_guarded_when_enum_raises(monkeypatch):
    def boom():
        raise OSError("no user32")

    monkeypatch.setattr(screens, "_IS_WINDOWS", True)
    monkeypatch.setattr(screens, "_enum_windows", boom)
    assert screens.list_windows() == []


def test_set_dpi_aware_is_call_once_safe(monkeypatch):
    # Off-Windows it is a guarded no-op; calling twice must not raise.
    monkeypatch.setattr(screens, "_IS_WINDOWS", False)
    monkeypatch.setattr(screens, "_dpi_aware_done", False)
    screens.set_dpi_aware()
    screens.set_dpi_aware()


# --------------------------------------------------------------------------- #
# Import safety off-Windows: no ctypes.WINFUNCTYPE at module scope
# --------------------------------------------------------------------------- #
def test_no_module_level_winfunctype_prototypes():
    # ctypes.WINFUNCTYPE only exists on Windows; building the callback prototypes at
    # import/module scope would make `import ai_record.screens` raise off-Windows. They
    # must be constructed lazily inside the (guarded) enumerators instead.
    assert not hasattr(screens, "_MONITORENUMPROC")
    assert not hasattr(screens, "_WNDENUMPROC")
    # The lazy builders exist and produce callable prototypes on this platform.
    assert callable(screens._monitor_enum_proc_type)
    assert callable(screens._wnd_enum_proc_type)


def test_screens_import_safe_without_winfunctype(monkeypatch):
    # Simulate a non-Windows ctypes (no WINFUNCTYPE) and re-import the module: it must
    # import cleanly because nothing references WINFUNCTYPE at module scope.
    import ctypes
    import importlib

    monkeypatch.delattr(ctypes, "WINFUNCTYPE", raising=False)
    mod = importlib.reload(screens)
    assert mod is not None
    # Restore a clean module for the rest of the suite.
    monkeypatch.undo()
    importlib.reload(screens)


# --------------------------------------------------------------------------- #
# list_windows filters minimized (IsIconic) and DWM-cloaked windows
# --------------------------------------------------------------------------- #
class _FakeUser32:
    def __init__(self, iconic):
        self._iconic = iconic

    def IsIconic(self, hwnd):
        return 1 if self._iconic else 0


def test_window_is_capturable_skips_minimized(monkeypatch):
    monkeypatch.setattr(screens, "_is_cloaked", lambda hwnd: False)
    assert screens._window_is_capturable(_FakeUser32(iconic=True), 123) is False
    assert screens._window_is_capturable(_FakeUser32(iconic=False), 123) is True


def test_window_is_capturable_skips_cloaked(monkeypatch):
    # Not minimized, but DWM-cloaked → still skipped (captures as black).
    monkeypatch.setattr(screens, "_is_cloaked", lambda hwnd: True)
    assert screens._window_is_capturable(_FakeUser32(iconic=False), 123) is False


def test_is_cloaked_guarded_off_windows():
    # No dwmapi / bad call → treated as "not cloaked", never raises.
    assert screens._is_cloaked(0) is False
