import pytest

from tools.frozen_payload_stress_audit import (
    paired_summary,
    parse_submission,
    percentile,
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


def test_parse_submission_requires_label_and_path():
    assert parse_submission("control=/tmp/control.json")[0] == "control"
    with pytest.raises(Exception):
        parse_submission("missing-separator")
