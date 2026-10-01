#!/usr/bin/env python3
"""Test process-persistent PRNG hypotheses on the fixed 20-task discovery set.

The current validator W&B run started between discovery tasks 4 and 5.  That
gives a useful natural restart boundary: if the unpublished implementation
seeded a process-global PRNG from a small constant or the process start clock,
the 16 consecutive discovery triplets beginning with task ``f05ef562`` must be
one continuous stream.  This tool tests that claim without opening the holdout
set and without making any network or submission writes.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import random
import tempfile
from typing import Any, Iterable

import numpy as np


DEFAULT_DISCOVERY = Path("artifacts/research/preseed_datasets/discovery.json")
DEFAULT_OUTPUT = Path("artifacts/research/preseed_stateful_prng_search.json")
DEFAULT_PROCESS_START = "2026-09-26T14:57:51.192497Z"

_TARGETS: list[list[int]] = []


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_process_segment(path: Path, process_start: str) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    boundary = _parse_time(process_start)
    records = sorted(payload.get("records") or [], key=lambda item: item["created_at"])
    return [item for item in records if _parse_time(item["created_at"]) >= boundary]


def _python_triplet(rng: random.Random, method: str) -> list[int]:
    if method == "sample":
        return rng.sample(range(100, 1000), 3)
    if method == "float-unique":
        values: list[int] = []
        while len(values) < 3:
            value = 100 + int(rng.random() * 900)
            if value not in values:
                values.append(value)
        return values
    raise ValueError(method)


def _python_stream(seed: Any, method: str, count: int) -> list[list[int]]:
    rng = random.Random(seed)
    return [_python_triplet(rng, method) for _ in range(count)]


def _init_worker(targets: list[list[int]]) -> None:
    global _TARGETS
    _TARGETS = targets


def _search_python_range(job: tuple[int, int]) -> dict[str, Any]:
    start, stop = job
    hits: list[dict[str, Any]] = []
    first = _TARGETS[0]
    for seed in range(start, stop):
        for method in ("sample", "float-unique"):
            rng = random.Random(seed)
            if _python_triplet(rng, method) != first:
                continue
            matched = 1
            for expected in _TARGETS[1:]:
                if _python_triplet(rng, method) != expected:
                    break
                matched += 1
            hits.append({"seed": seed, "method": method, "matched_tasks": matched})
    return {"start": start, "stop": stop, "hits": hits}


def search_python_integer_seeds(
    targets: list[list[int]], maximum: int, workers: int
) -> dict[str, Any]:
    workers = max(1, workers)
    chunk = max(1, (maximum + workers * 8 - 1) // (workers * 8))
    jobs = [(start, min(maximum, start + chunk)) for start in range(0, maximum, chunk)]
    if workers == 1:
        _init_worker(targets)
        results = [_search_python_range(job) for job in jobs]
    else:
        with mp.Pool(workers, initializer=_init_worker, initargs=(targets,)) as pool:
            results = pool.map(_search_python_range, jobs)
    hits = [hit for result in results for hit in result["hits"]]
    return {
        "seed_range": [0, maximum - 1] if maximum else [],
        "methods": ["sample", "float-unique"],
        "candidates_tested": maximum * 2,
        "first_triplet_hits": hits,
        "full_stream_hits": [hit for hit in hits if hit["matched_tasks"] == len(targets)],
    }


def search_python_time_seeds(
    targets: list[list[int]], process_start: datetime, windows: dict[str, int]
) -> dict[str, Any]:
    scales = {"seconds": 1, "milliseconds": 1_000, "microseconds": 1_000_000}
    families: dict[str, Any] = {}
    for label, radius in windows.items():
        scale = scales[label]
        center = int(process_start.timestamp() * scale)
        hits: list[dict[str, Any]] = []
        for seed in range(center - radius, center + radius + 1):
            for method in ("sample", "float-unique"):
                rng = random.Random(seed)
                if _python_triplet(rng, method) != targets[0]:
                    continue
                matched = 1
                for expected in targets[1:]:
                    if _python_triplet(rng, method) != expected:
                        break
                    matched += 1
                hits.append(
                    {
                        "delta": seed - center,
                        "method": method,
                        "matched_tasks": matched,
                    }
                )
        families[label] = {
            "center": center,
            "radius": radius,
            "candidates_tested": (radius * 2 + 1) * 2,
            "first_triplet_hits": hits,
            "full_stream_hits": [
                hit for hit in hits if hit["matched_tasks"] == len(targets)
            ],
        }
    return families


def _numpy_triplet(seed: int, engine: str, method: str) -> list[int]:
    if engine == "random-state":
        rng: Any = np.random.RandomState(seed & 0xFFFFFFFF)
        if method == "choice":
            values = rng.choice(np.arange(100, 1000), 3, replace=False)
        else:
            values = []
            while len(values) < 3:
                value = int(rng.randint(100, 1000))
                if value not in values:
                    values.append(value)
            return values
    else:
        rng = np.random.default_rng(seed)
        if method == "choice":
            values = rng.choice(np.arange(100, 1000), 3, replace=False)
        else:
            values = []
            while len(values) < 3:
                value = int(rng.integers(100, 1000))
                if value not in values:
                    values.append(value)
            return values
    return [int(value) for value in values]


def _numpy_next_triplet(rng: Any, engine: str, method: str) -> list[int]:
    if method == "choice":
        values = rng.choice(np.arange(100, 1000), 3, replace=False)
        return [int(value) for value in values]
    values: list[int] = []
    while len(values) < 3:
        value = int(
            rng.randint(100, 1000)
            if engine == "random-state"
            else rng.integers(100, 1000)
        )
        if value not in values:
            values.append(value)
    return values


def _search_numpy_range(job: tuple[int, int]) -> dict[str, Any]:
    start, stop = job
    methods = [
        ("random-state", "choice"),
        ("random-state", "integers-unique"),
        ("default-rng", "choice"),
        ("default-rng", "integers-unique"),
    ]
    hits: list[dict[str, Any]] = []
    for seed in range(start, stop):
        for engine, method in methods:
            if _numpy_triplet(seed, engine, method) != _TARGETS[0]:
                continue
            rng: Any = (
                np.random.RandomState(seed)
                if engine == "random-state"
                else np.random.default_rng(seed)
            )
            matched = 0
            for expected in _TARGETS:
                if _numpy_next_triplet(rng, engine, method) != expected:
                    break
                matched += 1
            hits.append(
                {
                    "seed": seed,
                    "engine": engine,
                    "method": method,
                    "matched_tasks": matched,
                }
            )
    return {"start": start, "stop": stop, "hits": hits}


def search_numpy_integer_seeds(
    targets: list[list[int]], maximum: int, workers: int
) -> dict[str, Any]:
    workers = max(1, workers)
    chunk = max(1, (maximum + workers * 8 - 1) // (workers * 8))
    jobs = [(start, min(maximum, start + chunk)) for start in range(0, maximum, chunk)]
    if workers == 1:
        _init_worker(targets)
        results = [_search_numpy_range(job) for job in jobs]
    else:
        with mp.Pool(workers, initializer=_init_worker, initargs=(targets,)) as pool:
            results = pool.map(_search_numpy_range, jobs)
    hits = [hit for result in results for hit in result["hits"]]
    return {
        "seed_range": [0, maximum - 1] if maximum else [],
        "methods": [
            "random-state:choice",
            "random-state:integers-unique",
            "default-rng:choice",
            "default-rng:integers-unique",
        ],
        "candidates_tested": maximum * 4,
        "first_triplet_hits": hits,
        "full_stream_hits": [hit for hit in hits if hit["matched_tasks"] == len(targets)],
    }


def search_numpy_process_seconds(
    targets: list[list[int]], process_start: datetime, radius: int
) -> dict[str, Any]:
    center = int(process_start.timestamp())
    hits: list[dict[str, Any]] = []
    methods = [
        ("random-state", "choice"),
        ("random-state", "integers-unique"),
        ("default-rng", "choice"),
        ("default-rng", "integers-unique"),
    ]
    for seed in range(center - radius, center + radius + 1):
        for engine, method in methods:
            if _numpy_triplet(seed, engine, method) == targets[0]:
                # A fresh NumPy stream is reconstructed only for a rare first hit.
                if engine == "random-state":
                    rng: Any = np.random.RandomState(seed & 0xFFFFFFFF)
                else:
                    rng = np.random.default_rng(seed)
                generated: list[list[int]] = []
                for _ in targets:
                    if method == "choice":
                        values = rng.choice(np.arange(100, 1000), 3, replace=False)
                        generated.append([int(value) for value in values])
                    else:
                        values = []
                        while len(values) < 3:
                            value = int(
                                rng.randint(100, 1000)
                                if engine == "random-state"
                                else rng.integers(100, 1000)
                            )
                            if value not in values:
                                values.append(value)
                        generated.append(values)
                matched = 0
                for actual, expected in zip(generated, targets):
                    if actual != expected:
                        break
                    matched += 1
                hits.append(
                    {
                        "delta_seconds": seed - center,
                        "engine": engine,
                        "method": method,
                        "matched_tasks": matched,
                    }
                )
    return {
        "center": center,
        "radius": radius,
        "methods": [f"{engine}:{method}" for engine, method in methods],
        "candidates_tested": (radius * 2 + 1) * len(methods),
        "first_triplet_hits": hits,
        "full_stream_hits": [hit for hit in hits if hit["matched_tasks"] == len(targets)],
    }


def search_named_material(targets: list[list[int]], materials: Iterable[Any]) -> dict[str, Any]:
    rows = []
    for material in materials:
        label = str(material)
        seeds: list[Any] = [material]
        encoded = label.encode()
        for algorithm in ("sha256", "sha512", "blake2b"):
            digest = hashlib.new(algorithm, encoded).digest()
            seeds.extend((int.from_bytes(digest, "big"), int.from_bytes(digest, "little")))
        for seed in seeds:
            for method in ("sample", "float-unique"):
                generated = _python_stream(seed, method, len(targets))
                matched = 0
                for actual, expected in zip(generated, targets):
                    if actual != expected:
                        break
                    matched += 1
                rows.append(
                    {
                        "material": label,
                        "derived_seed_type": type(seed).__name__,
                        "method": method,
                        "matched_tasks": matched,
                    }
                )
    return {
        "candidates_tested": len(rows),
        "full_stream_hits": [row for row in rows if row["matched_tasks"] == len(targets)],
        "best_match": max((row["matched_tasks"] for row in rows), default=0),
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
    parser.add_argument("--discovery", type=Path, default=DEFAULT_DISCOVERY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--process-start", default=DEFAULT_PROCESS_START)
    parser.add_argument("--max-fixed-seed", type=int, default=10_000_000)
    parser.add_argument("--max-numpy-seed", type=int, default=1_000_000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--second-radius", type=int, default=86_400)
    parser.add_argument("--millisecond-radius", type=int, default=60_000)
    parser.add_argument("--microsecond-radius", type=int, default=100_000)
    args = parser.parse_args()

    process_start = _parse_time(args.process_start)
    segment = load_process_segment(args.discovery.resolve(), args.process_start)
    targets = [list(map(int, item["seeds"])) for item in segment]
    if not targets:
        raise SystemExit("no discovery records exist after the process start boundary")

    integer_search = search_python_integer_seeds(
        targets, max(0, args.max_fixed_seed), max(1, args.workers)
    )
    numpy_integer_search = search_numpy_integer_seeds(
        targets, max(0, args.max_numpy_seed), max(1, args.workers)
    )
    time_search = search_python_time_seeds(
        targets,
        process_start,
        {
            "seconds": max(0, args.second_radius),
            "milliseconds": max(0, args.millisecond_radius),
            "microseconds": max(0, args.microsecond_radius),
        },
    )
    numpy_seconds = search_numpy_process_seconds(
        targets, process_start, max(0, args.second_radius)
    )
    named = search_named_material(
        targets,
        [
            55,
            119,
            470,
            2026,
            20260926,
            "55",
            "119",
            "seus",
            "niome",
            "non2mca3",
            "validator-119-3.0.0",
            "2026-09-26",
            "9d9347a7ffab85a04eda6c36b9e87c59c8bb4049",
        ],
    )
    candidates_tested = (
        integer_search["candidates_tested"]
        + numpy_integer_search["candidates_tested"]
        + sum(item["candidates_tested"] for item in time_search.values())
        + numpy_seconds["candidates_tested"]
        + named["candidates_tested"]
    )
    exact = (
        integer_search["full_stream_hits"]
        + numpy_integer_search["full_stream_hits"]
        + [hit for item in time_search.values() for hit in item["full_stream_hits"]]
        + numpy_seconds["full_stream_hits"]
        + named["full_stream_hits"]
    )
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "stateful-process-prng-search",
        "split": {
            "discovery_total": 20,
            "process_segment_tasks": len(segment),
            "first_task_id": segment[0]["task_id"],
            "last_task_id": segment[-1]["task_id"],
            "process_start": args.process_start,
            "holdout_opened": False,
        },
        "search": {
            "candidates_tested": candidates_tested,
            "discovery_exact_candidates": len(exact),
            "families": {
                "python-fixed-integer": integer_search,
                "numpy-fixed-integer": numpy_integer_search,
                "python-process-time": time_search,
                "numpy-process-seconds": numpy_seconds,
                "named-material": named,
            },
            "exact_candidates": exact,
        },
        "interpretation": (
            "A full hit must reproduce every consecutive triplet after the observed "
            "validator process-start boundary. A miss rejects only bounded constant/time "
            "seeding; OS-entropy, hidden reseeding, and unobserved interleaved draws remain open."
        ),
        "safety": {
            "read_only": True,
            "discovery_only": True,
            "holdout_opened": False,
            "network_requests": False,
            "credentials_accepted": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "process_segment_tasks": len(segment),
                "candidates_tested": candidates_tested,
                "discovery_exact_candidates": len(exact),
                "first_triplet_hits": len(integer_search["first_triplet_hits"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
