"""Integration: synthetic WAV → pipeline (MOCK transcriber) → store; plus recovery."""

from __future__ import annotations

import json

from ai_record.audio.capture import FileCaptureSource
from ai_record.audio.vad import FakeVad
from ai_record.config import Settings, resolve_preset, resolve_sessions_root
from ai_record.pipeline import Pipeline
from ai_record.store import RawSegmentWriter, SessionStore, _now_iso
from ai_record.transcriber import MockTranscriber
from tests.audio_helpers import sequence, silence, tone


def _settings(tmp_path):
    return Settings(hardware_preset="cpu", sessions_root=str(tmp_path / "sessions"),
                    diarization_realtime=False)


def _pipeline(settings, store, session, msgs):
    preset = resolve_preset(settings)
    tr = MockTranscriber(text_fn=lambda u: f"utt@{u.audio_start_sample}")
    return Pipeline(
        settings, preset, tr, store, session,
        broadcast=msgs.append,
        vad_factory=lambda: FakeVad(frame_samples=320, threshold=0.02),
    )


def test_stt_first_pipeline_writes_transcript(tmp_path):
    settings = _settings(tmp_path)
    store = SessionStore(resolve_sessions_root(settings), settings)
    session = store.create("integration")
    msgs: list[dict] = []
    pipe = _pipeline(settings, store, session, msgs)

    pipe.start()
    audio = sequence(silence(0.4), tone(1.0), silence(0.9), tone(1.0), silence(0.7))
    FileCaptureSource("them", audio).feed_into(pipe)
    assert pipe.wait_idle(timeout=15.0)
    pipe.stop()
    store.finalize(session.session_id)

    # STT-first: at least one utterance message, all of type utterance/status
    utt_msgs = [m for m in msgs if m.get("type") == "utterance"]
    assert len(utt_msgs) >= 2
    assert all("record" in m for m in utt_msgs)

    # transcript.jsonl + transcript.md written, schema 2
    data = store.load_session(session.session_id)
    assert len(data.utterances) >= 2
    assert all(u.schema == 2 for u in data.utterances)
    assert all(u.source == "them" for u in data.utterances)
    jsonl = store._jsonl(session.session_id).read_text(encoding="utf-8").strip().splitlines()
    for line in jsonl:
        rec = json.loads(line)
        assert rec["schema"] == 2
        assert rec["effective_model"] == "mock"
    md = store._md(session.session_id).read_text(encoding="utf-8")
    assert "utt@" in md


def test_incomplete_session_recovery(tmp_path):
    settings = _settings(tmp_path)
    store = SessionStore(resolve_sessions_root(settings), settings)
    session = store.create("crashy")
    sid = session.session_id

    # Simulate crash-safe raw audio with NO transcript yet, and no ended_at.
    rw = RawSegmentWriter(session.dir, "them", seconds=60)
    rw.mark_epoch(0, _now_iso(), 0)
    audio = sequence(silence(0.4), tone(1.0), silence(0.9), tone(1.0), silence(0.7))
    rw.write(audio, 0, 0)
    rw.close_and_concat()

    assert any(m.session_id == sid for m in store.detect_incomplete())

    tr = MockTranscriber(text="recovered")
    n = store.recover_offline(sid, tr, vad=FakeVad(frame_samples=320, threshold=0.02))
    assert n >= 2

    data = store.load_session(sid)
    assert len(data.utterances) == n
    assert data.meta.recovered is True
    assert data.meta.ended_at is not None
    assert all(m.session_id != sid for m in store.detect_incomplete())
