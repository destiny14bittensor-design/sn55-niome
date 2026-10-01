#!/usr/bin/env python3
"""Checkpoint and validate exhaustive uint32 process-PRNG initializer scans."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any

try:
    from tools.preseed_generator_lab import atomic_json
    from tools.preseed_numpy_prelude_search import generate_layout, load_layout
    from tools.preseed_numpy_interleaved_search import shuffle_sizes_for_segment
    from tools.preseed_runtime_identity_search import python_stream
except ModuleNotFoundError:
    from preseed_generator_lab import atomic_json
    from preseed_numpy_prelude_search import generate_layout, load_layout
    from preseed_numpy_interleaved_search import shuffle_sizes_for_segment
    from preseed_runtime_identity_search import python_stream


def validate_hits(
    engine: str,
    hits: list[int],
    discovery: Path,
    shuffle_constraints: Path | None,
    process_start: str,
) -> list[dict[str, Any]]:
    prelude, segment = load_layout(discovery, process_start, [654, 347, 964])
    targets = [[int(value) for value in row["seeds"]] for row in segment]
    validated = []
    for seed in sorted(set(int(value) for value in hits)):
        if engine == "numpy-randomstate-integers-unique":
            sizes = shuffle_sizes_for_segment(segment, shuffle_constraints)
            first, actual = generate_layout(seed, "integers-unique", prelude, sizes)
            matched = int(first == prelude)
            for left, right in zip(actual, targets):
                if left != right:
                    break
                matched += 1
        elif engine == "python-random-integers-unique":
            actual = python_stream(seed, "randint-unique", len(targets) + 1)
            expected = [prelude, *targets]
            matched = 0
            for left, right in zip(actual, expected):
                if left != right:
                    break
                matched += 1
        else:
            raise ValueError(engine)
        validated.append(
            {
                "seed_u32": seed,
                "matched_tasks_including_prelude": matched,
                "full_discovery_segment_exact": matched == len(targets) + 1,
            }
        )
    return validated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument(
        "--engine",
        choices=(
            "numpy-randomstate-integers-unique",
            "python-random-integers-unique",
        ),
        required=True,
    )
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--shuffle-constraints", type=Path)
    parser.add_argument("--process-start", default="2026-09-26T14:57:51.192497Z")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--stop", type=int, default=1 << 32)
    parser.add_argument("--chunk-size", type=int, default=100_000_000)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    previous = {}
    if args.output.exists():
        previous = json.loads(args.output.read_text(encoding="utf-8"))
    search = previous.get("search") or {}
    cursor = max(int(args.start), int(search.get("stop_exclusive") or args.start))
    prelude_hits = int(search.get("prelude_hits") or 0)
    prelude_candidates = [
        int(value) for value in search.get("prelude_candidates") or []
    ]
    first_hits = [int(value) for value in search.get("first_discovery_hits") or []]
    while cursor < args.stop:
        stop = min(int(args.stop), cursor + max(1, int(args.chunk_size)))
        completed = subprocess.run(
            [
                str(args.binary.resolve()),
                str(cursor),
                str(stop),
                str(max(1, args.threads)),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        row = json.loads(completed.stdout.strip().splitlines()[-1])
        prelude_hits += int(row["prelude_hits"])
        prelude_candidates.extend(
            int(value) for value in row.get("prelude_candidates") or []
        )
        first_hits.extend(int(value) for value in row["first_discovery_hits"])
        cursor = stop
        validated = validate_hits(
            args.engine,
            first_hits,
            args.discovery.resolve(),
            args.shuffle_constraints.resolve() if args.shuffle_constraints else None,
            args.process_start,
        )
        report = {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "exhaustive-uint32-process-prng-initializer-search",
            "engine": args.engine,
            "split": {
                "fixed_discovery_total": 20,
                "restart_prelude": [654, 347, 964],
                "holdout_opened": False,
            },
            "search": {
                "start": int(args.start),
                "stop_exclusive": cursor,
                "target_stop_exclusive": int(args.stop),
                "candidates_tested": cursor - int(args.start),
                "prelude_hits": prelude_hits,
                "prelude_candidates": sorted(set(prelude_candidates)),
                "first_discovery_hits": sorted(set(first_hits)),
                "validated_candidates": validated,
                "discovery_exact_candidates": sum(
                    bool(item["full_discovery_segment_exact"]) for item in validated
                ),
                "families": {args.engine: {"tested": cursor - int(args.start)}},
                "complete": cursor == int(args.stop),
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
