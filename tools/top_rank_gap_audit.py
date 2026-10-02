#!/usr/bin/env python3
"""Audit a public NIOME leaderboard against an explicit rank objective.

Only the public task and score APIs are read.  The report deliberately focuses
on score components that are actionable before a future task's seed is known;
it never writes a submission or uses validator credentials.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
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
