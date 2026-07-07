import contextlib
import json
import threading
import wave

import numpy as np
import pytest

from ai_record.config import Settings, resolve_sessions_root
from ai_record.store import (
    InvalidSessionId,
    RawSegmentWriter,
    SessionStore,
    UtteranceRecord,
    WavWriter,
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


def test_finalize_concats_all_segments_exact(store: SessionStore, monkeypatch):
    """SessionStore.finalize() must concat EVERY segment, not just 000 (Critical #2)."""
    import shutil

    # Keep the audio so the concatenated canonical WAV survives finalize for inspection
    # (default finalize now deletes audio unless keep_audio is set — Feature 2).
    # ffmpeg absent → the merge step is skipped and the per-source canonical WAVs are
    # left in place, which is exactly what this test inspects.
    monkeypatch.setattr(shutil, "which", lambda name: None)
    store.settings.keep_audio = True
    store.settings.audio_export_format = "wav"
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


# --------------------------------------------------------------------------- #
# finalize output artefacts (Feature 2)
# --------------------------------------------------------------------------- #
def _store_with(tmp_path, **overrides) -> SessionStore:
    settings = Settings(sessions_root=str(tmp_path / "s"), **overrides)
    return SessionStore(resolve_sessions_root(settings), settings)


def test_finalize_save_txt_writes_plain_transcript(tmp_path):
    store = _store_with(tmp_path, save_txt=True)
    sid = store.create("txt").session_id
    store.append_utterance(_rec(store, sid, text="hello world", speaker="Alice"))
    store.finalize(sid)
    txt = store._dir(sid) / "transcript.txt"
    assert txt.exists()
    body = txt.read_text(encoding="utf-8")
    assert "hello world" in body
    assert "**" not in body  # plain text, no markdown markers


def test_finalize_without_save_txt_omits_txt(tmp_path):
    store = _store_with(tmp_path, save_txt=False)
    sid = store.create("notxt").session_id
    store.append_utterance(_rec(store, sid, text="hi"))
    store.finalize(sid)
    assert not (store._dir(sid) / "transcript.txt").exists()


def test_finalize_deletes_audio_when_not_kept(tmp_path):
    store = _store_with(tmp_path, keep_audio=False)
    sid = store.create("noaudio").session_id
    d = store._dir(sid)
    w = WavWriter(str(d / "audio_them.wav"))
    w.write(np.zeros(16000, dtype=np.float32))
    w.close()
    (d / "samples.idx").write_text('{"kind":"segment"}\n', encoding="utf-8")
    store.finalize(sid)
    assert not (d / "audio_them.wav").exists()
    assert not (d / "samples.idx").exists()


def test_finalize_transcodes_mp3_with_mocked_ffmpeg(tmp_path, monkeypatch):
    import shutil
    import subprocess

    store = _store_with(tmp_path, keep_audio=True, audio_export_format="mp3")
    sid = store.create("mp3").session_id
    d = store._dir(sid)
    w = WavWriter(str(d / "audio_you.wav"))
    w.write(np.zeros(16000, dtype=np.float32))
    w.close()

    monkeypatch.setattr(shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        out = cmd[-1]  # ffmpeg output path is the last arg
        with open(out, "wb") as f:
            f.write(b"ID3fake-mp3-bytes")
        return _Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    store.finalize(sid)
    # Merged into a single combined audio.mp3 (per-source wavs removed on success).
    assert (d / "audio.mp3").exists()
    assert not (d / "audio_you.wav").exists()


def test_finalize_keeps_wav_when_ffmpeg_missing(tmp_path, monkeypatch):
    import shutil

    store = _store_with(tmp_path, keep_audio=True, audio_export_format="mp3")
    sid = store.create("noffmpeg").session_id
    d = store._dir(sid)
    w = WavWriter(str(d / "audio_you.wav"))
    w.write(np.zeros(16000, dtype=np.float32))
    w.close()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    store.finalize(sid)
    # ffmpeg absent → keep the wav, no mp3 produced.
    assert (d / "audio_you.wav").exists()
    assert not (d / "audio_you.mp3").exists()


def test_finalize_merges_wav_when_format_wav(tmp_path, monkeypatch):
    import shutil
    import subprocess

    store = _store_with(tmp_path, keep_audio=True, audio_export_format="wav")
    sid = store.create("wav").session_id
    d = store._dir(sid)
    w = WavWriter(str(d / "audio_them.wav"))
    w.write(np.zeros(16000, dtype=np.float32))
    w.close()

    monkeypatch.setattr(shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        with open(cmd[-1], "wb") as f:  # ffmpeg output path = last arg
            f.write(b"RIFFfake-wav-bytes")
        return _Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    store.finalize(sid)
    # Merged into a single combined audio.wav; the per-source wav is removed on success.
    assert (d / "audio.wav").exists()
    assert not (d / "audio_them.wav").exists()


# --------------------------------------------------------------------------- #
# finalize driven by output_formats multi-select (new feature)
# --------------------------------------------------------------------------- #
def test_finalize_output_formats_txt_writes_transcript_txt(tmp_path):
    store = _store_with(tmp_path, output_formats=["md", "txt"])
    sid = store.create("of-txt").session_id
    store.append_utterance(_rec(store, sid, text="alpha beta", speaker="Alice"))
    store.finalize(sid)
    txt = store._dir(sid) / "transcript.txt"
    assert txt.exists()
    assert "alpha beta" in txt.read_text(encoding="utf-8")


def test_finalize_output_formats_md_only_removes_wav(tmp_path):
    store = _store_with(tmp_path, output_formats=["md"])
    sid = store.create("of-md").session_id
    d = store._dir(sid)
    w = WavWriter(str(d / "audio_them.wav"))
    w.write(np.zeros(16000, dtype=np.float32))
    w.close()
    (d / "samples.idx").write_text('{"kind":"segment"}\n', encoding="utf-8")
    store.finalize(sid)
    assert not (d / "audio_them.wav").exists()
    assert not (d / "samples.idx").exists()
    assert not (store._dir(sid) / "transcript.txt").exists()


def test_finalize_output_formats_mp3_transcodes_and_removes_wav(tmp_path, monkeypatch):
    import shutil
    import subprocess

    store = _store_with(tmp_path, output_formats=["md", "mp3"])
    sid = store.create("of-mp3").session_id
    d = store._dir(sid)
    w = WavWriter(str(d / "audio_you.wav"))
    w.write(np.zeros(16000, dtype=np.float32))
    w.close()

    monkeypatch.setattr(shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)

    class _Result:
        returncode = 0

    def fake_run(cmd, **kwargs):
        with open(cmd[-1], "wb") as f:  # ffmpeg output path = last arg
            f.write(b"ID3fake-mp3-bytes")
        return _Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    store.finalize(sid)
    assert (d / "audio.mp3").exists()  # single merged file
    assert not (d / "audio_you.wav").exists()


def test_finalize_legacy_save_txt_still_writes_txt(tmp_path):
    # Backward-compat: legacy boolean is OR-ed with output_formats.
    store = _store_with(tmp_path, output_formats=["md"], save_txt=True)
    sid = store.create("legacy-txt").session_id
    store.append_utterance(_rec(store, sid, text="legacy line"))
    store.finalize(sid)
    txt = store._dir(sid) / "transcript.txt"
    assert txt.exists()
    assert "legacy line" in txt.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# merged audio (Feature 2) + video mux (Feature 3) — argv shapes, ffmpeg mocked
# --------------------------------------------------------------------------- #
def _mock_ffmpeg(monkeypatch, *, mux_returncode: int = 0) -> list[list[str]]:
    """Mock shutil.which + subprocess.run; return the list of argv seen. ffmpeg
    outputs (last arg) are created non-empty so success paths fire. Mux commands
    (identified by ``-map``) can be forced to fail via ``mux_returncode``."""
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda name: "ffmpeg" if name == "ffmpeg" else None)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        is_mux = "-map" in cmd
        rc = mux_returncode if is_mux else 0
        if rc == 0:
            with open(cmd[-1], "wb") as f:
                f.write(b"x")  # non-empty output so the success guard passes
        class _R:
            returncode = rc
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _wav(d, name, samples=16000):
    w = WavWriter(str(d / name))
    w.write(np.zeros(samples, dtype=np.float32))
    w.close()


def test_merge_two_sources_uses_single_amix_command(tmp_path, monkeypatch):
    store = _store_with(tmp_path, output_formats=["md", "mp3"])
    sid = store.create("mix2").session_id
    d = store._dir(sid)
    _wav(d, "audio_you.wav")
    _wav(d, "audio_them.wav")
    calls = _mock_ffmpeg(monkeypatch)
    store.finalize(sid)
    merges = [c for c in calls if "-map" not in c]
    assert len(merges) == 1                     # ONE combined command, not per-source
    argv = merges[0]
    assert "-filter_complex" in argv
    assert argv[argv.index("-filter_complex") + 1] == "amix=inputs=2:normalize=0"
    assert argv.count("-i") == 2                 # both sources fed in
    assert argv[-1].endswith("audio.mp3")        # single merged output
    assert (d / "audio.mp3").exists()
    assert not (d / "audio_you.wav").exists()
    assert not (d / "audio_them.wav").exists()


def test_merge_single_source_has_no_amix(tmp_path, monkeypatch):
    store = _store_with(tmp_path, output_formats=["md", "mp3"])
    sid = store.create("mix1").session_id
    d = store._dir(sid)
    _wav(d, "audio_you.wav")  # only one source present
    calls = _mock_ffmpeg(monkeypatch)
    store.finalize(sid)
    merges = [c for c in calls if "-map" not in c]
    assert len(merges) == 1
    argv = merges[0]
    assert "-filter_complex" not in argv         # single input → no amix
    assert argv.count("-i") == 1
    assert argv[-1].endswith("audio.mp3")


def test_merge_ephemeral_writes_and_spawns_nothing(tmp_path, monkeypatch):
    store = _store_with(tmp_path, output_formats=["md", "mp3"])
    sess = store.create("nope", persist=False)   # ephemeral ("Không lưu")
    calls = _mock_ffmpeg(monkeypatch)
    store.finalize(sess.session_id)
    assert calls == []                            # no ffmpeg spawned at all
    assert not (store.root / sess.session_id).exists()  # nothing on disk


def test_video_mux_maps_streams_and_outputs_mp4(tmp_path, monkeypatch):
    store = _store_with(tmp_path, output_formats=["md", "mp3"], video_mux_audio=True)
    sid = store.create("vid").session_id
    d = store._dir(sid)
    _wav(d, "audio_you.wav")
    (d / "screen.mkv").write_bytes(b"fake-mkv-video-bytes")
    store.set_meta_fields(sid, {"video": {"container": "mkv", "screen": {"mode": "full"}}})
    calls = _mock_ffmpeg(monkeypatch)
    store.finalize(sid)
    mux = [c for c in calls if "-map" in c]
    assert len(mux) == 1
    argv = mux[0]
    assert "0:v:0" in argv and "1:a:0" in argv
    assert argv[argv.index("-c:v") + 1] == "copy"
    assert "-shortest" in argv
    assert argv[-1].endswith(".mp4")
    assert (d / "screen.mp4").exists()
    assert not (d / "screen.mkv").exists()        # silent source replaced


def test_video_muxed_even_without_audio_output_format(tmp_path, monkeypatch):
    # User rule: a saved video must ALWAYS carry the recorded sound — even at the md-only
    # default (no mp3/wav requested). The merged audio is produced for the mux, then the
    # standalone audio file is removed (only the video keeps its audio track).
    store = _store_with(tmp_path, output_formats=["md"], video_mux_audio=True)
    sid = store.create("vid").session_id
    d = store._dir(sid)
    _wav(d, "audio_you.wav")
    _wav(d, "audio_them.wav")
    (d / "screen.mkv").write_bytes(b"fake-mkv-video-bytes")
    store.set_meta_fields(sid, {"video": {"container": "mkv", "screen": {"mode": "full"}}})
    calls = _mock_ffmpeg(monkeypatch)
    store.finalize(sid)
    assert any("-map" in c for c in calls)               # video WAS muxed with audio
    assert (d / "screen.mp4").exists()
    assert not (d / "screen.mkv").exists()
    assert not (d / "audio.mp3").exists()                # standalone audio not kept (md-only)
    assert not list(d.glob("*.wav"))                     # raw wavs cleaned


def test_video_mux_disabled_skips_mux(tmp_path, monkeypatch):
    store = _store_with(tmp_path, output_formats=["md", "mp3"], video_mux_audio=False)
    sid = store.create("nomux").session_id
    d = store._dir(sid)
    _wav(d, "audio_you.wav")
    (d / "screen.mkv").write_bytes(b"fake-mkv-video-bytes")
    store.set_meta_fields(sid, {"video": {"container": "mkv", "screen": {"mode": "full"}}})
    calls = _mock_ffmpeg(monkeypatch)
    store.finalize(sid)
    assert [c for c in calls if "-map" in c] == []  # no mux command
    assert (d / "screen.mkv").exists()               # silent video untouched
    assert not (d / "screen.mp4").exists()


def test_video_mux_failure_keeps_original_video(tmp_path, monkeypatch):
    store = _store_with(tmp_path, output_formats=["md", "mp3"], video_mux_audio=True)
    sid = store.create("muxfail").session_id
    d = store._dir(sid)
    _wav(d, "audio_you.wav")
    (d / "screen.mkv").write_bytes(b"fake-mkv-video-bytes")
    store.set_meta_fields(sid, {"video": {"container": "mkv", "screen": {"mode": "full"}}})
    calls = _mock_ffmpeg(monkeypatch, mux_returncode=1)  # mux fails
    store.finalize(sid)
    assert [c for c in calls if "-map" in c]         # a mux was attempted
    assert (d / "screen.mkv").exists()               # original silent video preserved
    assert not (d / "screen.mp4").exists()
