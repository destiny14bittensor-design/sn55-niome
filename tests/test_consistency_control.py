import pytest

from niome_subnet.genomics.consistency_control import (
    ConsistencySample,
    all_seed_hdr_share_for_target,
    decide_consistency_target,
)


def sample(task_id: str, normalized_top: float) -> ConsistencySample:
    baseline = 250.0
    return ConsistencySample(
        task_id=task_id,
        top_score=baseline * normalized_top,
        baseline_score=baseline,
        realized_consistency=1.0,
    )


def test_insufficient_history_fails_back_to_maximum_score():
    decision = decide_consistency_target(
        [sample("a", 0.68), sample("b", 0.70)]
    )

    assert decision.mode == "max-score"
    assert decision.target_consistency is None
    assert "at least 3" in decision.reason


def test_target_uses_normalized_quantile_and_safety_margin():
    decision = decide_consistency_target(
        [
            sample("a", 0.66),
            sample("b", 0.68),
            sample("c", 0.70),
            sample("d", 0.72),
            sample("e", 0.74),
        ]
    )

    # q80 is 0.724 for five linearly-interpolated samples; add a 5% margin.
    assert decision.mode == "targeted"
    assert decision.required_consistency == pytest.approx(0.7602)
    assert decision.target_consistency == pytest.approx(0.7602)
    assert decision.all_seed_hdr_share == pytest.approx(
        ((0.7602 - 0.70) / 0.30) ** 0.5 + 0.05
    )


def test_unsafe_or_impossible_target_fails_back_to_maximum_score():
    too_high = decide_consistency_target(
        [sample("a", 0.90), sample("b", 0.92), sample("c", 0.94)]
    )
    too_low = decide_consistency_target(
        [sample("a", 0.40), sample("b", 0.42), sample("c", 0.44)]
    )

    assert too_high.mode == "max-score"
    assert too_high.required_consistency > 0.85
    assert too_low.mode == "max-score"
    assert too_low.required_consistency < 0.60


def test_full_hdr_share_is_clamped_at_both_anchors():
    assert all_seed_hdr_share_for_target(0.60) == 0.0
    assert all_seed_hdr_share_for_target(0.70) == pytest.approx(0.0)
    assert all_seed_hdr_share_for_target(0.85) == pytest.approx(0.5**0.5 + 0.05)
    assert all_seed_hdr_share_for_target(1.10) == 1.0
