#!/usr/bin/env python3
"""Fail-closed policy for the currently deployed NIOME seed regime.

Historical seed records are useful for change-point and implementation-family
research, but they must never be mixed into the active generator fit.  This
module is intentionally small so the collector, lab, and dashboard
orchestrator all enforce the same boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


CURRENT_EPOCH_ID = "post-score-random-triple-100-999-v1"
CURRENT_EPOCH_STARTED_AT = "2026-09-25T22:54:35.523942+00:00"
CURRENT_SEED_COUNT = 3
CURRENT_SEED_LOW = 100
CURRENT_SEED_HIGH = 999
CURRENT_PUBLICATION_ORDER = "score-before-public-seed"


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_START = parse_utc(CURRENT_EPOCH_STARTED_AT)
assert _START is not None


def is_current_epoch_time(value: Any) -> bool:
    parsed = parse_utc(value)
    return parsed is not None and parsed >= _START


def valid_current_seed_label(record_or_seeds: Mapping[str, Any] | Sequence[int]) -> bool:
    seeds = (
        list(record_or_seeds.get("seeds") or [])
        if isinstance(record_or_seeds, Mapping)
        else list(record_or_seeds)
    )
    return (
        len(seeds) == CURRENT_SEED_COUNT
        and len(set(seeds)) == CURRENT_SEED_COUNT
        and all(CURRENT_SEED_LOW <= int(seed) <= CURRENT_SEED_HIGH for seed in seeds)
    )


def configured_epoch_view(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return the configured epoch and stop at the first incompatible label.

    Unknown/unpublished seeds are retained because they are prospective rows.
    A labeled row with another count, range, or duplicate value closes the
    epoch and blocks prediction until a new policy is explicitly configured.
    """
    ordered = sorted(
        (record for record in records if is_current_epoch_time(record.get("created_at"))),
        key=lambda record: str(record.get("created_at") or ""),
    )
    retained: list[Mapping[str, Any]] = []
    violation: Mapping[str, Any] | None = None
    for record in ordered:
        seeds = list(record.get("seeds") or [])
        if seeds and not valid_current_seed_label(seeds):
            violation = record
            break
        retained.append(record)

    labeled = [record for record in retained if valid_current_seed_label(record)]
    seedless = [record for record in retained if not record.get("seeds")]
    return {
        "id": CURRENT_EPOCH_ID,
        "configured_started_at": CURRENT_EPOCH_STARTED_AT,
        "status": "change-detected" if violation is not None else "stable",
        "gate_open": violation is None,
        "expected": {
            "seed_count": CURRENT_SEED_COUNT,
            "distinct": True,
            "seed_range": [CURRENT_SEED_LOW, CURRENT_SEED_HIGH],
            "publication_order": CURRENT_PUBLICATION_ORDER,
        },
        "records": retained,
        "labeled_records": labeled,
        "seedless_records": seedless,
        "violation": (
            {
                "task_id": str(violation.get("task_id") or violation.get("id") or ""),
                "created_at": violation.get("created_at"),
                "seeds": list(violation.get("seeds") or []),
                "reason": "seed-count-range-or-distinctness-changed",
            }
            if violation is not None
            else None
        ),
        "historical_records_excluded": sum(
            not is_current_epoch_time(record.get("created_at")) for record in records
        ),
    }


def public_policy_metadata(view: Mapping[str, Any]) -> dict[str, Any]:
    """Remove retained task bodies before embedding policy state in reports."""
    return {
        key: value
        for key, value in view.items()
        if key not in {"records", "labeled_records", "seedless_records"}
    } | {
        "retained_tasks": len(view.get("records") or []),
        "labeled_tasks": len(view.get("labeled_records") or []),
        "seedless_tasks": len(view.get("seedless_records") or []),
    }
