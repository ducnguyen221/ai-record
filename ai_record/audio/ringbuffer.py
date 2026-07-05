"""Thread-safe numpy ring buffer with a dropped-sample counter (SPEC.md §4.6).

The *live* ring buffer is lossy by design: capture must never block on downstream
work, so when the buffer is full the oldest unread samples are overwritten and a
``dropped_frames`` counter is incremented. The crash-safe WAV (store.RawSegmentWriter)
remains the source of truth. Reads report the absolute sample index of the block so
the segmenter can stay sample-accurate even across drops (SPEC.md §4.8).
"""

from __future__ import annotations

import threading

import numpy as np


class RingBuffer:
    """A single-producer/single-consumer float32 mono ring buffer."""

    def __init__(self, capacity_samples: int) -> None:
        if capacity_samples <= 0:
            raise ValueError("capacity_samples must be > 0")
        self._capacity = int(capacity_samples)
        self._buf = np.zeros(self._capacity, dtype=np.float32)
        self._write = 0          # next write position
        self._count = 0          # unread samples currently stored
        self._read_abs = 0       # absolute index of the next sample to be read
        self._dropped = 0        # total samples overwritten before being read
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def dropped_frames(self) -> int:
        with self._lock:
            return self._dropped

    def available(self) -> int:
        with self._lock:
            return self._count

    def write(self, pcm: np.ndarray) -> int:
        """Append ``pcm`` (float32 mono). Returns the number of dropped samples.

        If ``pcm`` is longer than the capacity only the most recent ``capacity``
        samples are kept.
        """
        data = np.ascontiguousarray(pcm, dtype=np.float32).reshape(-1)
        n = data.size
        if n == 0:
            return 0
        with self._lock:
            if n >= self._capacity:
                # Keep only the newest `capacity` samples.
                data = data[-self._capacity:]
                n = data.size
                dropped_now = self._count + (int(pcm.size) - n)
                self._buf[:] = data
                self._write = 0
                self._read_abs += dropped_now
                self._count = self._capacity
                self._dropped += dropped_now
                return dropped_now

            free = self._capacity - self._count
            dropped_now = 0
            if n > free:
                dropped_now = n - free
                self._read_abs += dropped_now
                self._count -= dropped_now
                self._dropped += dropped_now

            end = self._write + n
            if end <= self._capacity:
                self._buf[self._write:end] = data
            else:
                first = self._capacity - self._write
                self._buf[self._write:] = data[:first]
                self._buf[: n - first] = data[first:]
            self._write = end % self._capacity
            self._count += n
            return dropped_now

    def read(self, max_samples: int) -> tuple[np.ndarray, int]:
        """Read up to ``max_samples``. Returns ``(pcm, start_abs)``.

        ``start_abs`` is the absolute (drop-adjusted) sample index of the first
        returned sample. When nothing is available returns an empty array.
        """
        with self._lock:
            n = min(max_samples, self._count)
            start_abs = self._read_abs
            if n <= 0:
                return np.empty(0, dtype=np.float32), start_abs
            read_pos = (self._write - self._count) % self._capacity
            end = read_pos + n
            if end <= self._capacity:
                out = self._buf[read_pos:end].copy()
            else:
                first = self._capacity - read_pos
                out = np.concatenate((self._buf[read_pos:], self._buf[: n - first]))
            self._count -= n
            self._read_abs += n
            return out, start_abs
