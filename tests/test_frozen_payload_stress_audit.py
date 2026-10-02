import pytest

from tools.frozen_payload_stress_audit import (
    best_of_pair_summary,
    paired_summary,
    parse_order_variant,
    parse_submission,
    percentile,
    salted_order,
    submission_summary,
)


def test_percentile_interpolates_small_sample():
    assert percentile([1.0, 3.0, 5.0], 0.25) == 2.0
    assert percentile([], 0.25) is None


def test_submission_summary_reports_lower_tail_and_validity():
    result = submission_summary([
        {"score": 1.0, "consistency": 4.0, "invalid_experiments": 0},
        {"score": 3.0, "consistency": 6.0, "invalid_experiments": 1},
        {"score": 5.0, "consistency": 8.0, "invalid_experiments": 0},
    ])

    assert result["score_p25"] == 2.0
    assert result["score_median"] == 3.0
    assert result["consistency_p25"] == 5.0
    assert result["all_valid"] is False


def test_paired_summary_uses_right_minus_left_direction():
    left = [
        {"score": 1.0},
        {"score": 3.0},
        {"score": 5.0},
    ]
    right = [
        {"score": 2.0},
        {"score": 2.0},
        {"score": 5.0},
    ]

    result = paired_summary(left, right)

    assert result["left_wins"] == 1
    assert result["right_wins"] == 1
    assert result["ties"] == 1
    assert result["right_win_rate"] == pytest.approx(1 / 3)
    assert result["right_minus_left_median"] == 0.0


def test_best_of_pair_summary_measures_portfolio_tail():
    left = [
        {"score": 1.0, "consistency": 2.0, "invalid_experiments": 0},
        {"score": 5.0, "consistency": 6.0, "invalid_experiments": 0},
    ]
    right = [
        {"score": 3.0, "consistency": 4.0, "invalid_experiments": 0},
        {"score": 4.0, "consistency": 5.0, "invalid_experiments": 0},
    ]

    result = best_of_pair_summary(left, right)

    assert result["score_minimum"] == 3.0
    assert result["score_median"] == 4.0
    assert result["consistency_median"] == 5.0


def test_parse_submission_requires_label_and_path():
    assert parse_submission("control=/tmp/control.json")[0] == "control"
    with pytest.raises(Exception):
        parse_submission("missing-separator")


def test_salted_order_is_deterministic_and_preserves_rows():
    rows = [
        {"experiment_id": "first", "value": 1},
        {"experiment_id": "second", "value": 2},
        {"experiment_id": "third", "value": 3},
    ]

    first = salted_order(rows, "rank30-order-a-v1")
    second = salted_order(rows, "rank30-order-a-v1")

    assert first == second
    assert sorted(item["experiment_id"] for item in first) == [
        "first",
        "second",
        "third",
    ]
    assert rows == [
        {"experiment_id": "first", "value": 1},
        {"experiment_id": "second", "value": 2},
        {"experiment_id": "third", "value": 3},
    ]


def test_parse_order_variant_requires_source_and_salt():
    assert parse_order_variant("candidate=base:rank30-order-a-v1") == (
        "candidate",
        "base",
        "rank30-order-a-v1",
    )
    with pytest.raises(Exception):
        parse_order_variant("candidate=base")
