"""Pipeline wiring: capture → segmenter → STT (emit) → store + broadcast.

Runs the segmenters and the single STT worker in background threads, with the
fallback ladder (SPEC.md §4.4) monitoring backlog. STT is *STT-first* (SPEC.md
§4.5): each transcript is persisted and broadcast immediately; translation and
diarization are left as clean patch points for M2/M3 (a ``patch`` message type
exists but M1 never sends one).

The pipeline is dependency-injected (transcriber, VAD, store, broadcast) so the
whole thing runs on CPU with no hardware or models in tests.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

import numpy as np

from .config import LadderStep, Preset, Settings
from .audio.ringbuffer import RingBuffer
from .audio.segmenter import Segmenter, SourceEpoch, Utterance
from .audio.vad import Vad, make_vad
from .store import SessionStore, UtteranceRecord, _now_iso
from .transcriber import TranscriberProtocol

log = logging.getLogger("ai_record.pipeline")

BroadcastFn = Callable[[dict], None]
SOURCES = ("them", "you")


class _TimedQueue(queue.Queue):
    """Queue that stamps enqueue time so the ladder can measure oldest-item age."""

    def put(self, item, block: bool = True, timeout: float | None = None) -> None:
        super().put((time.monotonic(), item), block, timeout)

    def get(self, block: bool = True, timeout: float | None = None):
        _ts, item = super().get(block, timeout)
        return item

    def oldest_age(self) -> float:
        with self.mutex:
            if self.queue:
                return time.monotonic() - self.queue[0][0]
            return 0.0


class _GatedQueue:
    """Wraps the STT queue so a segmenter's ``put`` is suppressed in audio-only mode.

    In ladder step 8 (AUDIO_ONLY, SPEC.md §4.4) live STT is turned off: segmenters
    keep running (raw WAV capture / recovery still work), but their utterances are
    NOT enqueued for live transcription — the audio is recovered offline instead.
    """

    def __init__(self, inner: "_TimedQueue", suppressed: Callable[[], bool]) -> None:
        self._inner = inner
        self._suppressed = suppressed

    def put(self, item, *a, **k) -> None:
        if self._suppressed():
            return
        self._inner.put(item, *a, **k)


class LadderController:
    """Auto-downgrade / step-up controller with hysteresis (SPEC.md §4.4)."""

    def __init__(self, settings: Settings, transcriber: TranscriberProtocol, on_status: Callable[[], None]) -> None:
        self.settings = settings
        self.transcriber = transcriber
        self.on_status = on_status
        self.step = LadderStep.NONE
        self._clear_since: float | None = None

    def should_downgrade(self, backlog: int, oldest_age: float) -> bool:
        return (
            backlog > self.settings.backpressure_utt_threshold
            or oldest_age > self.settings.backpressure_lag_seconds
        )

    def evaluate(self, backlog: int, oldest_age: float) -> None:
        if not self.settings.auto_downgrade_on_backpressure:
            return
        now = time.monotonic()
        if self.should_downgrade(backlog, oldest_age):
            self._clear_since = None
            if self.step < LadderStep.AUDIO_ONLY:
                self.step = LadderStep(int(self.step) + 1)
                self.transcriber.apply_ladder_step(self.step)
                log.info("ladder step DOWN → %s (backlog=%d age=%.1fs)", self.step.name, backlog, oldest_age)
                self.on_status()
        elif self.step > LadderStep.NONE:
            if self._clear_since is None:
                self._clear_since = now
            elif now - self._clear_since >= self.settings.recovery_stable_seconds:
                self.step = LadderStep(int(self.step) - 1)
                self.transcriber.apply_ladder_step(self.step)
                self._clear_since = now
                log.info("ladder step UP → %s", self.step.name)
                self.on_status()


class Pipeline:
    """Owns the ring buffers, segmenters and STT worker for one recording session."""

    def __init__(
        self,
        settings: Settings,
        preset: Preset,
        transcriber: TranscriberProtocol,
        store: SessionStore,
        session,
        broadcast: BroadcastFn | None = None,
        *,
        sources: tuple[str, ...] = SOURCES,
        vad_factory: Callable[[], Vad] | None = None,
        ring_seconds: float = 30.0,
        epoch_states: dict[str, SourceEpoch] | None = None,
    ) -> None:
        self.settings = settings
        self.preset = preset
        self.transcriber = transcriber
        self.store = store
        self.session = session
        self.session_id = session.session_id
        self.broadcast = broadcast or (lambda msg: None)
        self.sources = sources

        cap = int(ring_seconds * settings.target_sample_rate)
        self.rings: dict[str, RingBuffer] = {s: RingBuffer(cap) for s in sources}
        # Shared epoch holders so a device reopen mid-recording reaches the segmenter.
        self.epoch_states: dict[str, SourceEpoch] = epoch_states or {s: SourceEpoch() for s in sources}
        vad_factory = vad_factory or (lambda: make_vad(settings))
        self.segmenters: dict[str, Segmenter] = {
            s: Segmenter(s, settings, vad_factory(), epoch_state=self.epoch_states.get(s))
            for s in sources
        }

        # Set while the ladder is at AUDIO_ONLY: segmenters stop feeding live STT.
        self._audio_only = threading.Event()
        self.stt_queue: _TimedQueue = _TimedQueue(maxsize=64)
        self._stop = threading.Event()
        self._eof: dict[str, threading.Event] = {s: threading.Event() for s in sources}
        self._seg_threads: dict[str, threading.Thread] = {}
        self._stt_thread: threading.Thread | None = None
        self.ladder = LadderController(settings, transcriber, self._broadcast_status)
        self._utterance_count = 0

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        for s in self.sources:
            gated = _GatedQueue(self.stt_queue, self._audio_only.is_set)
            t = threading.Thread(
                target=self.segmenters[s].run,
                args=(self.rings[s], gated, self._stop, self._eof[s]),
                name=f"segmenter-{s}",
                daemon=True,
            )
            t.start()
            self._seg_threads[s] = t
        self._stt_thread = threading.Thread(target=self._stt_worker, name="stt-worker", daemon=True)
        self._stt_thread.start()
        self._broadcast_status()

    def feed(self, source: str, pcm: np.ndarray) -> int:
        """Feed resampled 16 kHz mono audio into a source's ring buffer."""
        return self.rings[source].write(pcm)

    def mark_eof(self, source: str | None = None) -> None:
        """Signal no-more-input so the segmenter flushes and exits (tests/recovery)."""
        for s in (self.sources if source is None else (source,)):
            self._eof[s].set()

    def wait_idle(self, timeout: float = 15.0) -> bool:
        """Wait until segmenters exit and the STT queue is fully drained."""
        deadline = time.monotonic() + timeout
        for s in self.sources:
            t = self._seg_threads.get(s)
            if t is not None:
                t.join(timeout=max(0.0, deadline - time.monotonic()))
        while time.monotonic() < deadline:
            if self.stt_queue.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return self.stt_queue.unfinished_tasks == 0

    def stop(self) -> None:
        self.mark_eof()
        self.wait_idle(timeout=10.0)
        self._stop.set()
        for t in self._seg_threads.values():
            t.join(timeout=2.0)
        if self._stt_thread is not None:
            self._stt_thread.join(timeout=5.0)

    # ------------------------------------------------------------------ #
    def _stt_worker(self) -> None:
        while not self._stop.is_set():
            try:
                utt = self.stt_queue.get(timeout=0.1)
            except queue.Empty:
                self.ladder.evaluate(0, 0.0)
                self._sync_audio_only()
                if all(self._eof[s].is_set() for s in self.sources) and self.stt_queue.empty():
                    # drain complete; keep looping until stopped (server owns lifecycle)
                    time.sleep(0.02)
                continue
            backlog = self.stt_queue.qsize()
            oldest = self.stt_queue.oldest_age()
            self.ladder.evaluate(backlog, oldest)
            self._sync_audio_only()
            try:
                self._process(utt)
            except Exception as exc:  # never let one utterance kill the worker
                log.exception("STT worker error: %s", exc)
            finally:
                self.stt_queue.task_done()

    def _sync_audio_only(self) -> None:
        """Mirror the ladder's AUDIO_ONLY rung into the segmenter feed gate."""
        if self.ladder.step >= LadderStep.AUDIO_ONLY:
            if not self._audio_only.is_set():
                self._audio_only.set()
        elif self._audio_only.is_set():
            self._audio_only.clear()

    def _process(self, utt: Utterance) -> None:
        tr = self.transcriber.transcribe(utt)
        if tr is None:
            return  # dropped (hallucination guard / silence)
        rec = self.store._record_from(self.session_id, utt.source, utt, tr)
        self.store.append_utterance(rec)
        self._utterance_count += 1
        # STT-first: emit immediately (SPEC.md §4.5).
        self.broadcast({"type": "utterance", "record": rec.to_dict()})

    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        model, compute = self.transcriber.current_model()
        degraded: list[str] = []
        if self.ladder.step >= LadderStep.AUDIO_ONLY:
            degraded.append("audio_only")
        elif self.ladder.step > LadderStep.NONE:
            degraded.append("stt_catching_up")
        dropped = sum(r.dropped_frames for r in self.rings.values())
        return {
            "recording": not self._stop.is_set(),
            "session_id": self.session_id,
            "preset": self.preset.name,
            "effective_model": model,
            "effective_compute_type": compute,
            "ladder_step": int(self.ladder.step),
            "degraded_states": degraded,
            "dropped_frames": dropped,
            "utterance_count": self._utterance_count,
        }

    def _broadcast_status(self) -> None:
        st = self.status()
        st["type"] = "status"
        st["note"] = ""
        self.broadcast(st)
