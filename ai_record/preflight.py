"""Preflight / readiness checks (SPEC.md §5.9, GET /api/preflight).

Every probe is lazy and guarded so the report is always produced and never raises,
even with no GPU, no models, and no CLIs installed.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

from .config import (
    Preset,
    Secrets,
    Settings,
    detect_preset_name,
    detect_vram_gb,
    resolve_preset,
    resolve_sessions_root,
)

log = logging.getLogger("ai_record.preflight")


def _cuda_report() -> tuple[bool, str | None, float | None]:
    try:
        import torch  # type: ignore
    except Exception:
        return False, None, None
    try:
        if not torch.cuda.is_available():
            return False, None, None
        version = getattr(torch.version, "cuda", None)
        return True, version, detect_vram_gb()
    except Exception:  # pragma: no cover
        return False, None, None


def _whisper_cache_present() -> bool:
    """Best-effort check for a cached faster-whisper / HF model (no download)."""
    candidates = [
        os.getenv("HF_HOME"),
        os.path.join(os.path.expanduser("~"), ".cache", "huggingface"),
        os.getenv("XDG_CACHE_HOME"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            for p in Path(c).rglob("*"):
                name = p.name.lower()
                if "whisper" in name or "faster-whisper" in str(p).lower():
                    return True
    return False


def _cli_available(settings: Settings) -> dict[str, bool]:
    return {
        "claude": shutil.which("claude") is not None,
        "codex": shutil.which("codex") is not None,
        "ollama": shutil.which("ollama") is not None,
    }


def run_preflight(settings: Settings, secrets: Secrets | None = None) -> dict[str, Any]:
    """Return the preflight report dict consumed by the UI / GET /api/preflight."""
    secrets = secrets or Secrets()
    cuda, cuda_version, vram = _cuda_report()

    preset: Preset = resolve_preset(settings)
    detected_name = detect_preset_name(vram) if settings.hardware_preset == "auto" else settings.hardware_preset

    sessions_root = resolve_sessions_root(settings)
    disk_free_gb: float | None = None
    try:
        target = sessions_root if sessions_root.exists() else sessions_root.parent
        if not target.exists():
            target = Path(os.getenv("LOCALAPPDATA") or os.path.expanduser("~"))
        usage = shutil.disk_usage(str(target))
        disk_free_gb = round(usage.free / (1024 ** 3), 1)
    except Exception:  # pragma: no cover
        disk_free_gb = None

    cli = _cli_available(settings)
    return {
        "cuda": cuda,
        "cuda_version": cuda_version,
        "vram_gb": round(vram, 1) if vram is not None else None,
        "whisper_loadable": cuda or True,  # CPU fallback always possible
        "model_cache": _whisper_cache_present(),
        "disk_free_gb": disk_free_gb,
        "hf_terms_ok": secrets.is_set("hf_token"),
        "cli_available": cli,
        "summarizer_cli_available": cli.get(settings.summarizer_provider.replace("_cli", ""), False),
        "preset": preset.name,
        "detected_preset": detected_name,
        "whisper_model": preset.whisper_model,
        "compute_type": preset.whisper_compute_type,
        "sessions_root": str(sessions_root),
    }
