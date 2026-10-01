import random

from tools.preseed_shuffle_sampled_bits import (
    recover_choices,
    replay_choices,
    sample_completion,
    sample_row,
)


def test_choice_inversion_round_trips_arbitrary_permutations():
    rng = random.Random(7)
    for size in range(2, 12):
        permutation = list(range(size))
        rng.shuffle(permutation)
        assert replay_choices(size, recover_choices(permutation)) == permutation


def test_sampled_completion_preserves_observed_domain_subsequence():
    initial = ["a", "b", "a", "c", "d"]
    observed = ["c", "a", "b"]
    completed = sample_completion(initial, observed, random.Random(1))
    tokens = [initial[position] for position in completed]
    cursor = iter(tokens)
    assert all(any(value == target for value in cursor) for target in observed)


def test_complete_unique_order_has_only_stable_bits():
    row = {
        "task_id": "synthetic",
        "initial_uids": list(range(5)),
        "initial_domain_sequence": [f"u{uid}" for uid in range(5)],
        "ordered_uid_domains": ["u3", "u4", "u0", "u2", "u1"],
        "uid_domains": {f"u{uid}": [uid] for uid in range(5)},
    }
    result = sample_row(row, 20, 11)
    expected_bits = sum(choice.bit_length() or 1 for choice in range(4, 0, -1))
    assert result["stable_bit_count"] == expected_bits
