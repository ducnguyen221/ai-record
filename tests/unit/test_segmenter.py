import dataclasses

import numpy as np

from ai_record.audio.segmenter import Segmenter
from ai_record.audio.vad import SAMPLE_RATE, FakeVad
from ai_record.config import Settings
from tests.audio_helpers import sequence, silence, tone


def _seg(settings: Settings) -> Segmenter:
    return Segmenter("them", settings, FakeVad(frame_samples=320, threshold=0.02))


def test_single_utterance(settings):
    audio = sequence(silence(0.5), tone(1.0), silence(0.8))
    utts = _seg(settings).run_array(audio)
    assert len(utts) == 1
    u = utts[0]
    assert u.forced_cut is False
    assert u.source == "them"
    assert u.duration >= 0.25
    # pre-roll: utterance starts before the speech onset at 0.5 s
    assert u.audio_start_sample < int(0.5 * SAMPLE_RATE)


def test_two_utterances_split_by_silence(settings):
    audio = sequence(silence(0.3), tone(0.8), silence(0.9), tone(0.8), silence(0.8))
    utts = _seg(settings).run_array(audio)
    assert len(utts) == 2


def test_short_burst_dropped(settings):
    # 100 ms speech < min_speech_ms (250 ms) → dropped
    audio = sequence(silence(0.3), tone(0.1), silence(0.9))
    utts = _seg(settings).run_array(audio)
    assert utts == []


def test_forced_cut(settings):
    s = dataclasses.replace(settings, max_utterance_seconds=1)
    audio = sequence(silence(0.3), tone(2.6), silence(0.8))
    utts = _seg(s).run_array(audio)
    assert len(utts) >= 2
    assert any(u.forced_cut for u in utts)


def test_sample_bounds_monotonic(settings):
    audio = sequence(silence(0.3), tone(0.8), silence(0.9), tone(0.8), silence(0.8))
    utts = _seg(settings).run_array(audio)
    for u in utts:
        assert u.audio_end_sample > u.audio_start_sample
        assert u.end > u.start
    if len(utts) == 2:
        assert utts[1].audio_start_sample >= utts[0].audio_end_sample
