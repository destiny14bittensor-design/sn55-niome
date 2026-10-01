#!/usr/bin/env python3
"""Search legacy NumPy streams with validator shuffle calls interleaved.

The deployed validator publicly executes ``np.random.shuffle(miner_uids)``
once before validation.  If unpublished seed generation reused the same legacy
global RandomState, testing seed triplets without those shuffle calls advances
the wrong state.  This bounded search reproduces ``shuffle -> three seeds`` for
every Discovery task after the observed process-start boundary.
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
    from tools.preseed_stateful_prng_search import load_process_segment
except ModuleNotFoundError:
    from preseed_stateful_prng_search import load_process_segment


DEFAULT_PROCESS_START = "2026-09-26T14:57:51.192497Z"
_TARGETS: list[list[int]] = []
_SHUFFLE_SIZES: list[int] = []


def _init_worker(targets: list[list[int]], sizes: list[int]) -> None:
    global _TARGETS, _SHUFFLE_SIZES
    _TARGETS = targets
    _SHUFFLE_SIZES = sizes


def _seed_triplet(rng: np.random.RandomState, method: str) -> list[int]:
    if method == "choice":
        return [
            int(value)
            for value in rng.choice(np.arange(100, 1000), 3, replace=False)
        ]
    values: list[int] = []
    while len(values) < 3:
        value = int(rng.randint(100, 1000))
        if value not in values:
            values.append(value)
    return values


def generate_interleaved(
    seed: int, method: str, sizes: list[int], count: int | None = None
) -> list[list[int]]:
    rng = np.random.RandomState(seed & 0xFFFFFFFF)
    output = []
    for size in sizes[:count]:
        values = np.arange(size, dtype=np.int64)
        rng.shuffle(values)
        output.append(_seed_triplet(rng, method))
    return output


def _search_range(job: tuple[int, int]) -> dict[str, Any]:
    start, stop = job
    hits = []
    for seed in range(start, stop):
        for method in ("choice", "integers-unique"):
            rng = np.random.RandomState(seed & 0xFFFFFFFF)
            matched = 0
            for size, expected in zip(_SHUFFLE_SIZES, _TARGETS):
                values = np.arange(size, dtype=np.int64)
                rng.shuffle(values)
                if _seed_triplet(rng, method) != expected:
                    break
                matched += 1
            if matched:
                hits.append({"seed": seed, "method": method, "matched_tasks": matched})
    return {"start": start, "stop": stop, "hits": hits}


def search_ranges(
    targets: list[list[int]],
    sizes: list[int],
    ranges: list[tuple[int, int]],
    workers: int,
) -> dict[str, Any]:
    jobs: list[tuple[int, int]] = []
    chunk_target = max(1, sum(stop - start for start, stop in ranges) // (max(1, workers) * 8))
    for range_start, range_stop in ranges:
        for start in range(range_start, range_stop, chunk_target):
            jobs.append((start, min(range_stop, start + chunk_target)))
    if workers <= 1:
        _init_worker(targets, sizes)
        results = [_search_range(job) for job in jobs]
    else:
        with mp.Pool(
            max(1, workers), initializer=_init_worker, initargs=(targets, sizes)
        ) as pool:
            results = pool.map(_search_range, jobs)
    hits = [hit for result in results for hit in result["hits"]]
    candidate_seeds = sum(stop - start for start, stop in ranges)
    return {
        "ranges": [[start, stop - 1] for start, stop in ranges],
        "methods": ["choice", "integers-unique"],
        "candidates_tested": candidate_seeds * 2,
        "first_triplet_hits": hits,
        "full_stream_hits": [hit for hit in hits if hit["matched_tasks"] == len(targets)],
    }


def shuffle_sizes_for_segment(
    segment: list[dict[str, Any]], constraint_path: Path | None
) -> list[int]:
    sizes_by_task: dict[str, int] = {}
    if constraint_path and constraint_path.exists():
        payload = json.loads(constraint_path.read_text(encoding="utf-8"))
        sizes_by_task = {
            str(row["task_id"]): int(row["shuffle_size"])
            for row in payload.get("rounds") or []
        }
    known = list(sizes_by_task.values())
    fallback = max(set(known), key=known.count) if known else 256
    return [sizes_by_task.get(str(row["task_id"]), fallback) for row in segment]


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
    parser.add_argument("--max-fixed-seed", type=int, default=1_000_000)
    parser.add_argument("--time-radius-seconds", type=int, default=86_400)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_numpy_interleaved_search.json"),
    )
    args = parser.parse_args()

    segment = load_process_segment(args.discovery.resolve(), args.process_start)
    targets = [list(map(int, row["seeds"])) for row in segment]
    sizes = shuffle_sizes_for_segment(
        segment, args.shuffle_constraints.resolve() if args.shuffle_constraints else None
    )
    process_seconds = int(
        datetime.fromisoformat(args.process_start.replace("Z", "+00:00"))
        .astimezone(timezone.utc)
        .timestamp()
    )
    fixed = search_ranges(
        targets,
        sizes,
        [(0, max(0, args.max_fixed_seed))],
        args.workers,
    )
    radius = max(0, args.time_radius_seconds)
    timed = search_ranges(
        targets,
        sizes,
        [(process_seconds - radius, process_seconds + radius + 1)],
        args.workers,
    )
    exact = fixed["full_stream_hits"] + timed["full_stream_hits"]
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "numpy-global-shuffle-seed-interleaving",
        "split": {
            "discovery_total": 20,
            "process_segment_tasks": len(segment),
            "holdout_opened": False,
        },
        "search": {
            "candidates_tested": fixed["candidates_tested"] + timed["candidates_tested"],
            "discovery_exact_candidates": len(exact),
            "families": {"fixed-seed": fixed, "process-time-seed": timed},
            "exact_candidates": exact,
        },
        "shuffle_sizes": sizes,
        "interpretation": (
            "This closes bounded legacy RandomState seeds only when exactly one validator "
            "shuffle precedes each seed triplet and no other global NumPy draws interleave."
        ),
        "safety": {
            "discovery_only": True,
            "holdout_opened": False,
            "public_read_only": True,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "candidates_tested": report["search"]["candidates_tested"],
                "discovery_exact_candidates": len(exact),
                "first_triplet_hits": len(fixed["first_triplet_hits"]) + len(timed["first_triplet_hits"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
