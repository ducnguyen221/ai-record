"""Speaker diarization — two tiers, confidence-aware (SPEC.md §5.5, addendum M3/M4).

Tier 1 (:class:`RealtimeDiarizer`) is online cosine clustering over speaker
embeddings, run on the pipeline's post-worker and delivered as a ``patch``. Tier 2
(:class:`OfflineDiarizer`) is an on-demand pyannote re-diarization pass that relabels
``them`` utterances against ``audio_them.wav`` **sample time**.

All heavy libraries (``resemblyzer``, ``speechbrain``, ``pyannote.audio``, ``torch``)
are imported lazily; the embedder and the pyannote pipeline are injectable so the
whole module is import-safe and testable on CPU with none of them installed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

import numpy as np

from .config import Preset, Settings

log = logging.getLogger("ai_record.diarizer")

SAMPLE_RATE = 16000
UNKNOWN = "Speaker ?"
_MARGIN_SCALE = 0.2          # cosine-margin → confidence scaling (SPEC.md §5.5)
_UNTRUSTED_CONF_CAP = 0.49   # confidence cap while a centroid is not yet trusted


# --------------------------------------------------------------------------- #
# Public data types
# --------------------------------------------------------------------------- #
@dataclass
class Assignment:
    """Result of labelling one utterance (SPEC.md §5.5)."""

    speaker: str                 # "You" | "Speaker N" | "Speaker ?"
    confidence: float | None     # cosine-margin score, None for mic/unknown
    is_overlap: bool = False
    forced_overflow: bool = False


@dataclass
class SpeakerSpan:
    """A speaker-homogeneous span on the ``audio_them.wav`` sample timeline (tier 2)."""

    start_sample: int
    end_sample: int
    speaker: str


@runtime_checkable
class Embedder(Protocol):
    """Speaker-embedding backend operating on 16 kHz mono float32."""

    def embed(self, pcm: np.ndarray) -> np.ndarray: ...


class HfTokenRequired(RuntimeError):
    """Raised when tier-2 diarization needs an HF token that is not configured."""


# --------------------------------------------------------------------------- #
# Real embedders (lazy) — Resemblyzer default, ECAPA opt-in
# --------------------------------------------------------------------------- #
class ResemblyzerEmbedder:
    """Resemblyzer voice encoder (default, CPU). Lazy import."""

    def __init__(self) -> None:
        self._enc = None

    def embed(self, pcm: np.ndarray) -> np.ndarray:  # pragma: no cover - needs the model
        if self._enc is None:
            from resemblyzer import VoiceEncoder  # type: ignore

            self._enc = VoiceEncoder("cpu")
        return np.asarray(self._enc.embed_utterance(np.asarray(pcm, dtype=np.float32)), dtype=np.float32)


class EcapaEmbedder:
    """SpeechBrain ECAPA-TDNN embedder (opt-in / gpu_16gb_plus). Lazy import."""

    def __init__(self, device: str = "cpu") -> None:
        self.device = device
        self._model = None

    def embed(self, pcm: np.ndarray) -> np.ndarray:  # pragma: no cover - needs the model
        import torch  # type: ignore

        if self._model is None:
            from speechbrain.inference import EncoderClassifier  # type: ignore

            self._model = EncoderClassifier.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb", run_opts={"device": self.device}
            )
        t = torch.from_numpy(np.asarray(pcm, dtype=np.float32)).unsqueeze(0)
        with torch.no_grad():
            emb = self._model.encode_batch(t).squeeze().cpu().numpy()
        return np.asarray(emb, dtype=np.float32)


def make_embedder(settings: Settings, preset: Preset) -> Embedder:
    """Construct the configured embedder (Resemblyzer default; SPEC.md §5.5)."""
    name = settings.diarization_embedder or preset.diarization_embedder or "resemblyzer"
    device = settings.diarization_device or preset.diarization_device or "cpu"
    if name == "ecapa":
        return EcapaEmbedder(device=device)
    return ResemblyzerEmbedder()


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))  # inputs are L2-normalized


# --------------------------------------------------------------------------- #
# Tier 1 — realtime online clustering
# --------------------------------------------------------------------------- #
@dataclass
class _Centroid:
    mean: np.ndarray            # L2-normalized running mean
    accum_sec: float            # accumulated trusted speech
    count: int

    @property
    def trusted(self) -> bool:
        return self._trusted

    _trusted: bool = False


class RealtimeDiarizer:
    """Online cosine clustering with confidence + no-drift centroid rules (SPEC.md §5.5)."""

    def __init__(self, settings: Settings, preset: Preset, *, embedder: Embedder | None = None) -> None:
        self.settings = settings
        self.preset = preset
        self._embedder = embedder
        self.embedder_name = settings.diarization_embedder or preset.diarization_embedder or "resemblyzer"
        self.centroids: dict[str, _Centroid] = {}
        self._n_created = 0

    # ------------------------------------------------------------------ #
    def _ensure_embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = make_embedder(self.settings, self.preset)
        return self._embedder

    def _threshold(self) -> float:
        if self.embedder_name == "ecapa":
            return self.settings.sim_threshold_ecapa
        return self.settings.sim_threshold_resemblyzer

    def reset(self) -> None:
        self.centroids.clear()
        self._n_created = 0

    def rename(self, old_label: str, new_label: str) -> None:
        """Rename a speaker label; future matches keep the new name (SPEC.md §5.5)."""
        if old_label in self.centroids and old_label != new_label:
            self.centroids[new_label] = self.centroids.pop(old_label)

    # ------------------------------------------------------------------ #
    def label(self, utt, *, is_overlap: bool = False) -> Assignment:
        """Assign a speaker to one utterance (SPEC.md §5.5)."""
        if utt.source == "you":
            return Assignment(speaker="You", confidence=None)

        duration_ms = utt.duration * 1000.0
        too_short = duration_ms < self.settings.min_embed_ms

        # Short OR overlap → "Speaker ?"; never create/update a centroid.
        if too_short or is_overlap:
            return Assignment(speaker=UNKNOWN, confidence=0.0, is_overlap=is_overlap)

        try:
            emb = _l2_normalize(self._ensure_embedder().embed(utt.pcm))
        except Exception as exc:  # pragma: no cover - embedder failure
            log.warning("embedder failed: %s", exc)
            return Assignment(speaker=UNKNOWN, confidence=0.0)

        threshold = self._threshold()
        best_label, best_sim, second_sim = self._nearest(emb)

        if best_label is not None and best_sim >= threshold:
            confidence = self._confidence(best_sim, second_sim, threshold)
            cen = self.centroids[best_label]
            if not cen.trusted:
                confidence = min(confidence, _UNTRUSTED_CONF_CAP)
            # Only update the centroid on a confident, non-overlap, long-enough hit.
            if confidence >= self.settings.centroid_update_min_conf:
                self._update_centroid(best_label, emb, utt.duration)
            else:
                cen.accum_sec += utt.duration  # still accrue trust, don't move the mean
                cen._trusted = cen.accum_sec >= self.settings.min_speaker_speech_s
            return Assignment(speaker=best_label, confidence=confidence)

        # No match ≥ threshold → new speaker, unless we've hit the cap.
        if self._n_created >= self.settings.max_speakers:
            log.warning("max_speakers (%d) reached — labelling utterance 'Speaker ?'",
                        self.settings.max_speakers)
            return Assignment(speaker=UNKNOWN, confidence=0.0, forced_overflow=True)

        label = self._new_speaker(emb, utt.duration)
        # A brand-new centroid is untrusted until it accumulates enough speech.
        confidence = self._confidence(best_sim if best_label else 0.0, second_sim, threshold)
        confidence = min(confidence, _UNTRUSTED_CONF_CAP)
        return Assignment(speaker=label, confidence=confidence)

    # ------------------------------------------------------------------ #
    def _nearest(self, emb: np.ndarray) -> tuple[str | None, float, float]:
        best_label: str | None = None
        best_sim = -1.0
        second_sim = -1.0
        for label, cen in self.centroids.items():
            sim = _cosine(emb, cen.mean)
            if sim > best_sim:
                second_sim = best_sim
                best_sim, best_label = sim, label
            elif sim > second_sim:
                second_sim = sim
        return best_label, best_sim, second_sim

    def _confidence(self, best_sim: float, second_sim: float, threshold: float) -> float:
        ref = second_sim if second_sim >= 0.0 else threshold
        return float(np.clip((best_sim - ref) / _MARGIN_SCALE, 0.0, 1.0))

    def _new_speaker(self, emb: np.ndarray, duration: float) -> str:
        self._n_created += 1
        label = f"Speaker {self._n_created}"
        trusted = duration >= self.settings.min_speaker_speech_s
        self.centroids[label] = _Centroid(mean=emb.copy(), accum_sec=duration, count=1, _trusted=trusted)
        return label

    def _update_centroid(self, label: str, emb: np.ndarray, duration: float) -> None:
        cen = self.centroids[label]
        cen.count += 1
        cen.mean = _l2_normalize(cen.mean + (emb - cen.mean) / cen.count)
        cen.accum_sec += duration
        cen._trusted = cen.accum_sec >= self.settings.min_speaker_speech_s


# --------------------------------------------------------------------------- #
# Tier 2 — offline accurate re-diarization (sample-time)
# --------------------------------------------------------------------------- #
DiarizeFn = Callable[[str, str | None], list[SpeakerSpan]]


class OfflineDiarizer:
    """On-demand pyannote re-diarization + sample-time relabel (SPEC.md §5.5 tier 2)."""

    def __init__(self, settings: Settings, secrets=None, *, diarize_fn: DiarizeFn | None = None) -> None:
        self.settings = settings
        if secrets is None:
            from .config import Secrets

            secrets = Secrets()
        self.secrets = secrets
        self._diarize_fn = diarize_fn

    def available(self) -> tuple[bool, str]:
        """(ok, reason). Needs an HF token unless a diarize_fn is injected."""
        if self._diarize_fn is not None:
            return True, ""
        if not self.secrets.is_set("hf_token"):
            return False, "HF token required"
        return True, ""

    def rediarize(self, session_dir: str) -> list[SpeakerSpan]:
        """Run pyannote on ``audio_them.wav`` and return speaker spans (sample time)."""
        ok, why = self.available()
        if not ok:
            raise HfTokenRequired(why)
        wav = Path(session_dir) / "audio_them.wav"
        if not wav.exists():
            raise FileNotFoundError(f"missing {wav} — audio not persisted / not finalized")
        token = self.secrets.get("hf_token")
        fn = self._diarize_fn or self._pyannote_diarize
        return fn(str(wav), token)

    def _pyannote_diarize(self, wav_path: str, hf_token: str | None) -> list[SpeakerSpan]:  # pragma: no cover
        from pyannote.audio import Pipeline  # type: ignore

        pipeline = Pipeline.from_pretrained(self.settings.pyannote_model, use_auth_token=hf_token)
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                pipeline.to(torch.device("cuda"))
        except Exception:
            pass
        annotation = pipeline(wav_path)
        spans: list[SpeakerSpan] = []
        for segment, _track, speaker in annotation.itertracks(yield_label=True):
            spans.append(
                SpeakerSpan(
                    start_sample=int(segment.start * SAMPLE_RATE),
                    end_sample=int(segment.end * SAMPLE_RATE),
                    speaker=str(speaker),
                )
            )
        return spans


def relabel_them_utterances(records, spans: list[SpeakerSpan]) -> dict[int, str]:
    """Overlap-weighted majority relabel of ``them`` utterances (SPEC.md §5.5, §4.8).

    Compares each ``them`` utterance's ``[audio_start_sample, audio_end_sample]``
    against the pyannote spans on the same sample timeline, assigns the speaker with
    the greatest total overlap, and maps raw pyannote ids to stable ``Speaker A/B/…``
    in order of first appearance. Returns ``{seq: label}`` for changed utterances.
    """
    stable: dict[str, str] = {}

    def _stable_label(raw: str) -> str:
        if raw not in stable:
            stable[raw] = f"Speaker {chr(ord('A') + len(stable))}"
        return stable[raw]

    out: dict[int, str] = {}
    for rec in records:
        if rec.source != "them":
            continue
        a = rec.audio_start_sample
        b = rec.audio_end_sample
        if a is None or b is None or b <= a:
            continue
        totals: dict[str, int] = {}
        for span in spans:
            lo = max(a, span.start_sample)
            hi = min(b, span.end_sample)
            if hi > lo:
                totals[span.speaker] = totals.get(span.speaker, 0) + (hi - lo)
        if not totals:
            continue
        winner = max(totals, key=totals.get)
        out[rec.seq] = _stable_label(winner)
    return out


def make_realtime_diarizer(settings: Settings, preset: Preset, embedder: Embedder | None = None) -> RealtimeDiarizer:
    return RealtimeDiarizer(settings, preset, embedder=embedder)


def make_offline_diarizer(settings: Settings, secrets=None) -> OfflineDiarizer:
    """Factory the server calls (monkeypatched in tests to inject a fake diarize_fn)."""
    return OfflineDiarizer(settings, secrets)
