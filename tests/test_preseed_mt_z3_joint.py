from collections import Counter

from tools.preseed_mt_z3_joint import chronological_rows, interval_mask, missing_group_counts


def test_interval_mask_matches_numpy_legacy_masks():
    assert interval_mask(1) == 1
    assert interval_mask(2) == 3
    assert interval_mask(255) == 255
    assert interval_mask(899) == 1023


def test_missing_group_counts_preserves_multiplicity():
    row = {
        "initial_domain_sequence": ["a", "b", "a", "c"],
        "ordered_group_domains": ["a", "c"],
    }
    assert missing_group_counts(row) == Counter({"a": 1, "b": 1})


def test_chronological_rows_keeps_sealed_public_shuffle():
    payload = {
        "rounds": [
            {"shuffle_size": 0, "task_id": "skip"},
            {"shuffle_size": 256, "task_id": "discovery"},
            {"shuffle_size": 256, "task_id": "sealed"},
        ]
    }
    assert [row["task_id"] for row in chronological_rows(payload, 2)] == [
        "discovery",
        "sealed",
    ]
