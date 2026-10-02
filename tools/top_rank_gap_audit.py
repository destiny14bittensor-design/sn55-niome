#!/usr/bin/env python3
"""Audit a public NIOME leaderboard against an explicit rank objective.

Only the public task and score APIs are read.  The report deliberately focuses
on score components that are actionable before a future task's seed is known;
it never writes a submission or uses validator credentials.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.public_score_cluster_audit import (
    fetch_recent_completed_tasks,
    fetch_task_scores,
)


BREAKDOWN_FIELDS = (
    "total_weighted_score",
    "consistency_score",
    "consistency_factor",
    "distribution_fidelity_score",
    "distribution_fidelity_factor",
    "n_valid_experiments",
)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def component_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in BREAKDOWN_FIELDS:
        values = [
            float((row.get("breakdown") or {}).get(field))
            for row in rows
            if isinstance((row.get("breakdown") or {}).get(field), (int, float))
        ]
        result[field] = (
            {
                "minimum": min(values),
                "median": statistics.median(values),
                "maximum": max(values),
            }
            if values
            else None
        )
    return result


def task_report(
    task: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    target_rank: int,
    miners: dict[str, str],
) -> dict[str, Any]:
    ordered = sorted(
        rows,
        key=lambda row: float(row.get("final_score") or 0.0),
        reverse=True,
    )
    target_score = (
        float(ordered[target_rank - 1].get("final_score") or 0.0)
        if len(ordered) >= target_rank
        else None
    )
    lanes: dict[str, Any] = {}
    for label, hotkey in miners.items():
        match = next(
            (row for row in ordered if str(row.get("miner_hotkey") or "") == hotkey),
            None,
        )
        if match is None:
            lanes[label] = {"published": False}
            continue
        score = float(match.get("final_score") or 0.0)
        breakdown = match.get("breakdown") or {}
        weighted = float(breakdown.get("total_weighted_score") or 0.0)
        fidelity = float(
            breakdown.get("distribution_fidelity_factor")
            or breakdown.get("distribution_fidelity_score")
            or 0.0
        )
        consistency = float(
            breakdown.get("consistency_score")
            or 100.0 * float(breakdown.get("consistency_factor") or 0.0)
        )
        required_consistency = (
            100.0 * target_score / (weighted * fidelity)
            if target_score is not None and weighted > 0.0 and fidelity > 0.0
            else None
        )
        lanes[label] = {
            "published": True,
            "rank": ordered.index(match) + 1,
            "score": score,
            "gap_to_target": score - target_score if target_score is not None else None,
            "breakdown": {field: breakdown.get(field) for field in BREAKDOWN_FIELDS},
            "required_consistency_score_at_current_weight_and_fidelity": required_consistency,
            "consistency_gain_ratio": (
                required_consistency / consistency
                if required_consistency is not None and consistency > 0.0
                else None
            ),
        }
    return {
        "task_id": task["task_id"],
        "created_at": task.get("created_at"),
        "participants": len(ordered),
        "target_rank": target_rank,
        "target_score": target_score,
        "target_group_components": component_summary(ordered[:target_rank]),
        "miners": lanes,
    }


def persistent_cohort_summary(
    task_rows: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    *,
    target_rank: int,
) -> dict[str, Any]:
    history: dict[str, list[dict[str, float]]] = defaultdict(list)
    for _, rows in task_rows:
        ordered = sorted(
            rows,
            key=lambda row: float(row.get("final_score") or 0.0),
            reverse=True,
        )
        task_components = {
            "weighted": [
                float((row.get("breakdown") or {}).get("total_weighted_score") or 0.0)
                for row in ordered
            ],
            "fidelity": [
                float((row.get("breakdown") or {}).get("distribution_fidelity_factor") or 0.0)
                for row in ordered
            ],
            "consistency": [
                float((row.get("breakdown") or {}).get("consistency_score") or 0.0)
                for row in ordered
            ],
        }
        for rank, row in enumerate(ordered, 1):
            hotkey = str(row.get("miner_hotkey") or "")
            if not hotkey:
                continue
            breakdown = row.get("breakdown") or {}
            observation = {
                "rank": float(rank),
                "weighted": float(breakdown.get("total_weighted_score") or 0.0),
                "fidelity": float(
                    breakdown.get("distribution_fidelity_factor") or 0.0
                ),
                "consistency": float(breakdown.get("consistency_score") or 0.0),
            }
            for component in ("weighted", "fidelity", "consistency"):
                values = task_components[component]
                observation[f"{component}_percentile"] = (
                    sum(value <= observation[component] for value in values)
                    / len(values)
                    if values
                    else 0.0
                )
            history[hotkey].append(observation)

    minimum_rounds = max(1, math.ceil(len(task_rows) * 2.0 / 3.0))
    persistent = [observations for observations in history.values() if len(observations) >= minimum_rounds]
    profiles = []
    qualifying_observations: list[dict[str, float]] = []
    for observations in persistent:
        top_target_rate = sum(
            item["rank"] <= target_rank for item in observations
        ) / len(observations)
        if top_target_rate < 0.25:
            continue
        qualifying_observations.extend(observations)
        profiles.append(
            {
                "rounds": len(observations),
                "top_target_rate": top_target_rate,
                "median_rank": statistics.median(item["rank"] for item in observations),
                "median_weighted": statistics.median(
                    item["weighted"] for item in observations
                ),
                "median_fidelity": statistics.median(
                    item["fidelity"] for item in observations
                ),
                "median_consistency": statistics.median(
                    item["consistency"] for item in observations
                ),
                "median_weighted_percentile": statistics.median(
                    item["weighted_percentile"] for item in observations
                ),
                "median_fidelity_percentile": statistics.median(
                    item["fidelity_percentile"] for item in observations
                ),
                "median_consistency_percentile": statistics.median(
                    item["consistency_percentile"] for item in observations
                ),
            }
        )
    profiles.sort(key=lambda item: (-item["top_target_rate"], item["median_rank"]))

    def median_field(field: str) -> float | None:
        values = [float(item[field]) for item in profiles]
        return statistics.median(values) if values else None

    def outcome_summary(success: bool) -> dict[str, Any]:
        selected = [
            item
            for item in qualifying_observations
            if (item["rank"] <= target_rank) is success
        ]
        return {
            "observations": len(selected),
            "median_rank": (
                statistics.median(item["rank"] for item in selected)
                if selected
                else None
            ),
            "median_weighted_percentile": (
                statistics.median(item["weighted_percentile"] for item in selected)
                if selected
                else None
            ),
            "median_fidelity_percentile": (
                statistics.median(item["fidelity_percentile"] for item in selected)
                if selected
                else None
            ),
            "median_consistency_percentile": (
                statistics.median(item["consistency_percentile"] for item in selected)
                if selected
                else None
            ),
        }

    return {
        "minimum_rounds": minimum_rounds,
        "persistent_hotkeys": len(persistent),
        "qualifying_cohort_size": len(profiles),
        "cohort_medians": {
            "top_target_rate": median_field("top_target_rate"),
            "rank": median_field("median_rank"),
            "weighted": median_field("median_weighted"),
            "fidelity": median_field("median_fidelity"),
            "consistency": median_field("median_consistency"),
            "weighted_percentile": median_field("median_weighted_percentile"),
            "fidelity_percentile": median_field("median_fidelity_percentile"),
            "consistency_percentile": median_field(
                "median_consistency_percentile"
            ),
        },
        "conditional_outcomes": {
            "target_reached": outcome_summary(True),
            "target_missed": outcome_summary(False),
        },
        "anonymized_profiles": profiles,
    }


def build_report(
    task_rows: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    *,
    target_rank: int,
    miners: dict[str, str],
) -> dict[str, Any]:
    rounds = [
        task_report(task, rows, target_rank=target_rank, miners=miners)
        for task, rows in task_rows
    ]
    cutoffs = [
        float(item["target_score"])
        for item in rounds
        if item.get("target_score") is not None
    ]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "public-read-only-rank-gap-audit",
        "target_rank": target_rank,
        "rounds": rounds,
        "persistent_top_target_cohort": persistent_cohort_summary(
            task_rows, target_rank=target_rank
        ),
        "cutoff_distribution": {
            "rounds": len(cutoffs),
            "minimum": min(cutoffs) if cutoffs else None,
            "median": statistics.median(cutoffs) if cutoffs else None,
            "p75": percentile(cutoffs, 0.75),
            "p90": percentile(cutoffs, 0.90),
            "maximum": max(cutoffs) if cutoffs else None,
        },
        "safety": {
            "public_only": True,
            "uses_credentials": False,
            "writes_submissions": False,
            "post_score_observation_only": True,
            "uses_current_score_for_current_submission": False,
        },
    }


def parse_miner(value: str) -> tuple[str, str]:
    label, separator, hotkey = value.partition("=")
    if not separator or not label.strip() or not hotkey.strip():
        raise argparse.ArgumentTypeError("--miner must be LABEL=SS58")
    return label.strip(), hotkey.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=30)
    parser.add_argument("--target-rank", type=int, default=30)
    parser.add_argument("--miner", action="append", type=parse_miner, default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.tasks < 1 or args.target_rank < 1:
        parser.error("--tasks and --target-rank must be positive")

    tasks = fetch_recent_completed_tasks(args.tasks)
    task_rows = []
    for task in tasks:
        rows, _ = fetch_task_scores(task["task_id"])
        task_rows.append((task, rows))
    report = build_report(
        task_rows,
        target_rank=args.target_rank,
        miners=dict(args.miner),
    )
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(args.output)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
