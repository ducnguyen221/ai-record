"""Hardware-free tests for ai_record.capture_video.

Argv builders are asserted flag-by-flag; the recorder/manager lifecycle runs entirely
against an in-process ``FakeProcess`` via the ``spawn=`` seam. NEVER spawns real
ffmpeg and never opens a display.
"""

from __future__ import annotations

import logging
import os
import types

import pytest

import ai_record.capture_video as cv
import ai_record.screens as screens
from ai_record.config import Settings


def _settings(**over):
    # Use the REAL config.Settings field names (video_screen_fps / video_camera_fps /
    # video_capture_cursor) so the getattr() lookups in the builders resolve.
    base = dict(
        video_screen_fps=30,
        video_camera_fps=25,
        video_capture_cursor=True,
        video_encoder="auto",
        video_container="mkv",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# Fake process + spawner (the injected `spawn` seam)
# --------------------------------------------------------------------------- #
class _FakeStdin:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    def flush(self):
        pass


class FakeProcess:
    """Minimal Popen stand-in. ``exit_codes`` is a queue returned by poll()."""

    def __init__(self, argv, *, exit_codes=None, **kwargs):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.pid = 4321
        self.stdin = _FakeStdin()
        self.terminated = False
        self.killed = False
        self._returncode = None
        self._polls = list(exit_codes) if exit_codes else []

    def poll(self):
        if self._returncode is not None:
            return self._returncode
        if self._polls:
            v = self._polls.pop(0)
            if v is not None:
                self._returncode = v
            return v
        return None

    def wait(self, timeout=None):
        # 'q' on stdin (or terminate/kill) makes ffmpeg exit; simulate a clean exit.
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def terminate(self):
        self.terminated = True
        if self._returncode is None:
            self._returncode = -15

    def kill(self):
        self.killed = True
        self._returncode = -9


class Spawner:
    """Callable spawn seam: records argv, writes a stub file, hands back a FakeProcess.

    ``scripts`` is a per-spawn list of exit-code queues (indexed by spawn order).
    ``raise_on`` — a substring; if the out path (argv[-1]) contains it, raise.
    """

    def __init__(self, scripts=None, raise_on=None):
        self.calls = []
        self.procs = []
        self.scripts = scripts or []
        self.raise_on = raise_on

    def __call__(self, argv, **kwargs):
        out = argv[-1]
        if self.raise_on and self.raise_on in str(out):
            raise RuntimeError("simulated spawn failure")
        with open(out, "wb") as f:  # non-empty so status()['bytes'] > 0
            f.write(b"\x00" * 32)
        idx = len(self.procs)
        codes = self.scripts[idx] if idx < len(self.scripts) else None
        p = FakeProcess(argv, exit_codes=codes, **kwargs)
        self.procs.append(p)
        self.calls.append(list(argv))
        return p


# --------------------------------------------------------------------------- #
# build_screen_args
# --------------------------------------------------------------------------- #
def test_build_screen_args_full_nvenc(tmp_path):
    out = str(tmp_path / "screen.mkv")
    argv = cv.build_screen_args({"mode": "full"}, out, _settings())
    assert "ffmpeg" in argv[0].lower()
    assert "-f" in argv and argv[argv.index("-f") + 1] == "gdigrab"
    assert argv[argv.index("-framerate") + 1] == "30"
    assert argv[argv.index("-draw_mouse") + 1] == "1"
    assert argv[argv.index("-i") + 1] == "desktop"
    assert "h264_nvenc" in argv  # auto → nvenc primary
    assert argv[argv.index("-pix_fmt") + 1] == "yuv420p"
    assert "-an" in argv
    assert argv[-1] == out
    # no offsets/size in full-desktop mode
    assert "-offset_x" not in argv and "-video_size" not in argv


def test_build_screen_args_draw_mouse_off():
    argv = cv.build_screen_args({"mode": "full"}, "o.mkv", _settings(video_capture_cursor=False))
    assert argv[argv.index("-draw_mouse") + 1] == "0"


def test_build_screen_args_honors_real_settings_fields():
    # Integration: a REAL config.Settings (not a SimpleNamespace) must flow fps + cursor
    # through the argv builder — regression for the getattr key mismatch (video_fps vs
    # video_screen_fps, camera_fps vs video_camera_fps, video_draw_mouse vs
    # video_capture_cursor).
    s = Settings(video_screen_fps=48, video_camera_fps=12, video_capture_cursor=False)
    argv = cv.build_screen_args({"mode": "full"}, "o.mkv", s)
    assert argv[argv.index("-framerate") + 1] == "48"
    assert argv[argv.index("-draw_mouse") + 1] == "0"
    cam = cv.build_camera_args("Cam", "c.mkv", s)
    assert cam[cam.index("-framerate") + 1] == "12"


def test_build_screen_args_display_negative_offset_even_size(monkeypatch):
    # A monitor to the LEFT of primary with ODD dims → negative offset + evened size.
    monitors = [
        {"id": "D2", "name": "D2", "x": -1921, "y": -3, "w": 1367, "h": 769, "dpi": 96},
    ]
    monkeypatch.setattr(screens, "list_monitors", lambda: monitors)
    argv = cv.build_screen_args({"mode": "display", "display_id": "D2"}, "o.mkv", _settings())
    assert argv[argv.index("-offset_x") + 1] == "-1921"
    assert argv[argv.index("-offset_y") + 1] == "-3"
    # 1367x769 (odd) → 1366x768 (even)
    assert argv[argv.index("-video_size") + 1] == "1366x768"
    assert argv[argv.index("-i") + 1] == "desktop"


def test_build_screen_args_region_passthrough(tmp_path, monkeypatch):
    # Region is re-clamped at record start against the virtual-desktop bounds, so give
    # bounds wide enough (incl. negative x) that this in-range region passes through.
    monkeypatch.setattr(
        screens, "virtual_screen_bounds", lambda: {"x": -1920, "y": 0, "w": 3840, "h": 1080}
    )
    argv = cv.build_screen_args(
        {"mode": "region", "region": {"x": -10, "y": 40, "w": 641, "h": 481}},
        "o.mkv", _settings(),
    )
    assert argv[argv.index("-offset_x") + 1] == "-10"
    assert argv[argv.index("-offset_y") + 1] == "40"
    assert argv[argv.index("-video_size") + 1] == "640x480"  # odd → even
    assert argv[argv.index("-i") + 1] == "desktop"


def test_build_screen_args_region_reclamped_at_start(monkeypatch):
    # A stale region (picked before a display change) that now overflows the desktop is
    # re-clamped at start so gdigrab never gets out-of-bounds/odd geometry.
    monkeypatch.setattr(
        screens, "virtual_screen_bounds", lambda: {"x": 0, "y": 0, "w": 1920, "h": 1080}
    )
    argv = cv.build_screen_args(
        {"mode": "region", "region": {"x": 1900, "y": 1000, "w": 800, "h": 800}},
        "o.mkv", _settings(),
    )
    ox = int(argv[argv.index("-offset_x") + 1])
    oy = int(argv[argv.index("-offset_y") + 1])
    w, h = (int(v) for v in argv[argv.index("-video_size") + 1].split("x"))
    # Clamped fully inside the desktop with even dims.
    assert ox + w <= 1920 and oy + h <= 1080
    assert w % 2 == 0 and h % 2 == 0


def test_build_screen_args_window_title():
    argv = cv.build_screen_args(
        {"mode": "window", "window_title": "My Meeting"}, "o.mkv", _settings()
    )
    assert argv[argv.index("-i") + 1] == "title=My Meeting"


def test_build_screen_args_window_forces_even_dims():
    # window mode has no -video_size, so an even-forcing -vf must guard yuv420p.
    argv = cv.build_screen_args(
        {"mode": "window", "window_title": "W"}, "o.mkv", _settings()
    )
    assert argv[argv.index("-vf") + 1] == "scale=trunc(iw/2)*2:trunc(ih/2)*2"


def test_build_screen_args_full_forces_even_dims():
    argv = cv.build_screen_args({"mode": "full"}, "o.mkv", _settings())
    assert argv[argv.index("-vf") + 1] == "scale=trunc(iw/2)*2:trunc(ih/2)*2"


def test_build_screen_args_libx264_when_forced():
    argv = cv.build_screen_args({"mode": "full"}, "o.mkv", _settings(video_encoder="libx264"))
    assert "libx264" in argv
    assert "h264_nvenc" not in argv
    assert argv[argv.index("-crf") + 1] == "23"


def test_build_screen_args_mp4_movflags():
    argv = cv.build_screen_args({"mode": "full"}, "o.mp4", _settings(video_container="mp4"))
    assert argv[argv.index("-movflags") + 1] == "+frag_keyframe+empty_moov+default_base_moof"


def test_build_screen_args_mkv_no_movflags():
    argv = cv.build_screen_args({"mode": "full"}, "o.mkv", _settings(video_container="mkv"))
    assert "-movflags" not in argv


# --------------------------------------------------------------------------- #
# build_camera_args
# --------------------------------------------------------------------------- #
def test_build_camera_args_dshow(tmp_path):
    out = str(tmp_path / "camera.mkv")
    argv = cv.build_camera_args("Integrated Camera", out, _settings())
    assert argv[argv.index("-f") + 1] == "dshow"
    assert argv[argv.index("-framerate") + 1] == "25"
    assert argv[argv.index("-i") + 1] == "video=Integrated Camera"
    assert "h264_nvenc" in argv
    assert "-an" in argv
    assert argv[-1] == out


def test_build_camera_args_libx264_override():
    argv = cv.build_camera_args("Cam", "o.mkv", _settings(), encoder_override="libx264")
    assert "libx264" in argv and "h264_nvenc" not in argv


# --------------------------------------------------------------------------- #
# VideoRecorder lifecycle
# --------------------------------------------------------------------------- #
def test_recorder_start_stop_status(tmp_path):
    out = str(tmp_path / "screen.mkv")
    argv = cv.build_screen_args({"mode": "full"}, out, _settings(video_encoder="libx264"))
    sp = Spawner()
    rec = cv.VideoRecorder(argv, out, spawn=sp, startup_grace_s=0.0)
    rec.start()
    st = rec.status()
    assert st["recording"] is True
    assert st["file"] == out
    assert st["bytes"] == 32
    assert st["error"] is None
    # stop writes 'q' to stdin and finalizes.
    final = rec.stop()
    assert final["recording"] is False
    assert sp.procs[0].stdin.data == b"q\n"
    assert rec.poll() == 0


def test_recorder_nvenc_falls_back_to_libx264(tmp_path):
    out = str(tmp_path / "screen.mkv")
    s = _settings(video_encoder="auto")
    argv = cv.build_screen_args({"mode": "full"}, out, s)
    fallback = cv.build_screen_args({"mode": "full"}, out, s, encoder_override="libx264")
    # First (nvenc) process exits nonzero immediately → fallback spawns; 2nd stays up.
    sp = Spawner(scripts=[[1], None])
    rec = cv.VideoRecorder(argv, out, spawn=sp, encoder_fallback_argv=fallback, startup_grace_s=0.5)
    rec.start()
    assert len(sp.procs) == 2
    assert "h264_nvenc" in sp.procs[0].argv
    assert "libx264" in sp.procs[1].argv
    assert rec.error is not None and "fallback" in rec.error
    assert rec.status()["recording"] is True


def test_recorder_fallback_also_dies_reports_real_failure(tmp_path):
    out = str(tmp_path / "screen.mkv")
    s = _settings(video_encoder="auto")
    argv = cv.build_screen_args({"mode": "full"}, out, s)
    fallback = cv.build_screen_args({"mode": "full"}, out, s, encoder_override="libx264")
    # BOTH the nvenc primary and the libx264 fallback exit nonzero at startup.
    sp = Spawner(scripts=[[1], [7]])
    rec = cv.VideoRecorder(argv, out, spawn=sp, encoder_fallback_argv=fallback, startup_grace_s=0.5)
    rec.start()
    assert len(sp.procs) == 2
    # Error reflects the FALLBACK death, not the stale "retrying with fallback" message.
    assert rec.error is not None
    assert "fallback encoder also exited 7" in rec.error
    assert rec.status()["recording"] is False


def test_job_object_creation_failure_is_logged(tmp_path, caplog, monkeypatch):
    # When the kill-on-close Job Object can't be created, the degraded "no orphan on
    # crash" guarantee must be visible via a WARNING (not silently swallowed).
    monkeypatch.setattr(cv, "_create_kill_on_close_job", lambda: None)
    monkeypatch.setattr(cv.os, "name", "nt")  # force the Windows warning branch
    out = str(tmp_path / "screen.mkv")
    argv = cv.build_screen_args({"mode": "full"}, out, _settings(video_encoder="libx264"))
    rec = cv.VideoRecorder(argv, out, spawn=Spawner(), startup_grace_s=0.0)
    with caplog.at_level(logging.WARNING, logger="ai_record.capture_video"):
        rec.start()
    assert any(
        "Job Object" in r.message or "orphan" in r.message for r in caplog.records
    )


def test_recorder_no_fallback_when_primary_healthy(tmp_path):
    out = str(tmp_path / "screen.mkv")
    s = _settings(video_encoder="auto")
    argv = cv.build_screen_args({"mode": "full"}, out, s)
    fallback = cv.build_screen_args({"mode": "full"}, out, s, encoder_override="libx264")
    sp = Spawner(scripts=[None])  # primary never exits during startup grace
    rec = cv.VideoRecorder(argv, out, spawn=sp, encoder_fallback_argv=fallback, startup_grace_s=0.0)
    rec.start()
    assert len(sp.procs) == 1
    assert rec.error is None


# --------------------------------------------------------------------------- #
# VideoCaptureManager
# --------------------------------------------------------------------------- #
def test_manager_builds_both_recorders(tmp_path):
    sp = Spawner()
    mgr = cv.VideoCaptureManager(
        str(tmp_path),
        {"screen": {"mode": "full"}, "camera": {"device": "Cam"}},
        _settings(video_encoder="libx264"),
        spawn=sp,
    )
    res = mgr.start()
    assert res["errors"] == []
    assert res["screen"]["recording"] is True
    assert res["camera"]["recording"] is True
    # correct file names in the session dir
    files = {os.path.basename(c[-1]) for c in sp.calls}
    assert files == {"screen.mkv", "camera.mkv"}
    stop = mgr.stop()
    assert stop["screen"]["recording"] is False
    assert stop["camera"]["recording"] is False


def test_screen_recorder_gpu_chain_hevc_then_libx264(tmp_path):
    # UAT finding: h264_nvenc caps at 4096px wide, so ultrawide desktops must fall
    # back to hevc_nvenc (GPU, ≤8192) BEFORE libx264 (CPU). Auto encoder → chain of both.
    mgr = cv.VideoCaptureManager(
        str(tmp_path), {"screen": {"mode": "full"}, "camera": None},
        _settings(video_encoder="auto"), spawn=Spawner(),
    )
    rec = mgr._build_screen_recorder({"mode": "full"})
    assert rec.argv.count("h264_nvenc") == 1               # primary = h264_nvenc
    fb = rec._fallbacks
    assert len(fb) == 2
    assert "hevc_nvenc" in fb[0] and "libx264" not in fb[0]  # 1st fallback = GPU HEVC
    assert "libx264" in fb[1]                                # 2nd fallback = CPU
    # explicit libx264 => no nvenc chain
    rec2 = cv.VideoCaptureManager(
        str(tmp_path), {"screen": {"mode": "full"}, "camera": None},
        _settings(video_encoder="libx264"), spawn=Spawner(),
    )._build_screen_recorder({"mode": "full"})
    assert rec2._fallbacks == []


def test_manager_isolates_one_recorder_failure(tmp_path):
    # Screen spawn raises; camera must still start and the error is captured.
    sp = Spawner(raise_on="screen.")
    mgr = cv.VideoCaptureManager(
        str(tmp_path),
        {"screen": {"mode": "full"}, "camera": {"device": "Cam"}},
        _settings(video_encoder="libx264"),
        spawn=sp,
    )
    res = mgr.start()
    assert res["screen"] is None
    assert any(e.startswith("screen:") for e in res["errors"])
    assert res["camera"] is not None and res["camera"]["recording"] is True


def test_manager_stops_leaked_recorder_on_start_exception(tmp_path):
    # If a recorder object already spawned an ffmpeg child and THEN start() raises, the
    # manager must .stop() it before discarding so no orphan process is leaked.
    stopped = {"n": 0}

    class LeakyRecorder:
        def start(self):
            raise RuntimeError("boom after the child spawned")

        def stop(self):
            stopped["n"] += 1
            return {"recording": False}

        def status(self):
            return {"recording": False}

    mgr = cv.VideoCaptureManager(
        str(tmp_path), {"screen": {"mode": "full"}, "camera": None},
        _settings(video_encoder="libx264"), spawn=Spawner(),
    )
    mgr._build_screen_recorder = lambda req: LeakyRecorder()
    res = mgr.start()
    assert res["screen"] is None
    assert any(e.startswith("screen:") for e in res["errors"])
    assert stopped["n"] == 1  # the leaked recorder was stopped, not just dropped


def test_manager_screen_only(tmp_path):
    sp = Spawner()
    mgr = cv.VideoCaptureManager(
        str(tmp_path),
        {"screen": {"mode": "full"}, "camera": None},
        _settings(video_encoder="libx264"),
        spawn=sp,
    )
    res = mgr.start()
    assert res["camera"] is None
    assert res["screen"]["recording"] is True
    assert mgr.status()["camera"] is None


# --------------------------------------------------------------------------- #
# list_video_targets — guarded shape
# --------------------------------------------------------------------------- #
def test_list_video_targets_guarded_when_ffmpeg_missing(monkeypatch):
    monkeypatch.setattr(cv.screens, "list_monitors", lambda: [])
    monkeypatch.setattr(cv.screens, "list_windows", lambda: [])

    def probe_must_not_run(_ff):
        raise AssertionError("probe must not run when ffmpeg is missing")

    out = cv.list_video_targets(probe=probe_must_not_run, ffmpeg=None)
    assert out == {"cameras": [], "displays": [], "windows": [], "ffmpeg_available": False}


def test_list_video_targets_parses_cameras(monkeypatch):
    monkeypatch.setattr(cv.screens, "list_monitors", lambda: [])
    monkeypatch.setattr(cv.screens, "list_windows", lambda: [])
    stderr = (
        '[dshow @ 0000] "Integrated Camera" (video)\n'
        '[dshow @ 0000]   Alternative name "@device_pnp_cam0"\n'
        '[dshow @ 0000] "Integrated Camera" (video)\n'
        '[dshow @ 0000]   Alternative name "@device_pnp_cam1"\n'
        '[dshow @ 0000] "Microphone (Realtek)" (audio)\n'
    )
    out = cv.list_video_targets(probe=lambda ff: stderr, ffmpeg="ffmpeg")
    assert out["ffmpeg_available"] is True
    # two cameras with the SAME name → ids disambiguated by the alternative name.
    assert out["cameras"] == [
        {"id": "@device_pnp_cam0", "name": "Integrated Camera"},
        {"id": "@device_pnp_cam1", "name": "Integrated Camera"},
    ]


def test_list_video_targets_unique_camera_uses_name(monkeypatch):
    monkeypatch.setattr(cv.screens, "list_monitors", lambda: [])
    monkeypatch.setattr(cv.screens, "list_windows", lambda: [])
    stderr = (
        '[dshow @ 0] "Logitech Webcam" (video)\n'
        '[dshow @ 0]   Alternative name "@device_pnp_xyz"\n'
    )
    out = cv.list_video_targets(probe=lambda ff: stderr, ffmpeg="ffmpeg")
    assert out["cameras"] == [{"id": "Logitech Webcam", "name": "Logitech Webcam"}]


def test_duplicate_camera_without_alt_name_dshow_escaped(monkeypatch):
    monkeypatch.setattr(cv.screens, "list_monitors", lambda: [])
    monkeypatch.setattr(cv.screens, "list_windows", lambda: [])
    # Two cameras with the SAME name and NO alternative name → ids disambiguated with a
    # BACKSLASH-ESCAPED colon so `-i video=<id>` is not misparsed at dshow's `:` (the
    # video=…:audio=… separator).
    stderr = (
        '[dshow @ 0] "USB Camera" (video)\n'
        '[dshow @ 0] "USB Camera" (video)\n'
    )
    out = cv.list_video_targets(probe=lambda ff: stderr, ffmpeg="ffmpeg")
    ids = [c["id"] for c in out["cameras"]]
    assert ids == ["USB Camera\\:0", "USB Camera\\:1"]
    # No RAW (unescaped) colon survives into the ffmpeg input token.
    argv = cv.build_camera_args(ids[1], "o.mkv", _settings())
    dev = argv[argv.index("-i") + 1]
    assert dev == "video=USB Camera\\:1"
    body = dev[len("video="):]
    assert ":" not in body.replace("\\:", "")  # every colon is escaped


def test_parse_dshow_name_with_embedded_quote():
    # dshow does not escape a ``"`` inside a device name; the greedy name regex must
    # capture the WHOLE name instead of truncating at the first inner quote.
    stderr = '[dshow @ 0] "My "Studio" Cam" (video)\n'
    cams = cv._parse_dshow_video_devices(stderr)
    assert cams == [{"name": 'My "Studio" Cam', "alt": None}]


def test_list_video_targets_camera_probe_failure_is_guarded(monkeypatch):
    monkeypatch.setattr(cv.screens, "list_monitors", lambda: [])
    monkeypatch.setattr(cv.screens, "list_windows", lambda: [])

    def boom(_ff):
        raise OSError("dshow probe blew up")

    out = cv.list_video_targets(probe=boom, ffmpeg="ffmpeg")
    assert out["cameras"] == []
    assert out["ffmpeg_available"] is True
