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
    # Now returns (primary, alt, is_overlap) tuples (review I5).
    assert labels[1][0] == "Speaker A"   # first appearance → A
    assert labels[2][0] == "Speaker B"
    assert labels[1][2] is False         # a 1000-sample sliver of B is not overlap
    assert labels[1][1] is None
    assert 3 not in labels               # "you" is never relabeled


def test_relabel_marks_overlap_and_preserves_alt():
    """A `them` utterance genuinely straddling two speakers keeps a secondary candidate
    and is flagged is_overlap — driven by real sample-time overlap, not a canned winner."""
    # utterance 0..20000; A owns 0..11000, B owns 9000..20000 → both substantial.
    records = [_rec(1, "them", 0, 20000)]
    spans = [SpeakerSpan(0, 11000, "RAW_A"), SpeakerSpan(9000, 20000, "RAW_B")]
    primary, alt, is_overlap = relabel_them_utterances(records, spans)[1]
    assert primary in ("Speaker A", "Speaker B")
    assert alt is not None and alt != primary
    assert is_overlap is True


def test_relabel_ignores_utterances_without_span_overlap():
    records = [_rec(1, "them", 100000, 110000)]
    spans = [SpeakerSpan(0, 1000, "X")]
    assert relabel_them_utterances(records, spans) == {}


def test_relabel_supports_more_than_26_speakers():
    """`Speaker A..Z` then `Speaker AA` — no `[ \\ ]` past 26 (review nit)."""
    records = [_rec(i + 1, "them", i * 1000, i * 1000 + 1000) for i in range(28)]
    spans = [SpeakerSpan(i * 1000, i * 1000 + 1000, f"RAW_{i}") for i in range(28)]
    labels = relabel_them_utterances(records, spans)
    assert labels[26][0] == "Speaker Z"    # 26th distinct
    assert labels[27][0] == "Speaker AA"   # 27th distinct
    assert labels[28][0] == "Speaker AB"


def test_rediarize_persists_primary_alt_and_overlap(tmp_path):
    """End-to-end (store): sample-time overlap drives the labels, and the secondary
    candidate + overlap flag are persisted by rewrite_after_rediarize (review I5)."""
    from ai_record.config import Settings, resolve_sessions_root
    from ai_record.store import SessionStore
    from tests.unit.test_store import _rec as store_rec

    settings = Settings(hardware_preset="cpu", sessions_root=str(tmp_path / "s"))
    store = SessionStore(resolve_sessions_root(settings), settings)
    sid = store.create("ov").session_id
    rec = store_rec(store, sid, source="them", start=0.0)
    rec.audio_start_sample = 0          # utterance straddles two speakers on the timeline
    rec.audio_end_sample = 20000
    store.append_utterance(rec)
    store.finalize(sid)

    records = store.load_session(sid).utterances
    spans = [SpeakerSpan(0, 11000, "RAW_A"), SpeakerSpan(9000, 20000, "RAW_B")]
    labels = relabel_them_utterances(records, spans)
    store.rewrite_after_rediarize(sid, labels)

    them = store.load_session(sid).utterances[0]
    assert them.diarization_source == "offline"
    assert them.is_overlap is True
    assert them.speaker_alt is not None and them.speaker_alt != them.speaker


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
