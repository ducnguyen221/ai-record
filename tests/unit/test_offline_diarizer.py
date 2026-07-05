"""M4 offline diarizer: overlap-majority relabel + HF-token gating + injected fn."""

from __future__ import annotations

import types

import numpy as np
import pytest

from ai_record.config import Secrets, Settings
from ai_record.diarizer import (
    HfTokenRequired,
    OfflineDiarizer,
    SpeakerSpan,
    relabel_them_utterances,
)
from ai_record.store import WavWriter


def _rec(seq, source, a, b):
    return types.SimpleNamespace(
        seq=seq, source=source, audio_start_sample=a, audio_end_sample=b
    )


def test_relabel_overlap_weighted_majority():
    records = [
        _rec(1, "them", 0, 16000),
        _rec(2, "them", 32000, 48000),
        _rec(3, "you", 0, 16000),
    ]
    spans = [
        SpeakerSpan(0, 15000, "SPK_RAW_A"),
        SpeakerSpan(15000, 64000, "SPK_RAW_B"),
    ]
    labels = relabel_them_utterances(records, spans)
    assert labels[1] == "Speaker A"   # first appearance → A
    assert labels[2] == "Speaker B"
    assert 3 not in labels            # "you" is never relabeled


def test_relabel_ignores_utterances_without_span_overlap():
    records = [_rec(1, "them", 100000, 110000)]
    spans = [SpeakerSpan(0, 1000, "X")]
    assert relabel_them_utterances(records, spans) == {}


def test_offline_requires_hf_token():
    sec = Secrets()
    sec.clear("hf_token")
    d = OfflineDiarizer(Settings(hardware_preset="cpu"), sec)
    ok, why = d.available()
    assert not ok
    assert "HF token" in why
    with pytest.raises(HfTokenRequired):
        d.rediarize("/does/not/exist")


def test_offline_with_injected_fn(tmp_path):
    wav = tmp_path / "audio_them.wav"
    w = WavWriter(str(wav))
    w.write(np.zeros(1600, dtype=np.float32))
    w.close()
    spans = [SpeakerSpan(0, 1600, "X")]
    d = OfflineDiarizer(
        Settings(hardware_preset="cpu"), diarize_fn=lambda path, token: spans
    )
    ok, _ = d.available()
    assert ok
    assert d.rediarize(str(tmp_path)) == spans
