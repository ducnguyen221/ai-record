"""Source-epoch propagation (Important #4) and audio-only STT gating (Important #2)."""

from __future__ import annotations

from ai_record.audio.capture import FileCaptureSource
from ai_record.audio.segmenter import Segmenter, SourceEpoch
from ai_record.audio.vad import FakeVad
from ai_record.config import LadderStep, Settings, resolve_preset, resolve_sessions_root
from ai_record.pipeline import Pipeline
from ai_record.store import SessionStore
from ai_record.transcriber import MockTranscriber
from tests.audio_helpers import sequence, silence, tone


def _settings(tmp_path):
    return Settings(hardware_preset="cpu", sessions_root=str(tmp_path / "sessions"),
                    diarization_realtime=False)


def test_segmenter_stamps_current_epoch_after_bump():
    s = Settings(hardware_preset="cpu")
    holder = SourceEpoch(epoch_id=0)
    seg = Segmenter("them", s, FakeVad(frame_samples=320, threshold=0.02), epoch_state=holder)

    speech = sequence(silence(0.4), tone(1.0), silence(0.9))
    first = seg.run_array(speech, start_sample=0)
    assert first and all(u.source_epoch_id == 0 for u in first)

    # Simulate a device reopen: capture bumps the shared holder.
    holder.epoch_id = 1
    later = seg.run_array(speech, start_sample=len(speech))
    assert later and all(u.source_epoch_id == 1 for u in later)


def test_audio_only_mode_stops_live_stt_feeding(tmp_path):
    settings = _settings(tmp_path)
    store = SessionStore(resolve_sessions_root(settings), settings)
    session = store.create("ao")
    pipe = Pipeline(
        settings, resolve_preset(settings), MockTranscriber(), store, session,
        sources=("them",),
        vad_factory=lambda: FakeVad(frame_samples=320, threshold=0.02),
    )
    # Force ladder to AUDIO_ONLY before feeding: no utterance should be transcribed.
    pipe.ladder.step = LadderStep.AUDIO_ONLY
    pipe._audio_only.set()
    pipe.start()
    audio = sequence(silence(0.4), tone(1.0), silence(0.9), tone(1.0), silence(0.7))
    FileCaptureSource("them", audio).feed_into(pipe)
    assert pipe.wait_idle(timeout=15.0)
    pipe.stop()
    assert pipe._utterance_count == 0
    assert len(store.load_session(session.session_id).utterances) == 0


def test_live_stt_resumes_when_not_audio_only(tmp_path):
    settings = _settings(tmp_path)
    store = SessionStore(resolve_sessions_root(settings), settings)
    session = store.create("live")
    pipe = Pipeline(
        settings, resolve_preset(settings), MockTranscriber(), store, session,
        sources=("them",),
        vad_factory=lambda: FakeVad(frame_samples=320, threshold=0.02),
    )
    pipe.start()  # ladder NONE, audio_only clear
    audio = sequence(silence(0.4), tone(1.0), silence(0.9), tone(1.0), silence(0.7))
    FileCaptureSource("them", audio).feed_into(pipe)
    assert pipe.wait_idle(timeout=15.0)
    pipe.stop()
    assert pipe._utterance_count >= 2
