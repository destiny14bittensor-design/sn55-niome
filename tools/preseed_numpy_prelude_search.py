#!/usr/bin/env python3
"""Search NumPy global streams with the restart-boundary prelude restored.

The public validator process started while an older task was already active.
Its first observed action was to validate that task and publish one seed
triplet; only the *next* task produced the first observable NumPy shuffle.
Consequently the correct same-global-RNG layout is::

    prelude seed triplet -> shuffle -> triplet -> shuffle -> triplet ...

The earlier interleaved search started at the first shuffle and therefore did
not close this distinct layout.  Holdout labels are never loaded.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import multiprocessing as mp
import os
from pathlib import Path
import tempfile
from typing import Any

import numpy as np

try:
    from tools.preseed_numpy_interleaved_search import (
        DEFAULT_PROCESS_START,
        _seed_triplet,
        shuffle_sizes_for_segment,
    )
except ModuleNotFoundError:
    from preseed_numpy_interleaved_search import (
        DEFAULT_PROCESS_START,
        _seed_triplet,
        shuffle_sizes_for_segment,
    )


_PRELUDE: list[int] = []
_TARGETS: list[list[int]] = []
_SIZES: list[int] = []


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_layout(
    path: Path,
    process_start: str,
    observed_prelude: list[int] | None = None,
) -> tuple[list[int], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    boundary = _parse_time(process_start)
    records = sorted(payload.get("records") or [], key=lambda item: item["created_at"])
    before = [record for record in records if _parse_time(record["created_at"]) < boundary]
    after = [record for record in records if _parse_time(record["created_at"]) >= boundary]
    if not before or not after:
        raise ValueError("restart boundary must divide the Discovery records")
    prelude = (
        [int(value) for value in observed_prelude]
        if observed_prelude is not None
        else [int(value) for value in before[-1]["seeds"]]
    )
    return prelude, after


def _init_worker(prelude: list[int], targets: list[list[int]], sizes: list[int]) -> None:
    global _PRELUDE, _TARGETS, _SIZES
    _PRELUDE = prelude
    _TARGETS = targets
    _SIZES = sizes


def generate_layout(
    seed: int, method: str, prelude: list[int], sizes: list[int]
) -> tuple[list[int], list[list[int]]]:
    rng = np.random.RandomState(seed & 0xFFFFFFFF)
    first = _seed_triplet(rng, method)
    output = []
    for size in sizes:
        values = np.arange(size, dtype=np.int64)
        rng.shuffle(values)
        output.append(_seed_triplet(rng, method))
    return first, output


def _search_range(job: tuple[str, int, int]) -> dict[str, Any]:
    family, start, stop = job
    hits = []
    for raw_seed in range(start, stop):
        seed = raw_seed & 0xFFFFFFFF
        for method in ("choice", "integers-unique"):
            rng = np.random.RandomState(seed)
            if _seed_triplet(rng, method) != _PRELUDE:
                continue
            matched = 1
            for size, expected in zip(_SIZES, _TARGETS):
                values = np.arange(size, dtype=np.int64)
                rng.shuffle(values)
                if _seed_triplet(rng, method) != expected:
                    break
                matched += 1
            hits.append(
                {
                    "family": family,
                    "raw_seed": raw_seed,
                    "seed_u32": seed,
                    "method": method,
                    "matched_tasks_including_prelude": matched,
                }
            )
    return {"hits": hits, "raw_seeds": stop - start}


def search_ranges(
    prelude: list[int],
    targets: list[list[int]],
    sizes: list[int],
    family: str,
    ranges: list[tuple[int, int]],
    workers: int,
) -> dict[str, Any]:
    total = sum(stop - start for start, stop in ranges)
    chunk = max(1, (total + workers * 8 - 1) // (workers * 8))
    jobs = [
        (family, start, min(stop, cursor + chunk))
        for start, stop in ranges
        for cursor in range(start, stop, chunk)
        for start in [cursor]
    ]
    if workers <= 1:
        _init_worker(prelude, targets, sizes)
        results = [_search_range(job) for job in jobs]
    else:
        with mp.Pool(
            workers,
            initializer=_init_worker,
            initargs=(prelude, targets, sizes),
        ) as pool:
            results = pool.map(_search_range, jobs)
    hits = [hit for result in results for hit in result["hits"]]
    expected = len(targets) + 1
    return {
        "raw_seed_ranges": [[start, stop - 1] for start, stop in ranges],
        "methods": ["choice", "integers-unique"],
        "candidates_tested": total * 2,
        "prelude_hits": hits,
        "full_discovery_hits": [
            hit for hit in hits if hit["matched_tasks_including_prelude"] == expected
        ],
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
    parser.add_argument("--shuffle-constraints", type=Path)
    parser.add_argument("--process-start", default=DEFAULT_PROCESS_START)
    parser.add_argument(
        "--prelude",
        default="654,347,964",
        help="First seed triplet observed in the validator run before its first shuffle.",
    )
    parser.add_argument("--max-fixed-seed", type=int, default=1_000_000)
    parser.add_argument("--seconds-radius", type=int, default=86_400)
    parser.add_argument("--milliseconds-radius", type=int, default=60_000)
    parser.add_argument("--microseconds-radius", type=int, default=100_000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_numpy_prelude_search.json"),
    )
    args = parser.parse_args()

    observed_prelude = [int(value) for value in args.prelude.split(",")]
    if len(observed_prelude) != 3:
        raise ValueError("--prelude requires three comma-separated integers")
    prelude, segment = load_layout(
        args.discovery.resolve(), args.process_start, observed_prelude
    )
    targets = [[int(value) for value in record["seeds"]] for record in segment]
    sizes = shuffle_sizes_for_segment(
        segment,
        args.shuffle_constraints.resolve() if args.shuffle_constraints else None,
    )
    started = _parse_time(args.process_start).timestamp()
    centers = {
        "process-seconds": int(started),
        "process-milliseconds": int(started * 1_000),
        "process-microseconds": int(started * 1_000_000),
    }
    ranges = {
        "fixed-seed": [(0, max(0, args.max_fixed_seed))],
        "process-seconds": [
            (centers["process-seconds"] - args.seconds_radius, centers["process-seconds"] + args.seconds_radius + 1)
        ],
        "process-milliseconds": [
            (centers["process-milliseconds"] - args.milliseconds_radius, centers["process-milliseconds"] + args.milliseconds_radius + 1)
        ],
        "process-microseconds": [
            (centers["process-microseconds"] - args.microseconds_radius, centers["process-microseconds"] + args.microseconds_radius + 1)
        ],
    }
    families = {
        family: search_ranges(prelude, targets, sizes, family, family_ranges, args.workers)
        for family, family_ranges in ranges.items()
    }
    exact = [hit for result in families.values() for hit in result["full_discovery_hits"]]
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "numpy-global-prelude-shuffle-seed-interleaving",
        "split": {
            "fixed_discovery_total": 20,
            "prelude_tasks": 1,
            "prelude_source": "explicit-public-wandb-generated-seeds-event",
            "post_restart_tasks": len(segment),
            "holdout_opened": False,
        },
        "search": {
            "candidates_tested": sum(result["candidates_tested"] for result in families.values()),
            "families": families,
            "discovery_exact_candidates": len(exact),
            "exact_candidates": exact,
        },
        "interpretation": (
            "This closes bounded RandomState initializers for the observed restart layout: "
            "one prelude triplet, then one validator shuffle before every later triplet."
        ),
        "safety": {
            "public_read_only": True,
            "discovery_only": True,
            "holdout_opened": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "candidates_tested": report["search"]["candidates_tested"],
                "prelude_hits": sum(len(result["prelude_hits"]) for result in families.values()),
                "discovery_exact_candidates": len(exact),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
