"""M2–M4 endpoint contract (addendum §E4) via TestClient: languages, summarize,
summary, export, rename count, rediarize lifecycle, capture start mode/sources."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

import ai_record.diarizer as dz
import ai_record.summarizer as sm
from ai_record.config import Secrets, Settings, resolve_sessions_root
from ai_record.diarizer import OfflineDiarizer, SpeakerSpan
from ai_record.server import AppState, create_app
from ai_record.store import SessionStore, WavWriter
from tests.unit.test_store import _rec

TOKEN = "test-token-123"
H = {"X-AI-Record-Token": TOKEN}


@pytest.fixture
def client(tmp_path):
    settings = Settings(sessions_root=str(tmp_path / "s"), consent_acknowledged=False)
    store = SessionStore(resolve_sessions_root(settings), settings)
    state = AppState(settings, store=store, secrets=Secrets(), token=TOKEN, port=8848)
    with TestClient(create_app(state)) as c:
        c.ai_state = state
        c.ai_store = store
        yield c


# --------------------------------------------------------------------------- #
# /api/languages
# --------------------------------------------------------------------------- #
def test_languages(client):
    r = client.get("/api/languages", headers=H)
    assert r.status_code == 200
    codes = [lang["code"] for lang in r.json()["languages"]]
    assert "ja" in codes and "en" in codes and "vi" in codes
    assert r.json()["target"] == "vi"


# --------------------------------------------------------------------------- #
# summarize + summary
# --------------------------------------------------------------------------- #
def test_summarize_writes_summary_and_get(client, monkeypatch):
    store = client.ai_store
    sid = store.create("sum").session_id
    store.append_utterance(_rec(store, sid, text="alpha"))
    store.append_utterance(_rec(store, sid, text="beta", start=2.0))

    class Fake:
        name = "fakeprov"

        def available(self):
            return True, ""

        def summarize(self, prompt, transcript, meta):
            return "## Notes\n" + transcript  # echoes text → verbatim for reformat

    monkeypatch.setattr(sm, "make_provider", lambda name, settings, secrets=None: Fake())

    r = client.post(f"/api/sessions/{sid}/summarize", headers=H, json={"scenario": "reformat"})
    assert r.status_code == 200
    body = r.json()
    assert body["scenario"] == "reformat"
    assert body["provider"] == "fakeprov"
    assert body["reformat_fallback"] is False
    assert "alpha" in body["markdown"]

    data = store.load_session(sid)
    assert data.summary and "alpha" in data.summary
    assert data.meta.summary_scenario == "reformat"

    g = client.get(f"/api/sessions/{sid}/summary", headers=H)
    assert g.status_code == 200
    assert g.json()["scenario"] == "reformat"


def test_summary_404_before_generation(client):
    sid = client.ai_store.create("x").session_id
    assert client.get(f"/api/sessions/{sid}/summary", headers=H).status_code == 404


def test_summarize_unavailable_returns_error(client, monkeypatch):
    store = client.ai_store
    sid = store.create("sum2").session_id
    store.append_utterance(_rec(store, sid, text="a"))

    class Down:
        name = "down"

        def available(self):
            return False, "Claude CLI not found"

        def summarize(self, *a):  # pragma: no cover - never called
            raise AssertionError

    monkeypatch.setattr(sm, "make_provider", lambda name, settings, secrets=None: Down())
    r = client.post(f"/api/sessions/{sid}/summarize", headers=H, json={})
    # Provider unavailable → HTTP 503 with a reason (was a misleading 200 {error}; I2).
    assert r.status_code == 503
    assert "error" in r.json()


def test_summarize_provider_error_returns_502(client, monkeypatch):
    """A provider that RAN but failed (SummarizerError) → 502, not an unhandled 500 (I1)."""
    store = client.ai_store
    sid = store.create("sum3").session_id
    store.append_utterance(_rec(store, sid, text="a"))

    class Boom:
        name = "boom"

        def available(self):
            return True, ""

        def summarize(self, *a):
            raise sm.SummarizerError("claude exited 1: boom")

    monkeypatch.setattr(sm, "make_provider", lambda name, settings, secrets=None: Boom())
    r = client.post(f"/api/sessions/{sid}/summarize", headers=H, json={"scenario": "minutes"})
    assert r.status_code == 502
    assert "error" in r.json()


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def test_export_headers_and_body(client):
    store = client.ai_store
    sid = store.create("exp").session_id
    store.append_utterance(_rec(store, sid, text="hello"))
    r = client.get(f"/api/sessions/{sid}/export?what=transcript&fmt=md", headers=H)
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert f"{sid}-transcript.md" in r.headers["content-disposition"]
    assert "hello" in r.text
    bad = client.get(f"/api/sessions/{sid}/export?what=bogus&fmt=md", headers=H)
    assert bad.status_code == 422


# --------------------------------------------------------------------------- #
# rename count
# --------------------------------------------------------------------------- #
def test_rename_returns_updated_count(client):
    store = client.ai_store
    sid = store.create("rn").session_id
    store.append_utterance(_rec(store, sid, text="a", speaker="Speaker 1"))
    store.append_utterance(_rec(store, sid, text="b", start=2.0, speaker="Speaker 1"))
    r = client.post(f"/api/sessions/{sid}/speakers/rename", headers=H,
                    json={"old": "Speaker 1", "new": "Alice"})
    assert r.status_code == 200
    assert r.json()["updated_count"] == 2


# --------------------------------------------------------------------------- #
# rediarize lifecycle
# --------------------------------------------------------------------------- #
def test_rediarize_conflict_during_capture(client):
    store = client.ai_store
    sid = store.create("rd").session_id
    client.ai_state.active_session_id = sid
    try:
        r = client.post(f"/api/sessions/{sid}/rediarize", headers=H)
        assert r.status_code == 409
    finally:
        client.ai_state.active_session_id = None


def test_rediarize_relabels_with_injected_pyannote(client, monkeypatch):
    store = client.ai_store
    sid = store.create("rd2").session_id
    store.append_utterance(_rec(store, sid, source="them", text="x", start=0.0))
    store.append_utterance(_rec(store, sid, source="them", text="y", start=2.0))
    store.finalize(sid)
    # audio_them.wav must exist for the tier-2 pass.
    w = WavWriter(str(store._dir(sid) / "audio_them.wav"))
    w.write(np.zeros(64000, dtype=np.float32))
    w.close()

    spans = [SpeakerSpan(0, 16000, "A"), SpeakerSpan(16000, 64000, "B")]
    monkeypatch.setattr(
        dz, "make_offline_diarizer",
        lambda s, sec: OfflineDiarizer(s, sec, diarize_fn=lambda p, t: spans),
    )
    r = client.post(f"/api/sessions/{sid}/rediarize", headers=H)
    assert r.status_code == 200
    assert r.json()["status"] == "started"
    client.ai_state._rediarize_threads[sid].join(5)

    st = client.get(f"/api/sessions/{sid}/rediarize/status", headers=H).json()
    assert st["state"] == "done"
    data = store.load_session(sid)
    them = [u for u in data.utterances if u.source == "them"]
    assert all(u.diarization_source == "offline" for u in them)
    assert {u.speaker for u in them} == {"Speaker A", "Speaker B"}


def test_rediarize_without_hf_token_reports_error(client):
    store = client.ai_store
    sid = store.create("rd3").session_id
    store.finalize(sid)
    client.ai_state.secrets.clear("hf_token")
    r = client.post(f"/api/sessions/{sid}/rediarize", headers=H)
    assert r.status_code == 200
    client.ai_state._rediarize_threads[sid].join(5)
    st = client.get(f"/api/sessions/{sid}/rediarize/status", headers=H).json()
    assert st["state"] == "error"
    assert "HF token" in st.get("error", "")


# --------------------------------------------------------------------------- #
# capture start: mode + sources (addendum §E1)
# --------------------------------------------------------------------------- #
def test_capture_start_passes_mode_and_sources(client, monkeypatch):
    state = client.ai_state
    state.settings = state.settings.update({"consent_acknowledged": True})
    calls: dict = {}

    def fake_start(st, title, mode="meeting", sources=None):
        calls.update(title=title, mode=mode, sources=sources)
        return "sid-1", {"you": True}

    monkeypatch.setattr("ai_record.server._start_capture", fake_start)
    r = client.post("/api/capture/start", headers=H,
                    json={"title": "note", "mode": "dictation", "sources": ["you"]})
    assert r.status_code == 200
    assert r.json()["session_id"] == "sid-1"
    assert calls["mode"] == "dictation"
    assert calls["sources"] == ["you"]


def test_capture_start_rejects_empty_sources(client):
    state = client.ai_state
    state.settings = state.settings.update({"consent_acknowledged": True})
    r = client.post("/api/capture/start", headers=H, json={"sources": ["bogus"]})
    assert r.status_code == 422


def test_store_persists_dictation_mode(client):
    sid = client.ai_store.create("dict", mode="dictation").session_id
    assert client.ai_store.load_session(sid).meta.mode == "dictation"
