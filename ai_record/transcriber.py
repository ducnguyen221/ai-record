"""Speech-to-text worker (SPEC.md §5.3): preset-driven, STT-first, guarded.

``faster_whisper`` and ``torch`` are imported lazily, so this module is import-safe
with no GPU and no models. Tests inject a :class:`MockTranscriber`. The hallucination
guard is a pure function (:func:`is_hallucination`) so it can be unit-tested directly.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

import numpy as np

from .config import LadderStep, Preset, Settings
from .audio.segmenter import Utterance

log = logging.getLogger("ai_record.transcriber")

# Model-downgrade rungs used by the OOM handler and the fallback ladder (SPEC.md §5.3/§4.4).
_MODEL_LADDER: list[tuple[str, str]] = [
    ("large-v3", "int8_float16"),
    ("medium", "int8_float16"),
    ("small", "int8"),
]

_PUNCT_ONLY = re.compile(r"^[\s\W_]*$", re.UNICODE)


class _CudaOOM(Exception):
    """Internal marker for a CUDA out-of-memory failure during transcription."""


@dataclass
class Transcript:
    """Result of transcribing one :class:`Utterance` (SPEC.md §5.3)."""

    source: str
    start: float
    end: float
    text: str
    lang: str
    lang_prob: float
    avg_logprob: float
    no_speech_prob: float
    stt_latency_ms: int
    effective_model: str
    effective_compute_type: str


@runtime_checkable
class TranscriberProtocol(Protocol):
    """Interface the pipeline depends on (real or mock)."""

    def load(self) -> None: ...

    def transcribe(self, utt: Utterance) -> Transcript | None: ...

    def current_model(self) -> tuple[str, str]: ...

    def apply_ladder_step(self, step: LadderStep) -> None: ...


def utterance_rms(pcm: np.ndarray) -> float:
    if pcm.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(pcm, dtype=np.float64))))


def is_hallucination(
    text: str,
    *,
    no_speech_prob: float,
    avg_logprob: float,
    rms: float,
    settings: Settings,
) -> bool:
    """Return True when the STT output should be dropped (SPEC.md §5.3 guards).

    Drop when ANY of:
      * empty / punctuation-only text
      * utterance RMS below ``min_rms``
      * text matches the hallucination denylist (case-insensitive, trimmed)
      * high no-speech probability AND low average logprob
    """
    stripped = (text or "").strip()
    if not stripped or _PUNCT_ONLY.match(stripped):
        return True
    if rms < settings.min_rms:
        return True
    low = stripped.lower()
    for phrase in settings.hallucination_denylist:
        if low == phrase.strip().lower():
            return True
    if no_speech_prob > settings.no_speech_threshold and avg_logprob < settings.logprob_drop_threshold:
        return True
    return False


class Transcriber:
    """faster-whisper wrapper. Lazy model load; OOM/ladder model downgrade."""

    def __init__(
        self,
        settings: Settings,
        preset: Preset,
        *,
        on_status: Callable[[dict], None] | None = None,
        on_recover: Callable[[Utterance], None] | None = None,
    ) -> None:
        self.settings = settings
        self.preset = preset
        self._model = None
        self._model_name = preset.whisper_model
        self._compute_type = preset.whisper_compute_type
        self._device = preset.whisper_device
        self._beam = preset.beam(settings.latency_mode)
        # Status broadcaster + offline-recovery sink (SPEC.md §4.4/§5.3). Utterances
        # that can't be transcribed even after a downgrade are queued here instead of
        # being silently dropped; the server can later recover them offline.
        self.on_status = on_status
        self.on_recover = on_recover
        self.pending_recovery: list[Utterance] = []

    # ------------------------------------------------------------------ #
    def current_model(self) -> tuple[str, str]:
        return self._model_name, self._compute_type

    def _new_model(self, model_name: str, compute_type: str, device: str) -> Any:
        """Instantiate a faster-whisper model (single seam; monkeypatched in tests)."""
        from faster_whisper import WhisperModel  # type: ignore

        return WhisperModel(model_name, device=device, compute_type=compute_type)

    def _ensure_model(self) -> None:
        """Load the current (already-resolved) model if none is live."""
        if self._model is None:
            self._model = self._new_model(self._model_name, self._compute_type, self._device)

    def load(self) -> None:
        """Load the model per preset, downgrading on OOM (SPEC.md §5.3)."""
        attempts = self._downgrade_chain(self._model_name, self._compute_type, self._device)
        last_exc: Exception | None = None
        for model_name, compute_type, device in attempts:
            try:
                self._model = self._new_model(model_name, compute_type, device)
                self._model_name, self._compute_type, self._device = model_name, compute_type, device
                log.info("loaded whisper %s (%s, %s)", model_name, compute_type, device)
                return
            except Exception as exc:  # OOM or load failure → try next rung
                last_exc = exc
                log.warning("whisper load failed for %s/%s/%s: %s", model_name, compute_type, device, exc)
                self._empty_cache()
        raise RuntimeError(f"could not load any whisper model: {last_exc}")

    @staticmethod
    def _downgrade_chain(model: str, compute: str, device: str) -> list[tuple[str, str, str]]:
        chain: list[tuple[str, str, str]] = [(model, compute, device)]
        started = False
        for m, c in _MODEL_LADDER:
            if m == model:
                started = True
                continue
            if started:
                chain.append((m, c, device))
        chain.append(("small", "int8", "cpu"))  # last resort
        # de-dup preserving order
        seen: set[tuple[str, str, str]] = set()
        uniq: list[tuple[str, str, str]] = []
        for item in chain:
            if item not in seen:
                seen.add(item)
                uniq.append(item)
        return uniq

    @staticmethod
    def _empty_cache() -> None:
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------ #
    def apply_ladder_step(self, step: LadderStep) -> None:
        """Live beam/compute/model swap for the fallback ladder (SPEC.md §4.4)."""
        if step >= LadderStep.BEAM_1:
            self._beam = 1
        # Cheaper compute first (fp16 → int8_float16), keeping the model size, before
        # dropping to a smaller model — a real downgrade for gpu_16gb_plus (float16).
        if step >= LadderStep.WHISPER_INT8_FLOAT16 and self._compute_type == "float16":
            self._swap_model(self._model_name, "int8_float16")
        if step >= LadderStep.WHISPER_MEDIUM:
            self._swap_model("medium", "int8_float16")
        if step >= LadderStep.WHISPER_SMALL:
            self._swap_model("small", "int8")

    def _swap_model(self, model_name: str, compute_type: str) -> None:
        if (model_name, compute_type) == (self._model_name, self._compute_type):
            return
        self._model_name, self._compute_type = model_name, compute_type
        self._model = None  # force lazy reload on next transcribe

    # ------------------------------------------------------------------ #
    def transcribe(self, utt: Utterance) -> Transcript | None:
        t0 = time.perf_counter()
        rms = utterance_rms(utt.pcm)
        if rms < self.settings.min_rms:
            return None
        if self._model is None:
            self.load()

        try:
            seg_list, info = self._run_model(utt)
        except _CudaOOM:
            # First OOM: free VRAM, downgrade one rung, and RETRY the SAME utterance
            # once on the smaller model rather than dropping it (SPEC.md §5.3).
            self._empty_cache()
            downgraded = self._downgrade_after_oom()
            self._emit_status("degraded", f"cuda_oom→{self._model_name}/{self._compute_type}")
            if not downgraded:
                self._queue_for_recovery(utt, "cuda_oom_no_downgrade")
                return None
            try:
                self._ensure_model()
                seg_list, info = self._run_model(utt)
            except _CudaOOM:
                # Still OOM after downgrade: hand off to offline recovery, don't drop.
                self._empty_cache()
                self._queue_for_recovery(utt, "cuda_oom_persisted")
                return None
            except Exception as exc:
                log.error("transcribe retry failed: %s", exc)
                return None
        except Exception as exc:
            log.error("transcribe failed: %s", exc)
            return None

        text = "".join(s.text for s in seg_list).strip()
        avg_logprob = float(np.mean([s.avg_logprob for s in seg_list])) if seg_list else -10.0
        no_speech = float(np.mean([s.no_speech_prob for s in seg_list])) if seg_list else 1.0

        if is_hallucination(
            text,
            no_speech_prob=no_speech,
            avg_logprob=avg_logprob,
            rms=rms,
            settings=self.settings,
        ):
            return None

        latency_ms = int((time.perf_counter() - t0) * 1000)
        return Transcript(
            source=utt.source,
            start=utt.start,
            end=utt.end,
            text=text,
            lang=getattr(info, "language", "") or "",
            lang_prob=float(getattr(info, "language_probability", 0.0) or 0.0),
            avg_logprob=avg_logprob,
            no_speech_prob=no_speech,
            stt_latency_ms=latency_ms,
            effective_model=self._model_name,
            effective_compute_type=self._compute_type,
        )

    def _run_model(self, utt: Utterance) -> tuple[list, Any]:
        """Run the loaded model, normalizing a CUDA OOM into :class:`_CudaOOM`."""
        try:
            segments, info = self._model.transcribe(
                utt.pcm,
                language=self.settings.force_language or None,
                vad_filter=self.settings.whisper_vad_filter,
                beam_size=self._beam,
                temperature=[0.0, 0.2, 0.4],
                condition_on_previous_text=False,
            )
            return list(segments), info
        except Exception as exc:
            if self._is_oom(exc):
                log.warning("CUDA OOM in transcribe (%s/%s)", self._model_name, self._compute_type)
                raise _CudaOOM(str(exc)) from exc
            raise

    def _emit_status(self, event: str, detail: str) -> None:
        if self.on_status is not None:
            with contextlib.suppress(Exception):
                self.on_status({"type": "status", "note": f"stt:{event}:{detail}", "recording": True})

    def _queue_for_recovery(self, utt: Utterance, reason: str) -> None:
        """Mark an utterance for offline recovery instead of dropping it silently."""
        self.pending_recovery.append(utt)
        if self.on_recover is not None:
            with contextlib.suppress(Exception):
                self.on_recover(utt)
        self._emit_status("recover_queued", reason)
        log.warning("utterance queued for offline recovery (%s)", reason)

    @staticmethod
    def _is_oom(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "out of memory" in msg or "cuda" in msg and "memory" in msg

    def _downgrade_after_oom(self) -> bool:
        """Drop one model rung after an OOM. Returns True if anything changed."""
        for i, (m, c) in enumerate(_MODEL_LADDER):
            if m == self._model_name:
                if i + 1 < len(_MODEL_LADDER):
                    self._swap_model(*_MODEL_LADDER[i + 1])
                    return True
                if (self._model_name, self._compute_type, self._device) != ("small", "int8", "cpu"):
                    self._model_name, self._compute_type, self._device = "small", "int8", "cpu"
                    self._model = None
                    return True
                return False  # already at the smallest CPU rung
        self._swap_model("small", "int8")
        return True


class MockTranscriber:
    """Deterministic transcriber for tests/integration (no models).

    Returns a canned :class:`Transcript` per utterance. ``text_fn`` may customise
    the emitted text from the utterance; ``drop_predicate`` can force ``None``.
    """

    def __init__(
        self,
        text: str = "mock transcript",
        lang: str = "en",
        lang_prob: float = 0.99,
        *,
        text_fn=None,
        drop_predicate=None,
        model: str = "mock",
        compute_type: str = "int8",
    ) -> None:
        self._text = text
        self._lang = lang
        self._lang_prob = lang_prob
        self._text_fn = text_fn
        self._drop = drop_predicate
        self._model = model
        self._compute = compute_type

    def load(self) -> None:  # no-op
        return None

    def current_model(self) -> tuple[str, str]:
        return self._model, self._compute

    def apply_ladder_step(self, step: LadderStep) -> None:  # no-op
        return None

    def transcribe(self, utt: Utterance) -> Transcript | None:
        if self._drop is not None and self._drop(utt):
            return None
        text = self._text_fn(utt) if self._text_fn else self._text
        return Transcript(
            source=utt.source,
            start=utt.start,
            end=utt.end,
            text=text,
            lang=self._lang,
            lang_prob=self._lang_prob,
            avg_logprob=-0.3,
            no_speech_prob=0.02,
            stt_latency_ms=5,
            effective_model=self._model,
            effective_compute_type=self._compute,
        )
