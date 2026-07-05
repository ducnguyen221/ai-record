"""M3 realtime diarizer: clustering, confidence, unknown, overflow, rename."""

from __future__ import annotations

import math

import numpy as np
import pytest

from ai_record.config import Settings, resolve_preset
from ai_record.diarizer import UNKNOWN, RealtimeDiarizer
from ai_record.audio.segmenter import Utterance


class FakeEmbedder:
    """Returns caller-controlled vectors in call order (never touches a model)."""

    def __init__(self, vectors) -> None:
        self.vectors = [np.asarray(v, dtype=np.float32) for v in vectors]
        self.i = 0

    def embed(self, pcm: np.ndarray) -> np.ndarray:
        v = self.vectors[self.i]
        self.i += 1
        return v


def _diar(vectors=None, **skw) -> RealtimeDiarizer:
    s = Settings(hardware_preset="cpu", **skw)
    return RealtimeDiarizer(s, resolve_preset(s), embedder=FakeEmbedder(vectors or []))


def _utt(duration: float = 1.0, source: str = "them") -> Utterance:
    n = int(duration * 16000)
    return Utterance(
        source=source,
        pcm=np.zeros(n, dtype=np.float32),
        start=0.0,
        end=duration,
        audio_start_sample=0,
        audio_end_sample=n,
        source_epoch_id=0,
        source_offset_sec=0.0,
        forced_cut=False,
    )


def test_mic_is_always_you():
    d = _diar()
    a = d.label(_utt(source="you"))
    assert a.speaker == "You"
    assert a.confidence is None


def test_short_utterance_is_unknown_no_centroid():
    d = _diar([[1, 0, 0]])
    a = d.label(_utt(duration=0.5))  # 500 ms < min_embed_ms (800)
    assert a.speaker == UNKNOWN
    assert d.centroids == {}


def test_identical_vectors_cluster_together():
    d = _diar([[1, 0, 0], [1, 0, 0]])
    a1 = d.label(_utt())
    a2 = d.label(_utt())
    assert a1.speaker == "Speaker 1"
    assert a2.speaker == "Speaker 1"


def test_distant_vector_makes_new_speaker():
    d = _diar([[1, 0, 0], [0, 1, 0]])
    assert d.label(_utt()).speaker == "Speaker 1"
    assert d.label(_utt()).speaker == "Speaker 2"


def test_model_specific_threshold_boundary():
    above = [0.75, math.sqrt(1 - 0.75 ** 2), 0.0]   # cos 0.75 ≥ 0.70 → same
    d = _diar([[1, 0, 0], above])
    d.label(_utt())
    assert d.label(_utt()).speaker == "Speaker 1"

    below = [0.60, math.sqrt(1 - 0.60 ** 2), 0.0]   # cos 0.60 < 0.70 → new
    d2 = _diar([[1, 0, 0], below])
    d2.label(_utt())
    assert d2.label(_utt()).speaker == "Speaker 2"


def test_overlap_is_unknown_and_never_updates_centroid():
    d = _diar([[1, 0, 0]], min_speaker_speech_s=1.0)
    d.label(_utt(duration=2.0))  # Speaker 1, trusted
    count_before = d.centroids["Speaker 1"].count
    a = d.label(_utt(duration=2.0), is_overlap=True)
    assert a.speaker == UNKNOWN
    assert a.is_overlap is True
    assert d.centroids["Speaker 1"].count == count_before  # no embed, no update


def test_max_speakers_overflow_is_unknown_not_forced():
    d = _diar([[1, 0, 0], [0, 1, 0], [0, 0, 1]], max_speakers=2)
    d.label(_utt())
    d.label(_utt())
    a = d.label(_utt())  # third distinct → cap hit
    assert a.speaker == UNKNOWN
    assert a.forced_overflow is True


def test_rename_propagates_to_future_matches():
    d = _diar([[1, 0, 0], [1, 0, 0]])
    d.label(_utt())
    d.rename("Speaker 1", "Alice")
    assert d.label(_utt()).speaker == "Alice"


def test_confidence_populated_for_trusted_centroid():
    d = _diar([[1, 0, 0], [1, 0, 0]], min_speaker_speech_s=1.0)
    d.label(_utt(duration=2.0))          # trusted at creation (2.0 ≥ 1.0)
    a = d.label(_utt(duration=2.0))
    assert a.confidence is not None
    assert a.confidence > 0.5            # trusted → not capped low
