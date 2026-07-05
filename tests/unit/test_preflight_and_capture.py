"""Preflight whisper_loadable (Important #7) + import-safe backend polling (#5)."""

from __future__ import annotations

import sys
import types

from ai_record.audio.capture import (
    PyAudioWpatchBackend,
    SoundcardBackend,
    SourceHealth,
)
from ai_record.config import Secrets, Settings
from ai_record.preflight import run_preflight


def test_whisper_loadable_false_when_library_missing(monkeypatch):
    # Simulate faster-whisper absent (env-independent) → gate must be False,
    # not the old always-true `cuda or True`.
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    report = run_preflight(Settings(hardware_preset="cpu"), Secrets())
    assert report["whisper_loadable"] is False


def test_whisper_loadable_true_when_library_present(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", types.ModuleType("faster_whisper"))
    report = run_preflight(Settings(hardware_preset="cpu"), Secrets())
    assert report["whisper_loadable"] is True


def test_backend_current_device_id_is_import_safe(monkeypatch):
    # Simulate the audio lib being unavailable → must return "" gracefully, never raise.
    monkeypatch.setitem(sys.modules, "soundcard", None)
    assert SoundcardBackend().current_device_id() == ""
    assert PyAudioWpatchBackend().current_device_id() == ""


def test_source_health_reports_seconds_and_counters():
    h = SourceHealth(silent_seconds=1.5, overrun_count=2, underrun_count=1)
    d = h.to_dict()
    assert d["silent_seconds"] == 1.5
    assert d["overrun_count"] == 2
    assert d["underrun_count"] == 1
    assert "silent_frames" not in d
