"""Official-score gap analysis for an explicit leaderboard target."""

from __future__ import annotations

import math
from typing import Any, Mapping


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _metrics(item: Mapping[str, Any]) -> dict[str, float | None]:
    breakdown = item.get("breakdown") or {}
    return {
        "score": _number(item.get("final_score")),
        "weighted": _number(breakdown.get("total_weighted_score")),
        "consistency": _number(breakdown.get("consistency_factor")),
        "fidelity": _number(breakdown.get("distribution_fidelity_factor")),
    }


def analyze_rank_target(
    scoreboard: list[dict[str, Any]],
    hotkeys: Mapping[str, str],
    *,
    target_rank: int,
    safety_margin: float = 0.03,
) -> dict[str, Any]:
    """Explain what each lane must change to reach a public rank cutoff."""

    if target_rank < 1:
        raise ValueError("target_rank must be positive")
    if safety_margin < 0:
        raise ValueError("safety_margin must be non-negative")
    ordered = sorted(
        (
            item
            for item in scoreboard
            if _number(item.get("final_score")) is not None
        ),
        key=lambda item: float(item["final_score"]),
        reverse=True,
    )
    if len(ordered) < target_rank:
        raise ValueError("scoreboard does not contain the requested rank")
    cutoff = ordered[target_rank - 1]
    cutoff_metrics = _metrics(cutoff)
    cutoff_score = float(cutoff_metrics["score"] or 0.0)
    safe_score = cutoff_score * (1.0 + safety_margin)
    lanes: dict[str, Any] = {}
    for lane, hotkey in hotkeys.items():
        item = next(
            (row for row in ordered if row.get("miner_hotkey") == hotkey),
            None,
        )
        if item is None:
            lanes[lane] = {"published": False, "hotkey": hotkey}
            continue
        metrics = _metrics(item)
        weighted = metrics["weighted"]
        fidelity = metrics["fidelity"]
        consistency = metrics["consistency"]
        denominator = (
            weighted * fidelity
            if weighted is not None and fidelity is not None and weighted > 0 and fidelity > 0
            else None
        )
        required_consistency = cutoff_score / denominator if denominator else None
        safe_required_consistency = safe_score / denominator if denominator else None
        lanes[lane] = {
            "published": True,
            "hotkey": hotkey,
            "rank": ordered.index(item) + 1,
            **metrics,
            "gap_to_cutoff": float(metrics["score"] or 0.0) - cutoff_score,
            "required_consistency_at_current_weighted_fidelity": required_consistency,
            "safe_required_consistency_at_current_weighted_fidelity": safe_required_consistency,
            "relative_consistency_lift": (
                required_consistency / consistency - 1.0
                if required_consistency is not None and consistency
                else None
            ),
            "score_identity_error": (
                float(metrics["score"] or 0.0) - weighted * consistency * fidelity
                if weighted is not None and consistency is not None and fidelity is not None
                else None
            ),
        }
    return {
        "participants": len(ordered),
        "target_rank": target_rank,
        "target_cutoff": cutoff_metrics,
        "target_hotkey": cutoff.get("miner_hotkey"),
        "safety_margin": safety_margin,
        "safe_target_score": safe_score,
        "lanes": lanes,
    }
