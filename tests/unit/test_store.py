import contextlib
import json
import wave

import numpy as np

from ai_record.store import (
    RawSegmentWriter,
    SessionStore,
    UtteranceRecord,
    read_wav_mono16k,
    _now_iso,
)
from tests.audio_helpers import tone


def _rec(store: SessionStore, sid: str, source="them", start=0.0, text="hello", speaker="Them") -> UtteranceRecord:
    seq = store.next_seq(sid)
    return UtteranceRecord(
        id=f"u_{seq:06d}",
        session_id=sid,
        seq=seq,
        source=source,
        speaker=speaker,
        start=start,
        end=start + 1.0,
        duration=1.0,
        text=text,
        lang="en",
        lang_prob=0.99,
        audio_start_sample=int(start * 16000),
        audio_end_sample=int((start + 1.0) * 16000),
        source_epoch_id=0,
        source_offset_sec=0.0,
        forced_cut=False,
        no_speech_prob=0.02,
        avg_logprob=-0.3,
        effective_model="mock",
        effective_compute_type="int8",
        stt_latency_ms=5,
        created_at=_now_iso(),
    )


def test_roundtrip_and_schema(store: SessionStore):
    sess = store.create("standup")
    sid = sess.session_id
    store.append_utterance(_rec(store, sid, text="one"))
    store.append_utterance(_rec(store, sid, text="two", start=2.0))
    data = store.load_session(sid)
    assert [u.text for u in data.utterances] == ["one", "two"]
    assert all(u.schema == 2 for u in data.utterances)
    # transcript.md rendered
    md = (store._md(sid)).read_text(encoding="utf-8")
    assert "one" in md and "two" in md


def test_patch_utterance_visible_on_read(store: SessionStore):
    sess = store.create("m")
    sid = sess.session_id
    store.append_utterance(_rec(store, sid, text="orig"))
    store.patch_utterance(sid, 1, {"translation": "dịch", "translation_provider": "nllb"})
    data = store.load_session(sid)
    assert data.utterances[0].translation == "dịch"
    assert data.utterances[0].translation_provider == "nllb"


def test_utterances_since(store: SessionStore):
    sess = store.create("m")
    sid = sess.session_id
    for i in range(3):
        store.append_utterance(_rec(store, sid, text=f"t{i}", start=float(i)))
    since = store.utterances_since(sid, 1)
    assert [u.seq for u in since] == [2, 3]


def test_rename_speaker_atomic(store: SessionStore):
    sess = store.create("m")
    sid = sess.session_id
    store.append_utterance(_rec(store, sid, speaker="Speaker 1"))
    store.append_utterance(_rec(store, sid, speaker="Speaker 1", start=2.0))
    n = store.rename_speaker(sid, "Speaker 1", "Tanaka")
    assert n == 2
    data = store.load_session(sid)
    assert all(u.speaker == "Tanaka" for u in data.utterances)
    assert data.meta.speakers.get("Speaker 1") == "Tanaka"


def test_finalize_sorts_by_start(store: SessionStore):
    sess = store.create("m")
    sid = sess.session_id
    store.append_utterance(_rec(store, sid, text="late", start=5.0))
    store.append_utterance(_rec(store, sid, text="early", start=1.0))
    store.finalize(sid)
    md = store._md(sid).read_text(encoding="utf-8")
    assert md.index("early") < md.index("late")
    meta = store.load_session(sid).meta
    assert meta.ended_at is not None


def test_schema1_migration(store: SessionStore):
    sess = store.create("m")
    sid = sess.session_id
    legacy = {
        "id": "u_000001", "session_id": sid, "seq": 1, "source": "them",
        "speaker": "Speaker 1", "start": 0.0, "end": 1.0, "duration": 1.0,
        "text": "legacy", "lang": "en", "lang_prob": 0.9,
        "no_speech_prob": 0.02, "avg_logprob": -0.3, "created_at": _now_iso(),
        "schema": 1,
    }
    store._jsonl(sid).write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    data = store.load_session(sid)
    u = data.utterances[0]
    assert u.schema == 2
    assert u.audio_start_sample is None
    assert u.forced_cut is False
    assert u.source_epoch_id == 0


def test_partial_trailing_line_tolerated(store: SessionStore):
    sess = store.create("m")
    sid = sess.session_id
    store.append_utterance(_rec(store, sid, text="good"))
    with store._jsonl(sid).open("a", encoding="utf-8") as fh:
        fh.write('{"id": "u_2", "seq": 2, partial')  # truncated crash line
    data = store.load_session(sid)
    assert [u.text for u in data.utterances] == ["good"]


def test_raw_segment_writer_valid_wav_and_concat(store: SessionStore):
    sess = store.create("m")
    rw = RawSegmentWriter(sess.dir, "them", seconds=1)
    rw.mark_epoch(0, _now_iso(), 0)
    pcm = tone(2.5)  # spans multiple 1-second segments
    rw.write(pcm, 0, 0)
    canonical = rw.close_and_concat()
    # canonical has a valid header and correct-ish length
    with contextlib.closing(wave.open(canonical, "rb")) as wf:
        assert wf.getframerate() == 16000
        assert wf.getnchannels() == 1
        assert wf.getnframes() > 0
    back = read_wav_mono16k(canonical)
    assert abs(back.size - pcm.size) <= 16000  # within a segment of the source
    # samples.idx sidecar recorded epoch + segments
    lines = (store._dir(sess.session_id) / "samples.idx").read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(x)["kind"] for x in lines]
    assert "epoch" in kinds and "segment" in kinds


def test_detect_incomplete_and_retention(store: SessionStore):
    sess = store.create("open")
    assert any(m.session_id == sess.session_id for m in store.detect_incomplete())
    store.finalize(sess.session_id)
    assert all(m.session_id != sess.session_id for m in store.detect_incomplete())
