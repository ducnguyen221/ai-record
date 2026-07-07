"""Streaming VAD segmentation → sample-accurate utterances (SPEC.md §5.2).

One :class:`Segmenter` per source. It converts a continuous 16 kHz mono stream
into discrete :class:`Utterance` chunks bounded by natural pauses, dropping
silence and keeping latency low. Bounds are computed from the source's cumulative
sample index (carried from capture, SPEC.md §4.8) — never from a wall clock.

The class is deliberately usable two ways:
  * ``run(ring, out_queue, stop_event, eof_event)`` — threaded live path.
  * ``run_array(pcm, start_sample)`` — synchronous, deterministic (tests).
Both drive the same state machine (``_push_frame`` / ``_flush``).
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

import numpy as np

from .vad import SAMPLE_RATE, Vad

_IDLE = "idle"
_SPEECH = "speech"


@dataclass
class SourceEpoch:
    """Mutable holder for a source's current epoch/offset (SPEC.md §4.8).

    Shared by reference between the capture runner (which bumps it on a device
    reopen) and the segmenter (which stamps each emitted utterance from it), so
    utterances recorded after a device change carry the correct ``source_epoch_id``
    instead of a stale ``0``.
    """

    epoch_id: int = 0
    offset_sec: float = 0.0


@dataclass
class Utterance:
    """A finalized speech chunk with sample-accurate bounds (SPEC.md §5.2)."""

    source: str
    pcm: np.ndarray
    start: float
    end: float
    audio_start_sample: int
    audio_end_sample: int
    source_epoch_id: int
    source_offset_sec: float
    forced_cut: bool

    @property
    def duration(self) -> float:
        return self.end - self.start


class Segmenter:
    """Per-source VAD state machine emitting :class:`Utterance` objects."""

    def __init__(
        self,
        source: str,
        settings,
        vad: Vad,
        *,
        source_epoch_id: int = 0,
        source_offset_sec: float = 0.0,
        epoch_state: SourceEpoch | None = None,
    ) -> None:
        self.source = source
        self.settings = settings
        self.vad = vad
        self.source_epoch_id = source_epoch_id
        self.source_offset_sec = source_offset_sec
        # When present, the live epoch/offset are read from this shared holder at
        # emit time so a device reopen mid-recording is reflected in utterances.
        self._epoch_state = epoch_state

        self.frame_samples = int(vad.frame_samples)
        frame_ms = self.frame_samples / (SAMPLE_RATE / 1000.0)
        self.frame_ms = frame_ms

        def _frames(ms: float) -> int:
            return max(1, round(ms / frame_ms))

        self.speech_start_frames = _frames(settings.speech_start_ms)
        self.silence_end_frames = _frames(settings.silence_end_ms)
        self.pre_roll_frames = max(0, round(settings.pre_roll_ms / frame_ms))
        self.forced_search_frames = max(1, round(500.0 / frame_ms))
        self.min_speech_samples = int(settings.min_speech_ms / 1000.0 * SAMPLE_RATE)
        self.max_utt_samples = int(settings.max_utterance_seconds * SAMPLE_RATE)

        self._reset_state()

    # ------------------------------------------------------------------ #
    def _reset_state(self) -> None:
        self.state = _IDLE
        self._pre_roll: list[tuple[np.ndarray, int]] = []   # (frame, start_sample)
        self._utt_frames: list[np.ndarray] = []
        self._utt_starts: list[int] = []
        self._speech_run = 0
        self._silence_run = 0
        self.vad.reset()

    # ------------------------------------------------------------------ #
    def _current_epoch(self) -> tuple[int, float]:
        if self._epoch_state is not None:
            return self._epoch_state.epoch_id, self._epoch_state.offset_sec
        return self.source_epoch_id, self.source_offset_sec

    def _seconds(self, sample: int, offset_sec: float) -> float:
        return offset_sec + sample / SAMPLE_RATE

    def _build(self, frames: list[np.ndarray], starts: list[int], forced: bool) -> Utterance | None:
        if not frames:
            return None
        pcm = np.concatenate(frames).astype(np.float32)
        start_sample = starts[0]
        end_sample = starts[-1] + frames[-1].size
        if end_sample - start_sample < self.min_speech_samples:
            return None
        epoch_id, offset_sec = self._current_epoch()
        return Utterance(
            source=self.source,
            pcm=pcm,
            start=self._seconds(start_sample, offset_sec),
            end=self._seconds(end_sample, offset_sec),
            audio_start_sample=start_sample,
            audio_end_sample=end_sample,
            source_epoch_id=epoch_id,
            source_offset_sec=offset_sec,
            forced_cut=forced,
        )

    def _emit_natural(self) -> Utterance | None:
        # Drop the trailing silence frames from the emitted audio.
        keep = len(self._utt_frames) - self._silence_run
        keep = max(keep, 0)
        frames = self._utt_frames[:keep]
        starts = self._utt_starts[:keep]
        utt = self._build(frames, starts, forced=False)
        self._reset_state()
        return utt

    def _forced_cut(self) -> Utterance | None:
        """Cut at the most recent low-energy frame within the last ~500 ms if possible."""
        n = len(self._utt_frames)
        window_start = max(0, n - self.forced_search_frames)
        cut = n
        best_rms = None
        for i in range(n - 1, window_start - 1, -1):
            rms = float(np.sqrt(np.mean(np.square(self._utt_frames[i], dtype=np.float64))))
            if best_rms is None or rms < best_rms:
                best_rms = rms
                cut = i + 1  # cut after the low-energy frame
        if cut >= n or cut <= 0:
            cut = n  # no better boundary — hard cut
        frames = self._utt_frames[:cut]
        starts = self._utt_starts[:cut]
        utt = self._build(frames, starts, forced=True)
        # Continue a new utterance with the remaining tail (stay in SPEECH).
        tail_frames = self._utt_frames[cut:]
        tail_starts = self._utt_starts[cut:]
        self._utt_frames = tail_frames
        self._utt_starts = tail_starts
        self._silence_run = 0
        self._speech_run = self.speech_start_frames
        self.state = _SPEECH
        return utt

    # ------------------------------------------------------------------ #
    def _push_frame(self, frame: np.ndarray, start_sample: int) -> list[Utterance]:
        """Advance the state machine by one fixed-size frame. Returns emitted utterances."""
        out: list[Utterance] = []
        speech = self.vad.is_speech(frame)

        if self.state == _IDLE:
            self._pre_roll.append((frame, start_sample))
            if self.pre_roll_frames and len(self._pre_roll) > self.pre_roll_frames:
                self._pre_roll.pop(0)
            self._speech_run = self._speech_run + 1 if speech else 0
            if self._speech_run >= self.speech_start_frames:
                # Promote pre-roll (incl. the onset frames) into the utterance.
                self._utt_frames = [f for f, _ in self._pre_roll]
                self._utt_starts = [s for _, s in self._pre_roll]
                self._pre_roll = []
                self._silence_run = 0
                self.state = _SPEECH
            return out

        # SPEECH
        self._utt_frames.append(frame)
        self._utt_starts.append(start_sample)
        self._silence_run = 0 if speech else self._silence_run + 1

        utt_samples = (self._utt_starts[-1] + frame.size) - self._utt_starts[0]
        if self._silence_run >= self.silence_end_frames:
            u = self._emit_natural()
            if u is not None:
                out.append(u)
        elif utt_samples >= self.max_utt_samples:
            u = self._forced_cut()
            if u is not None:
                out.append(u)
        return out

    def _flush(self) -> Utterance | None:
        """Emit any in-progress utterance at end-of-stream."""
        if self.state == _SPEECH and self._utt_frames:
            keep = len(self._utt_frames) - self._silence_run
            keep = max(keep, 0)
            utt = self._build(self._utt_frames[:keep], self._utt_starts[:keep], forced=False)
            self._reset_state()
            return utt
        self._reset_state()
        return None

    # ------------------------------------------------------------------ #
    def run_array(self, pcm: np.ndarray, start_sample: int = 0) -> list[Utterance]:
        """Segment a whole in-memory array synchronously (deterministic; tests)."""
        data = np.ascontiguousarray(pcm, dtype=np.float32).reshape(-1)
        fs = self.frame_samples
        out: list[Utterance] = []
        n_frames = data.size // fs
        for i in range(n_frames):
            frame = data[i * fs:(i + 1) * fs]
            out.extend(self._push_frame(frame, start_sample + i * fs))
        tail = self._flush()
        if tail is not None:
            out.append(tail)
        return out

    def run(
        self,
        ring,
        out_queue: "queue.Queue",
        stop_event: threading.Event,
        eof_event: threading.Event | None = None,
        poll_interval: float = 0.05,
    ) -> None:
        """Threaded live path: pull frames from ``ring`` and push utterances.

        Exits when ``stop_event`` is set, or when ``eof_event`` is set and the ring
        is drained (flushing any in-progress utterance).
        """
        fs = self.frame_samples
        carry = np.empty(0, dtype=np.float32)
        carry_start = 0
        while True:
            if stop_event.is_set():
                break
            block, start_abs = ring.read(fs * 16)
            if block.size == 0:
                if eof_event is not None and eof_event.is_set() and ring.available() == 0:
                    break
                time.sleep(poll_interval)
                continue
            if carry.size == 0:
                carry = block
                carry_start = start_abs
            else:
                carry = np.concatenate((carry, block))
            # Chop into fixed frames.
            n_frames = carry.size // fs
            for i in range(n_frames):
                frame = carry[i * fs:(i + 1) * fs]
                for utt in self._push_frame(frame, carry_start + i * fs):
                    out_queue.put(utt)
            consumed = n_frames * fs
            carry = carry[consumed:].copy()
            carry_start += consumed
        tail = self._flush()
        if tail is not None:
            out_queue.put(tail)
