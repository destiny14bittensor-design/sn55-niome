from tools.public_score_cluster_audit import build_report, score_fingerprint


def _row(task, hotkey, score=1.0):
    return {
        "task_id": task,
        "miner_hotkey": hotkey,
        "final_score": score,
        "weight": 0.1,
        "breakdown": {
            "consistency_factor": 0.7,
            "consistency_score": 2.0,
            "distribution_fidelity_factor": 0.8,
            "distribution_fidelity_score": 3.0,
            "total_weighted_score": 4.0,
            "n_valid_experiments": 250,
        },
    }


def test_fingerprint_ignores_identity_fields():
    assert score_fingerprint(_row("t", "a")) == score_fingerprint(_row("t", "b"))


def test_report_marks_repeated_pair_and_distinct_coldkeys_without_inferring_coordination():
    task_rows = []
    for task in ("t1", "t2"):
        task_rows.append(
            (
                {"task_id": task, "created_at": "2026-01-01T00:00:00Z"},
                [_row(task, "a"), _row(task, "b"), _row(task, "c", 2.0)],
                {"raw_rows": 3, "unique_score_ids": 3, "unique_hotkeys": 3, "duplicate_hotkey_rows_removed": 0},
            )
        )
    report = build_report(task_rows, lambda hotkey: {"a": "ca", "b": "cb"}.get(hotkey))
    pair = report["repeated_identical_pairs"][0]
    assert pair["hotkeys"] == ["a", "b"]
    assert pair["round_count"] == 2
    assert pair["same_coldkey"] is False
    assert report["safety"]["coordination_is_not_inferred"] is True
