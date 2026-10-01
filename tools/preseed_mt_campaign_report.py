#!/usr/bin/env python3
"""Aggregate disjoint MT checkpoint scans without unsafe result promotion."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


def result_key(result: dict[str, Any]) -> str:
    profile = result.get("checkpoint_rejections")
    if isinstance(profile, dict):
        return ",".join(
            f"{int(draw)}:{int(value)}"
            for draw, value in sorted(profile.items(), key=lambda item: int(item[0]))
        )
    return str(int(result["cumulative_rejections"]))


def aggregate_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not reports:
        raise ValueError("at least one report is required")
    records: dict[str, list[str]] = {}
    available_values: set[int] = set()
    shard_descriptors: list[dict[str, Any]] = []
    predictive = []
    sat_reports = []
    sealed_opened = 0
    for report in reports:
        summary = report.get("summary") or {}
        scan = summary.get("assumption_checkpoint_scan") or {}
        available = scan.get("profiles_available")
        if available is None:
            available = scan.get("states_available")
        if available is not None:
            available_values.add(int(available))
        shard_descriptors.append(
            {
                "shard_count": int(scan.get("shard_count") or 1),
                "shard_index": int(scan.get("shard_index") or 0),
                "selected": int(scan.get("states_selected") or 0),
                "attempted": int(scan.get("states_attempted") or 0),
            }
        )
        for result in scan.get("results") or []:
            records.setdefault(result_key(result), []).append(str(result["status"]))
        if summary.get("status") == "sat":
            sat_reports.append(report)
        if summary.get("excluded_discovery_predicted"):
            predictive.append(report)
        sealed_opened += int(summary.get("sealed_seed_labels_opened") or 0)
    if len(available_values) > 1:
        raise ValueError("reports disagree on the assumption universe size")
    available = next(iter(available_values), 0)
    conflicts = {
        key: statuses
        for key, statuses in records.items()
        if len(set(statuses)) > 1
    }
    duplicates = sum(max(0, len(statuses) - 1) for statuses in records.values())
    status_counts = {
        status: sum(status in statuses for statuses in records.values())
        for status in ("sat", "unsat", "unknown")
    }
    complete_coverage = bool(available and len(records) == available)
    globally_unsat = bool(
        complete_coverage
        and not conflicts
        and records
        and all(statuses == ["unsat"] for statuses in records.values())
    )
    campaign_status = (
        "predictive-candidate"
        if predictive
        else "sat-witness"
        if sat_reports
        else "unsat"
        if globally_unsat
        else "unknown"
    )
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "mt-checkpoint-campaign-aggregate",
        "summary": {
            "status": campaign_status,
            "reports": len(reports),
            "assumptions_available": available,
            "assumptions_attempted_unique": len(records),
            "complete_coverage": complete_coverage,
            "globally_unsat": globally_unsat,
            "duplicate_attempts": duplicates,
            "conflicting_results": len(conflicts),
            "status_counts": status_counts,
            "sat_reports": len(sat_reports),
            "predictive_candidates": len(predictive),
            "sealed_seed_labels_opened": sealed_opened,
            "shards": shard_descriptors,
        },
        "interpretation": (
            "A local shard can contribute a SAT witness. Global UNSAT is valid only "
            "after every assumption in the common universe is covered and decided UNSAT."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = aggregate_reports(
        [json.loads(path.read_text(encoding="utf-8")) for path in args.reports]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps(payload["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
