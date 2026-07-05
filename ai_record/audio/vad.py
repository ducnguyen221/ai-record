"""Voice-activity detection abstraction (SPEC.md §5.2).

Three implementations:
  * :class:`SileroVad`    — preferred, lazy-imports ``silero-vad`` + ``torch``.
  * :class:`WebrtcVad`    — fallback, lazy-imports ``webrtcvad``.
  * :class:`FakeVad`      — deterministic energy-threshold VAD for tests
                            (no third-party dependency).

Every VAD exposes ``frame_samples`` (the fixed analysis window), ``is_speech`` and
``reset``. The segmenter is written purely against this interface so tests inject a
:class:`FakeVad` and never touch hardware/model downloads.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import numpy as np

log = logging.getLogger("ai_record.vad")

SAMPLE_RATE = 16000


@runtime_checkable
class Vad(Protocol):
    """Frame-wise voice-activity detector operating on 16 kHz mono float32."""

    frame_samples: int

    def is_speech(self, frame: np.ndarray) -> bool: ...

    def reset(self) -> None: ...


class FakeVad:
    """Energy-threshold VAD — deterministic, dependency-free (for tests/CPU).

    A frame is "speech" when its RMS exceeds ``threshold``.
    """

    def __init__(self, frame_samples: int = 320, threshold: float = 0.02) -> None:
        self.frame_samples = int(frame_samples)
        self.threshold = float(threshold)

    def is_speech(self, frame: np.ndarray) -> bool:
        if frame.size == 0:
            return False
        rms = float(np.sqrt(np.mean(np.square(frame, dtype=np.float64))))
        return rms >= self.threshold

    def reset(self) -> None:  # stateless
        return None


class WebrtcVad:
    """webrtcvad fallback (10/20/30 ms frames). Lazy import."""

    def __init__(self, frame_ms: int = 20, aggressiveness: int = 2) -> None:
        if frame_ms not in (10, 20, 30):
            raise ValueError("webrtcvad supports 10/20/30 ms frames only")
        self.frame_samples = int(SAMPLE_RATE * frame_ms / 1000)
        self._aggr = int(aggressiveness)
        self._vad = None

    def _ensure(self) -> None:
        if self._vad is None:
            import webrtcvad  # type: ignore

            self._vad = webrtcvad.Vad(self._aggr)

    def is_speech(self, frame: np.ndarray) -> bool:
        self._ensure()
        if frame.size != self.frame_samples:
            return False
        pcm16 = np.clip(frame, -1.0, 1.0)
        pcm16 = (pcm16 * 32767.0).astype("<i2").tobytes()
        try:
            return bool(self._vad.is_speech(pcm16, SAMPLE_RATE))
        except Exception as exc:  # pragma: no cover
            log.debug("webrtcvad error: %s", exc)
            return False

    def reset(self) -> None:
        return None


class SileroVad:
    """Silero VAD (512-sample / 32 ms windows @ 16 kHz). Lazy import of torch."""

    def __init__(self, threshold: float = 0.5, device: str = "cpu") -> None:
        self.frame_samples = 512
        self.threshold = float(threshold)
        self._device = device
        self._model = None
        self._torch = None

    def _ensure(self) -> None:
        if self._model is not None:
            return
        import torch  # type: ignore
        from silero_vad import load_silero_vad  # type: ignore

        self._torch = torch
        self._model = load_silero_vad()
        if self._device == "cuda" and torch.cuda.is_available():
            self._model = self._model.to("cuda")

    def is_speech(self, frame: np.ndarray) -> bool:
        self._ensure()
        assert self._torch is not None
        if frame.size != self.frame_samples:
            return False
        t = self._torch.from_numpy(np.ascontiguousarray(frame, dtype=np.float32))
        if self._device == "cuda":
            t = t.to("cuda")
        with self._torch.no_grad():
            prob = float(self._model(t, SAMPLE_RATE).item())
        return prob >= self.threshold

    def reset(self) -> None:
        if self._model is not None:
            try:
                self._model.reset_states()
            except Exception:  # pragma: no cover
                pass


def make_vad(settings) -> Vad:
    """Construct the configured VAD, falling back to :class:`FakeVad` if deps are absent."""
    engine = getattr(settings, "vad_engine", "silero")
    frame_ms = getattr(settings, "frame_ms", 20)
    try:
        if engine == "webrtcvad":
            return WebrtcVad(frame_ms=frame_ms, aggressiveness=settings.vad_aggressiveness)
        return SileroVad(device=getattr(settings, "vad_device", "cpu"))
    except Exception as exc:  # pragma: no cover - deps missing at runtime
        log.warning("VAD engine %s unavailable (%s); using energy FakeVad", engine, exc)
        return FakeVad(frame_samples=int(SAMPLE_RATE * frame_ms / 1000))
