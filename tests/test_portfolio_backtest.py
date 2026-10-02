import json

import pytest

from tools.portfolio_backtest import reference_submission, replay_policy


def test_reference_submission_returns_ids_and_sha(tmp_path):
    path = tmp_path / "submission.json"
    path.write_text(
        json.dumps([
            {"experiment_id": "first"},
            {"experiment_id": "second"},
        ]),
        encoding="utf-8",
    )

    identifiers, digest = reference_submission(path)

    assert identifiers == {"first", "second"}
    assert len(digest) == 64


def test_reference_submission_rejects_duplicate_ids(tmp_path):
    path = tmp_path / "submission.json"
    path.write_text(
        json.dumps([
            {"experiment_id": "same"},
            {"experiment_id": "same"},
        ]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unique experiment_id"):
        reference_submission(path)


def test_replay_policy_does_not_label_placeholder_seed_as_published(monkeypatch):
    submission = [{"experiment_id": "one"}]
    evaluation = type(
        "Evaluation",
        (),
        {
            "final_score": 12.0,
            "per_seed": [{"stage5": {"final_score": 12.0}}],
            "breakdown": {"n_valid_experiments": 1},
            "invalid_experiments": [],
        },
    )()
    monkeypatch.setattr(
        "tools.portfolio_backtest.build_submission",
        lambda **kwargs: (submission, {
            "selection_strategy": "ranked-reservoir",
            "joint_bucket_quotas": {},
        }),
    )
    monkeypatch.setattr(
        "tools.portfolio_backtest.evaluate_submission",
        lambda *args, **kwargs: evaluation,
    )

    result, _ = replay_policy(
        task_id="task",
        contract={"seed": 0},
        reference={},
        chromosome="",
        cell_types={},
        policy_id="champion-reservoir003-cas65-v3",
    )

    assert result["selection_score"] == 12.0
    assert result["published_seed_available"] is False
    assert result["published_seed_replay_score"] is None
    assert result["published_seed_replay_breakdown"] is None
