from tools.preseed_fisher_yates_model import (
    construct_group_shuffle,
    invert_exact_shuffle,
    is_subsequence,
    replay_shuffle,
)


def test_exact_fisher_yates_inversion_round_trips():
    initial = list(range(8))
    choices = [3, 1, 5, 0, 2, 1, 0]
    final = replay_shuffle(initial, choices)
    assert invert_exact_shuffle(initial, final) == choices


def test_subsequence_handles_repeated_group_labels():
    assert is_subsequence(["a", "b", "a"], ["x", "a", "a", "b", "a"])
    assert not is_subsequence(["a", "b", "b"], ["a", "b", "a"])


def test_constructive_group_witness_handles_duplicates_and_missing_labels():
    result = construct_group_shuffle(
        ["a", "a", "b", "c", "d"], ["a", "c", "a", "d"]
    )
    assert result["status"] == "sat"
    final = replay_shuffle(
        ["a", "a", "b", "c", "d"], result["descending_choices"]
    )
    assert is_subsequence(["a", "c", "a", "d"], final)
