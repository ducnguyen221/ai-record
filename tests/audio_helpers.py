"""Synthetic 16 kHz mono audio builders for the test suite (no hardware/models)."""

from __future__ import annotations

import numpy as np

SR = 16000


def tone(duration_s: float, freq: float = 220.0, amp: float = 0.3, sr: int = SR) -> np.ndarray:
    n = int(duration_s * sr)
    t = np.arange(n, dtype=np.float32) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(duration_s: float, sr: int = SR) -> np.ndarray:
    return np.zeros(int(duration_s * sr), dtype=np.float32)


def sequence(*parts: np.ndarray) -> np.ndarray:
    return np.concatenate(parts).astype(np.float32)
