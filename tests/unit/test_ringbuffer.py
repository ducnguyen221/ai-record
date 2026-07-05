import numpy as np

from ai_record.audio.ringbuffer import RingBuffer


def test_roundtrip_start_index():
    rb = RingBuffer(1000)
    rb.write(np.arange(100, dtype=np.float32))
    block, start = rb.read(50)
    assert start == 0
    assert np.allclose(block, np.arange(50))
    block2, start2 = rb.read(50)
    assert start2 == 50
    assert np.allclose(block2, np.arange(50, 100))


def test_wraparound():
    rb = RingBuffer(8)
    rb.write(np.arange(6, dtype=np.float32))
    rb.read(6)  # advance past the seam
    rb.write(np.arange(6, 12, dtype=np.float32))  # wraps
    block, start = rb.read(6)
    assert start == 6
    assert np.allclose(block, np.arange(6, 12))


def test_overflow_counts_drops_and_tracks_abs_index():
    rb = RingBuffer(4)
    rb.write(np.arange(4, dtype=np.float32))     # full: [0,1,2,3]
    dropped = rb.write(np.arange(4, 6, dtype=np.float32))  # drop 2 oldest
    assert dropped == 2
    assert rb.dropped_frames == 2
    block, start = rb.read(4)
    # oldest two (0,1) were dropped; remaining is 2,3,4,5 with abs start index 2
    assert start == 2
    assert np.allclose(block, np.array([2, 3, 4, 5]))


def test_write_longer_than_capacity_keeps_newest():
    rb = RingBuffer(4)
    dropped = rb.write(np.arange(10, dtype=np.float32))
    assert dropped == 6
    block, start = rb.read(4)
    assert np.allclose(block, np.array([6, 7, 8, 9]))
    assert start == 6
