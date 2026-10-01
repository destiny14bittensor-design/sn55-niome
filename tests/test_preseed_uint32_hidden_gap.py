import random

import numpy as np

from tools.preseed_uint32_hidden_gap import (
    python_mt_raw_words,
    scan_numpy_shuffle_prefix_gaps,
    scan_python_float_gaps,
    scan_python_gaps,
)


def test_python_state_bridge_matches_getrandbits_words():
    rng = random.Random(123456789)
    for bits in (10, 10, 7, 32, 10):
        rng.getrandbits(bits)
    expected_rng = random.Random()
    expected_rng.setstate(rng.getstate())
    expected = np.asarray(
        [expected_rng.getrandbits(32) for _ in range(64)], dtype=np.uint32
    )
    assert np.array_equal(python_mt_raw_words(rng, 64), expected)


def test_python_gap_scanner_finds_exact_triplet_after_hidden_word():
    # randrange(100, 1000) takes the high ten bits of each MT word.
    raw = np.asarray(
        [999 << 22, 391 << 22, 110 << 22, 279 << 22, 0], dtype=np.uint32
    )
    assert 1 in list(scan_python_gaps(raw, 1))


def test_numpy_shuffle_prefix_scanner_finds_correlated_tuple():
    values = (28, 109, 156, 160)
    key = sum(value << (8 * index) for index, value in enumerate(values))
    raw = np.asarray([999, *values, 0], dtype=np.uint32)
    hits = list(
        scan_numpy_shuffle_prefix_gaps(raw, np.asarray([key], dtype=np.uint64), 1)
    )
    assert 1 in hits


def test_python_float_gap_scanner_finds_exact_triplet():
    rng = random.Random(987654)
    raw = np.asarray([0, *[rng.getrandbits(32) for _ in range(6)]], dtype=np.uint32)
    replay = random.Random(987654)
    target = [100 + int(replay.random() * 900) for _ in range(3)]
    assert 1 in list(scan_python_float_gaps(raw, 1, *target))
