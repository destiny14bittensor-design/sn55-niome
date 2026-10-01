from tools.preseed_fisher_yates_model import replay_shuffle
from tools.preseed_shuffle_prefix_tuples import exact_prefix_tuples


def test_exact_permutation_yields_one_correlated_tuple():
    initial = [f"u{index}" for index in range(8)]
    choices = [3, 1, 4, 0, 2, 1, 0]
    result = exact_prefix_tuples(initial, replay_shuffle(initial, choices), depth=4)
    assert result["status"] == "sat"
    assert result["choice_indices"] == [7, 6, 5, 4]
    assert result["choice_tuples"] == [tuple(choices[:4])]


def test_incomplete_depth_is_not_emitted_after_cap():
    result = exact_prefix_tuples(
        ["a", "b", "c", "d"], ["a", "b", "c"], depth=3, max_states=2
    )
    assert result["status"] == "unknown"
    assert result["completed_depth"] == 1
    assert all(len(values) == 1 for values in result["choice_tuples"])
