#!/usr/bin/env python3
"""Search per-round clock reseeding near public seed-generation events.

Unlike the fixed-offset clock search, this permits private request/log latency
to differ for every round. It is a fingerprint test: a narrow cluster of
independent offsets under one API would support per-task clock reseeding.
Only the fixed Discovery labels are loaded.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import multiprocessing as mp
from pathlib import Path
import random
import tempfile
from typing import Any

try:
    from tools.preseed_event_clock_search import generated_seed_times
    from tools.preseed_shuffle_leak_audit import fetch_log_nodes
    from tools.preseed_validation_time_search import validation_times
except ModuleNotFoundError:
    from preseed_event_clock_search import generated_seed_times
    from preseed_shuffle_leak_audit import fetch_log_nodes
    from preseed_validation_time_search import validation_times


LOW = 100
HIGH = 1000


def parse_microseconds(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def triplet(seed: int | float, method: str) -> list[int]:
    rng = random.Random(seed)
    if method.endswith("sample"):
        return rng.sample(range(LOW, HIGH), 3)
    values: list[int] = []
    while len(values) < 3:
        value = rng.randrange(LOW, HIGH)
        if value not in values:
            values.append(value)
    return values


def candidate_seed(center_us: int, delta: int, unit: str, method: str) -> int | float:
    if unit == "seconds":
        return center_us // 1_000_000 + delta
    if unit == "milliseconds":
        return center_us // 1_000 + delta
    if unit == "microseconds":
        return center_us + delta
    if unit == "float-microseconds":
        return (center_us + delta) / 1_000_000.0
    raise ValueError(unit)


def search_window(
    center_us: int,
    expected: list[int],
    unit: str,
    method: str,
    radius: int,
) -> list[int]:
    return [
        delta
        for delta in range(-radius, radius + 1)
        if triplet(candidate_seed(center_us, delta, unit, method), method) == expected
    ]


def _job(job: tuple[str, int, list[int], str, str, int]) -> dict[str, Any]:
    task_id, center_us, expected, unit, method, radius = job
    hits = search_window(center_us, expected, unit, method, radius)
    return {
        "task_id": task_id,
        "unit": unit,
        "method": method,
        "radius": radius,
        "hit_count": len(hits),
        "offsets": hits[:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument(
        "--anchor",
        choices=("created", "validation", "generated-seed-log"),
        default="generated-seed-log",
    )
    parser.add_argument("--seconds-radius", type=int, default=120)
    parser.add_argument("--milliseconds-radius", type=int, default=5_000)
    parser.add_argument("--microseconds-radius", type=int, default=250_000)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--entity", default="genomes")
    parser.add_argument("--project", default="niome")
    parser.add_argument("--run", default="non2mca3")
    parser.add_argument(
        "--output", type=Path,
        default=Path("artifacts/research/preseed_event_clock_independent.json"),
    )
    args = parser.parse_args()
    payload = json.loads(args.discovery.read_text(encoding="utf-8"))
    records = [dict(row) for row in payload.get("records") or []]
    if len(records) != 20:
        raise ValueError("requires the fixed 20-task Discovery set")
    if args.anchor == "created":
        anchors = {
            str(row["task_id"]): parse_microseconds(str(row["created_at"]))
            for row in records
        }
    else:
        nodes = fetch_log_nodes(args.entity, args.project, args.run)
        timestamp_map = (
            validation_times(nodes)
            if args.anchor == "validation"
            else generated_seed_times(nodes)
        )
        anchors = {
            task_id: int(round(timestamp * 1_000_000))
            for task_id, timestamp in timestamp_map.items()
        }
    rows = [
        (
            str(row["task_id"]),
            anchors[str(row["task_id"])],
            [int(value) for value in row["seeds"]],
        )
        for row in records
        if str(row["task_id"]) in anchors
    ]
    configurations = (
        ("seconds", "python-sample", max(0, args.seconds_radius)),
        ("seconds", "python-randrange", max(0, args.seconds_radius)),
        ("milliseconds", "python-sample", max(0, args.milliseconds_radius)),
        ("milliseconds", "python-randrange", max(0, args.milliseconds_radius)),
        ("microseconds", "python-sample", max(0, args.microseconds_radius)),
        ("microseconds", "python-randrange", max(0, args.microseconds_radius)),
        ("float-microseconds", "python-sample", max(0, args.microseconds_radius)),
        ("float-microseconds", "python-randrange", max(0, args.microseconds_radius)),
    )
    jobs = [
        (task_id, center, expected, unit, method, radius)
        for task_id, center, expected in rows
        for unit, method, radius in configurations
    ]
    if args.workers <= 1:
        results = [_job(job) for job in jobs]
    else:
        with mp.Pool(max(1, args.workers)) as pool:
            results = pool.map(_job, jobs)
    by_configuration: dict[str, dict[str, Any]] = {}
    for result in results:
        key = f"{result['unit']}:{result['method']}"
        item = by_configuration.setdefault(
            key,
            {
                "tasks": len(rows),
                "tasks_with_hits": 0,
                "total_hits": 0,
                "radius": result["radius"],
                "hits": [],
            },
        )
        item["tasks_with_hits"] += result["hit_count"] > 0
        item["total_hits"] += result["hit_count"]
        if result["hit_count"]:
            item["hits"].append(
                {
                    "task_id": result["task_id"],
                    "offsets": result["offsets"],
                    "hit_count": result["hit_count"],
                }
            )
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "per-round-independent-clock-reseed-search",
        "anchor": args.anchor,
        "split": {
            "fixed_discovery_total": 20,
            "discovery_with_anchor": len(rows),
            "holdout_opened": False,
        },
        "search": {
            "configurations": by_configuration,
            "candidate_initializations": sum(
                (2 * radius + 1) * len(rows)
                for _unit, _method, radius in configurations
            ),
        },
        "interpretation": (
            "Many rounds matching one narrow clock/API configuration supports per-task "
            "clock reseeding. Isolated hits must be compared with the random-hit expectation."
        ),
        "safety": {
            "discovery_only": True,
            "holdout_opened": False,
            "public_read_only": True,
            "submission_writes": False,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=args.output.parent, delete=False
    ) as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(args.output)
    print(json.dumps({
        "output": str(args.output.resolve()),
        "anchor": args.anchor,
        "tasks": len(rows),
        "configurations": {
            key: {"tasks_with_hits": value["tasks_with_hits"], "total_hits": value["total_hits"]}
            for key, value in by_configuration.items()
        },
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
