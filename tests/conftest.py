"""Shared fixtures. Ensures the repo root is importable and provides temp settings."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_record.config import Settings, resolve_sessions_root  # noqa: E402
from ai_record.store import SessionStore  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_settings_file(tmp_path_factory, monkeypatch):
    """Never let a test write the REAL %LOCALAPPDATA%\\ai-record\\settings.json
    (a server PUT /api/settings calls Settings.save() with no path). Redirect the
    settings file to a throwaway per-test location."""
    d = tmp_path_factory.mktemp("cfg")
    monkeypatch.setattr("ai_record.config.settings_path", lambda: d / "settings.json", raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        hardware_preset="cpu",
        sessions_root=str(tmp_path / "sessions"),
        translate_enabled=False,
        diarization_realtime=False,
    )


@pytest.fixture
def store(settings: Settings) -> SessionStore:
    return SessionStore(resolve_sessions_root(settings), settings)
