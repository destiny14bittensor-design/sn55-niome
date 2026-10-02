#!/usr/bin/env python3
"""Chronological public-task replay for the four honest builder policies.

The builder sees a placeholder seed, exactly as it does before scoring.  Only
after candidate selection is frozen is the published task seed used by the
local validator clone.  This prevents hindsight leakage into the backtest.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from niome_subnet.genomics.builder_policy import BUILDER_POLICIES
from niome_subnet.genomics.seed_policy import deterministic_stress_seeds
from niome_subnet.genomics.submission_builder import build_submission
from niome_subnet.miner.portfolio_audit import jaccard, pearson
from niome_subnet.miner.task_processor import (
    _candidate_variants,
    _select_candidate_index,
)
from tools.local_validator.artifacts import ArtifactBundle, read_first_fasta, sha256_bytes
from tools.local_validator.evaluator import evaluate_submission


TASKS_URL = "https://niome-api.genomes.io/api/v3/tasks"
SCORES_URL = "https://niome-api.genomes.io/api/v3/miners/scores"
CELL_TYPES_URL = "https://niome-api.genomes.io/api/v3/data/cell-types?format=json"
POLICY_ORDER = (
    "champion-v1",
    "champion-reservoir001-cas55-v3",
    "champion-reservoir003-cas65-v3",
    "champion-reservoir005-v3",
)


def _json_default(value: Any) -> Any:
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _get_json(session: requests.Session, url: str, **params) -> Any:
    response = session.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def _items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("items", "results", "data", "scores"):
            if isinstance(value.get(key), list):
                return [item for item in value[key] if isinstance(item, dict)]
    return []


def _candidate_result(index: int, label: str, rows, result) -> dict[str, Any]:
    return {
        "index": index,
        "label": label,
        "rows": len(rows),
        "final_score": result.final_score,
        "per_seed_final_scores": [
            float(item["stage5"]["final_score"]) for item in result.per_seed
        ],
        "breakdown": result.breakdown,
    }


def _artifact_bundle(
    contract: dict[str, Any],
    reference: dict[str, Any],
    chromosome: str,
    cell_types: dict[str, Any],
) -> ArtifactBundle:
    return ArtifactBundle(
        contract=contract,
        hbb_reference=reference,
        chromosome_11=chromosome,
        cell_types=cell_types,
        manifest={"source": "public-task-backtest"},
    )


def replay_policy(
    *,
    task_id: str,
    contract: dict[str, Any],
    reference: dict[str, Any],
    chromosome: str,
    cell_types: dict[str, Any],
    policy_id: str,
) -> tuple[dict[str, Any], set[str]]:
    policy = BUILDER_POLICIES[policy_id]
    build_contract = dict(contract)
    build_contract["seed"] = 0
    submission, diagnostics = build_submission(
        contract=build_contract,
        reference=reference,
        chromosome_11=chromosome,
        cell_types=cell_types,
        selection_profile=policy.unknown_seed_selection_profile,
        builder_policy=policy,
    )
    candidates = _candidate_variants(submission, policy)
    stress = deterministic_stress_seeds(
        task_id,
        domain=f"robust-holdout|{policy.stress_seed_ensemble_id}",
        count=3,
    )
    stress_contract = dict(build_contract)
    stress_contract["seed"] = ",".join(str(seed) for seed in stress)
    stress_artifacts = _artifact_bundle(
        stress_contract, reference, chromosome, cell_types
    )
    candidate_scores = []
    for index, (label, rows) in enumerate(candidates):
        result = evaluate_submission(rows, stress_artifacts)
        candidate_scores.append(_candidate_result(index, label, rows, result))
    selected_index = _select_candidate_index(candidate_scores, policy)
    selected_label, selected = candidates[selected_index]
    official_artifacts = _artifact_bundle(
        contract, reference, chromosome, cell_types
    )
    official = evaluate_submission(selected, official_artifacts)
    raw = json.dumps(selected, separators=(",", ":")).encode()
    return (
        {
            "policy_id": policy_id,
            "role": policy.role,
            "selected_candidate": selected_label,
            "rows": len(selected),
            "submission_sha256": sha256_bytes(raw),
            "official_seed_replay_score": official.final_score,
            "official_seed_replay_breakdown": official.breakdown,
            "invalid_experiments": len(official.invalid_experiments),
            "stress_seeds": stress,
            "stress_candidates": candidate_scores,
            "builder": {
                "selection_strategy": diagnostics["selection_strategy"],
                "joint_bucket_quotas": diagnostics["joint_bucket_quotas"],
            },
        },
        {str(row["experiment_id"]) for row in selected},
    )


def _split(index: int, count: int) -> str:
    if count >= 15:
        if index < 8:
            return "design"
        if index < 11:
            return "selection"
        return "final"
    if index >= max(0, count - 4):
        return "final"
    return "design"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=15)
    parser.add_argument("--target-rank", type=int, default=80)
    parser.add_argument(
        "--require-scores",
        action="store_true",
        help="skip tasks whose official scoreboard has not been published",
    )
    parser.add_argument("--chromosome", type=Path, default=ROOT / "data" / "chr11.fa")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts" / "portfolio" / "backtest.json",
    )
    args = parser.parse_args()
    if args.tasks < 1:
        parser.error("--tasks must be positive")

    session = requests.Session()
    tasks = _items(_get_json(session, TASKS_URL, page=1, per_page=args.tasks))
    tasks = sorted(tasks[: args.tasks], key=lambda item: str(item.get("created_at") or ""))
    if not tasks:
        raise RuntimeError("public task API returned no tasks")
    cell_types = _get_json(session, CELL_TYPES_URL)
    chromosome, _ = read_first_fasta(args.chromosome)

    rounds = []
    score_series = {policy_id: [] for policy_id in POLICY_ORDER}
    for index, task in enumerate(tasks):
        task_id = str(task["id"])
        content = task["content"]
        contract = content["contract"]
        reference = content["hbb_reference"]
        published_scores = _items(_get_json(session, SCORES_URL, task_id=task_id))
        score_values = sorted(
            [
                float(item["final_score"])
                for item in published_scores
                if isinstance(item.get("final_score"), (int, float))
            ],
            reverse=True,
        )
        if args.require_scores and not score_values:
            print(f"skipping unscored task {task_id}", flush=True)
            continue
        target_cutoff = (
            score_values[args.target_rank - 1]
            if len(score_values) >= args.target_rank
            else None
        )
        policies = {}
        ids = {}
        for policy_id in POLICY_ORDER:
            print(
                f"building {index + 1}/{len(tasks)} {task_id} {policy_id}",
                flush=True,
            )
            result, result_ids = replay_policy(
                task_id=task_id,
                contract=contract,
                reference=reference,
                chromosome=chromosome,
                cell_types=cell_types,
                policy_id=policy_id,
            )
            replay_score = result["official_seed_replay_score"]
            result["estimated_public_rank"] = 1 + sum(
                score > replay_score for score in score_values
            ) if score_values else None
            result["target_rank"] = args.target_rank
            result["target_cutoff"] = target_cutoff
            result["target_rank_replay"] = bool(
                target_cutoff is not None and replay_score >= target_cutoff
            )
            policies[policy_id] = result
            ids[policy_id] = result_ids
            score_series[policy_id].append(replay_score)
        overlaps = []
        for left_index, left in enumerate(POLICY_ORDER):
            for right in POLICY_ORDER[left_index + 1 :]:
                overlaps.append(
                    {"left": left, "right": right, "jaccard": jaccard(ids[left], ids[right])}
                )
        rounds.append(
            {
                "task_id": task_id,
                "created_at": task.get("created_at"),
                "split": _split(index, len(tasks)),
                "target_rank": args.target_rank,
                "target_cutoff": target_cutoff,
                "best_replay_score": max(
                    value["official_seed_replay_score"] for value in policies.values()
                ),
                "best_estimated_rank": min(
                    value["estimated_public_rank"]
                    for value in policies.values()
                    if value["estimated_public_rank"] is not None
                ) if score_values else None,
                "any_target_rank_replay": any(value["target_rank_replay"] for value in policies.values()),
                "policies": policies,
                "payload_overlaps": overlaps,
            }
        )
        print(f"replayed {index + 1}/{len(tasks)} {task_id}", flush=True)

    correlations = []
    for left_index, left in enumerate(POLICY_ORDER):
        for right in POLICY_ORDER[left_index + 1 :]:
            correlations.append(
                {"left": left, "right": right, "correlation": pearson(score_series[left], score_series[right])}
            )
    final_rounds = [item for item in rounds if item["split"] == "final"]
    gates = {
        "all_submissions_valid": all(
            policy["invalid_experiments"] == 0
            for item in rounds
            for policy in item["policies"].values()
        ),
        "best_of_four_target_three_of_final_four": (
            len(final_rounds) == 4
            and sum(item["any_target_rank_replay"] for item in final_rounds) >= 3
        ),
        "payload_jaccard_below_0_65": all(
            overlap["jaccard"] is not None and overlap["jaccard"] < 0.65
            for item in final_rounds
            for overlap in item["payload_overlaps"]
        ),
        "score_correlation_below_0_75": all(
            item["correlation"] is not None and item["correlation"] < 0.75
            for item in correlations
        ),
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "unknown-seed build -> frozen stress selection -> published-seed replay",
        "split": "chronological 8 design / 3 selection / 4 final when 15 tasks are supplied",
        "task_count": len(tasks),
        "policies": list(POLICY_ORDER),
        "gates": gates,
        "promotable": all(gates.values()),
        "correlations": correlations,
        "rounds": rounds,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(json.dumps({"gates": gates, "promotable": report["promotable"]}, indent=2))
    return 0 if report["promotable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
