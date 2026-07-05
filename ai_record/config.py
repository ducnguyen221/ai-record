"""Settings, hardware presets, VRAM auto-detect, and OS-keychain secrets.

This module is import-safe with no GPU, no audio hardware, and no heavy
dependencies installed. ``torch`` and ``keyring`` are imported lazily inside
functions and guarded, so importing ``ai_record.config`` never fails.

See SPEC.md §4.3 (presets/VRAM), §5.10 (settings & secrets), §7 (config keys).
"""

from __future__ import annotations

import dataclasses
import enum
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ai_record.config")

APP_NAME = "ai-record"
APP_VERSION = "2.0"
KEYRING_SERVICE = "ai-record"
SECRET_NAMES = ("hf_token", "gemini_api_key")

# Summarization scenarios (addendum §E2). Each is a named, user-editable prompt
# template; the transcript is appended (hardened, delimited) by the summarizer.
# Default scenario is ``reformat`` (lossless structuring with an integrity guard).
# Valid session output artefacts (Feature: output formats multi-select). "md" is the
# base transcript and is ALWAYS produced even if omitted from the list.
OUTPUT_FORMATS_VALID: tuple[str, ...] = ("md", "txt", "mp3", "wav", "summary")

SUMMARY_SCENARIOS: tuple[str, ...] = (
    "reformat",
    "minutes",
    "study_notes",
    "action_tracker",
    "article",
)

DEFAULT_SUMMARY_SCENARIOS: dict[str, str] = {
    "reformat": (
        "You are given a meeting transcript. Restructure it WITHOUT changing, adding, "
        "removing, translating, paraphrasing, or reordering the wording of any utterance. "
        "You may ONLY: (a) group related consecutive utterances into thematic sections, and "
        "(b) add Markdown structure — `##` topic headings and bullet lists — while preserving "
        "each original 'Speaker + [timestamp]' label and its EXACT text. Every original "
        "utterance's text MUST still appear verbatim in your output. Output Markdown only."
    ),
    "minutes": (
        "Read and understand the whole meeting transcript, then write concise meeting minutes, "
        "Vietnamese-first. Group by context and include: a short overview, key discussion points, "
        "decisions made, action items (with owner if stated), and open questions/risks. "
        "This is a lossy summary. Output Markdown."
    ),
    "study_notes": (
        "Restructure this transcript into NotebookLM-style study notes for self-study: key "
        "concepts & definitions, a short Q&A / flashcard list, and a 'things to remember' section, "
        "grouped by topic. Vietnamese-first. Output Markdown."
    ),
    "action_tracker": (
        "From this transcript, extract ONLY a checklist of action items, decisions, owners, and "
        "deadlines/follow-ups — nothing else. Use Markdown checkboxes. Output Markdown."
    ),
    "article": (
        "Rewrite the discussion in this transcript into a clean, readable article/blog post in "
        "flowing prose that explains what was covered. Vietnamese-first. Output Markdown."
    ),
}


# --------------------------------------------------------------------------- #
# Local-summarizer model catalog (curated Ollama models — see models.py)
# --------------------------------------------------------------------------- #
# Literal used only if the catalog JSON is unreadable at import time.
_OLLAMA_DEFAULT_FALLBACK = "qwen2.5:7b"


def load_model_catalog() -> dict[str, Any]:
    """Load the curated summarizer-model catalog (safe fallback).

    Thin re-export of :func:`ai_record.models.load_model_catalog` so callers that
    already import :mod:`ai_record.config` have one place to reach the catalog.
    """
    from .models import load_model_catalog as _load

    return _load()


def _default_ollama_model() -> str:
    """Resolve the default Ollama model tag from the catalog (literal fallback)."""
    try:
        from .models import default_model

        return default_model()
    except Exception:  # pragma: no cover - defensive; models.py is import-safe
        return _OLLAMA_DEFAULT_FALLBACK


# --------------------------------------------------------------------------- #
# App-data directory helpers
# --------------------------------------------------------------------------- #
def localappdata_dir() -> Path:
    """Return ``%LOCALAPPDATA%\\ai-record`` (falls back to a temp dir off-Windows)."""
    base = os.getenv("LOCALAPPDATA")
    if not base:
        base = os.path.join(tempfile.gettempdir(), "LocalAppData")
    return Path(base) / APP_NAME


def default_sessions_root() -> Path:
    return localappdata_dir() / "sessions"


def settings_path() -> Path:
    return localappdata_dir() / "settings.json"


def resolve_sessions_root(settings: "Settings") -> Path:
    """Resolve the configured ``sessions_root``, expanding the LOCALAPPDATA token."""
    raw = settings.sessions_root
    if not raw or raw.startswith("%LOCALAPPDATA%"):
        return default_sessions_root()
    return Path(os.path.expandvars(raw)).expanduser()


# --------------------------------------------------------------------------- #
# Hardware presets (SPEC.md §4.3)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Preset:
    """A resolved real-time hardware stack."""

    name: str
    whisper_model: str
    whisper_compute_type: str
    whisper_device: str            # "cuda" | "cpu"
    beam_fast: int
    beam_quality: int
    translation_device: str        # "cuda" | "cpu"
    diarization_embedder: str      # "resemblyzer" | "ecapa"
    diarization_device: str        # "cuda" | "cpu"
    diarization_realtime: bool

    def beam(self, latency_mode: str) -> int:
        return self.beam_quality if latency_mode == "quality" else self.beam_fast


PRESETS: dict[str, Preset] = {
    "cpu": Preset(
        name="cpu",
        whisper_model="small",
        whisper_compute_type="int8",
        whisper_device="cpu",
        beam_fast=1,
        beam_quality=1,
        translation_device="cpu",
        diarization_embedder="resemblyzer",
        diarization_device="cpu",
        diarization_realtime=False,
    ),
    "gpu_8gb": Preset(
        name="gpu_8gb",
        whisper_model="medium",
        whisper_compute_type="int8_float16",
        whisper_device="cuda",
        beam_fast=1,
        beam_quality=5,
        translation_device="cpu",
        diarization_embedder="resemblyzer",
        diarization_device="cpu",
        diarization_realtime=False,
    ),
    "gpu_12gb": Preset(
        name="gpu_12gb",
        whisper_model="large-v3",
        whisper_compute_type="int8_float16",
        whisper_device="cuda",
        beam_fast=1,
        beam_quality=5,
        translation_device="cpu",
        diarization_embedder="resemblyzer",
        diarization_device="cpu",
        diarization_realtime=True,
    ),
    "gpu_16gb_plus": Preset(
        name="gpu_16gb_plus",
        whisper_model="large-v3",
        whisper_compute_type="float16",
        whisper_device="cuda",
        beam_fast=5,
        beam_quality=5,
        translation_device="cuda",
        diarization_embedder="ecapa",
        diarization_device="cuda",
        diarization_realtime=True,
    ),
}


def detect_vram_gb() -> float | None:
    """Return total VRAM of GPU 0 in GiB, or ``None`` if CUDA is unavailable.

    ``torch`` is imported lazily so this module stays import-safe without it.
    """
    try:
        import torch  # type: ignore
    except Exception:  # pragma: no cover - torch absent in CI
        return None
    try:
        if not torch.cuda.is_available():
            return None
        total = torch.cuda.get_device_properties(0).total_memory
        return total / (1024 ** 3)
    except Exception:  # pragma: no cover - driver hiccup
        return None


def detect_preset_name(vram_gb: float | None) -> str:
    """Map detected VRAM to a preset name (SPEC.md §4.3)."""
    if vram_gb is None:
        return "cpu"
    if vram_gb > 15:
        return "gpu_16gb_plus"
    if vram_gb >= 10:
        return "gpu_12gb"
    return "gpu_8gb"


def resolve_preset(settings: "Settings") -> Preset:
    """Resolve the effective :class:`Preset`, honouring ``hardware_preset`` and overrides.

    ``auto`` detects VRAM. Explicit knob overrides in ``settings`` win over the
    preset defaults (SPEC.md §4.3 last bullet).
    """
    name = settings.hardware_preset
    if name == "auto":
        name = detect_preset_name(detect_vram_gb())
    base = PRESETS.get(name, PRESETS["cpu"])

    overrides: dict[str, Any] = {"name": base.name}
    if settings.whisper_model:
        overrides["whisper_model"] = settings.whisper_model
    if settings.whisper_compute_type:
        overrides["whisper_compute_type"] = settings.whisper_compute_type
    if settings.translation_device:
        overrides["translation_device"] = settings.translation_device
    if settings.diarization_embedder:
        overrides["diarization_embedder"] = settings.diarization_embedder
    if settings.diarization_device:
        overrides["diarization_device"] = settings.diarization_device
    return dataclasses.replace(base, **overrides)


# --------------------------------------------------------------------------- #
# Fallback ladder (SPEC.md §4.4)
# --------------------------------------------------------------------------- #
class LadderStep(enum.IntEnum):
    """Ordered auto-downgrade rungs. 0 = no degradation, 8 = audio-only."""

    NONE = 0
    BEAM_1 = 1
    TRANSLATION_CPU = 2
    DIARIZATION_OFF = 3
    WHISPER_INT8_FLOAT16 = 4
    WHISPER_MEDIUM = 5
    WHISPER_SMALL = 6
    TRANSLATION_OFF = 7
    AUDIO_ONLY = 8

    @property
    def max_step(self) -> int:  # pragma: no cover - trivial
        return int(LadderStep.AUDIO_ONLY)


# --------------------------------------------------------------------------- #
# Settings (SPEC.md §7)
# --------------------------------------------------------------------------- #
_ENUMS: dict[str, tuple[str, ...]] = {
    "hardware_preset": ("auto", "cpu", "gpu_8gb", "gpu_12gb", "gpu_16gb_plus"),
    "audio_backend": ("auto", "soundcard", "pyaudiowpatch"),
    "vad_engine": ("silero", "webrtcvad"),
    "vad_device": ("cpu", "cuda"),
    "whisper_model": ("", "small", "medium", "large-v2", "large-v3"),
    "whisper_compute_type": ("", "float16", "int8_float16", "int8"),
    "latency_mode": ("fast", "quality"),
    "translation_provider": ("nllb", "gemini"),
    "translation_device": ("", "cuda", "cpu"),
    "diarization_embedder": ("", "ecapa", "resemblyzer"),
    "diarization_device": ("", "cuda", "cpu"),
    "summarizer_provider": ("claude_cli", "codex_cli", "gemini", "ollama"),
    "theme": ("auto", "light", "dark"),
    "audio_export_format": ("mp3", "wav"),
}


@dataclass
class Settings:
    """All non-secret configuration (SPEC.md §7). Secrets live in the keychain."""

    # consent / server
    consent_acknowledged: bool = False
    consent_acknowledged_at: str | None = None
    server_port: int = 8848
    sessions_root: str = "%LOCALAPPDATA%/ai-record/sessions"

    # hardware / capture
    hardware_preset: str = "auto"
    audio_backend: str = "auto"
    persist_audio: bool = True
    raw_segment_seconds: int = 60
    silent_loopback_warn_s: int = 20
    silence_rms_eps: float = 1e-4
    device_reopen_retries: int = 5
    target_sample_rate: int = 16000

    # segmentation / VAD
    frame_ms: int = 20
    vad_engine: str = "silero"
    vad_device: str = "cpu"
    vad_aggressiveness: int = 2
    pre_roll_ms: int = 300
    speech_start_ms: int = 150
    silence_end_ms: int = 600
    min_speech_ms: int = 250
    max_utterance_seconds: int = 15
    forced_cut_overlap_ms: int = 200

    # whisper / STT
    whisper_model: str = ""
    whisper_compute_type: str = ""
    latency_mode: str = "fast"
    whisper_vad_filter: bool = True
    force_language: str | None = None
    no_speech_threshold: float = 0.6
    logprob_drop_threshold: float = -1.0
    min_rms: float = 0.005
    hallucination_denylist: list[str] = field(
        default_factory=lambda: [
            "thank you",
            "thanks for watching",
            "please subscribe",
            "ご視聴ありがとうございました",
            "字幕",
        ]
    )

    # backpressure / ladder
    auto_downgrade_on_backpressure: bool = True
    backpressure_utt_threshold: int = 2
    backpressure_lag_seconds: int = 3
    recovery_stable_seconds: int = 30

    # translation (M2 — plumbed, off by default)
    translate_enabled: bool = False
    target_lang: str = "vi"
    source_languages: list[str] = field(default_factory=list)
    translation_provider: str = "nllb"
    nllb_model: str = "facebook/nllb-200-distilled-600M"
    translation_device: str = ""
    translate_min_duration_s: float = 1.0
    translate_min_lang_prob: float = 0.6
    translate_batch_window_ms: int = 400
    translate_batch_max_s: float = 4.0
    translation_max_staleness_s: float = 8.0

    # diarization (M3/M4 — plumbed)
    diarization_enabled: bool = True
    diarization_realtime: bool = True
    diarization_embedder: str = ""
    diarization_device: str = ""
    sim_threshold_ecapa: float = 0.75
    sim_threshold_resemblyzer: float = 0.70
    centroid_update_min_conf: float = 0.6
    min_speaker_speech_s: float = 3.0
    min_embed_ms: int = 800
    max_speakers: int = 8
    pyannote_model: str = "pyannote/speaker-diarization-3.1"

    # summarization (M4)
    summarizer_provider: str = "claude_cli"
    summary_prompt: str = ""
    summary_scenarios: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_SUMMARY_SCENARIOS)
    )
    summary_use_translation: bool = True
    summary_max_chars: int = 48000
    summary_timeout_s: int = 300
    ollama_model: str = field(default_factory=lambda: _default_ollama_model())
    ollama_url: str = "http://localhost:11434"

    # storage / durability
    retention_days: int = 0
    fsync_interval_ms: int = 1000

    # output artefacts (transcript.md is ALWAYS written; these are opt-in extras)
    #
    # ``output_formats`` is the canonical, extensible multi-select chosen before Start
    # (valid items: OUTPUT_FORMATS_VALID; unknowns are ignored; "md" is always present).
    # The legacy booleans below are kept for backward-compat and OR-ed with the list at
    # finalize (see store._write_optional_outputs).
    output_formats: list[str] = field(default_factory=lambda: ["md"])
    keep_audio: bool = False           # keep per-source audio after finalize (else deleted)
    audio_export_format: str = "mp3"   # "mp3" (transcode via ffmpeg) | "wav" (leave as-is)
    save_txt: bool = False             # also write a plain-text transcript.txt on finalize

    # websocket robustness
    ws_client_queue_max: int = 256
    ws_client_slow_deadline_s: int = 10

    # ui
    theme: str = "auto"

    # read-only
    app_version: str = APP_VERSION

    # ----------------------------------------------------------------- #
    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        for key, allowed in _ENUMS.items():
            val = getattr(self, key)
            if val is None:
                continue
            if val not in allowed:
                raise ValueError(f"invalid value for {key!r}: {val!r} (allowed: {allowed})")
        if not (0 <= self.vad_aggressiveness <= 3):
            raise ValueError("vad_aggressiveness must be 0..3")
        if self.frame_ms not in (10, 20, 30):
            raise ValueError("frame_ms must be 10, 20 or 30")
        if self.server_port < 1 or self.server_port > 65535:
            raise ValueError("server_port out of range")
        if self.retention_days < 0:
            raise ValueError("retention_days must be >= 0")
        if self.max_speakers < 1:
            raise ValueError("max_speakers must be >= 1")
        # output_formats: keep only valid items (dedupe, preserve order), always "md".
        cleaned: list[str] = []
        for f in self.output_formats or []:
            if f in OUTPUT_FORMATS_VALID and f not in cleaned:
                cleaned.append(f)
        if "md" not in cleaned:
            cleaned.insert(0, "md")
        self.output_formats = cleaned

    # ----------------------------------------------------------------- #
    @classmethod
    def _field_names(cls) -> set[str]:
        return {f.name for f in fields(cls)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        """Build from a dict, ignoring unknown keys (with a warning)."""
        known = cls._field_names()
        clean: dict[str, Any] = {}
        for k, v in data.items():
            if k in known:
                clean[k] = v
            else:
                log.warning("ignoring unknown settings key: %s", k)
        # never load a secret value from JSON even if present
        clean.pop("hf_token", None)
        clean.pop("gemini_api_key", None)
        return cls(**clean)

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Settings":
        p = Path(path) if path else settings_path()
        if not p.exists():
            s = cls()
            try:
                s.save(p)
            except OSError:  # pragma: no cover - fs perms
                log.warning("could not persist default settings to %s", p)
            return s
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.error("failed to read settings %s: %s; using defaults", p, exc)
            return cls()
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def save(self, path: str | os.PathLike[str] | None = None) -> None:
        p = Path(path) if path else settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        _set_owner_only_acl(p)

    def update(self, partial: dict[str, Any]) -> "Settings":
        """Return a validated copy with ``partial`` applied (unknown keys ignored)."""
        merged = self.to_dict()
        known = self._field_names()
        for k, v in partial.items():
            if k in known and k not in ("app_version",):
                merged[k] = v
            else:
                log.warning("ignoring settings update key: %s", k)
        return Settings.from_dict(merged)

    def redacted(self, secrets: "Secrets | None" = None) -> dict[str, Any]:
        """Return settings for the API with secrets shown only as booleans."""
        out = self.to_dict()
        sec = secrets or Secrets()
        out["hf_token_is_set"] = sec.is_set("hf_token")
        out["gemini_api_key_is_set"] = sec.is_set("gemini_api_key")
        return out

    def acknowledge_consent(self) -> "Settings":
        return self.update(
            {
                "consent_acknowledged": True,
                "consent_acknowledged_at": datetime.now(timezone.utc).isoformat(),
            }
        )


def _set_owner_only_acl(path: Path) -> None:
    """Best-effort owner-only ACL on Windows (defense-in-depth, SPEC.md §5.10)."""
    if os.name != "nt":
        return
    try:  # pragma: no cover - platform/permission dependent
        user = os.getenv("USERNAME") or os.getenv("USER") or ""
        if not user:
            return
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except Exception as exc:  # pragma: no cover
        log.debug("icacls ACL set failed: %s", exc)


# --------------------------------------------------------------------------- #
# Secrets — OS keychain via keyring (lazy, with in-memory fallback for tests)
# --------------------------------------------------------------------------- #
_MEMORY_SECRETS: dict[str, str] = {}


class Secrets:
    """Keyring-backed secret store (SPEC.md §5.10).

    ``keyring`` is imported lazily; if it is unavailable (e.g. CI) an in-memory
    fallback keeps the interface working so nothing has to know the difference.
    """

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self.service = service

    def _keyring(self):  # -> module | None
        try:
            import keyring  # type: ignore

            return keyring
        except Exception:  # pragma: no cover - keyring absent in CI
            return None

    def get(self, name: str) -> str | None:
        if name not in SECRET_NAMES:
            raise ValueError(f"unknown secret name: {name!r}")
        kr = self._keyring()
        if kr is None:
            return _MEMORY_SECRETS.get(name)
        try:
            return kr.get_password(self.service, name)
        except Exception as exc:  # pragma: no cover
            log.error("keyring get failed for %s: %s", name, exc)
            return _MEMORY_SECRETS.get(name)

    def set(self, name: str, value: str) -> None:
        if name not in SECRET_NAMES:
            raise ValueError(f"unknown secret name: {name!r}")
        kr = self._keyring()
        if kr is None:
            _MEMORY_SECRETS[name] = value
            return
        try:
            kr.set_password(self.service, name, value)
        except Exception as exc:  # pragma: no cover
            log.error("keyring set failed for %s: %s", name, exc)
            _MEMORY_SECRETS[name] = value

    def clear(self, name: str) -> None:
        if name not in SECRET_NAMES:
            raise ValueError(f"unknown secret name: {name!r}")
        _MEMORY_SECRETS.pop(name, None)
        kr = self._keyring()
        if kr is None:
            return
        try:
            kr.delete_password(self.service, name)
        except Exception:  # pragma: no cover - not set / backend error
            pass

    def is_set(self, name: str) -> bool:
        return bool(self.get(name))
