"""Dual WASAPI capture behind a uniform backend contract (SPEC.md §5.1).

All hardware libraries (``soundcard``, ``PyAudioWPatch``, ``soxr``, ``scipy``) are
imported lazily inside methods, so this module is import-safe with none of them
installed. Real capture only happens when :meth:`CaptureManager.start` is called on
a machine with audio hardware; unit/integration tests use :class:`FileCaptureSource`
instead and never touch WASAPI.

Responsibilities: open loopback ("them") + mic ("you") streams, report the actual
opened format, downmix to mono, resample to 16 kHz, maintain a per-source sample
counter + ``source_epoch_id``, tee raw audio to the crash-safe writer, push frames
into per-source ring buffers, and emit health telemetry.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

import numpy as np

from ..store import RawSegmentWriter
from .ringbuffer import RingBuffer

log = logging.getLogger("ai_record.capture")

TARGET_RATE = 16000


# --------------------------------------------------------------------------- #
# Backend contract (SPEC.md §5.1)
# --------------------------------------------------------------------------- #
@dataclass
class OpenedFormat:
    sample_rate: int
    channels: int
    sample_format: str          # "float32" | "int16"
    device_id: str
    device_name: str
    block_frames: int
    block_duration_ms: float


@dataclass
class SourceHealth:
    rms: float = 0.0
    silent_frames: int = 0
    overrun_count: int = 0
    underrun_count: int = 0
    reopen_count: int = 0
    last_epoch_open_wall: str = ""

    def to_dict(self) -> dict:
        return {
            "rms": self.rms,
            "silent_frames": self.silent_frames,
            "overrun_count": self.overrun_count,
            "underrun_count": self.underrun_count,
            "reopen_count": self.reopen_count,
            "last_epoch_open_wall": self.last_epoch_open_wall,
        }


class AudioBackend(Protocol):
    def open(self, role: str, settings) -> OpenedFormat: ...
    def read(self) -> tuple[np.ndarray, int]: ...
    def close(self) -> None: ...
    def current_device_id(self) -> str: ...


@dataclass
class AudioFrame:
    source: str
    pcm: np.ndarray
    n_samples: int
    audio_start_sample: int
    source_epoch_id: int


@dataclass
class CaptureSource:
    source: str
    available: bool
    opened: OpenedFormat | None = None
    health: SourceHealth = field(default_factory=SourceHealth)


# --------------------------------------------------------------------------- #
# Resampling (SPEC.md §5.1) — streaming soxr, scipy fallback, identity if absent
# --------------------------------------------------------------------------- #
class _Resampler:
    """Stateful resampler to 16 kHz mono. Uses soxr if available, else scipy."""

    def __init__(self, in_rate: int, out_rate: int = TARGET_RATE) -> None:
        self.in_rate = in_rate
        self.out_rate = out_rate
        self._stream = None
        self._mode = "identity" if in_rate == out_rate else "pending"

    def process(self, pcm: np.ndarray) -> np.ndarray:
        if self._mode == "identity":
            return pcm.astype(np.float32)
        if self._stream is None and self._mode == "pending":
            self._init_stream()
        if self._mode == "soxr":
            return self._stream.resample_chunk(pcm).astype(np.float32)
        if self._mode == "scipy":
            from scipy.signal import resample_poly  # type: ignore
            from math import gcd

            g = gcd(self.out_rate, self.in_rate)
            up, down = self.out_rate // g, self.in_rate // g
            return resample_poly(pcm, up, down).astype(np.float32)
        return pcm.astype(np.float32)

    def _init_stream(self) -> None:
        try:
            import soxr  # type: ignore

            self._stream = soxr.ResampleStream(self.in_rate, self.out_rate, 1, dtype="float32")
            self._mode = "soxr"
        except Exception:
            self._mode = "scipy"  # per-chunk resample_poly

    def flush(self) -> np.ndarray:
        if self._mode == "soxr" and self._stream is not None:
            try:
                return self._stream.resample_chunk(np.empty(0, dtype=np.float32), last=True).astype(np.float32)
            except Exception:  # pragma: no cover
                return np.empty(0, dtype=np.float32)
        return np.empty(0, dtype=np.float32)


def _to_mono(pcm: np.ndarray, channels: int) -> np.ndarray:
    if channels > 1 and pcm.ndim == 2:
        return pcm.mean(axis=1).astype(np.float32)
    return pcm.reshape(-1).astype(np.float32)


# --------------------------------------------------------------------------- #
# Backends (lazy) — real WASAPI capture
# --------------------------------------------------------------------------- #
class SoundcardBackend:
    """WASAPI capture via the ``soundcard`` package (preferred)."""

    def __init__(self) -> None:
        self._rec = None
        self._ctx = None
        self._fmt: OpenedFormat | None = None

    def open(self, role: str, settings) -> OpenedFormat:
        import soundcard as sc  # type: ignore

        if role == "them":
            spk = sc.default_speaker()
            mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
            dev_name = spk.name
        else:
            mic = sc.default_microphone()
            dev_name = mic.name
        native_rate = 48000
        native_channels = 2 if role == "them" else 1
        self._ctx = mic.recorder(samplerate=native_rate, channels=native_channels, blocksize=1024)
        self._rec = self._ctx.__enter__()
        self._fmt = OpenedFormat(
            sample_rate=native_rate,
            channels=native_channels,
            sample_format="float32",
            device_id=str(getattr(mic, "id", dev_name)),
            device_name=str(dev_name),
            block_frames=1024,
            block_duration_ms=1024 / native_rate * 1000,
        )
        return self._fmt

    def read(self) -> tuple[np.ndarray, int]:
        data = self._rec.record(numframes=1024)  # (frames, channels) float32
        return np.asarray(data, dtype=np.float32), int(data.shape[0])

    def close(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.__exit__(None, None, None)
            except Exception:  # pragma: no cover
                pass
            self._ctx = None
            self._rec = None

    def current_device_id(self) -> str:
        return self._fmt.device_id if self._fmt else ""


class PyAudioWpatchBackend:
    """WASAPI loopback via ``PyAudioWPatch`` (fallback); explicit byte decoding."""

    def __init__(self) -> None:
        self._pa = None
        self._stream = None
        self._fmt: OpenedFormat | None = None
        self._np_dtype = np.float32

    def open(self, role: str, settings) -> OpenedFormat:
        import pyaudiowpatch as pyaudio  # type: ignore

        self._pa = pyaudio.PyAudio()
        if role == "them":
            dev = self._pa.get_default_wasapi_loopback()
        else:
            dev = self._pa.get_default_input_device_info()
        rate = int(dev["defaultSampleRate"])
        channels = int(dev.get("maxInputChannels", 1)) or 1
        fmt = pyaudio.paFloat32
        self._np_dtype = np.float32
        block = 1024
        self._stream = self._pa.open(
            format=fmt,
            channels=channels,
            rate=rate,
            input=True,
            frames_per_buffer=block,
            input_device_index=dev["index"],
        )
        self._channels = channels
        self._block = block
        self._fmt = OpenedFormat(
            sample_rate=rate,
            channels=channels,
            sample_format="float32",
            device_id=str(dev["index"]),
            device_name=str(dev.get("name", "")),
            block_frames=block,
            block_duration_ms=block / rate * 1000,
        )
        return self._fmt

    def read(self) -> tuple[np.ndarray, int]:
        raw = self._stream.read(self._block, exception_on_overflow=False)
        arr = np.frombuffer(raw, dtype=self._np_dtype)
        if self._channels > 1:
            arr = arr.reshape(-1, self._channels)
        return arr, arr.shape[0] if arr.ndim == 2 else arr.size

    def close(self) -> None:
        if self._stream is not None:
            with _suppress():
                self._stream.stop_stream()
                self._stream.close()
            self._stream = None
        if self._pa is not None:
            with _suppress():
                self._pa.terminate()
            self._pa = None

    def current_device_id(self) -> str:
        return self._fmt.device_id if self._fmt else ""


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return True


def make_backend(settings) -> AudioBackend:
    """Pick a backend per ``audio_backend`` (auto → soundcard, then pyaudiowpatch)."""
    choice = getattr(settings, "audio_backend", "auto")
    if choice == "pyaudiowpatch":
        return PyAudioWpatchBackend()
    if choice == "soundcard":
        return SoundcardBackend()
    # auto: prefer soundcard, fall back if its import fails at open()
    try:
        import soundcard  # type: ignore  # noqa: F401

        return SoundcardBackend()
    except Exception:
        return PyAudioWpatchBackend()


# --------------------------------------------------------------------------- #
# CaptureManager
# --------------------------------------------------------------------------- #
StatusCb = Callable[[str, str, str], None]


class _SourceRunner:
    """Runs one source's capture loop in its own thread (SPEC.md §4.6)."""

    def __init__(self, source: str, ring: RingBuffer, raw: RawSegmentWriter | None, settings, on_status: StatusCb) -> None:
        self.source = source
        self.ring = ring
        self.raw = raw
        self.settings = settings
        self.on_status = on_status
        self.health = SourceHealth()
        self.opened: OpenedFormat | None = None
        self.available = False
        self.cum_samples = 0
        self.epoch = 0
        self._backend: AudioBackend | None = None
        self._resampler: _Resampler | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._silent_since: float | None = None

    def start(self) -> bool:
        try:
            self._backend = make_backend(self.settings)
            self.opened = self._backend.open(self.source, self.settings)
            self._resampler = _Resampler(self.opened.sample_rate, TARGET_RATE)
            self.available = True
            self._open_epoch(initial=True)
            self._thread = threading.Thread(target=self._loop, name=f"capture-{self.source}", daemon=True)
            self._thread.start()
            return True
        except Exception as exc:
            log.warning("capture source %s failed to open: %s", self.source, exc)
            self.available = False
            self.on_status(self.source, "error", str(exc))
            return False

    def _open_epoch(self, initial: bool) -> None:
        if not initial:
            self.epoch += 1
        self.health.reopen_count = self.epoch
        wall = datetime.now(timezone.utc).astimezone().isoformat()
        self.health.last_epoch_open_wall = wall
        if self.raw is not None:
            self.raw.mark_epoch(self.epoch, wall, self.cum_samples)

    def _loop(self) -> None:
        assert self._backend is not None and self._resampler is not None
        eps = self.settings.silence_rms_eps
        warn_s = self.settings.silent_loopback_warn_s
        while not self._stop.is_set():
            try:
                raw, _frames = self._backend.read()
            except Exception as exc:
                self._handle_device_change(exc)
                continue
            mono = _to_mono(raw, self.opened.channels if self.opened else 1)
            pcm = self._resampler.process(mono)
            if pcm.size == 0:
                continue
            rms = float(np.sqrt(np.mean(np.square(pcm, dtype=np.float64))))
            self.health.rms = rms
            if rms < eps:
                self.health.silent_frames += pcm.size
                if self.source == "them":
                    self._maybe_warn_silent(warn_s)
            else:
                self._silent_since = None
            if self.raw is not None:
                self.raw.write(pcm, self.cum_samples, self.epoch)
            self.ring.write(pcm)
            self.cum_samples += pcm.size

    def _maybe_warn_silent(self, warn_s: float) -> None:
        now = time.monotonic()
        if self._silent_since is None:
            self._silent_since = now
        elif now - self._silent_since >= warn_s:
            self.on_status(self.source, "silent", "no audio from speakers")
            self._silent_since = now  # rate-limit

    def _handle_device_change(self, exc: Exception) -> None:
        log.warning("capture %s read error (device change?): %s", self.source, exc)
        for attempt in range(self.settings.device_reopen_retries):
            if self._stop.is_set():
                return
            try:
                self._backend.close()
            except Exception:
                pass
            time.sleep(0.5)
            try:
                self.opened = self._backend.open(self.source, self.settings)
                self._resampler = _Resampler(self.opened.sample_rate, TARGET_RATE)
                self._open_epoch(initial=False)
                self.on_status(self.source, "reopened", f"attempt {attempt + 1}")
                return
            except Exception as e2:
                log.warning("reopen %s attempt %d failed: %s", self.source, attempt + 1, e2)
        self.available = False
        self.on_status(self.source, "lost", "device lost")
        self._stop.set()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._backend is not None:
            with _suppress():
                self._backend.close()


class CaptureManager:
    """Manage both capture sources (SPEC.md §5.1)."""

    def __init__(
        self,
        ring_you: RingBuffer,
        ring_them: RingBuffer,
        raw_you: RawSegmentWriter | None,
        raw_them: RawSegmentWriter | None,
        settings,
        on_status: StatusCb | None = None,
    ) -> None:
        self.settings = settings
        self.on_status: StatusCb = on_status or (lambda *a: None)
        self._runners = {
            "them": _SourceRunner("them", ring_them, raw_them, settings, self.on_status),
            "you": _SourceRunner("you", ring_you, raw_you, settings, self.on_status),
        }

    def start(self) -> list[CaptureSource]:
        up: list[CaptureSource] = []
        for source, runner in self._runners.items():
            ok = runner.start()
            up.append(CaptureSource(source=source, available=ok, opened=runner.opened, health=runner.health))
        if not any(cs.available for cs in up):
            return []
        return up

    def stop(self) -> None:
        for runner in self._runners.values():
            runner.stop()

    def sources_status(self) -> list[CaptureSource]:
        return [
            CaptureSource(source=s, available=r.available, opened=r.opened, health=r.health)
            for s, r in self._runners.items()
        ]


# --------------------------------------------------------------------------- #
# FileCaptureSource — deterministic capture for tests/recovery (no hardware)
# --------------------------------------------------------------------------- #
class FileCaptureSource:
    """Stream a bundled 16 kHz mono WAV/array into a pipeline (SPEC.md §9.2)."""

    def __init__(self, source: str, pcm: np.ndarray) -> None:
        self.source = source
        self.pcm = np.ascontiguousarray(pcm, dtype=np.float32).reshape(-1)

    def feed_into(self, pipeline, chunk: int = 1600) -> None:
        for i in range(0, self.pcm.size, chunk):
            pipeline.feed(self.source, self.pcm[i:i + chunk])
        pipeline.mark_eof(self.source)
