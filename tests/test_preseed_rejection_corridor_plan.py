import numpy as np

from tools.preseed_rejection_corridor_plan import (
    checkpoint_draws,
    greedy_corridor_cover,
    simulate_rejection_profiles,
    simulate_rejection_profiles_with_gaps,
)


def test_checkpoint_draws_always_includes_final_draw():
    assert checkpoint_draws(10, 4) == [4, 8, 10]
    assert checkpoint_draws(8, 4) == [4, 8]


def test_rejection_simulation_is_deterministic_and_monotone():
    left = simulate_rejection_profiles(
        [2, 1, 3], [1, 3], samples=100, rng=np.random.default_rng(7)
    )
    right = simulate_rejection_profiles(
        [2, 1, 3], [1, 3], samples=100, rng=np.random.default_rng(7)
    )
    assert np.array_equal(left, right)
    assert np.all(left[:, 1] >= left[:, 0])


def test_greedy_corridor_coverage_is_monotone():
    profiles = np.array([[0, 0], [0, 1], [5, 5], [5, 6]], dtype=np.int32)
    indices, coverage = greedy_corridor_cover(
        profiles,
        half_width=1,
        corridor_count=2,
        candidate_count=4,
        rng=np.random.default_rng(11),
    )
    assert len(indices) == 2
    assert coverage[0] == 0.5
    assert coverage[1] == 1.0


def test_exact_gap_paths_reconstruct_checkpoint_profiles():
    checkpoints = [2, 4]
    profiles, gaps = simulate_rejection_profiles_with_gaps(
        [2, 1, 3, 2],
        checkpoints,
        samples=50,
        rng=np.random.default_rng(19),
    )
    assert gaps.shape == (50, 4)
    assert np.array_equal(profiles[:, 0], gaps[:, :2].sum(axis=1))
    assert np.array_equal(profiles[:, 1], gaps.sum(axis=1))
