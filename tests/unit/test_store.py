import contextlib
import json
import threading
import wave

import numpy as np
import pytest

from ai_record.store import (
    InvalidSessionId,
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


def test_patch_is_append_only_and_consolidated_on_finalize(store: SessionStore):
    """I4: patch_utterance appends to patches.jsonl (no O(N) transcript rewrite), is
    reflected on reads, and is consolidated into transcript.jsonl at finalize()."""
    sid = store.create("hot").session_id
    store.append_utterance(_rec(store, sid, text="a"))
    store.append_utterance(_rec(store, sid, text="b", start=2.0))

    store.patch_utterance(sid, 1, {"translation": "VI-a", "translation_provider": "nllb"})
    # append-only sidecar exists; the canonical transcript is NOT rewritten yet.
    assert (store._dir(sid) / "patches.jsonl").exists()
    assert "VI-a" not in store._jsonl(sid).read_text(encoding="utf-8")

    # reflected on both read paths (merge on read)
    data = store.load_session(sid)
    assert next(u for u in data.utterances if u.seq == 1).translation == "VI-a"
    since = store.utterances_since(sid, 0)
    assert next(u for u in since if u.seq == 1).translation == "VI-a"

    store.finalize(sid)
    # consolidated: sidecar removed, value now in the canonical transcript.
    assert not (store._dir(sid) / "patches.jsonl").exists()
    assert "VI-a" in store._jsonl(sid).read_text(encoding="utf-8")
    data2 = store.load_session(sid)
    assert next(u for u in data2.utterances if u.seq == 1).translation == "VI-a"


def test_patch_does_not_block_append_lock(store: SessionStore):
    """I4: patch_utterance must not need the STT transcript write lock — proven by
    patching successfully while that lock is held by another thread."""
    sid = store.create("noblock").session_id
    store.append_utterance(_rec(store, sid, text="a"))
    done = threading.Event()

    def _do_patch():
        store.patch_utterance(sid, 1, {"translation": "x"})
        done.set()

    with store._lock(sid).write():        # hold the transcript RWLock as STT would
        t = threading.Thread(target=_do_patch)
        t.start()
        assert done.wait(2.0), "patch_utterance blocked on the transcript write lock"
    t.join(2.0)


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
    pcm = tone(3.5)  # spans FOUR 1-second segments (000..003)
    rw.write(pcm, 0, 0)
    canonical = rw.close_and_concat()
    # canonical has a valid header
    with contextlib.closing(wave.open(canonical, "rb")) as wf:
        assert wf.getframerate() == 16000
        assert wf.getnchannels() == 1
        assert wf.getnframes() == pcm.size  # EXACT sample count, no tolerance
    back = read_wav_mono16k(canonical)
    assert back.size == pcm.size
    # four per-minute (here per-second) segment files were produced
    segs = sorted((store._dir(sess.session_id)).glob("audio_them.[0-9][0-9][0-9].wav"))
    assert len(segs) == 4
    # samples.idx sidecar recorded epoch + segments
    lines = (store._dir(sess.session_id) / "samples.idx").read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(x)["kind"] for x in lines]
    assert "epoch" in kinds and "segment" in kinds


def test_finalize_concats_all_segments_exact(store: SessionStore):
    """SessionStore.finalize() must concat EVERY segment, not just 000 (Critical #2)."""
    sess = store.create("m")
    sid = sess.session_id
    # Two independent writers whose segments must both be fully recovered.
    for source in ("you", "them"):
        rw = RawSegmentWriter(store._dir(sid), source, seconds=1)
        rw.mark_epoch(0, _now_iso(), 0)
        rw.write(tone(3.5, freq=200 if source == "you" else 300), 0, 0)
        rw.close()  # ownership: capture stop closes; finalize concatenates
    store.finalize(sid)
    for source in ("you", "them"):
        canonical = store._dir(sid) / f"audio_{source}.wav"
        assert canonical.exists()
        with contextlib.closing(wave.open(str(canonical), "rb")) as wf:
            assert wf.getnframes() == int(3.5 * 16000)  # exact total across 4 segments


def test_session_id_traversal_rejected(store: SessionStore):
    """Traversal / absolute ids are rejected before any fs access (Critical #1)."""
    bad_ids = ["..\\docs", "../../x", "..%5Cdocs", "/etc/passwd", "C:\\Windows",
               "20260101-000000-../../escape", "not-a-session", ""]
    for bad in bad_ids:
        with pytest.raises(InvalidSessionId):
            store._dir(bad)
        with pytest.raises(InvalidSessionId):
            store.load_session(bad)
        with pytest.raises(InvalidSessionId):
            store.delete_session(bad)
        with pytest.raises(InvalidSessionId):
            store.delete_audio_only(bad)


def test_session_id_traversal_cannot_escape_root(tmp_path):
    """A backslash-traversal id must not read or delete a sibling of the root."""
    root = tmp_path / "sessions"
    outside = tmp_path / "docs"
    outside.mkdir(parents=True)
    (outside / "secret.txt").write_text("classified", encoding="utf-8")
    store = SessionStore(root)
    with pytest.raises(InvalidSessionId):
        store.delete_session("..\\docs")
    # The sibling directory and its file survive untouched.
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "classified"


def test_valid_generated_session_id_accepted(store: SessionStore):
    sess = store.create("Weekly Sync!!")  # slug is sanitised to [a-z0-9-]
    # round-trips cleanly through the validator
    assert store._dir(sess.session_id).exists()


def test_detect_incomplete_and_retention(store: SessionStore):
    sess = store.create("open")
    assert any(m.session_id == sess.session_id for m in store.detect_incomplete())
    store.finalize(sess.session_id)
    assert all(m.session_id != sess.session_id for m in store.detect_incomplete())
