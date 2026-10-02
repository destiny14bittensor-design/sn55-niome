from dataclasses import dataclass

from tools.local_validator.artifacts import ArtifactBundle
from tools.preseed_partial_score_replay import replay_oracles


@dataclass
class Result:
    final_score: float
    per_seed: list[dict]


def test_replay_oracles_builds_strict_seed_prefixes_and_uplifts():
    artifacts = ArtifactBundle(
        contract={"seed": 0},
        hbb_reference={},
        chromosome_11="ACGT",
        cell_types={},
        manifest={},
    )
    calls = []

    def builder(**kwargs):
        seeds = list(kwargs["round_seeds"])
        calls.append(seeds)
        return ([{"known": len(seeds)}], {"focused_candidates_added": len(seeds)})

    def evaluator(submission, scoring_artifacts):
        assert scoring_artifacts.contract["seed"] == "101,202,303"
        known = int(submission[0].get("known", 0))
        scores = [10.0 + 30.0 * known] * 3
        return Result(
            final_score=sum(scores) / 3,
            per_seed=[{"stage5": {"final_score": value}} for value in scores],
        )

    report = replay_oracles(
        artifacts,
        [{"robust": True}],
        [101, 202, 303],
        builder=builder,
        evaluator=evaluator,
    )
    assert calls == [[101], [101, 202], [101, 202, 303]]
    assert [row["final_score"] for row in report["scenarios"]] == [10.0, 40.0, 70.0, 100.0]
    assert report["scenarios"][-1]["uplift_over_robust"] == 90.0
