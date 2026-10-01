#!/usr/bin/env python3
"""Search actual validator-start clock hypotheses on fixed Discovery labels.

Task creation time is only a schedule proxy.  This search uses the public W&B
timestamp of ``Validating miners' submissions`` for each labelled Discovery
round and tests one fixed clock offset across all available rounds.  The
Holdout labels are never loaded.  Full improved-pagination logs are consumed so
the beginning of the long-lived validator process is not silently discarded.
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
import re
import tempfile
from typing import Any, Callable

import numpy as np

try:
    from tools.preseed_shuffle_leak_audit import fetch_log_nodes
except ModuleNotFoundError:
    from preseed_shuffle_leak_audit import fetch_log_nodes


TASK_RE = re.compile(r"Fetched task ([0-9a-f-]{36})")
VALIDATION_RE = re.compile(r"Validating miners' submissions")
LOW = 100
HIGH_EXCLUSIVE = 1000
_ROWS: list[tuple[float, list[int]]] = []


def validation_times(nodes: list[dict[str, Any]]) -> dict[str, float]:
    current = ""
    result: dict[str, float] = {}
    for node in nodes:
        line = str(node.get("line") or "")
        match = TASK_RE.search(line)
        if match:
            current = match.group(1)
        elif current and VALIDATION_RE.search(line):
            timestamp = str(node.get("timestamp") or "")
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            result[current] = parsed.astimezone(timezone.utc).timestamp()
    return result


def load_discovery_rows(path: Path, times: dict[str, float]) -> list[tuple[float, list[int]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for record in payload.get("records") or []:
        task_id = str(record["task_id"])
        if task_id in times:
            rows.append((times[task_id], [int(value) for value in record["seeds"]]))
    return rows


def _unique(draw: Callable[[], int]) -> list[int]:
    values: list[int] = []
    while len(values) < 3:
        value = int(draw())
        if value not in values:
            values.append(value)
    return values


def generate(seed: int, method: str) -> list[int]:
    if method == "python-sample":
        return random.Random(seed).sample(range(LOW, HIGH_EXCLUSIVE), 3)
    if method == "python-randrange":
        rng = random.Random(seed)
        return _unique(lambda: rng.randrange(LOW, HIGH_EXCLUSIVE))
    if method == "numpy-randomstate-choice":
        rng = np.random.RandomState(seed & 0xFFFFFFFF)
        return [int(value) for value in rng.choice(np.arange(LOW, HIGH_EXCLUSIVE), 3, replace=False)]
    if method == "numpy-randomstate-integers":
        rng = np.random.RandomState(seed & 0xFFFFFFFF)
        return _unique(lambda: rng.randint(LOW, HIGH_EXCLUSIVE))
    if method == "numpy-default-choice":
        rng = np.random.default_rng(seed)
        return [int(value) for value in rng.choice(np.arange(LOW, HIGH_EXCLUSIVE), 3, replace=False)]
    if method == "numpy-default-integers":
        rng = np.random.default_rng(seed)
        return _unique(lambda: rng.integers(LOW, HIGH_EXCLUSIVE))
    if method.startswith("digest-"):
        algorithm, endian = method.removeprefix("digest-").split("-")
        material = str(seed).encode()
        digest = hashlib.new(algorithm, material).digest()
        result = []
        offset = 0
        while len(result) < 3:
            if offset + 8 > len(digest):
                digest = hashlib.new(algorithm, digest).digest()
                offset = 0
            value = LOW + int.from_bytes(digest[offset : offset + 8], endian) % 900
            offset += 8
            if value not in result:
                result.append(value)
        return result
    raise ValueError(method)


METHODS = (
    "python-sample",
    "python-randrange",
    "numpy-randomstate-choice",
    "numpy-randomstate-integers",
    "numpy-default-choice",
    "numpy-default-integers",
    "digest-sha256-big",
    "digest-sha256-little",
    "digest-blake2b-big",
    "digest-blake2b-little",
)


def _init_worker(rows: list[tuple[float, list[int]]]) -> None:
    global _ROWS
    _ROWS = rows


def _search_job(job: tuple[str, int, int, int]) -> dict[str, Any]:
    unit, scale, start, stop = job
    hits = []
    first_time, first_expected = _ROWS[0]
    for delta in range(start, stop):
        first_seed = int(first_time * scale) + delta
        for method in METHODS:
            if generate(first_seed, method) != first_expected:
                continue
            matched = 1
            for timestamp, expected in _ROWS[1:]:
                if generate(int(timestamp * scale) + delta, method) != expected:
                    break
                matched += 1
            hits.append(
                {
                    "unit": unit,
                    "delta": delta,
                    "method": method,
                    "matched_tasks": matched,
                }
            )
    return {"hits": hits, "offsets": stop - start}


def search_unit(
    rows: list[tuple[float, list[int]]], unit: str, scale: int, radius: int, workers: int
) -> dict[str, Any]:
    total = radius * 2 + 1
    chunk = max(1, (total + workers * 8 - 1) // (workers * 8))
    jobs = [
        (unit, scale, start, min(radius + 1, start + chunk))
        for start in range(-radius, radius + 1, chunk)
    ]
    if workers <= 1:
        _init_worker(rows)
        results = [_search_job(job) for job in jobs]
    else:
        with mp.Pool(workers, initializer=_init_worker, initargs=(rows,)) as pool:
            results = pool.map(_search_job, jobs)
    hits = [hit for result in results for hit in result["hits"]]
    return {
        "unit": unit,
        "radius": radius,
        "methods": list(METHODS),
        "candidates_tested": total * len(METHODS),
        "first_triplet_hits": hits,
        "full_discovery_hits": [hit for hit in hits if hit["matched_tasks"] == len(rows)],
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
        default=Path("artifacts/research/preseed_validation_time_search.json"),
    )
    args = parser.parse_args()

    nodes = fetch_log_nodes(args.entity, args.project, args.run)
    rows = load_discovery_rows(args.discovery.resolve(), validation_times(nodes))
    if not rows:
        raise SystemExit("no Discovery rounds have public validation timestamps")
    families = {
        "seconds": search_unit(rows, "seconds", 1, max(0, args.seconds_radius), args.workers),
        "milliseconds": search_unit(
            rows, "milliseconds", 1_000, max(0, args.milliseconds_radius), args.workers
        ),
        "microseconds": search_unit(
            rows, "microseconds", 1_000_000, max(0, args.microseconds_radius), args.workers
        ),
    }
    exact = [hit for family in families.values() for hit in family["full_discovery_hits"]]
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "actual-validation-clock-fixed-offset-search",
        "split": {
            "fixed_discovery_total": 20,
            "discovery_with_public_validation_time": len(rows),
            "holdout_opened": False,
        },
        "search": {
            "candidates_tested": sum(family["candidates_tested"] for family in families.values()),
            "families": families,
            "discovery_exact_candidates": len(exact),
            "exact_candidates": exact,
        },
        "interpretation": (
            "A miss rejects only per-round PRNG/hash seeding from the public validation-start "
            "clock with one fixed offset inside the configured windows. It does not reject an "
            "OS-seeded backend RNG or variable private request latency."
        ),
        "safety": {
            "public_read_only": True,
            "discovery_only": True,
            "holdout_opened": False,
            "stores_endpoint_or_ip": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "discovery_with_public_validation_time": len(rows),
                "candidates_tested": report["search"]["candidates_tested"],
                "first_triplet_hits": sum(
                    len(family["first_triplet_hits"]) for family in families.values()
                ),
                "discovery_exact_candidates": len(exact),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
