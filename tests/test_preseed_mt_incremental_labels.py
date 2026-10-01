from tools.preseed_mt_incremental_labels import (
    balanced_choice_prefix_shards,
    balanced_first_choice_shards,
    incremental_stage_indices,
    seed_labels_replay,
    tuple_round_selected,
)


def test_incremental_stage_indices_start_at_initial_bound_prefix():
    assert incremental_stage_indices(8, 4) == [3, 4, 5, 6, 7]
    assert incremental_stage_indices(3, 9) == [2]


def test_tuple_round_window_can_bind_only_one_round():
    assert tuple_round_selected(1, 1, 1)
    assert not tuple_round_selected(2, 1, 1)
    assert tuple_round_selected(5, 3, 0)
    assert not tuple_round_selected(2, 3, 0)


def test_seed_label_replay_counts_only_observed_values():
    # maximum 3 uses a two-bit mask; the middle draw is intentionally unknown.
    result = seed_labels_replay([1, 2, 3], [3, 3, 3], [1, None, 3], {})
    assert result["valid"] is True
    assert result["labels_passed"] == 2


def test_seed_label_replay_detects_mismatch():
    result = seed_labels_replay([1, 2], [3, 3], [1, 3], {})
    assert result["valid"] is False
    assert result["labels_passed"] == 1


def test_balanced_first_choice_shards_are_disjoint_and_complete():
    tuples = [[1, index] for index in range(5)] + [[2, 0], [3, 0], [4, 0]]
    shards = balanced_first_choice_shards(tuples, 3)
    flattened = [value for shard in shards for value in shard]
    assert sorted(flattened) == [1, 2, 3, 4]
    assert len(flattened) == len(set(flattened))
    loads = [sum(values[0] in shard for values in tuples) for shard in shards]
    # The five tuples sharing first choice 1 are indivisible.  The greedy
    # partition therefore reaches the best possible maximum load of five.
    assert sorted(loads) == [1, 2, 5]


def test_balanced_choice_prefix_shards_preserve_exact_tuple_union():
    tuples = [[1, 2, value] for value in range(7)] + [
        [1, 3, 0],
        [2, 4, 0],
        [3, 5, 0],
    ]
    shards = balanced_choice_prefix_shards(tuples, 4, 2)
    prefixes = [prefix for shard in shards for prefix in shard]
    assert sorted(prefixes) == [(1, 2), (1, 3), (2, 4), (3, 5)]
    assert len(prefixes) == len(set(prefixes))
    reconstructed = [
        values
        for shard in shards
        for values in tuples
        if tuple(values[:2]) in set(shard)
    ]
    assert sorted(reconstructed) == sorted(tuples)
