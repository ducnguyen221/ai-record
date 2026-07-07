"""Hardware-free tests for ai_record.region_picker.

No real Tk window is ever created and no real child process is spawned: the picker now
runs its Tk overlay in a SEPARATE process (so a Tcl crash can't take down the app), and
these tests monkeypatch ``subprocess.Popen`` with a fake that writes the region file the
child would produce. Covers the geometry-string builder, the re-entrancy lock, the
subprocess argv, the returned-region path, and the bounded-wait timeout path.
"""

from __future__ import annotations

import json
import subprocess
import sys

from ai_record import region_picker as rp


# --------------------------------------------------------------------------- #
# _geometry_string — signed offsets, incl. negative (monitor left of / above primary)
# --------------------------------------------------------------------------- #
def test_geometry_string_positive_offsets():
    assert rp._geometry_string({"x": 0, "y": 0, "w": 3840, "h": 1080}) == "3840x1080+0+0"


def test_geometry_string_negative_offsets_well_formed():
    g = rp._geometry_string({"x": -1920, "y": -200, "w": 1920, "h": 1080})
    assert g == "1920x1080+-1920+-200"


# --------------------------------------------------------------------------- #
# Re-entrancy: a second concurrent pick_region() is rejected (no 2nd overlay)
# --------------------------------------------------------------------------- #
def test_pick_region_rejects_concurrent_calls():
    assert rp._PICK_LOCK.acquire(blocking=False)
    try:
        assert rp.pick_region() is None  # lock already held → immediate cancel
    finally:
        rp._PICK_LOCK.release()


class _FakeProc:
    def __init__(self, out_path, region, *, timeout=False):
        self._out = out_path
        self._region = region
        self._timeout = timeout
        self.killed = False

    def wait(self, timeout=None):
        if self._timeout:
            raise subprocess.TimeoutExpired(cmd="picker", timeout=timeout)
        # Simulate the child writing its result file before it exits.
        with open(self._out, "w", encoding="utf-8") as fh:
            json.dump({"region": self._region}, fh)

    def kill(self):
        self.killed = True


def test_pick_region_spawns_isolated_subprocess_and_returns_region(monkeypatch):
    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        # the --out path is the last arg
        out = argv[argv.index("--out") + 1]
        return _FakeProc(out, {"x": 10, "y": 20, "w": 640, "h": 480})

    monkeypatch.setattr(rp.subprocess, "Popen", fake_popen)
    got = rp.pick_region()
    assert got == {"x": 10, "y": 20, "w": 640, "h": 480}
    # Runs a SEPARATE python process (crash isolation), not tkinter in-thread.
    argv = captured["argv"]
    assert argv[0] == sys.executable
    assert argv[1:3] == ["-m", "ai_record.region_picker"]
    assert "--out" in argv


def test_pick_region_empty_selection_returns_none(monkeypatch):
    def fake_popen(argv, **kw):
        out = argv[argv.index("--out") + 1]
        return _FakeProc(out, None)  # user pressed Esc / tiny drag → null region

    monkeypatch.setattr(rp.subprocess, "Popen", fake_popen)
    assert rp.pick_region() is None


def test_pick_region_times_out_and_kills_child(monkeypatch):
    procs = []

    def fake_popen(argv, **kw):
        out = argv[argv.index("--out") + 1]
        p = _FakeProc(out, None, timeout=True)
        procs.append(p)
        return p

    monkeypatch.setattr(rp.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(rp, "_PICK_TIMEOUT_S", 0.01)
    assert rp.pick_region() is None
    assert procs and procs[0].killed  # timed-out child was killed
    # Lock released even on the timeout path.
    assert rp._PICK_LOCK.acquire(blocking=False)
    rp._PICK_LOCK.release()


def test_parent_never_imports_tkinter():
    # The whole point of the subprocess: the APP process must not touch tkinter/Tcl.
    import inspect
    src = inspect.getsource(rp.pick_region)
    assert "tkinter" not in src and "import tk" not in src
