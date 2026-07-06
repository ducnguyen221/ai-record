"""Device enumeration (`list_audio_devices` + `GET /api/audio-devices`) and
device-selection on `POST /api/capture/start` (input_device / output_device threading).

All hardware is mocked: `list_audio_devices` uses a fake `soundcard`, and the capture
layer (`_start_capture`) is stubbed so nothing touches WASAPI."""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

import ai_record.audio.capture as capture
from ai_record.config import Secrets, Settings, resolve_sessions_root
from ai_record.server import AppState, create_app
from ai_record.store import SessionStore

TOKEN = "test-token-123"
H = {"X-AI-Record-Token": TOKEN}


@pytest.fixture
def client(tmp_path):
    settings = Settings(sessions_root=str(tmp_path / "s"), consent_acknowledged=True)
    store = SessionStore(resolve_sessions_root(settings), settings)
    state = AppState(settings, store=store, secrets=Secrets(), token=TOKEN, port=8848)
    with TestClient(create_app(state)) as c:
        c.ai_state = state
        c.ai_store = store
        yield c


# --------------------------------------------------------------------------- #
# list_audio_devices (unit)
# --------------------------------------------------------------------------- #
def _fake_soundcard():
    class _Dev:
        def __init__(self, id, name):
            self.id = id
            self.name = name

    mod = types.ModuleType("soundcard")
    mod.all_microphones = lambda include_loopback=False: [_Dev("mic-1", "Mic One"), _Dev("mic-2", "Mic Two")]
    mod.all_speakers = lambda: [_Dev("spk-1", "Speaker One"), _Dev("spk-2", "Speaker Two")]
    mod.default_microphone = lambda: _Dev("mic-2", "Mic Two")
    mod.default_speaker = lambda: _Dev("spk-1", "Speaker One")
    return mod


def test_list_audio_devices_enumerates_and_marks_default(monkeypatch):
    monkeypatch.setitem(sys.modules, "soundcard", _fake_soundcard())
    out = capture.list_audio_devices()
    assert out["available"] is True
    assert [d["id"] for d in out["inputs"]] == ["mic-1", "mic-2"]
    assert [d["id"] for d in out["outputs"]] == ["spk-1", "spk-2"]
    # default is marked by id-match against default_microphone/default_speaker.
    defaults_in = [d["id"] for d in out["inputs"] if d["default"]]
    defaults_out = [d["id"] for d in out["outputs"] if d["default"]]
    assert defaults_in == ["mic-2"]
    assert defaults_out == ["spk-1"]
    # shape: every entry has id / name / default
    for d in out["inputs"] + out["outputs"]:
        assert set(d) == {"id", "name", "default"}


def test_list_audio_devices_unavailable_when_lib_absent(monkeypatch):
    # soundcard not importable → guarded empty result, never raises.
    monkeypatch.setitem(sys.modules, "soundcard", None)
    out = capture.list_audio_devices()
    assert out == {"inputs": [], "outputs": [], "available": False}


# --------------------------------------------------------------------------- #
# GET /api/audio-devices
# --------------------------------------------------------------------------- #
def test_audio_devices_requires_token(client):
    assert client.get("/api/audio-devices").status_code == 401


def test_audio_devices_returns_mocked_enumeration(client, monkeypatch):
    mocked = {
        "inputs": [{"id": "mic-1", "name": "Mic One", "default": True}],
        "outputs": [{"id": "spk-1", "name": "Speaker One", "default": True}],
        "available": True,
    }
    monkeypatch.setattr(capture, "list_audio_devices", lambda: mocked)
    r = client.get("/api/audio-devices", headers=H)
    assert r.status_code == 200
    assert r.json() == mocked


def test_audio_devices_absent_lib_returns_unavailable(client, monkeypatch):
    monkeypatch.setattr(
        capture, "list_audio_devices",
        lambda: {"inputs": [], "outputs": [], "available": False},
    )
    r = client.get("/api/audio-devices", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["inputs"] == [] and body["outputs"] == []


# --------------------------------------------------------------------------- #
# POST /api/capture/start — device selection threading
# --------------------------------------------------------------------------- #
def _stub_start(monkeypatch):
    """Stub ai_record.server._start_capture; return captured args + a fake result."""
    captured: dict = {}

    def fake(state, title, mode="meeting", sources=None, devices=None, *, ephemeral=False):
        captured["title"] = title
        captured["mode"] = mode
        captured["sources"] = sources
        captured["devices"] = devices
        captured["ephemeral"] = ephemeral
        return "sess-xyz", {s: True for s in (sources or ["them", "you"])}

    monkeypatch.setattr("ai_record.server._start_capture", fake)
    return captured


def test_capture_start_threads_both_devices(client, monkeypatch):
    captured = _stub_start(monkeypatch)
    r = client.post(
        "/api/capture/start", headers=H,
        json={"title": "meet", "input_device": "mic-1", "output_device": "spk-9"},
    )
    assert r.status_code == 200
    assert r.json()["session_id"] == "sess-xyz"
    # input_device → "you", output_device → "them"; both threaded to the capture layer.
    assert captured["devices"] == {"you": "mic-1", "them": "spk-9"}
    assert set(captured["sources"]) == {"you", "them"}


def test_capture_start_input_only_disables_them(client, monkeypatch):
    captured = _stub_start(monkeypatch)
    r = client.post(
        "/api/capture/start", headers=H,
        json={"input_device": "mic-1", "output_device": None},
    )
    assert r.status_code == 200
    assert captured["devices"] == {"you": "mic-1"}
    assert captured["sources"] == ["you"]


def test_capture_start_output_only_disables_you(client, monkeypatch):
    captured = _stub_start(monkeypatch)
    r = client.post(
        "/api/capture/start", headers=H,
        json={"input_device": None, "output_device": "spk-1"},
    )
    assert r.status_code == 200
    assert captured["devices"] == {"them": "spk-1"}
    assert captured["sources"] == ["them"]


def test_capture_start_both_null_is_422(client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("_start_capture must not run when both devices are null")

    monkeypatch.setattr("ai_record.server._start_capture", boom)
    r = client.post(
        "/api/capture/start", headers=H,
        json={"input_device": None, "output_device": None},
    )
    assert r.status_code == 422


def test_capture_start_backcompat_no_device_keys(client, monkeypatch):
    """No input_device/output_device keys → legacy mode/sources path, devices=None."""
    captured = _stub_start(monkeypatch)
    r = client.post(
        "/api/capture/start", headers=H,
        json={"title": "m", "mode": "meeting", "sources": ["you", "them"]},
    )
    assert r.status_code == 200
    assert captured["devices"] is None
    assert captured["sources"] == ["you", "them"]


def test_capture_start_backcompat_default_when_no_body(client, monkeypatch):
    """Bare start (no sources, no devices) still works unchanged."""
    captured = _stub_start(monkeypatch)
    r = client.post("/api/capture/start", headers=H, json={})
    assert r.status_code == 200
    assert captured["devices"] is None
    assert captured["sources"] is None
