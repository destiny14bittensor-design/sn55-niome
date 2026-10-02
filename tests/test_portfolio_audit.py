import json
from pathlib import Path

import pytest

from niome_subnet.miner.portfolio_audit import (
    build_portfolio_report,
    jaccard,
    pearson,
)


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _lane(root: Path, policy: str, lane_index: int) -> None:
    _write(
        root / "_runtime" / "miner.json",
        {"builder_policy": {"policy_id": policy}},
    )
    for round_index in range(4):
        task = root / f"task-{round_index}"
        ids = [f"lane-{lane_index}-round-{round_index}-row-{row}" for row in range(10)]
        _write(task / "submission.json", [{"experiment_id": value} for value in ids])
        _write(
            task / "manifest.json",
            {
                "builder_policy": {"policy_id": policy},
                "files": {"submission.json": f"sha-{lane_index}-{round_index}"},
            },
        )
        _write(
            task / "status.json",
            {"received_at": f"2026-10-0{round_index + 1}T00:00:00+00:00"},
        )
        _write(
            task / "local_validation.json",
            {"final_score": 50 + lane_index * round_index + round_index},
        )


def test_similarity_helpers_are_bounded_and_require_evidence():
    assert jaccard({"a", "b"}, {"b", "c"}) == 1 / 3
    assert pearson([1, 2], [2, 3]) is None
    assert pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)


def test_report_observes_distinct_runtime_policies_and_payloads(tmp_path):
    policies = {
        "bitcoin1": "champion-v1",
        "bitcoin2": "maximin-v1",
        "hype1": "fidelity-v1",
        "hype2": "upside-v1",
    }
    roots = {lane: tmp_path / lane for lane in policies}
    for lane_index, (lane, policy) in enumerate(policies.items()):
        _lane(roots[lane], policy, lane_index)

    report = build_portfolio_report(roots, expected_policies=policies)

    assert report["common_task_count"] == 4
    assert report["gates"]["runtime_policy_match"] is True
    assert report["gates"]["payload_distinctness"] is True
    assert report["gates"]["payload_jaccard_below_limit"] is True
    assert report["rounds"][-1]["task_id"] == "task-3"


def test_report_holds_when_runtime_has_not_been_switched(tmp_path):
    policies = {"bitcoin1": "champion-v1", "bitcoin2": "maximin-v1"}
    roots = {lane: tmp_path / lane for lane in policies}
    _lane(roots["bitcoin1"], "champion-v1", 0)
    _lane(roots["bitcoin2"], "champion-v1", 1)

    report = build_portfolio_report(roots, expected_policies=policies)

    assert report["status"] == "hold"
    assert report["gates"]["runtime_policy_match"] is False
