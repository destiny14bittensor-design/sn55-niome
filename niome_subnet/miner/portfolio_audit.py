"""Cross-lane evidence and promotion gates for the honest miner portfolio."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
from typing import Any, Mapping


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _submission_ids(path: Path) -> set[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    if not isinstance(value, list):
        return set()
    return {
        str(item.get("experiment_id"))
        for item in value
        if isinstance(item, dict) and item.get("experiment_id") is not None
    }


def jaccard(left: set[str], right: set[str]) -> float | None:
    union = left | right
    return len(left & right) / len(union) if union else None


def pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean) for x, y in zip(left, right)
    )
    left_scale = math.sqrt(sum((x - left_mean) ** 2 for x in left))
    right_scale = math.sqrt(sum((y - right_mean) ** 2 for y in right))
    if left_scale == 0.0 or right_scale == 0.0:
        return 1.0 if left == right else None
    return numerator / (left_scale * right_scale)


def _lane_tasks(root: Path) -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return tasks
    for task_dir in root.iterdir():
        if not task_dir.is_dir() or task_dir.name.startswith("_"):
            continue
        manifest = _json(task_dir / "manifest.json")
        local = _json(task_dir / "local_validation.json")
        status = _json(task_dir / "status.json")
        policy = manifest.get("builder_policy") or status.get("builder_policy") or {}
        if not policy:
            continue
        score = local.get("final_score")
        tasks[task_dir.name] = {
            "policy_id": policy.get("policy_id"),
            "score": float(score) if isinstance(score, (int, float)) else None,
            "submission_ids": _submission_ids(task_dir / "submission.json"),
            "submission_sha256": (manifest.get("files") or {}).get("submission.json"),
            "received_at": status.get("received_at"),
        }
    return tasks


def build_portfolio_report(
    lane_roots: Mapping[str, Path],
    *,
    expected_policies: Mapping[str, str],
    target_score: float = 50.0,
    max_jaccard: float = 0.65,
    max_score_correlation: float = 0.75,
    required_final_rounds: int = 4,
) -> dict[str, Any]:
    """Build a promotion report without treating local scores as official ranks."""

    lanes: dict[str, Any] = {}
    task_maps: dict[str, dict[str, dict[str, Any]]] = {}
    for lane, root in lane_roots.items():
        runtime = _json(root / "_runtime" / "miner.json")
        last_request = _json(root / "_runtime" / "last_request.json")
        tasks = _lane_tasks(root)
        task_maps[lane] = tasks
        observed_policy = (runtime.get("builder_policy") or {}).get("policy_id")
        lanes[lane] = {
            "root": str(root),
            "expected_policy": expected_policies.get(lane),
            "runtime_policy": observed_policy,
            "runtime_matches": observed_policy == expected_policies.get(lane),
            "last_request": last_request,
            "policy_task_count": len(tasks),
        }

    lane_names = list(lane_roots)
    common_task_ids = (
        set.intersection(*(set(tasks) for tasks in task_maps.values()))
        if task_maps
        else set()
    )
    common_tasks = sorted(
        common_task_ids,
        key=lambda task_id: max(
            str(task_maps[lane][task_id].get("received_at") or "")
            for lane in lane_names
        ),
    )
    pairs: list[dict[str, Any]] = []
    for left_index, left_lane in enumerate(lane_names):
        for right_lane in lane_names[left_index + 1 :]:
            overlaps = []
            left_scores = []
            right_scores = []
            for task_id in common_tasks:
                left = task_maps[left_lane][task_id]
                right = task_maps[right_lane][task_id]
                overlap = jaccard(left["submission_ids"], right["submission_ids"])
                if overlap is not None:
                    overlaps.append(overlap)
                if left["score"] is not None and right["score"] is not None:
                    left_scores.append(left["score"])
                    right_scores.append(right["score"])
            pairs.append(
                {
                    "left": left_lane,
                    "right": right_lane,
                    "rounds": len(common_tasks),
                    "median_payload_jaccard": (
                        statistics.median(overlaps) if overlaps else None
                    ),
                    "max_payload_jaccard": max(overlaps) if overlaps else None,
                    "local_score_correlation": pearson(left_scores, right_scores),
                }
            )

    round_rows = []
    for task_id in common_tasks:
        scores = {
            lane: task_maps[lane][task_id]["score"] for lane in lane_names
        }
        numeric = [value for value in scores.values() if value is not None]
        round_rows.append(
            {
                "task_id": task_id,
                "scores": scores,
                "best_local_score": max(numeric) if numeric else None,
                "local_target_met": bool(numeric and max(numeric) >= target_score),
                "all_payloads_distinct": len(
                    {
                        task_maps[lane][task_id]["submission_sha256"]
                        for lane in lane_names
                    }
                )
                == len(lane_names),
            }
        )

    final_rows = round_rows[-required_final_rounds:]
    evidence_ready = len(final_rows) == required_final_rounds
    distinct_policies = len(set(expected_policies.values())) == len(expected_policies)
    runtime_ready = all(row["runtime_matches"] for row in lanes.values())
    payload_gate = evidence_ready and all(
        row["all_payloads_distinct"] for row in final_rows
    )
    overlap_gate = evidence_ready and all(
        pair["max_payload_jaccard"] is not None
        and pair["max_payload_jaccard"] < max_jaccard
        for pair in pairs
    )
    correlation_values = [
        pair["local_score_correlation"]
        for pair in pairs
        if pair["local_score_correlation"] is not None
    ]
    correlation_gate = evidence_ready and bool(correlation_values) and all(
        value < max_score_correlation for value in correlation_values
    )
    score_gate = evidence_ready and sum(
        row["local_target_met"] for row in final_rows
    ) >= 3
    gates = {
        "distinct_policy_configuration": distinct_policies,
        "runtime_policy_match": runtime_ready,
        "four_round_evidence": evidence_ready,
        "payload_distinctness": payload_gate,
        "payload_jaccard_below_limit": overlap_gate,
        "local_score_correlation_below_limit": correlation_gate,
        "local_target_three_of_four": score_gate,
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "promotable" if all(gates.values()) else "hold",
        "score_warning": (
            "local deterministic stress scores are engineering gates, not an "
            "official rank-100 guarantee"
        ),
        "thresholds": {
            "target_score": target_score,
            "max_payload_jaccard": max_jaccard,
            "max_score_correlation": max_score_correlation,
            "required_final_rounds": required_final_rounds,
        },
        "gates": gates,
        "lanes": lanes,
        "common_task_count": len(common_tasks),
        "rounds": round_rows,
        "pairs": pairs,
    }
