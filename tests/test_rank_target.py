from __future__ import annotations

import pytest

from niome_subnet.miner.rank_target import analyze_rank_target


def row(hotkey: str, score: float, consistency: float) -> dict:
    return {
        "miner_hotkey": hotkey,
        "final_score": score,
        "breakdown": {
            "total_weighted_score": 400.0,
            "consistency_factor": consistency,
            "distribution_fidelity_factor": 0.8,
        },
    }


def test_rank_target_reports_required_consistency_and_gap() -> None:
    report = analyze_rank_target(
        [row("leader", 50.0, 0.15625), row("cutoff", 40.0, 0.125), row("mine", 32.0, 0.1)],
        {"won1": "mine"},
        target_rank=2,
        safety_margin=0.05,
    )

    lane = report["lanes"]["won1"]
    assert report["target_cutoff"]["score"] == 40.0
    assert report["safe_target_score"] == 42.0
    assert lane["rank"] == 3
    assert lane["gap_to_cutoff"] == -8.0
    assert lane["required_consistency_at_current_weighted_fidelity"] == 0.125
    assert lane["relative_consistency_lift"] == pytest.approx(0.25)
    assert lane["score_identity_error"] == pytest.approx(0.0)


def test_rank_target_rejects_missing_cutoff() -> None:
    with pytest.raises(ValueError, match="requested rank"):
        analyze_rank_target([row("mine", 32.0, 0.1)], {"won1": "mine"}, target_rank=2)
