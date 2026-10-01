from tools.preseed_shuffle_metagraph_align import (
    alignment_stats,
    offset_rank_key,
    offset_summary,
)


def test_alignment_counts_unique_and_ambiguous_positions_without_tokens():
    stats = alignment_stats(
        ["a", "b", "b", "c", "extra"],
        {"a": [1], "b": [2, 3], "c": [4], "missing": [5]},
    )
    assert stats == {
        "observed_positions": 5,
        "expected_queryable_uids": 5,
        "matched_multiset_positions": 4,
        "missing_expected_positions": 1,
        "extra_observed_positions": 1,
        "exact_unique_uid_positions": 2,
        "ambiguous_shared_endpoint_positions": 2,
        "counter_exact": False,
    }


def test_offset_summary_and_rank_prefer_observed_overlap():
    rounds = [
        {
            "block_offset": -4,
            "counter_exact": False,
            "matched_multiset_positions": 10,
            "exact_unique_uid_positions": 7,
            "missing_expected_positions": 2,
            "extra_observed_positions": 1,
        },
        {
            "block_offset": 0,
            "counter_exact": False,
            "matched_multiset_positions": 9,
            "exact_unique_uid_positions": 8,
            "missing_expected_positions": 1,
            "extra_observed_positions": 1,
        },
    ]
    lagged = offset_summary(rounds, -4)
    current = offset_summary(rounds, 0)
    assert lagged["aligned_rounds"] == 1
    assert lagged["matched_multiset_positions"] == 10
    assert offset_rank_key(lagged) > offset_rank_key(current)
