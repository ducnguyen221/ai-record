"""Hardware-free tests for ai_record.region_picker.

No real Tk window is ever created: the tkinter lifecycle (``_run_picker``) is
monkeypatched with light fakes. Covers the geometry-string builder (signed offsets),
the re-entrancy lock, and the bounded-wait timeout path.
"""

from __future__ import annotations

import time

from ai_record import region_picker as rp


# --------------------------------------------------------------------------- #
# _geometry_string — signed offsets, incl. negative (monitor left of / above primary)
# --------------------------------------------------------------------------- #
def test_geometry_string_positive_offsets():
    g = rp._geometry_string({"x": 0, "y": 0, "w": 3840, "h": 1080})
    assert g == "3840x1080+0+0"


def test_geometry_string_negative_offsets_well_formed():
    # Tk accepts `+-1920` for a negative absolute X; the +{x} form stays well-formed.
    g = rp._geometry_string({"x": -1920, "y": -200, "w": 1920, "h": 1080})
    assert g == "1920x1080+-1920+-200"


# --------------------------------------------------------------------------- #
# Re-entrancy: a second concurrent pick_region() is rejected (no 2nd Tk root)
# --------------------------------------------------------------------------- #
def test_pick_region_rejects_concurrent_calls():
    assert rp._PICK_LOCK.acquire(blocking=False)
    try:
        assert rp.pick_region() is None  # lock already held → immediate cancel
    finally:
        rp._PICK_LOCK.release()


# --------------------------------------------------------------------------- #
# Bounded wait: a picker that never resolves times out → None + root destroyed
# --------------------------------------------------------------------------- #
def test_pick_region_times_out_and_destroys_root(monkeypatch):
    destroyed = {"n": 0}

    class FakeRoot:
        def destroy(self):
            destroyed["n"] += 1

    def fake_run_picker(result):
        result["root"] = FakeRoot()  # stash so the timeout path can tear it down
        time.sleep(1.0)  # never sets result["region"] within the timeout

    monkeypatch.setattr(rp, "_run_picker", fake_run_picker)
    monkeypatch.setattr(rp, "_PICK_TIMEOUT_S", 0.05)

    out = rp.pick_region()
    assert out is None
    assert destroyed["n"] == 1  # stashed root was destroyed on timeout
    # Lock is released even on the timeout path (a later pick can proceed).
    assert rp._PICK_LOCK.acquire(blocking=False)
    rp._PICK_LOCK.release()


def test_pick_region_returns_selected_region(monkeypatch):
    def fake_run_picker(result):
        result["region"] = {"x": 10, "y": 20, "w": 640, "h": 480}

    monkeypatch.setattr(rp, "_run_picker", fake_run_picker)
    assert rp.pick_region() == {"x": 10, "y": 20, "w": 640, "h": 480}
