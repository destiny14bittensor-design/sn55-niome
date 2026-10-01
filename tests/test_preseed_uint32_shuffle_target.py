import numpy as np

from tools.preseed_uint32_shuffle_target import compatible_shuffle_candidates


def test_full_public_shuffle_candidate_validation_accepts_true_seed():
    initial = [f"u{index}" for index in range(32)]
    shuffled = np.asarray(initial, dtype=object)
    np.random.RandomState(42).shuffle(shuffled)
    observed = [value for index, value in enumerate(shuffled.tolist()) if index not in {3, 17}]
    assert compatible_shuffle_candidates([42], initial, observed) == [42]


def test_full_public_shuffle_candidate_validation_rejects_wrong_seed():
    initial = [f"u{index}" for index in range(32)]
    shuffled = np.asarray(initial, dtype=object)
    np.random.RandomState(42).shuffle(shuffled)
    observed = shuffled.tolist()
    assert compatible_shuffle_candidates([43], initial, observed) == []
