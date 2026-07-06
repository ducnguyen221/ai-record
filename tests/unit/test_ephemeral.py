"""Ephemeral ("Không lưu") recording mode: nothing is written to disk.

Covers the store-level in-memory session, the full pipeline path writing zero
files, the token-gated /api/summarize-text endpoint, the capture/start ephemeral
flag + status, and that retention/recovery ignore ephemeral sessions.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import ai_record.summarizer as sm
from ai_record.audio.capture import FileCaptureSource
from ai_record.audio.vad import FakeVad
from ai_record.config import Secrets, Settings, resolve_preset, resolve_sessions_root
from ai_record.pipeline import Pipeline
from ai_record.server import AppState, _stop_capture, create_app
from ai_record.store import SessionStore
from ai_record.transcriber import MockTranscriber
from tests.audio_helpers import sequence, silence, tone
from tests.unit.test_store import _rec

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
# store-level: ephemeral session lives entirely in memory
# --------------------------------------------------------------------------- #
def test_ephemeral_store_writes_no_files(store: SessionStore):
    root = store.root
    before = set(root.iterdir())

    sess = store.create("nosave", persist=False)
    assert store.is_ephemeral(sess.session_id)
    store.append_utterance(_rec(store, sess.session_id, text="one"))
    store.patch_utterance(sess.session_id, 1, {"translation": "xin chao"})
    store.finalize(sess.session_id)

    # Readers serve the in-memory copy (live UI / summarize still work)…
    data = store.load_session(sess.session_id)
    assert [u.text for u in data.utterances] == ["one"]
    assert data.utterances[0].translation == "xin chao"
    assert data.meta.ended_at is not None  # finalize stamped it in memory

    # …but NOTHING new appears under the sessions root.
    assert set(root.iterdir()) == before


def test_normal_store_still_persists(store: SessionStore):
    """Regression: a normal (persisted) session creates its directory + files."""
    sess = store.create("saved")
    store.append_utterance(_rec(store, sess.session_id, text="one"))
    d = store._dir(sess.session_id)
    assert d.exists()
    assert (d / "transcript.jsonl").exists()
    assert (d / "transcript.md").exists()
    assert (d / "meta.json").exists()
    assert not store.is_ephemeral(sess.session_id)


# --------------------------------------------------------------------------- #
# full pipeline path: ephemeral start → emit → stop writes zero files/dirs
# --------------------------------------------------------------------------- #
def test_ephemeral_pipeline_broadcasts_but_persists_nothing(tmp_path):
    settings = Settings(
        hardware_preset="cpu", sessions_root=str(tmp_path / "sessions"),
        diarization_realtime=False,
    )
    root = resolve_sessions_root(settings)
    store = SessionStore(root, settings)

    session = store.create("nosave", persist=False)
    assert store.is_ephemeral(session.session_id)

    msgs: list[dict] = []
    preset = resolve_preset(settings)
    tr = MockTranscriber(text_fn=lambda u: f"utt@{u.audio_start_sample}")
    pipe = Pipeline(
        settings, preset, tr, store, session, broadcast=msgs.append,
        vad_factory=lambda: FakeVad(frame_samples=320, threshold=0.02),
    )
    pipe.start()
    audio = sequence(silence(0.4), tone(1.0), silence(0.9), tone(1.0), silence(0.7))
    FileCaptureSource("them", audio).feed_into(pipe)
    assert pipe.wait_idle(timeout=15.0)
    pipe.stop()
    store.finalize(session.session_id)

    # The live transcript still flows over the broadcast (WS) channel…
    utt_msgs = [m for m in msgs if m.get("type") == "utterance"]
    assert len(utt_msgs) >= 2
    # …and is readable in-memory…
    assert len(store.load_session(session.session_id).utterances) >= 2
    # …but the sessions root has NO session folder or file whatsoever.
    assert list(root.iterdir()) == []


def test_stop_capture_deletes_ephemeral_from_memory():
    """_stop_capture drops the in-memory ephemeral session (no autosummary)."""
    settings = Settings(hardware_preset="cpu", diarization_realtime=False)
    store = SessionStore(resolve_sessions_root(settings), settings)
    state = AppState(settings, store=store, secrets=Secrets(), token=TOKEN)
    sess = store.create("nosave", persist=False)
    state.active_session_id = sess.session_id
    state.active_ephemeral = True

    out = _stop_capture(state)
    assert out == sess.session_id
    assert state.active_ephemeral is False
    assert state.active_session_id is None
    assert not store.is_ephemeral(sess.session_id)  # dropped from memory


# --------------------------------------------------------------------------- #
# capture/start ephemeral flag + status
# --------------------------------------------------------------------------- #
def test_capture_start_accepts_ephemeral_flag(client, monkeypatch):
    captured: dict = {}

    def fake(st, title, mode="meeting", sources=None, devices=None, *, ephemeral=False):
        captured["ephemeral"] = ephemeral
        st.active_ephemeral = ephemeral
        st.active_session_id = "sid-e"
        return "sid-e", {"you": True}

    monkeypatch.setattr("ai_record.server._start_capture", fake)
    r = client.post("/api/capture/start", headers=H,
                    json={"input_device": "mic-1", "ephemeral": True})
    assert r.status_code == 200
    assert r.json()["ephemeral"] is True
    assert captured["ephemeral"] is True
    st = client.get("/api/capture/status", headers=H).json()
    assert st["ephemeral"] is True


def test_capture_start_defaults_not_ephemeral(client, monkeypatch):
    captured: dict = {}

    def fake(st, title, mode="meeting", sources=None, devices=None, *, ephemeral=False):
        captured["ephemeral"] = ephemeral
        return "sid-n", {"you": True}

    monkeypatch.setattr("ai_record.server._start_capture", fake)
    r = client.post("/api/capture/start", headers=H, json={"input_device": "mic-1"})
    assert r.status_code == 200
    assert r.json()["ephemeral"] is False
    assert captured["ephemeral"] is False
    # A fresh status also reports ephemeral:false.
    assert client.get("/api/capture/status", headers=H).json()["ephemeral"] is False


# --------------------------------------------------------------------------- #
# /api/summarize-text — token-gated, no session
# --------------------------------------------------------------------------- #
class _Fake:
    name = "fakeprov"

    def available(self):
        return True, ""

    def summarize(self, prompt, transcript, meta):
        return "## Notes\n" + transcript  # echo the transcript


def test_summarize_text_returns_markdown(client, monkeypatch):
    monkeypatch.setattr(sm, "make_provider", lambda name, settings, secrets=None: _Fake())
    r = client.post("/api/summarize-text", headers=H,
                    json={"text": "[00:00] A: hello world", "scenario": "analyze"})
    assert r.status_code == 200
    body = r.json()
    assert body["scenario"] == "analyze"
    assert body["provider"] == "fakeprov"
    assert body["reformat_fallback"] is False
    assert "hello world" in body["markdown"]


def test_summarize_text_requires_token(client):
    assert client.post("/api/summarize-text", json={"text": "hi"}).status_code == 401


def test_summarize_text_rejects_empty(client):
    assert client.post("/api/summarize-text", headers=H, json={"text": "   "}).status_code == 422
    assert client.post("/api/summarize-text", headers=H, json={}).status_code == 422


def test_summarize_text_rejects_overlong(client, monkeypatch):
    state = client.ai_state
    state.settings = state.settings.update({"summary_max_chars": 50})

    def boom(*a, **k):
        raise AssertionError("build_summary must not run for over-long input")

    monkeypatch.setattr(sm, "make_provider", boom)
    r = client.post("/api/summarize-text", headers=H, json={"text": "x" * 51})
    assert r.status_code == 422


def test_summarize_text_unavailable_returns_503(client, monkeypatch):
    class Down:
        name = "down"

        def available(self):
            return False, "Claude CLI not found"

        def summarize(self, *a):  # pragma: no cover - never called
            raise AssertionError

    monkeypatch.setattr(sm, "make_provider", lambda name, settings, secrets=None: Down())
    r = client.post("/api/summarize-text", headers=H, json={"text": "hi there"})
    assert r.status_code == 503
    assert "error" in r.json()


def test_summarize_text_provider_error_returns_502(client, monkeypatch):
    class Boom:
        name = "boom"

        def available(self):
            return True, ""

        def summarize(self, *a):
            raise sm.SummarizerError("provider exited 1")

    monkeypatch.setattr(sm, "make_provider", lambda name, settings, secrets=None: Boom())
    r = client.post("/api/summarize-text", headers=H, json={"text": "hi there", "scenario": "minutes"})
    assert r.status_code == 502
    assert "error" in r.json()


# --------------------------------------------------------------------------- #
# retention / recovery ignore ephemeral sessions
# --------------------------------------------------------------------------- #
def test_retention_and_recovery_ignore_ephemeral(store: SessionStore):
    sess = store.create("nosave", persist=False)
    store.append_utterance(_rec(store, sess.session_id, text="x"))

    # Not on disk → never listed, never flagged incomplete, never pruned.
    assert store.list_sessions() == []
    assert store.detect_incomplete() == []

    store.settings = store.settings.update({"retention_days": 1})
    assert store.apply_retention() == 0  # does not choke on the in-memory session
