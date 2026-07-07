"""Video capture engine: ffmpeg argv builders + a guarded ffmpeg-child recorder.

Phase 1 records SCREEN (``gdigrab``) and/or CAMERA (``dshow``) to disk with NO audio
track (``-an``); the audio pipeline stays entirely separate. This module is
import-safe with no ffmpeg binary, no GPU, and no display:

* the ffmpeg *path* is resolved lazily via ``shutil.which`` (``"ffmpeg"`` literal
  fallback), so the argv builders are PURE and never touch the OS beyond that lookup;
* device enumeration shells out to ffmpeg only when it exists, behind an injectable
  probe seam so tests never spawn a real process;
* every child process is created through an injectable ``spawn`` seam (default
  :class:`subprocess.Popen`), wrapped in a Windows Job Object with
  ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` (ctypes, guarded) so a crash can't orphan
  ffmpeg. Tests pass a ``FakeProcess`` factory and never touch real hardware.

Follows the repo's guarded-subprocess conventions (``shutil.which`` +
``CREATE_NO_WINDOW`` + guarded returns) established in ``store._transcode_audio_to_mp3``
and ``audio.capture.list_audio_devices``.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import Counter
from typing import Any, Callable

from . import screens

log = logging.getLogger("ai_record.capture_video")

_IS_WINDOWS = os.name == "nt"


class _suppress:
    """Tiny swallow-everything context manager (mirrors audio.capture._suppress)."""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return True


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


# --------------------------------------------------------------------------- #
# Settings accessors (video_* fields may be absent on older Settings → getattr)
# --------------------------------------------------------------------------- #
def _container(settings) -> str:
    c = getattr(settings, "video_container", "mkv") or "mkv"
    return c if c in ("mkv", "mp4") else "mkv"


def _container_output_flags(container: str) -> list[str]:
    # Fragmented mp4 stays playable if the recording is killed mid-write.
    if container == "mp4":
        return ["-movflags", "+frag_keyframe+empty_moov+default_base_moof"]
    return []


def _effective_encoder(settings, override: str | None) -> str:
    if override:
        return override
    enc = getattr(settings, "video_encoder", "auto") or "auto"
    if enc == "auto":
        return "h264_nvenc"
    return enc


def _wants_nvenc_fallback(settings, override: str | None) -> bool:
    """True when the primary encoder is nvenc and a libx264 fallback should exist."""
    if override:
        return False
    enc = getattr(settings, "video_encoder", "auto") or "auto"
    return enc in ("auto", "h264_nvenc")


def _encoder_flags(encoder: str) -> list[str]:
    if encoder in ("h264_nvenc", "hevc_nvenc"):
        # Conservative NVENC: constant-ish quality VBR, no lookahead / no spatial AQ
        # (those inflate VRAM + latency and are unnecessary for a screen recording).
        # hevc_nvenc is the wide-desktop path: h264_nvenc caps at 4096px width, HEVC
        # NVENC goes to 8192, so ultrawide/multi-monitor captures still encode on GPU.
        return [
            "-c:v", encoder,
            "-preset", "p4",
            "-rc", "vbr",
            "-b:v", "8M",
            "-pix_fmt", "yuv420p",
        ]
    # libx264 (also the nvenc → software fallback).
    return [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
    ]


# --------------------------------------------------------------------------- #
# Device enumeration
# --------------------------------------------------------------------------- #
def _probe_dshow_devices(ffmpeg: str) -> str:
    """Run ffmpeg's dshow device listing and return its (device list) stderr text."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        capture_output=True,
        creationflags=_creationflags(),
        timeout=15,
    )
    return (proc.stderr or b"").decode("utf-8", "replace")


# Greedy (first-quote → last-quote) so a device name with an EMBEDDED ``"`` (dshow
# does not escape it) is captured whole instead of truncated at the first inner quote.
_NAME_RE = re.compile(r'"(.+)"')


def _parse_dshow_video_devices(stderr: str) -> list[dict]:
    """Parse ffmpeg dshow ``-list_devices`` stderr → ``[{"name","alt"}, ...]`` (video).

    Handles both the modern per-line tagged format (``"Cam" (video)`` followed by
    ``Alternative name "@device..."``) and the legacy section-header format
    (``DirectShow video devices`` / ``DirectShow audio devices``).
    """
    cams: list[dict] = []
    section: str | None = None
    pending: dict | None = None
    for line in stderr.splitlines():
        low = line.lower()
        if "directshow video devices" in low:
            section, pending = "video", None
            continue
        if "directshow audio devices" in low:
            section, pending = "audio", None
            continue
        if "alternative name" in low:
            m = _NAME_RE.search(line)
            if m and pending is not None:
                pending["alt"] = m.group(1)
            continue
        if "(video)" in low or "(audio)" in low:
            tag = "video" if "(video)" in low else "audio"
            m = _NAME_RE.search(line)
            if not m:
                continue
            if tag == "video":
                pending = {"name": m.group(1), "alt": None}
                cams.append(pending)
            else:
                pending = None
            continue
        # Untagged legacy line: a quoted device name inside a known section.
        m = _NAME_RE.search(line)
        if m and section == "video":
            pending = {"name": m.group(1), "alt": None}
            cams.append(pending)
        elif m and section == "audio":
            pending = None
    return cams


def _dshow_escape(value: str) -> str:
    """Escape a device name for embedding after ``video=`` in ffmpeg's dshow demuxer.

    dshow treats ``:`` as the ``video=…:audio=…`` separator and ``\\`` as its escape
    character, so a literal ``:`` or ``\\`` in a device name must be backslash-escaped
    (order matters: escape ``\\`` first). Without this a name/id containing ``:`` is
    misparsed as a (non-existent) audio device selector.
    """
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _assign_camera_ids(raw: list[dict]) -> list[dict]:
    """Give each camera an ``id`` that uniquely addresses it for ``-f dshow``.

    Unique names → ``id == name``. Duplicate names → prefer the dshow *Alternative
    name* (a stable ``@device_pnp_...`` moniker); if none, append an index with the
    separating colon DSHOW-ESCAPED (``Name\\:1``) so ``-i video=<id>`` stays valid and
    the colon is not misread as the ``video=…:audio=…`` separator. The id is what
    :func:`build_camera_args` feeds to ``-i video=<id>``.
    """
    name_counts = Counter(c["name"] for c in raw)
    dup_index: dict[str, int] = {}
    out: list[dict] = []
    for c in raw:
        name = c["name"]
        if name_counts[name] > 1:
            if c.get("alt"):
                dev_id = c["alt"]
            else:
                i = dup_index.get(name, 0)
                dup_index[name] = i + 1
                # Escape the name's own specials, then append a BACKSLASH-ESCAPED
                # colon + index → dshow reads one literal device name, no misparse.
                dev_id = f"{_dshow_escape(name)}\\:{i}"
        else:
            dev_id = name
        out.append({"id": dev_id, "name": name})
    return out


_UNSET = object()


def list_video_targets(
    *, probe: Callable[[str], str] | None = None, ffmpeg: Any = _UNSET
) -> dict:
    """Enumerate capturable video targets.

    Shape::

        {"cameras": [{"id": str, "name": str}, ...],
         "displays": [...screens.list_monitors()...],
         "windows":  [...screens.list_windows()...],
         "ffmpeg_available": bool}

    Cameras come from ffmpeg's dshow device list. Guarded: if ffmpeg is missing the
    camera list is empty and ``ffmpeg_available`` is ``False`` (the probe is not
    called). ``probe``/``ffmpeg`` are injectable so tests never spawn ffmpeg.
    """
    ff = _ffmpeg_path() if ffmpeg is _UNSET else ffmpeg
    available = bool(ff)
    cameras: list[dict] = []
    if available:
        try:
            probe_fn = probe or _probe_dshow_devices
            stderr = probe_fn(ff)
            cameras = _assign_camera_ids(_parse_dshow_video_devices(stderr))
        except Exception as exc:  # camera enumeration must never crash the caller
            log.warning("camera enumeration failed: %s", exc)
            cameras = []
    return {
        "cameras": cameras,
        "displays": screens.list_monitors(),
        "windows": screens.list_windows(),
        "ffmpeg_available": available,
    }


# --------------------------------------------------------------------------- #
# Argv builders (PURE)
# --------------------------------------------------------------------------- #
def _display_rect(display_id: Any) -> dict:
    """Resolve a monitor rect (physical, primary-relative) by id, with a fallback."""
    for m in screens.list_monitors():
        if str(m.get("id")) == str(display_id):
            return {"x": m["x"], "y": m["y"], "w": m["w"], "h": m["h"]}
    b = screens.virtual_screen_bounds()
    return {"x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"]}


def build_screen_args(
    target: dict, out_path: str, settings, *, encoder_override: str | None = None
) -> list[str]:
    """Build a PURE ffmpeg argv for a screen/desktop/window/region capture.

    ``target = {"mode": "full"|"display"|"window"|"region", "display_id"?,
    "window_hwnd"?, "window_title"?, "region"?}``. full/display/region capture
    ``-i desktop`` (display/region add ``-offset_x/-offset_y/-video_size`` with EVEN
    dimensions); window captures ``-i title=<...>``. No audio track (``-an``).
    """
    ff = _ffmpeg_path() or "ffmpeg"
    # NOTE: read the REAL Settings field names (config.Settings defines
    # video_screen_fps / video_capture_cursor), not the historical aliases.
    fps = int(getattr(settings, "video_screen_fps", 30) or 30)
    draw_mouse = getattr(settings, "video_capture_cursor", True)
    container = _container(settings)
    mode = target.get("mode", "full")

    args = [ff, "-hide_banner", "-y", "-f", "gdigrab", "-framerate", str(fps)]
    args += ["-draw_mouse", "1" if draw_mouse else "0"]

    if mode in ("display", "region"):
        if mode == "display":
            rect = _display_rect(target.get("display_id"))
            ox, oy = int(rect["x"]), int(rect["y"])
            w = screens._even_down(rect["w"])
            h = screens._even_down(rect["h"])
        else:
            # Re-clamp at RECORD start: the display layout may have changed between
            # pick-time and now, so a stale region could be out-of-bounds/odd. Guarded
            # (a display query failure must never break arg building).
            region = target.get("region") or {}
            try:
                region = screens.sanitize_region(
                    region, bounds=screens.virtual_screen_bounds()
                )
            except Exception:  # pragma: no cover - defensive
                pass
            reg = screens.region_to_ffmpeg(region)
            ox, oy = reg["offset_x"], reg["offset_y"]
            w, h = reg["w"], reg["h"]
        args += ["-offset_x", str(ox), "-offset_y", str(oy), "-video_size", f"{w}x{h}"]
        args += ["-i", "desktop"]
    elif mode == "window":
        title = target.get("window_title")
        if not title and target.get("window_hwnd") is not None:
            # gdigrab addresses windows by title, not hwnd; resolve it if we only have
            # the handle. Guarded — falls back to full desktop if lookup fails.
            title = _title_for_hwnd(target.get("window_hwnd"))
        if title:
            args += ["-i", f"title={title}"]
        else:
            args += ["-i", "desktop"]
    else:  # "full"
        args += ["-i", "desktop"]

    if mode in ("window", "full"):
        # window/full capture has no explicit even -video_size, but both encoders use
        # -pix_fmt yuv420p which REQUIRES even dimensions. Force even w/h so an
        # odd-sized window or desktop can't kill nvenc AND libx264 alike.
        args += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]

    args += _encoder_flags(_effective_encoder(settings, encoder_override))
    args += ["-an"]
    args += _container_output_flags(container)
    args += [str(out_path)]
    return args


def _title_for_hwnd(hwnd: Any) -> str | None:
    try:
        for w in screens.list_windows():
            if str(w.get("id")) == str(hwnd):
                return w.get("name")
    except Exception:  # pragma: no cover - defensive
        pass
    return None


def build_camera_args(
    device: str, out_path: str, settings, *, encoder_override: str | None = None
) -> list[str]:
    """Build a PURE ffmpeg argv for a dshow camera capture. No audio track (``-an``)."""
    ff = _ffmpeg_path() or "ffmpeg"
    fps = int(getattr(settings, "video_camera_fps", 30) or 30)
    container = _container(settings)

    args = [
        ff, "-hide_banner", "-y",
        "-f", "dshow",
        "-framerate", str(fps),
        "-i", f"video={device}",
    ]
    args += _encoder_flags(_effective_encoder(settings, encoder_override))
    args += ["-an"]
    args += _container_output_flags(container)
    args += [str(out_path)]
    return args


# --------------------------------------------------------------------------- #
# Windows Job Object (kill-on-close) — guarded ctypes
# --------------------------------------------------------------------------- #
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _create_kill_on_close_job():
    """Create a Job Object that kills its processes when the handle closes, or None."""
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32
        ]
        ok = kernel32.SetInformationJobObject(
            ctypes.c_void_p(job), _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(ctypes.c_void_p(job))
            return None
        return job
    except Exception:  # pragma: no cover - defensive
        return None


def _assign_process_to_job(job, proc) -> bool:
    """Assign a real Popen child (with a Windows ``_handle``) to a job. Guarded."""
    handle = getattr(proc, "_handle", None)
    if job is None or handle is None:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        return bool(kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(int(handle))))
    except Exception:  # pragma: no cover - defensive
        return False


# --------------------------------------------------------------------------- #
# VideoRecorder — wraps ONE ffmpeg child
# --------------------------------------------------------------------------- #
class VideoRecorder:
    """Own a single ffmpeg child: spawn, nvenc→libx264 fallback, graceful stop.

    All process interaction goes through the injected ``spawn`` seam so tests use a
    ``FakeProcess`` and never run real ffmpeg.
    """

    def __init__(
        self,
        argv,
        out_path,
        *,
        spawn=subprocess.Popen,
        encoder_fallback_argv=None,
        encoder_fallback_argvs=None,
        startup_grace_s: float = 1.5,
    ) -> None:
        self.argv = list(argv)
        self.out_path = str(out_path)
        self._spawn = spawn
        # An ordered chain of encoders to try if the previous one dies at startup
        # (e.g. h264_nvenc → hevc_nvenc → libx264). A single legacy fallback is also
        # accepted and treated as a one-element chain.
        if encoder_fallback_argvs:
            self._fallbacks = [list(a) for a in encoder_fallback_argvs]
        elif encoder_fallback_argv:
            self._fallbacks = [list(encoder_fallback_argv)]
        else:
            self._fallbacks = []
        self.encoder_fallback_argv = self._fallbacks[0] if self._fallbacks else None
        self._startup_grace_s = float(startup_grace_s)
        self._proc = None
        self._job = None
        self._job_lock = threading.Lock()
        self._log_fh = None
        self.error: str | None = None
        self._used_fallback = False
        self._stopped = False

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> "VideoRecorder":
        # Try the primary, then each fallback in order. Watch each non-final encoder
        # for a startup death (nonzero exit within the grace window) and advance; the
        # final one is accepted as-is (its death is reported, not retried).
        chain = [self.argv] + self._fallbacks
        for i, argv in enumerate(chain):
            self._proc = self._spawn_proc(argv)
            is_last = i == len(chain) - 1
            if is_last and i == 0:
                break  # single primary, nothing to fall back to → accept
            code = self._poll_within(self._startup_grace_s)
            if code is None or code == 0:
                break  # this encoder started (or ran cleanly)
            # This encoder died at startup.
            if is_last:
                self.error = f"fallback encoder also exited {code} at startup"
                break
            self._used_fallback = True
            self.error = (
                f"primary encoder exited {code} at startup; retrying with fallback encoder"
                if i == 0
                else f"encoder exited {code} at startup; retrying with next encoder"
            )
            self._close_job()
            self._close_log()
            # loop continues to the next encoder in the chain
        return self

    def _spawn_proc(self, argv):
        self._open_log()
        stderr = self._log_fh if self._log_fh is not None else subprocess.DEVNULL
        proc = self._spawn(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            creationflags=_creationflags(),
        )
        # Wrap in a kill-on-close Job Object so a parent crash can't orphan ffmpeg.
        # Surface a degraded guarantee (job unavailable / assignment failed) via a
        # WARNING so the "no orphan on crash" promise silently weakening is visible.
        with _suppress():
            self._job = _create_kill_on_close_job()
            if self._job is None:
                if os.name == "nt":
                    log.warning(
                        "kill-on-close Job Object unavailable; ffmpeg may orphan on crash"
                    )
            elif not _assign_process_to_job(self._job, proc):
                # A real child exposes a Windows handle; if assignment still fails the
                # guarantee is degraded. (A test FakeProcess has no _handle — skip it.)
                if getattr(proc, "_handle", None) is not None:
                    log.warning(
                        "assigning ffmpeg to Job Object failed; may orphan on crash"
                    )
        return proc

    def _open_log(self) -> None:
        if self._log_fh is not None:
            return
        try:
            # Append mode + unbuffered: ffmpeg's stderr drains to a file so its pipe
            # never fills and blocks. We never read it back here.
            self._log_fh = open(self.out_path + ".log", "ab", buffering=0)
        except Exception:
            self._log_fh = None

    def _poll_within(self, seconds: float):
        code = self._proc.poll()
        if code is not None:
            return code
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            time.sleep(0.02)
            code = self._proc.poll()
            if code is not None:
                return code
        return None

    def stop(self, timeout: float = 5) -> dict:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            # Ask ffmpeg to finalize the file by writing 'q' to stdin.
            try:
                if getattr(proc, "stdin", None) is not None:
                    proc.stdin.write(b"q\n")
                    proc.stdin.flush()
            except Exception:
                # Pipe already closed / child gone — terminate() below still handles it.
                log.debug("failed to write 'q' to ffmpeg stdin", exc_info=True)
            grace = max(0.1, timeout * 0.6)
            if not self._wait(proc, grace):
                with _suppress():
                    proc.terminate()
                if not self._wait(proc, max(0.1, timeout - grace)):
                    with _suppress():
                        proc.kill()
                    self._wait(proc, 2)
        self._close_log()
        self._close_job()
        self._stopped = True
        return self.status()

    @staticmethod
    def _wait(proc, timeout: float) -> bool:
        """Wait for ``proc`` up to ``timeout``; return True if it exited."""
        try:
            proc.wait(timeout=timeout)
            return True
        except Exception:
            return False

    def _close_log(self) -> None:
        if self._log_fh is not None:
            with _suppress():
                self._log_fh.close()
            self._log_fh = None

    def _close_job(self) -> None:
        # Idempotent: grab-and-null the handle under a lock so two threads (e.g. an
        # HTTP Stop racing the main-thread window-close) can't both CloseHandle it.
        with self._job_lock:
            job = self._job
            self._job = None
        if job:
            with _suppress():
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(job))

    # -- introspection ----------------------------------------------------- #
    def poll(self):
        if self._proc is None:
            return None
        return self._proc.poll()

    def status(self) -> dict:
        recording = False
        if self._proc is not None and not self._stopped:
            recording = self._proc.poll() is None
        return {
            "recording": recording,
            "file": self.out_path,
            "bytes": self._file_bytes(),
            "error": self.error,
        }

    def _file_bytes(self) -> int:
        try:
            return int(os.path.getsize(self.out_path))
        except OSError:
            return 0


# --------------------------------------------------------------------------- #
# VideoCaptureManager — 0..2 recorders (screen + camera)
# --------------------------------------------------------------------------- #
class VideoCaptureManager:
    """Build + supervise up to two :class:`VideoRecorder`s (screen, camera).

    ``video_request = {"screen": null|{...target...}, "camera": null|{"device": str}}``.
    A failure of ONE recorder is captured in ``errors`` and never aborts the other.
    """

    def __init__(self, session_dir, video_request: dict, settings, *, spawn=subprocess.Popen) -> None:
        self.session_dir = str(session_dir)
        self.video_request = video_request or {}
        self.settings = settings
        self._spawn = spawn
        self._screen: VideoRecorder | None = None
        self._camera: VideoRecorder | None = None

    def _build_screen_recorder(self, screen_req: dict) -> VideoRecorder:
        container = _container(self.settings)
        out = os.path.join(self.session_dir, f"screen.{container}")
        argv = build_screen_args(screen_req, out, self.settings)
        fallbacks = None
        if _wants_nvenc_fallback(self.settings, None):
            # h264_nvenc caps at 4096px wide; try hevc_nvenc (→8192, still GPU) before
            # dropping to CPU libx264 so ultrawide/multi-monitor captures stay on the GPU.
            fallbacks = [
                build_screen_args(screen_req, out, self.settings, encoder_override="hevc_nvenc"),
                build_screen_args(screen_req, out, self.settings, encoder_override="libx264"),
            ]
        return VideoRecorder(argv, out, spawn=self._spawn, encoder_fallback_argvs=fallbacks)

    def _build_camera_recorder(self, camera_req: dict) -> VideoRecorder:
        container = _container(self.settings)
        out = os.path.join(self.session_dir, f"camera.{container}")
        device = camera_req.get("device")
        argv = build_camera_args(device, out, self.settings)
        fallbacks = None
        if _wants_nvenc_fallback(self.settings, None):
            fallbacks = [
                build_camera_args(device, out, self.settings, encoder_override="hevc_nvenc"),
                build_camera_args(device, out, self.settings, encoder_override="libx264"),
            ]
        return VideoRecorder(argv, out, spawn=self._spawn, encoder_fallback_argvs=fallbacks)

    def start(self) -> dict:
        with _suppress():
            os.makedirs(self.session_dir, exist_ok=True)
        errors: list[str] = []
        screen_status = None
        camera_status = None

        screen_req = self.video_request.get("screen")
        if screen_req:
            try:
                self._screen = self._build_screen_recorder(screen_req)
                self._screen.start()
                screen_status = self._screen.status()
            except Exception as exc:
                log.warning("screen recorder failed to start: %s", exc)
                errors.append(f"screen: {exc}")
                # A recorder object may already own a spawned ffmpeg child — stop it
                # before discarding so no orphan process is leaked.
                if self._screen is not None:
                    with _suppress():
                        self._screen.stop()
                self._screen = None

        camera_req = self.video_request.get("camera")
        if camera_req:
            try:
                self._camera = self._build_camera_recorder(camera_req)
                self._camera.start()
                camera_status = self._camera.status()
            except Exception as exc:
                log.warning("camera recorder failed to start: %s", exc)
                errors.append(f"camera: {exc}")
                # Stop an already-spawned recorder before discarding (no orphan ffmpeg).
                if self._camera is not None:
                    with _suppress():
                        self._camera.stop()
                self._camera = None

        return {"screen": screen_status, "camera": camera_status, "errors": errors}

    def stop(self) -> dict:
        out: dict = {"screen": None, "camera": None}
        if self._screen is not None:
            with _suppress():
                out["screen"] = self._screen.stop()
        if self._camera is not None:
            with _suppress():
                out["camera"] = self._camera.stop()
        return out

    def status(self) -> dict:
        return {
            "screen": self._screen.status() if self._screen is not None else None,
            "camera": self._camera.status() if self._camera is not None else None,
        }
