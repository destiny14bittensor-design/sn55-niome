from tools.top_rank_gap_audit import build_report


def score(hotkey: str, value: float, consistency: float) -> dict:
    return {
        "miner_hotkey": hotkey,
        "final_score": value,
        "breakdown": {
            "total_weighted_score": 400.0,
            "consistency_score": consistency,
            "consistency_factor": consistency / 100.0,
            "distribution_fidelity_score": 0.9,
            "distribution_fidelity_factor": 0.9,
            "n_valid_experiments": 250,
        },
    }


def test_build_report_tracks_target_cutoff_and_required_consistency():
    rows = [score(f"other-{index}", 100.0 - index, 20.0) for index in range(35)]
    rows.append(score("tao-hotkey", 60.0, 10.0))

    report = build_report(
        [({"task_id": "task-1", "created_at": "2026-10-02"}, rows)],
        target_rank=30,
        miners={"tao1": "tao-hotkey", "missing": "not-present"},
    )

    current = report["rounds"][0]
    assert current["target_score"] == 71.0
    assert current["miners"]["tao1"]["gap_to_target"] == -11.0
    assert current["miners"]["tao1"][
        "required_consistency_score_at_current_weight_and_fidelity"
    ] == 71.0 / 3.6
    assert current["miners"]["missing"] == {"published": False}
    assert report["cutoff_distribution"]["median"] == 71.0
    assert report["persistent_top_target_cohort"]["minimum_rounds"] == 1
    assert report["persistent_top_target_cohort"]["qualifying_cohort_size"] == 30
    assert report["persistent_top_target_cohort"]["conditional_outcomes"][
        "target_reached"
    ]["observations"] == 30
    assert report["safety"]["uses_credentials"] is False
    assert report["safety"]["uses_current_score_for_current_submission"] is False
