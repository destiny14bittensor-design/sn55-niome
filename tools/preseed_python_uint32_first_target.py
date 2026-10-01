#!/usr/bin/env python3
"""Exhaust CPython ``random.seed(uint32)`` from the first Discovery triplet."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

try:
    from tools.preseed_generator_lab import atomic_json
except ModuleNotFoundError:
    from preseed_generator_lab import atomic_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--stop", type=int, default=1 << 32)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    completed = subprocess.run(
        [
            str(args.binary.resolve()),
            "--first-target",
            str(int(args.start)),
            str(int(args.stop)),
            str(max(1, int(args.threads))),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    worker = json.loads(completed.stdout.strip().splitlines()[-1])
    candidates = [int(value) for value in worker.get("prelude_candidates") or []]
    consecutive = [int(value) for value in worker.get("first_discovery_hits") or []]
    float_candidates = [
        int(value) for value in worker.get("float_target_candidates") or []
    ]
    float_consecutive = [
        int(value) for value in worker.get("float_following_hits") or []
    ]
    complete = int(args.start) == 0 and int(args.stop) == 1 << 32
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "exhaustive-python-uint32-first-discovery-search",
        "split": {
            "fixed_discovery_total": 20,
            "rounds_constrained": 2,
            "first_target": [491, 210, 379],
            "second_target": [795, 975, 199],
            "holdout_opened": False,
        },
        "search": {
            "start": int(args.start),
            "stop_exclusive": int(args.stop),
            "candidates_tested": int(worker["tested"]),
            "first_target_hits": len(candidates),
            "first_target_candidates": candidates,
            "float_first_target_hits": len(float_candidates),
            "float_first_target_candidates": float_candidates,
            "exact_candidates": consecutive,
            "float_exact_candidates": float_consecutive,
            "discovery_exact_candidates": len(set(consecutive + float_consecutive)),
            "families": {
                "python-random-uint32-fresh-before-first-discovery": {
                    "status": (
                        "candidate" if consecutive else "unsat" if complete else "partial"
                    ),
                    "tested": int(worker["tested"]),
                },
                "python-random-float-uint32-fresh-before-first-discovery": {
                    "status": (
                        "candidate" if float_consecutive else "unsat" if complete else "partial"
                    ),
                    "tested": int(worker["tested"]),
                },
            },
            "complete": complete,
        },
        "safety": {
            "discovery_only": True,
            "holdout_opened": False,
            "network_requests": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **report["search"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
