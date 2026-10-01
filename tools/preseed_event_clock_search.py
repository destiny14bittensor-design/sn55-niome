#!/usr/bin/env python3
"""Test seed-log and first-score clocks as per-round seed material.

The validation-start clock is covered by ``preseed_validation_time_search``.
This companion tests two distinct public anchors: the W&B source timestamp of
``Generated seeds`` and the earliest public score-row source timestamp.  For
each anchor it searches one fixed offset in seconds, milliseconds, and
microseconds with the same Python, NumPy, and digest implementations.  Only
the frozen Discovery set is loaded; Holdout labels are never read.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

try:
    from tools.early_score_monitor import public_cluster_rows, row_source_at
    from tools.preseed_shuffle_leak_audit import fetch_log_nodes
    from tools.preseed_validation_time_search import search_unit
except ModuleNotFoundError:
    from early_score_monitor import public_cluster_rows, row_source_at
    from preseed_shuffle_leak_audit import fetch_log_nodes
    from preseed_validation_time_search import search_unit


TASK_RE = re.compile(r"Fetched task ([0-9a-f-]{36})")
SEED_RE = re.compile(r"Generated seeds:\s*([0-9, ]+)")


def parse_timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def generated_seed_times(nodes: list[dict[str, Any]]) -> dict[str, float]:
    current = ""
    result: dict[str, float] = {}
    for node in nodes:
        line = str(node.get("line") or "")
        task = TASK_RE.search(line)
        if task:
            current = task.group(1)
        elif current and SEED_RE.search(line):
            result[current] = parse_timestamp(str(node.get("timestamp") or ""))
    return result


def earliest_score_times(
    task_ids: list[str],
    fetcher: Callable[[str], list[dict[str, Any]]] = public_cluster_rows,
) -> dict[str, float]:
    result = {}
    for task_id in task_ids:
        timestamps = [
            parse_timestamp(source)
            for row in fetcher(task_id)
            if (source := row_source_at(row)) is not None
        ]
        if timestamps:
            result[task_id] = min(timestamps)
    return result


def load_records(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [dict(row) for row in payload.get("records") or []]


def rows_for_anchor(
    records: list[dict[str, Any]], times: dict[str, float]
) -> list[tuple[float, list[int]]]:
    return [
        (times[str(row["task_id"])], [int(value) for value in row["seeds"]])
        for row in records
        if str(row["task_id"]) in times
    ]


def search_anchor(
    rows: list[tuple[float, list[int]]],
    *,
    seconds_radius: int,
    milliseconds_radius: int,
    microseconds_radius: int,
    workers: int,
) -> dict[str, Any]:
    return {
        "seconds": search_unit(rows, "seconds", 1, max(0, seconds_radius), workers),
        "milliseconds": search_unit(
            rows, "milliseconds", 1_000, max(0, milliseconds_radius), workers
        ),
        "microseconds": search_unit(
            rows, "microseconds", 1_000_000, max(0, microseconds_radius), workers
        ),
    }


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--entity", default="genomes")
    parser.add_argument("--project", default="niome")
    parser.add_argument("--run", default="non2mca3")
    parser.add_argument("--seconds-radius", type=int, default=600)
    parser.add_argument("--milliseconds-radius", type=int, default=100_000)
    parser.add_argument("--microseconds-radius", type=int, default=100_000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_event_clock_search.json"),
    )
    args = parser.parse_args()

    records = load_records(args.discovery.resolve())
    task_ids = [str(row["task_id"]) for row in records]
    anchors = {
        "generated-seed-log": rows_for_anchor(
            records,
            generated_seed_times(fetch_log_nodes(args.entity, args.project, args.run)),
        ),
        "first-public-score": rows_for_anchor(records, earliest_score_times(task_ids)),
    }
    families: dict[str, Any] = {}
    anchor_counts = {}
    for anchor, rows in anchors.items():
        if not rows:
            continue
        anchor_counts[anchor] = len(rows)
        results = search_anchor(
            rows,
            seconds_radius=args.seconds_radius,
            milliseconds_radius=args.milliseconds_radius,
            microseconds_radius=args.microseconds_radius,
            workers=max(1, args.workers),
        )
        for unit, result in results.items():
            families[f"{anchor}:{unit}"] = result

    exact = [
        {"family": name, **hit}
        for name, family in families.items()
        for hit in family["full_discovery_hits"]
    ]
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "public-event-clock-fixed-offset-search",
        "split": {
            "fixed_discovery_total": len(records),
            "anchor_discovery_counts": anchor_counts,
            "holdout_opened": False,
        },
        "search": {
            "candidates_tested": sum(
                int(family["candidates_tested"]) for family in families.values()
            ),
            "families": families,
            "discovery_exact_candidates": len(exact),
            "exact_candidates": exact,
        },
        "interpretation": (
            "A miss rejects only direct per-round seeding from the public seed-log or "
            "earliest-score clock with one fixed offset in the searched windows. It does "
            "not reject OS entropy, a private server clock with variable latency, or a "
            "long-lived RNG state."
        ),
        "safety": {
            "public_read_only": True,
            "discovery_only": True,
            "holdout_opened": False,
            "raw_score_rows_persisted": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "anchor_discovery_counts": anchor_counts,
                "candidates_tested": report["search"]["candidates_tested"],
                "discovery_exact_candidates": len(exact),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
