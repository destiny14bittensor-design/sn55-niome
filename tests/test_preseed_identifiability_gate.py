import math

import pytest

from tools.preseed_identifiability_gate import (
    build_report,
    exact_uid_information_upper_bound,
    ordered_distinct_bits,
)


def test_ordered_distinct_information():
    assert ordered_distinct_bits(100, 999, 3) == pytest.approx(
        math.log2(900 * 899 * 898)
    )


def test_twenty_tasks_do_not_identify_mt19937():
    state = {
        "split_policy": {"counts": {"discovery": 20}},
        "epoch_policy": {
            "expected": {
                "seed_count": 3,
                "seed_range": [100, 999],
                "distinct": True,
            }
        },
    }
    summary = build_report(state)["summary"]
    assert summary["seed_values"] == 60
    assert summary["maximum_label_information_bits"] < 600
    assert summary["minimum_unresolved_mt19937_bits_from_labels_only"] > 19_000
    assert summary["labels_only_mt19937_identifiable"] is False


def test_exact_uid_shuffle_information_is_an_optimistic_permutation_bound():
    constraints = {
        "rounds": [
            {
                "shuffle_size": 4,
                "ordered_uid_domains": ["?", "u2", "u1", "u2"],
                "uid_domains": {"?": [0, 1, 2, 3], "u1": [1], "u2": [2]},
            },
            {
                "shuffle_size": 3,
                "ordered_uid_domains": ["?", "?"],
                "uid_domains": {"?": [0, 1, 2]},
            },
        ]
    }
    bound = exact_uid_information_upper_bound(constraints)
    assert bound["shuffle_rounds"] == 2
    assert bound["exact_uid_observations"] == 2
    assert bound["maximum_shuffle_information_bits"] == pytest.approx(
        math.log2(4 * 3)
    )
