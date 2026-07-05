"""Transcriber OOM handling + ladder compute-downgrade (no torch / faster-whisper)."""

from __future__ import annotations

import numpy as np

from ai_record.config import LadderStep, Settings, resolve_preset
from ai_record.audio.segmenter import Utterance
from ai_record.transcriber import Transcriber


def _utt() -> Utterance:
    return Utterance(
        source="them",
        pcm=(0.3 * np.sin(np.linspace(0, 20, 16000))).astype(np.float32),
        start=0.0,
        end=1.0,
        audio_start_sample=0,
        audio_end_sample=16000,
        source_epoch_id=0,
        source_offset_sec=0.0,
        forced_cut=False,
    )


class _Seg:
    def __init__(self, text="hello world"):
        self.text = text
        self.avg_logprob = -0.3
        self.no_speech_prob = 0.02


class _Info:
    language = "en"
    language_probability = 0.98


class _FakeModel:
    """Whisper stand-in: raises CUDA OOM ``fail_times`` times, then succeeds."""

    def __init__(self, fail_times: int = 0, text: str = "hello world"):
        self.fail_times = fail_times
        self.calls = 0
        self._text = text

    def transcribe(self, pcm, **kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("CUDA failed with error out of memory")
        return iter([_Seg(self._text)]), _Info()


def _transcriber(settings=None):
    settings = settings or Settings(hardware_preset="cpu", min_rms=0.0)
    t = Transcriber(settings, resolve_preset(settings))
    # Force a GPU-shaped ladder so a downgrade path exists.
    t._model_name, t._compute_type, t._device = "large-v3", "int8_float16", "cuda"
    return t


def test_oom_downgrades_then_retries_same_utterance():
    statuses: list[dict] = []
    t = _transcriber()
    t.on_status = statuses.append
    first = _FakeModel(fail_times=1)   # OOM on first call
    second = _FakeModel(fail_times=0)  # smaller model succeeds
    t._model = first
    seq = iter([second])
    t._new_model = lambda *a, **k: next(seq)

    result = t.transcribe(_utt())
    assert result is not None
    assert result.text == "hello world"
    # downgraded off large-v3, retried on the smaller model, and emitted a status.
    assert t._model_name != "large-v3"
    assert second.calls == 1
    assert any("degraded" in s.get("note", "") for s in statuses)
    assert t.pending_recovery == []


def test_persistent_oom_queues_for_recovery_not_dropped():
    recovered: list[Utterance] = []
    statuses: list[dict] = []
    t = _transcriber()
    t.on_status = statuses.append
    t.on_recover = recovered.append
    t._model = _FakeModel(fail_times=99)          # keeps failing
    t._new_model = lambda *a, **k: _FakeModel(fail_times=99)

    u = _utt()
    result = t.transcribe(u)
    assert result is None                          # not returned...
    assert recovered == [u]                        # ...but NOT silently dropped
    assert t.pending_recovery == [u]
    assert any("recover_queued" in s.get("note", "") for s in statuses)


def test_ladder_downgrades_compute_before_model_size():
    settings = Settings(hardware_preset="cpu")
    t = Transcriber(settings, resolve_preset(settings))
    t._model_name, t._compute_type = "large-v3", "float16"  # gpu_16gb_plus shape
    t.apply_ladder_step(LadderStep.WHISPER_INT8_FLOAT16)
    assert (t._model_name, t._compute_type) == ("large-v3", "int8_float16")  # compute first
    t.apply_ladder_step(LadderStep.WHISPER_MEDIUM)
    assert t._model_name == "medium"
