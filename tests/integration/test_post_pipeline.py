"""Integration: STT-first pipeline delivers translation + speaker as late `patch`es."""

from __future__ import annotations

import numpy as np

from ai_record.audio.capture import FileCaptureSource
from ai_record.audio.vad import FakeVad
from ai_record.config import Settings, resolve_preset, resolve_sessions_root
from ai_record.diarizer import Assignment
from ai_record.pipeline import Pipeline
from ai_record.store import SessionStore
from ai_record.transcriber import MockTranscriber
from tests.audio_helpers import sequence, silence, tone


class FakeTranslator:
    name = "faketr"

    def is_supported(self, src, tgt):
        return True

    def available(self):
        return True

    def translate(self, text, src_lang, tgt_lang="vi"):
        return "VI:" + text


class FakeDiarizer:
    def label(self, utt, *, is_overlap=False):
        return Assignment(speaker="Speaker 1", confidence=0.9)


def _settings(tmp_path):
    return Settings(
        hardware_preset="cpu",
        sessions_root=str(tmp_path / "sessions"),
        translate_enabled=True,
        source_languages=[],
        translate_min_duration_s=0.3,
        diarization_enabled=True,
        diarization_realtime=True,
    )


def test_translation_and_speaker_patches(tmp_path):
    settings = _settings(tmp_path)
    store = SessionStore(resolve_sessions_root(settings), settings)
    session = store.create("post")
    msgs: list[dict] = []
    preset = resolve_preset(settings)
    tr = MockTranscriber(text_fn=lambda u: f"utt@{u.audio_start_sample}", lang="en")
    pipe = Pipeline(
        settings, preset, tr, store, session,
        broadcast=msgs.append,
        vad_factory=lambda: FakeVad(frame_samples=320, threshold=0.02),
        translator=FakeTranslator(),
        diarizer=FakeDiarizer(),
    )

    pipe.start()
    audio = sequence(silence(0.4), tone(1.0), silence(0.9), tone(1.0), silence(0.7))
    FileCaptureSource("them", audio).feed_into(pipe)
    assert pipe.wait_idle(timeout=15.0)
    pipe.stop()
    store.finalize(session.session_id)

    utt_msgs = [m for m in msgs if m.get("type") == "utterance"]
    patch_msgs = [m for m in msgs if m.get("type") == "patch"]
    assert utt_msgs, "STT utterances must be emitted"
    assert patch_msgs, "translation/speaker patches must be emitted"

    # STT-first ordering: the first utterance is broadcast before the first patch.
    first_utt = next(i for i, m in enumerate(msgs) if m.get("type") == "utterance")
    first_patch = next(i for i, m in enumerate(msgs) if m.get("type") == "patch")
    assert first_utt < first_patch

    # Patches carry flat fields keyed by seq (addendum §E4).
    assert all("seq" in m for m in patch_msgs)

    # Store reflects the late translation + speaker updates.
    data = store.load_session(session.session_id)
    assert any(u.translation and u.translation.startswith("VI:") for u in data.utterances)
    assert any(u.translation_provider == "faketr" for u in data.utterances)
    assert any(u.speaker == "Speaker 1" for u in data.utterances)
    assert all(u.diarization_source in ("realtime", "none") for u in data.utterances)


def test_no_translation_when_disabled(tmp_path):
    settings = _settings(tmp_path)
    settings = settings.update({"translate_enabled": False})
    store = SessionStore(resolve_sessions_root(settings), settings)
    session = store.create("post2")
    msgs: list[dict] = []
    preset = resolve_preset(settings)
    tr = MockTranscriber(text_fn=lambda u: f"utt@{u.audio_start_sample}", lang="en")
    pipe = Pipeline(
        settings, preset, tr, store, session,
        broadcast=msgs.append,
        vad_factory=lambda: FakeVad(frame_samples=320, threshold=0.02),
        translator=FakeTranslator(),
        diarizer=FakeDiarizer(),
    )
    pipe.start()
    audio = sequence(silence(0.4), tone(1.0), silence(0.9), tone(1.0), silence(0.7))
    FileCaptureSource("them", audio).feed_into(pipe)
    assert pipe.wait_idle(timeout=15.0)
    pipe.stop()
    store.finalize(session.session_id)

    data = store.load_session(session.session_id)
    # translation disabled → no translations, but speaker patches still applied.
    assert all(u.translation is None for u in data.utterances)
    assert any(u.speaker == "Speaker 1" for u in data.utterances)
