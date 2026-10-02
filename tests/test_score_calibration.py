from niome_subnet.dashboard.calibration import (
    build_calibration_model,
    calibrated_prediction,
    merge_calibration_records,
)


def records(count=10, residual=2.0):
    return [
        {
            "task_id": f"task-{index}",
            "received_at": f"2026-09-{index + 1:02d}T00:00:00+00:00",
            "builder_policy": "policy-a",
            "local_score": float(100 + index),
            "official_score": float(100 + index + residual),
        }
        for index in range(count)
    ]


def test_validated_residual_correction_and_interval():
    model = build_calibration_model(records())
    prediction = calibrated_prediction(150.0, model, builder_policy="policy-a")
    assert prediction["available"] is True
    assert prediction["scope"] == "policy"
    assert prediction["estimate"] == 152.0
    assert prediction["lower"] == 152.0
    assert prediction["upper"] == 152.0
    assert prediction["point_correction_applied"] is True


def test_correction_is_not_applied_when_holdout_gets_worse():
    sample = records(8, residual=3.0)
    sample[-2]["official_score"] = sample[-2]["local_score"]
    sample[-1]["official_score"] = sample[-1]["local_score"]
    model = build_calibration_model(sample)
    prediction = calibrated_prediction(150.0, model, builder_policy="policy-a")
    assert prediction["available"] is True
    assert prediction["estimate"] == 150.0
    assert prediction["point_correction_applied"] is False


def test_insufficient_records_fail_closed():
    model = build_calibration_model(records(4))
    prediction = calibrated_prediction(150.0, model, builder_policy="policy-a")
    assert prediction == {
        "available": False,
        "reason": "insufficient-training-records",
        "records": 4,
        "minimum_records": 8,
    }


def test_incremental_records_replace_duplicate_payload_and_keep_order():
    old = records(2)
    old[0]["submission_sha256"] = "sha-0"
    old[0]["source"] = "won1"
    old[1]["submission_sha256"] = "sha-1"
    old[1]["source"] = "won1"
    replacement = {**old[0], "official_score": 999.0}

    merged = merge_calibration_records(old, [replacement])

    assert len(merged) == 2
    assert merged[0]["official_score"] == 999.0
    assert merged[1]["submission_sha256"] == "sha-1"
