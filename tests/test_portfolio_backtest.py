import json

import pytest

from tools.portfolio_backtest import reference_submission


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
