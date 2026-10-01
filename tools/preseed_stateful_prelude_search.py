#!/usr/bin/env python3
"""Repeat bounded process-PRNG search with the restart prelude included.

This is the independent Python/NumPy-stream counterpart to the NumPy shuffle
layout search.  It models a seed generator whose state is separate from the
validator's shuffle state but which emitted the active pre-restart task's seed
triplet immediately after process start.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

try:
    from tools.preseed_numpy_prelude_search import load_layout
    from tools.preseed_stateful_prng_search import (
        DEFAULT_PROCESS_START,
        atomic_json,
        search_named_material,
        search_numpy_integer_seeds,
        search_numpy_process_seconds,
        search_python_integer_seeds,
        search_python_time_seeds,
    )
except ModuleNotFoundError:
    from preseed_numpy_prelude_search import load_layout
    from preseed_stateful_prng_search import (
        DEFAULT_PROCESS_START,
        atomic_json,
        search_named_material,
        search_numpy_integer_seeds,
        search_numpy_process_seconds,
        search_python_integer_seeds,
        search_python_time_seeds,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--process-start", default=DEFAULT_PROCESS_START)
    parser.add_argument("--prelude", default="654,347,964")
    parser.add_argument("--max-python-seed", type=int, default=10_000_000)
    parser.add_argument("--max-numpy-seed", type=int, default=1_000_000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_stateful_prelude_search.json"),
    )
    args = parser.parse_args()

    observed_prelude = [int(value) for value in args.prelude.split(",")]
    if len(observed_prelude) != 3:
        raise ValueError("--prelude requires three comma-separated integers")
    prelude, segment = load_layout(
        args.discovery.resolve(), args.process_start, observed_prelude
    )
    targets = [prelude] + [
        [int(value) for value in record["seeds"]] for record in segment
    ]
    started = datetime.fromisoformat(args.process_start.replace("Z", "+00:00")).astimezone(timezone.utc)
    python_fixed = search_python_integer_seeds(
        targets, max(0, args.max_python_seed), args.workers
    )
    python_time = search_python_time_seeds(
        targets,
        started,
        {"seconds": 86_400, "milliseconds": 60_000, "microseconds": 100_000},
    )
    numpy_fixed = search_numpy_integer_seeds(
        targets, max(0, args.max_numpy_seed), args.workers
    )
    numpy_time = search_numpy_process_seconds(targets, started, 86_400)
    named = search_named_material(
        targets,
        (
            "niome",
            "validator",
            "niome-validator",
            "genomes",
            "genomes/niome",
            "non2mca3",
            "validator-119-3.0.0",
            55,
            119,
        ),
    )
    families = {
        "python-fixed-integer": python_fixed,
        "python-process-time": python_time,
        "numpy-fixed-integer": numpy_fixed,
        "numpy-process-seconds": numpy_time,
        "named-material": named,
    }
    tested = (
        python_fixed["candidates_tested"]
        + sum(item["candidates_tested"] for item in python_time.values())
        + numpy_fixed["candidates_tested"]
        + numpy_time["candidates_tested"]
        + named["candidates_tested"]
    )
    exact = (
        python_fixed["full_stream_hits"]
        + [hit for item in python_time.values() for hit in item["full_stream_hits"]]
        + numpy_fixed["full_stream_hits"]
        + numpy_time["full_stream_hits"]
        + named["full_stream_hits"]
    )
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "stateful-process-prng-with-restart-prelude",
        "split": {
            "fixed_discovery_total": 20,
            "process_segment_with_prelude": len(targets),
            "prelude_source": "explicit-public-wandb-generated-seeds-event",
            "holdout_opened": False,
        },
        "search": {
            "candidates_tested": tested,
            "families": families,
            "discovery_exact_candidates": len(exact),
            "exact_candidates": exact,
        },
        "interpretation": (
            "A miss closes bounded independent Python/NumPy streams only when the active "
            "pre-restart task emitted the first triplet and no hidden draws interleaved."
        ),
        "safety": {
            "discovery_only": True,
            "holdout_opened": False,
            "network_requests": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({
        "output": str(args.output.resolve()),
        "candidates_tested": tested,
        "first_triplet_hits": (
            len(python_fixed["first_triplet_hits"])
            + sum(len(item["first_triplet_hits"]) for item in python_time.values())
            + len(numpy_fixed["first_triplet_hits"])
            + len(numpy_time["first_triplet_hits"])
        ),
        "discovery_exact_candidates": len(exact),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
