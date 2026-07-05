"""Local-summarizer model catalog + Ollama install/list helpers.

This module is the single-source-of-truth loader for ``summarizer_models.json``
(a curated list of Ollama models pullable from the official library) and a small,
*mockable* wrapper around ``ollama list`` so the server can report which models are
already pulled locally.

Import-safe: no heavy libraries, no network, and no subprocess at import time. The
``ollama`` binary is only touched when :func:`list_installed_models` is called, and
that call degrades gracefully (empty list) when ``ollama`` is absent or errors.

See SPEC.md §5.6 (summarizer providers) and the model-management addendum.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger("ai_record.models")

# Literal fallback used if the shipped JSON is unreadable (keeps callers alive).
_FALLBACK_DEFAULT = "qwen2.5:7b"
_FALLBACK_CATALOG: dict[str, Any] = {
    "default": _FALLBACK_DEFAULT,
    "updated": "",
    "models": [
        {
            "tag": "qwen2.5:7b",
            "family": "qwen2.5",
            "params": "7B",
            "vram_gb": 5,
            "tier": "8-12GB",
            "langs": "VN/JP/ZH best",
            "recommended": True,
            "note": "Best balance for Vietnamese + multilingual",
        }
    ],
}

_CATALOG_PATH = Path(__file__).parent / "summarizer_models.json"


def catalog_path() -> Path:
    """Absolute path to the packaged ``summarizer_models.json``."""
    return _CATALOG_PATH


def load_model_catalog() -> dict[str, Any]:
    """Load the curated model catalog with a safe fallback.

    Never raises: on any read/parse error (or a malformed shape) it returns the
    built-in fallback so the summarizer default resolution and the API endpoint
    keep working.
    """
    try:
        data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not load model catalog %s: %s; using fallback", _CATALOG_PATH, exc)
        return dict(_FALLBACK_CATALOG)
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        log.warning("model catalog %s has an unexpected shape; using fallback", _CATALOG_PATH)
        return dict(_FALLBACK_CATALOG)
    data.setdefault("default", _FALLBACK_DEFAULT)
    return data


def default_model() -> str:
    """Return the catalog's default model tag (falls back to ``qwen2.5:7b``)."""
    default = load_model_catalog().get("default")
    return default if isinstance(default, str) and default else _FALLBACK_DEFAULT


def ollama_available() -> bool:
    """True when the ``ollama`` binary is on PATH (no subprocess run)."""
    return shutil.which("ollama") is not None


def list_installed_models(timeout: float = 4.0) -> list[str]:
    """Return locally-pulled Ollama model tags via ``ollama list``.

    Guarded end-to-end: if ``ollama`` is absent, times out, or errors, returns an
    empty list rather than raising. On Windows the console window is suppressed via
    ``CREATE_NO_WINDOW``. Kept as a standalone function so tests can monkeypatch it.
    """
    if shutil.which("ollama") is None:
        return []
    try:
        proc = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={**os.environ, "NO_COLOR": "1"},
            shell=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:  # pragma: no cover - env dependent
        log.debug("ollama list failed: %s", exc)
        return []
    if proc.returncode != 0:  # pragma: no cover - env dependent
        return []
    return _parse_ollama_list(proc.stdout or "")


def _parse_ollama_list(output: str) -> list[str]:
    """Parse the NAME column out of ``ollama list`` tabular output.

    Example line: ``qwen2.5:7b   845dc...   4.7 GB   2 days ago``. The header row
    (``NAME  ID  SIZE  MODIFIED``) and blank lines are skipped.
    """
    tags: list[str] = []
    for line in output.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        first = line.split()[0]
        if first.upper() == "NAME":
            continue
        tags.append(first)
    return tags
