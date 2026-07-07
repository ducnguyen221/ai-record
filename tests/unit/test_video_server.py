"""VIDEO recording backend wiring (server + capture_helpers + config + store).

No ffmpeg is ever spawned: the audio collaborators and the VideoCaptureManager are
monkeypatched with light fakes. Reuses the ``TestClient(create_app(state))`` +
injected-token pattern from ``test_server.py``.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from fastapi.testclient import TestClient

from ai_record.config import Secrets, Settings, resolve_sessions_root
from ai_record.server import AppState, _stop_capture, create_app
from ai_record.store import SessionMeta, SessionStore

TOKEN = "test-token-123"
H = {"X-AI-Record-Token": TOKEN}


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        sessions_root=str(tmp_path / "s"),
        consent_acknowledged=True,
        hardware_preset="cpu",
        diarization_realtime=False,
    )
    store = SessionStore(resolve_sessions_root(settings), settings)
    state = AppState(settings, store=store, secrets=Secrets(), token=TOKEN, port=8848)
    with TestClient(create_app(state)) as c:
        c.ai_state = state
        c.ai_store = store
        yield c


# --------------------------------------------------------------------------- #
# Fakes for the audio path (so build_and_start runs with no hardware/ffmpeg)
# --------------------------------------------------------------------------- #
class _Ring:
    pass


class _Health:
    def to_dict(self):
        return {"ok": True}


class _SourceStatus:
    def __init__(self, source, available=True):
        self.source = source
        self.available = available
        self.health = _Health()


class FakeCaptureManager:
    def __init__(self, **kwargs):
        self._enabled = tuple(kwargs.get("enabled_sources") or ("them", "you"))
        self.stopped = False

    def start(self):
        return [_SourceStatus(s, True) for s in self._enabled]

    def sources_status(self):
        return [_SourceStatus(s, True) for s in self._enabled]

    def stop(self):
        self.stopped = True


class FakePipeline:
    def __init__(self, settings, preset, transcriber, store, session, *,
                 broadcast=None, epoch_states=None, translator=None, diarizer=None):
        self.rings = {"you": _Ring(), "them": _Ring()}
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def status(self):
        return {"effective_model": "fake"}


class FakeTranscriber:
    def __init__(self, settings, preset, on_status=None):
        pass


def _patch_audio(monkeypatch):
    monkeypatch.setattr("ai_record.transcriber.Transcriber", FakeTranscriber)
    monkeypatch.setattr("ai_record.pipeline.Pipeline", FakePipeline)
    monkeypatch.setattr("ai_record.audio.capture.CaptureManager", FakeCaptureManager)


def _install_video(monkeypatch, *, fail=False):
    """Patch capture_helpers.VideoCaptureManager with a recording fake; return the
    list of constructed instances (so tests can assert construction/stop)."""
    created: list = []

    class FakeVideoManager:
        def __init__(self, session_dir, video_request, settings, *, spawn=None):
            self.session_dir = session_dir
            self.video_request = video_request
            self.settings = settings
            self.spawn = spawn
            self.stopped = False
            created.append(self)

        def start(self):
            if fail:
                return {"screen": None, "camera": None, "errors": ["screen ffmpeg failed"]}
            return {"screen": {"state": "recording"}, "camera": None, "errors": []}

        def stop(self):
            self.stopped = True

        def status(self):
            return {"screen": {"state": "recording"}, "camera": None}

    monkeypatch.setattr(
        "ai_record.capture_helpers.VideoCaptureManager", FakeVideoManager, raising=False
    )
    return created


_VIDEO_BODY = {"input_device": "mic-1", "video": {"screen": {"mode": "full"}, "camera": None}}


# --------------------------------------------------------------------------- #
# (a) start with video → manager attached + video_errors:[]
# --------------------------------------------------------------------------- #
def test_start_with_video_attaches_manager(client, monkeypatch):
    _patch_audio(monkeypatch)
    created = _install_video(monkeypatch)

    r = client.post("/api/capture/start", headers=H, json={"title": "v", **_VIDEO_BODY})
    assert r.status_code == 200
    body = r.json()
    assert body["video_errors"] == []
    assert body["video_skipped"] is None
    assert len(created) == 1
    assert client.ai_state.video is created[0]
    # Chosen config recorded into session meta.
    meta = client.ai_store.load_session(body["session_id"]).meta
    assert meta.video["encoder"] == client.ai_state.settings.video_encoder
    assert meta.video["screen"] == {"mode": "full"}


# --------------------------------------------------------------------------- #
# (b) ephemeral + video → video_skipped:"ephemeral", NO manager, no video meta
# --------------------------------------------------------------------------- #
def test_ephemeral_video_is_skipped(client, monkeypatch):
    _patch_audio(monkeypatch)
    created = _install_video(monkeypatch)

    r = client.post(
        "/api/capture/start", headers=H,
        json={"title": "v", "ephemeral": True, **_VIDEO_BODY},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["video_skipped"] == "ephemeral"
    assert body["video_errors"] == []
    assert created == []  # NEVER constructed
    assert client.ai_state.video is None
    meta = client.ai_store.load_session(body["session_id"]).meta
    assert meta.video == {}  # no video meta recorded


# --------------------------------------------------------------------------- #
# (c) screen fails → video_errors non-empty but audio session still 200
# --------------------------------------------------------------------------- #
def test_video_screen_failure_does_not_fail_session(client, monkeypatch):
    _patch_audio(monkeypatch)
    created = _install_video(monkeypatch, fail=True)

    r = client.post("/api/capture/start", headers=H, json={"title": "v", **_VIDEO_BODY})
    assert r.status_code == 200
    body = r.json()
    assert body["video_errors"] == ["screen ffmpeg failed"]
    assert body["session_id"]  # audio session is up
    assert len(created) == 1  # manager still attached despite the screen error


# --------------------------------------------------------------------------- #
# (d) status payload includes "video"
# --------------------------------------------------------------------------- #
def test_status_includes_video(client, monkeypatch):
    # Fresh: no capture → video is None.
    fresh = client.get("/api/capture/status", headers=H).json()
    assert "video" in fresh and fresh["video"] is None

    _patch_audio(monkeypatch)
    _install_video(monkeypatch)
    client.post("/api/capture/start", headers=H, json={"title": "v", **_VIDEO_BODY})

    st = client.get("/api/capture/status", headers=H).json()
    assert st["video"] == {"screen": {"state": "recording"}, "camera": None}


# --------------------------------------------------------------------------- #
# (e) stop calls manager.stop()
# --------------------------------------------------------------------------- #
def test_stop_stops_video_manager(client, monkeypatch):
    _patch_audio(monkeypatch)
    created = _install_video(monkeypatch)
    client.post("/api/capture/start", headers=H, json={"title": "v", **_VIDEO_BODY})
    mgr = created[0]

    r = client.post("/api/capture/stop", headers=H)
    assert r.status_code == 200
    assert mgr.stopped is True
    assert client.ai_state.video is None


def test_stop_capture_direct_stops_video(client, monkeypatch):
    _patch_audio(monkeypatch)
    created = _install_video(monkeypatch)
    client.post("/api/capture/start", headers=H, json={"title": "v", **_VIDEO_BODY})
    mgr = created[0]
    _stop_capture(client.ai_state)
    assert mgr.stopped is True


# --------------------------------------------------------------------------- #
# Fix 4: blocking start/probe are offloaded off the event loop via asyncio.to_thread
# --------------------------------------------------------------------------- #
def test_start_offloads_blocking_work_to_thread(client, monkeypatch):
    _patch_audio(monkeypatch)
    _install_video(monkeypatch)
    calls: list[str] = []
    real = asyncio.to_thread

    async def spy(fn, *a, **k):
        calls.append(getattr(fn, "__name__", str(fn)))
        return await real(fn, *a, **k)

    monkeypatch.setattr("ai_record.server.asyncio.to_thread", spy)
    r = client.post("/api/capture/start", headers=H, json={"title": "v", **_VIDEO_BODY})
    assert r.status_code == 200
    assert "_start_capture" in calls  # start ran off the event loop


def test_video_devices_offloads_probe_to_thread(client, monkeypatch):
    calls: list[str] = []
    real = asyncio.to_thread

    async def spy(fn, *a, **k):
        calls.append(getattr(fn, "__name__", str(fn)))
        return await real(fn, *a, **k)

    monkeypatch.setattr("ai_record.server.asyncio.to_thread", spy)

    def probe():  # named so we can positively identify the offloaded callable
        return {"cameras": [], "displays": [], "windows": [], "ffmpeg_available": False}

    _inject_capture_video(monkeypatch, probe)
    r = client.get("/api/video-devices", headers=H)
    assert r.status_code == 200
    assert calls == ["probe"]  # the (patched) dshow probe ran off the event loop


# --------------------------------------------------------------------------- #
# Fix 5: _stop_capture is lock-guarded + idempotent → no double-stop on a Stop race
# --------------------------------------------------------------------------- #
def test_stop_capture_is_idempotent_no_double_stop(client, monkeypatch):
    _patch_audio(monkeypatch)
    created = _install_video(monkeypatch)
    client.post("/api/capture/start", headers=H, json={"title": "v", **_VIDEO_BODY})
    mgr = created[0]
    assert hasattr(client.ai_state, "_capture_lock")

    assert _stop_capture(client.ai_state) is not None
    assert mgr.stopped is True

    # A second Stop (e.g. main-thread window-close racing the HTTP Stop) sees the state
    # already cleared → no-op; the video manager is NOT stopped a second time.
    mgr.stopped = False
    assert _stop_capture(client.ai_state) is None
    assert mgr.stopped is False


# --------------------------------------------------------------------------- #
# malformed video body → 422
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "video",
    [
        {"screen": {"mode": "region"}},  # region missing
        {"screen": {"mode": "region", "region": {"x": 0, "y": 0, "w": 10}}},  # missing h
        {"screen": {"mode": "region", "region": {"x": 0, "y": 0, "w": 10, "h": "z"}}},  # non-int
        {"screen": {"mode": "bogus"}},  # bad mode
        {"screen": 5},  # not an object
        {"camera": {"device": 5}},  # device not a string
        "notadict",  # not an object
    ],
)
def test_malformed_video_returns_422(client, monkeypatch, video):
    def boom(*a, **k):
        raise AssertionError("_start_capture must not run for malformed video")

    monkeypatch.setattr("ai_record.server._start_capture", boom)
    r = client.post("/api/capture/start", headers=H, json={"input_device": "mic-1", "video": video})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# (f) GET /api/video-devices — guarded shape
# --------------------------------------------------------------------------- #
def _inject_capture_video(monkeypatch, fn):
    """Patch ``capture_video.list_video_targets`` whether or not the real (Agent A)
    module is importable. ``from . import capture_video`` binds from the package
    attribute, so patch there too."""
    import ai_record

    try:
        import ai_record.capture_video as mod  # real module (Agent A) if present
    except Exception:
        mod = types.ModuleType("ai_record.capture_video")
        monkeypatch.setitem(sys.modules, "ai_record.capture_video", mod)
    monkeypatch.setattr(mod, "list_video_targets", fn, raising=False)
    monkeypatch.setattr(ai_record, "capture_video", mod, raising=False)


def test_video_devices_passthrough(client, monkeypatch):
    payload = {
        "cameras": [{"id": "c0", "name": "Cam"}],
        "displays": [{"id": "0", "name": "Main", "x": 0, "y": 0, "w": 1920, "h": 1080}],
        "windows": [{"id": "w1", "name": "Editor"}],
        "ffmpeg_available": True,
    }
    _inject_capture_video(monkeypatch, lambda: payload)
    r = client.get("/api/video-devices", headers=H)
    assert r.status_code == 200
    assert r.json() == payload


def test_video_devices_guarded_on_failure(client, monkeypatch):
    def boom():
        raise RuntimeError("enumeration blew up")

    _inject_capture_video(monkeypatch, boom)
    r = client.get("/api/video-devices", headers=H)
    assert r.status_code == 200
    assert r.json() == {"cameras": [], "displays": [], "windows": [], "ffmpeg_available": False}


def test_video_devices_requires_token(client):
    assert client.get("/api/video-devices").status_code == 401


# --------------------------------------------------------------------------- #
# (g) POST /api/region/pick → 501 unsupported
# --------------------------------------------------------------------------- #
def test_region_pick_unsupported(client):
    r = client.post("/api/region/pick", headers=H)
    assert r.status_code == 501
    assert r.json() == {"unsupported": True}


def test_region_pick_requires_token(client):
    assert client.post("/api/region/pick").status_code == 401


# --------------------------------------------------------------------------- #
# (h) config round-trips new keys + enum validation
# --------------------------------------------------------------------------- #
def test_config_video_keys_roundtrip():
    s = Settings(
        video_screen_fps=24,
        video_camera_fps=15,
        video_encoder="libx264",
        video_container="mp4",
        video_capture_cursor=False,
        camera_device="cam0",
    )
    s2 = Settings.from_dict(s.to_dict())
    assert s2.video_screen_fps == 24
    assert s2.video_camera_fps == 15
    assert s2.video_encoder == "libx264"
    assert s2.video_container == "mp4"
    assert s2.video_capture_cursor is False
    assert s2.camera_device == "cam0"


def test_config_defaults():
    s = Settings()
    assert s.video_encoder == "auto"
    assert s.video_container == "mkv"
    assert s.video_screen_fps == 30
    assert s.video_camera_fps == 30
    assert s.video_capture_cursor is True
    assert s.camera_device == ""


def test_config_rejects_bad_video_encoder():
    with pytest.raises(ValueError):
        Settings(video_encoder="bogus")


def test_config_rejects_bad_video_container():
    with pytest.raises(ValueError):
        Settings(video_container="avi")


# --------------------------------------------------------------------------- #
# (i) SessionMeta round-trips video
# --------------------------------------------------------------------------- #
def test_session_meta_roundtrips_video():
    m = SessionMeta(
        session_id="20250101-000000-x",
        title="t",
        created_at="2025-01-01T00:00:00+00:00",
        video={"encoder": "libx264", "screen": {"mode": "full"}},
    )
    m2 = SessionMeta.from_dict(m.to_dict())
    assert m2.video == {"encoder": "libx264", "screen": {"mode": "full"}}


def test_session_meta_video_defaults_empty():
    # Backward compat: meta dict without ``video`` → default {}.
    m = SessionMeta.from_dict(
        {"session_id": "20250101-000000-x", "title": "t", "created_at": "2025-01-01T00:00:00+00:00"}
    )
    assert m.video == {}
