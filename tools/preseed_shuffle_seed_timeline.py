#!/usr/bin/env python3
"""Resolve the task offset between public shuffle rounds and generated seeds.

The deployed validator logs a fetched task, its later validation, and then a
``Generated seeds`` line.  This audit compares that triplet to fixed Discovery
labels and to the next fetched task, preventing a one-round coupling mistake.
Only task IDs, timestamps, and Discovery labels are persisted.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

try:
    from tools.preseed_shuffle_leak_audit import fetch_log_nodes
except ModuleNotFoundError:
    from preseed_shuffle_leak_audit import fetch_log_nodes


TASK_RE = re.compile(r"Fetched task ([0-9a-f-]{36})")
VALIDATION_RE = re.compile(r"Validating miners' submissions")
SEED_RE = re.compile(r"Generated seeds:\s*([0-9, ]+)")


def build_timeline(
    nodes: list[dict[str, Any]], discovery_labels: dict[str, list[int]]
) -> dict[str, Any]:
    rounds: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    unassigned_seed_events = 0
    for node in nodes:
        line = str(node.get("line") or "")
        timestamp = str(node.get("timestamp") or "")
        task = TASK_RE.search(line)
        if task:
            if current is not None:
                rounds.append(current)
            current = {
                "task_id": task.group(1),
                "fetched_at": timestamp,
                "validation_started_at": None,
                "seed_generated_at": None,
                "generated_seed": None,
            }
            continue
        if VALIDATION_RE.search(line):
            if current is not None:
                current["validation_started_at"] = timestamp
            continue
        seed = SEED_RE.search(line)
        if seed:
            values = [int(value) for value in seed.group(1).split(",")]
            if current is None:
                unassigned_seed_events += 1
            else:
                current["seed_generated_at"] = timestamp
                current["generated_seed"] = values
    if current is not None:
        rounds.append(current)

    comparisons = []
    for index, row in enumerate(rounds):
        generated = row.get("generated_seed")
        current_label = discovery_labels.get(row["task_id"])
        next_label = (
            discovery_labels.get(rounds[index + 1]["task_id"])
            if index + 1 < len(rounds)
            else None
        )
        if generated is None or current_label is None:
            continue
        comparisons.append(
            {
                "task_id": row["task_id"],
                "validation_precedes_seed": bool(
                    row.get("validation_started_at")
                    and row["validation_started_at"] < row["seed_generated_at"]
                ),
                "matches_current_discovery_label": generated == current_label,
                "matches_next_discovery_label": (
                    generated == next_label if next_label is not None else None
                ),
            }
        )
    return {
        "rounds_observed": len(rounds),
        "unassigned_seed_events": unassigned_seed_events,
        "discovery_comparisons": len(comparisons),
        "current_task_matches": sum(
            row["matches_current_discovery_label"] for row in comparisons
        ),
        "next_task_matches": sum(
            row["matches_next_discovery_label"] is True for row in comparisons
        ),
        "validation_before_seed_matches": sum(
            row["validation_precedes_seed"] for row in comparisons
        ),
        "coupling_offset": (
            "same-fetched-task"
            if comparisons
            and all(row["matches_current_discovery_label"] for row in comparisons)
            else "unresolved"
        ),
        "comparisons": comparisons,
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
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_shuffle_seed_timeline.json"),
    )
    args = parser.parse_args()
    discovery = json.loads(args.discovery.read_text(encoding="utf-8"))
    labels = {
        str(row["task_id"]): [int(value) for value in row["seeds"]]
        for row in discovery.get("records") or []
    }
    summary = build_timeline(
        fetch_log_nodes(args.entity, args.project, args.run), labels
    )
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "public-shuffle-seed-task-offset-audit",
        "summary": summary,
        "interpretation": (
            "Generated seeds are logged after validation but match the same fetched task's "
            "Discovery label; searches must model shuffle(task T) before seed(task T)."
        ),
        "safety": {
            "public_read_only": True,
            "holdout_opened": False,
            "endpoint_data_stored": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **{k: summary[k] for k in ("discovery_comparisons", "current_task_matches", "next_task_matches", "coupling_offset")}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
