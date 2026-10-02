"""Leakage-safe calibration for unknown-seed local score estimates."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import statistics
from typing import Any, Iterable


MIN_TRAINING_RECORDS = 8


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("a quantile requires at least one value")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _mae(actual: list[float], predicted: list[float]) -> float:
    return statistics.fmean(abs(left - right) for left, right in zip(actual, predicted))


def _fit_group(records: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(records, key=lambda item: str(item.get("received_at") or ""))
    count = len(ordered)
    residuals = [float(item["official_score"]) - float(item["local_score"]) for item in ordered]
    holdout_count = max(2, math.ceil(count * 0.2)) if count >= MIN_TRAINING_RECORDS else 0
    train_count = count - holdout_count
    point_offset = 0.0
    correction_validated = False
    raw_mae = None
    corrected_mae = None
    if train_count >= 5 and holdout_count:
        training_residuals = residuals[:train_count]
        candidate_offset = statistics.median(training_residuals)
        holdout = ordered[train_count:]
        actual = [float(item["official_score"]) for item in holdout]
        raw = [float(item["local_score"]) for item in holdout]
        corrected = [value + candidate_offset for value in raw]
        raw_mae = _mae(actual, raw)
        corrected_mae = _mae(actual, corrected)
        # Never make the point estimate worse merely to claim calibration.
        correction_validated = corrected_mae + 1e-9 < raw_mae
        if correction_validated:
            point_offset = statistics.median(residuals)

    prediction_residuals = [value - point_offset for value in residuals]
    return {
        "records": count,
        "minimum_records": MIN_TRAINING_RECORDS,
        "ready": count >= MIN_TRAINING_RECORDS,
        "method": "validated-median-residual",
        "point_correction_applied": correction_validated,
        "point_offset": point_offset,
        "residual_p10": _quantile(prediction_residuals, 0.10) if residuals else None,
        "residual_p50": _quantile(prediction_residuals, 0.50) if residuals else None,
        "residual_p90": _quantile(prediction_residuals, 0.90) if residuals else None,
        "backtest": {
            "training_records": train_count,
            "holdout_records": holdout_count,
            "raw_mae": raw_mae,
            "corrected_mae": corrected_mae,
        },
    }


def build_calibration_model(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    clean: list[dict[str, Any]] = []
    for item in records:
        local = _number(item.get("local_score"))
        official = _number(item.get("official_score"))
        if local is None or official is None:
            continue
        clean.append({**item, "local_score": local, "official_score": official})

    policy_models: dict[str, dict[str, Any]] = {}
    policies = sorted({str(item.get("builder_policy") or "") for item in clean if item.get("builder_policy")})
    for policy in policies:
        subset = [item for item in clean if item.get("builder_policy") == policy]
        if len(subset) >= MIN_TRAINING_RECORDS:
            policy_models[policy] = _fit_group(subset)

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "semantics": "unknown-seed-holdout-estimate",
        "global": _fit_group(clean),
        "policies": policy_models,
        "records": clean,
    }


def calibrated_prediction(
    local_score: Any,
    model: dict[str, Any] | None,
    *,
    builder_policy: str | None = None,
) -> dict[str, Any]:
    local = _number(local_score)
    if local is None or not isinstance(model, dict):
        return {"available": False, "reason": "local-score-or-model-unavailable"}
    group = (model.get("policies") or {}).get(builder_policy or "")
    scope = "policy"
    if not isinstance(group, dict) or not group.get("ready"):
        group = model.get("global")
        scope = "global"
    if not isinstance(group, dict) or not group.get("ready"):
        return {
            "available": False,
            "reason": "insufficient-training-records",
            "records": int((group or {}).get("records") or 0),
            "minimum_records": MIN_TRAINING_RECORDS,
        }

    offset = _number(group.get("point_offset")) or 0.0
    p10 = _number(group.get("residual_p10"))
    p90 = _number(group.get("residual_p90"))
    if p10 is None or p90 is None:
        return {"available": False, "reason": "invalid-model-interval"}
    records = int(group.get("records") or 0)
    confidence = "high" if records >= 40 else "moderate" if records >= 20 else "low"
    return {
        "available": True,
        "estimate": local + offset,
        "lower": local + offset + p10,
        "upper": local + offset + p90,
        "records": records,
        "scope": scope,
        "confidence": confidence,
        "point_correction_applied": bool(group.get("point_correction_applied")),
        "point_offset": offset,
        "backtest": dict(group.get("backtest") or {}),
        "model_generated_at": model.get("generated_at"),
    }
