#!/usr/bin/env python3
"""Search fixed hidden bounded draws between modern NumPy seed triplets.

The exhaustive initializer reports reduce each uint32 engine to a handful of
initializers matching the restart prelude.  This tool tests whether the later
Discovery triplets appear when every seed event is separated by one constant
number of ``integers(100, 1000)`` calls.  It never reads Holdout labels.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np

try:
    from tools.preseed_generator_lab import atomic_json
    from tools.preseed_numpy_prelude_search import load_layout
except ModuleNotFoundError:
    from preseed_generator_lab import atomic_json
    from preseed_numpy_prelude_search import load_layout


ENGINES = {
    "numpy-pcg64": np.random.PCG64,
    "numpy-pcg64dxsm": np.random.PCG64DXSM,
    "numpy-sfc64": np.random.SFC64,
    "numpy-philox": np.random.Philox,
}


def unique_triplet(rng: np.random.Generator) -> list[int]:
    result: list[int] = []
    while len(result) < 3:
        value = int(rng.integers(100, 1000))
        if value not in result:
            result.append(value)
    return result


def float_triplet(rng: np.random.Generator) -> list[int]:
    result: list[int] = []
    while len(result) < 3:
        value = 100 + int(float(rng.random()) * 900)
        if value not in result:
            result.append(value)
    return result


def candidate_rows(
    paths: Iterable[Path], method: str = "integers-unique"
) -> list[dict[str, Any]]:
    rows = []
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        search = report.get("search") or {}
        if search.get("method") != method:
            continue
        engine = str(search.get("engine") or "")
        if engine not in ENGINES:
            continue
        for initializer in search.get("first_triplet_candidates") or []:
            rows.append(
                {
                    "engine": engine,
                    "initializer": int(initializer),
                    "source_report": path.name,
                }
            )
    return rows


def fixed_gap_hits(
    engine: str,
    initializer: int,
    targets: list[list[int]],
    max_gap: int,
    chunk_size: int,
) -> list[int]:
    rng = np.random.Generator(ENGINES[engine](initializer))
    if unique_triplet(rng) != targets[0]:
        raise ValueError("initializer no longer reproduces the restart prelude")
    wanted = np.asarray(targets[1], dtype=np.int64)
    carry = np.empty(0, dtype=np.int64)
    consumed = 0
    possible: list[int] = []
    # Need starts 0..max_gap after the prelude, plus two values to complete the
    # last window.  Chunk overlap is retained explicitly in ``carry``.
    remaining = max_gap + 3
    while remaining > 0:
        count = min(max(3, chunk_size), remaining)
        fresh = rng.integers(100, 1000, size=count, dtype=np.int64)
        values = np.concatenate((carry, fresh)) if carry.size else fresh
        base = consumed - int(carry.size)
        if values.size >= 3:
            hits = np.flatnonzero(
                (values[:-2] == wanted[0])
                & (values[1:-1] == wanted[1])
                & (values[2:] == wanted[2])
            )
            possible.extend(
                base + int(hit)
                for hit in hits
                if 0 <= base + int(hit) <= max_gap
            )
        carry = values[-2:].copy()
        consumed += count
        remaining -= count

    exact: list[int] = []
    for gap in possible:
        replay = np.random.Generator(ENGINES[engine](initializer))
        if unique_triplet(replay) != targets[0]:
            continue
        matches = True
        for target in targets[1:]:
            if gap:
                replay.integers(100, 1000, size=gap, dtype=np.int64)
            if unique_triplet(replay) != target:
                matches = False
                break
        if matches:
            exact.append(gap)
    return sorted(set(exact))


def fixed_float_gap_hits(
    engine: str,
    initializer: int,
    targets: list[list[int]],
    max_gap: int,
    chunk_size: int,
) -> list[int]:
    """Find a constant count of hidden ``random()`` calls between triplets."""
    rng = np.random.Generator(ENGINES[engine](initializer))
    if float_triplet(rng) != targets[0]:
        raise ValueError("initializer no longer reproduces the restart prelude")
    wanted = np.asarray(targets[1], dtype=np.int64)
    carry = np.empty(0, dtype=np.int64)
    consumed = 0
    possible: list[int] = []
    remaining = max_gap + 3
    while remaining > 0:
        count = min(max(3, chunk_size), remaining)
        fresh = 100 + (rng.random(size=count) * 900).astype(np.int64)
        values = np.concatenate((carry, fresh)) if carry.size else fresh
        base = consumed - int(carry.size)
        if values.size >= 3:
            hits = np.flatnonzero(
                (values[:-2] == wanted[0])
                & (values[1:-1] == wanted[1])
                & (values[2:] == wanted[2])
            )
            possible.extend(
                base + int(hit)
                for hit in hits
                if 0 <= base + int(hit) <= max_gap
            )
        carry = values[-2:].copy()
        consumed += count
        remaining -= count

    exact: list[int] = []
    for gap in possible:
        replay = np.random.Generator(ENGINES[engine](initializer))
        if float_triplet(replay) != targets[0]:
            continue
        matches = True
        for target in targets[1:]:
            if gap:
                replay.random(size=gap)
            if float_triplet(replay) != target:
                matches = False
                break
        if matches:
            exact.append(gap)
    return sorted(set(exact))


def choice_triplet(rng: np.random.Generator) -> list[int]:
    return [
        int(value)
        for value in rng.choice(np.arange(100, 1000), 3, replace=False)
    ]


def _choice_windows(raw: np.ndarray, starts: np.ndarray) -> np.ndarray:
    """Evaluate NumPy's exact Floyd+shuffle choice at many raw32 offsets."""
    positions = starts.astype(np.int64, copy=True)
    draws = []
    for width in (898, 899, 900, 3, 2):
        threshold = ((1 << 32) - width) % width
        accepted = np.zeros(starts.size, dtype=bool)
        values = np.empty(starts.size, dtype=np.uint32)
        while not bool(np.all(accepted)):
            pending = np.flatnonzero(~accepted)
            if pending.size == 0:
                break
            if int(positions[pending].max(initial=0)) >= raw.size:
                raise RuntimeError("choice gap lookahead exhausted")
            words = raw[positions[pending]].astype(np.uint64)
            products = words * np.uint64(width)
            valid = (products & np.uint64(0xFFFFFFFF)) >= np.uint64(threshold)
            values[pending[valid]] = (products[valid] >> np.uint64(32)).astype(
                np.uint32
            )
            accepted[pending[valid]] = True
            positions[pending] += 1
        draws.append(values)

    first = draws[0].astype(np.int64)
    second_raw = draws[1].astype(np.int64)
    second = np.where(second_raw == first, 898, second_raw)
    third_raw = draws[2].astype(np.int64)
    third = np.where((third_raw == first) | (third_raw == second), 899, third_raw)
    output = np.column_stack((first, second, third))
    rows = np.arange(starts.size)
    shuffle_two = draws[3].astype(np.int64)
    saved = output[:, 2].copy()
    output[:, 2] = output[rows, shuffle_two]
    output[rows, shuffle_two] = saved
    shuffle_one = draws[4].astype(np.int64)
    saved = output[:, 1].copy()
    output[:, 1] = output[rows, shuffle_one]
    output[rows, shuffle_one] = saved
    return output + 100


def _skip_raw32(rng: np.random.Generator, count: int, chunk_size: int) -> None:
    remaining = count
    while remaining:
        current = min(remaining, chunk_size)
        rng.integers(0, 1 << 32, size=current, dtype=np.uint32)
        remaining -= current


def fixed_choice_gap_hits(
    engine: str,
    initializer: int,
    targets: list[list[int]],
    max_gap: int,
    chunk_size: int,
    lookahead: int = 128,
) -> list[int]:
    rng = np.random.Generator(ENGINES[engine](initializer))
    if choice_triplet(rng) != targets[0]:
        raise ValueError("initializer no longer reproduces the restart prelude")
    chunk_size = max(lookahead + 1, chunk_size)
    stride = chunk_size - lookahead
    raw = rng.integers(0, 1 << 32, size=chunk_size, dtype=np.uint32)
    base = 0
    possible: list[int] = []
    wanted = np.asarray(targets[1], dtype=np.int64)
    while base <= max_gap:
        count = min(stride, max_gap - base + 1)
        starts = np.arange(count, dtype=np.int64)
        generated = _choice_windows(raw, starts)
        hits = np.flatnonzero(np.all(generated == wanted, axis=1))
        possible.extend(base + int(hit) for hit in hits)
        if base + count > max_gap:
            break
        raw = np.concatenate(
            (
                raw[stride:],
                rng.integers(0, 1 << 32, size=stride, dtype=np.uint32),
            )
        )
        base += stride

    exact: list[int] = []
    for gap in possible:
        replay = np.random.Generator(ENGINES[engine](initializer))
        if choice_triplet(replay) != targets[0]:
            continue
        matches = True
        for target in targets[1:]:
            _skip_raw32(replay, gap, max(1, chunk_size))
            if choice_triplet(replay) != target:
                matches = False
                break
        if matches:
            exact.append(gap)
    return sorted(set(exact))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--initializer-report", type=Path, action="append", required=True)
    parser.add_argument("--process-start", default="2026-09-26T14:57:51.192497Z")
    parser.add_argument("--prelude", default="654,347,964")
    parser.add_argument("--max-gap", type=int, default=10_000_000)
    parser.add_argument("--chunk-size", type=int, default=1_000_000)
    parser.add_argument(
        "--method",
        choices=("integers-unique", "choice", "float-unique"),
        default="integers-unique",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_modern_uint32_fixed_gap.json"),
    )
    args = parser.parse_args()
    prelude = [int(value) for value in args.prelude.split(",")]
    observed_prelude, segment = load_layout(
        args.discovery.resolve(), args.process_start, prelude
    )
    targets = [observed_prelude] + [
        [int(value) for value in record["seeds"]] for record in segment
    ]
    rows = candidate_rows(
        (path.resolve() for path in args.initializer_report), args.method
    )
    tested = 0
    exact = []
    by_engine: dict[str, dict[str, int]] = {}
    for row in rows:
        if args.method == "choice":
            hits = fixed_choice_gap_hits(
                row["engine"],
                row["initializer"],
                targets,
                max(0, args.max_gap),
                max(129, args.chunk_size),
            )
        elif args.method == "float-unique":
            hits = fixed_float_gap_hits(
                row["engine"],
                row["initializer"],
                targets,
                max(0, args.max_gap),
                max(3, args.chunk_size),
            )
        else:
            hits = fixed_gap_hits(
                row["engine"],
                row["initializer"],
                targets,
                max(0, args.max_gap),
                max(3, args.chunk_size),
            )
        tested += max(0, args.max_gap) + 1
        stats = by_engine.setdefault(row["engine"], {"initializers": 0, "gaps_tested": 0})
        stats["initializers"] += 1
        stats["gaps_tested"] += max(0, args.max_gap) + 1
        for gap in hits:
            exact.append({**row, "fixed_hidden_bounded_draws": gap})
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "modern-numpy-uint32-persistent-fixed-hidden-gap",
        "split": {
            "restart_prelude": 1,
            "post_restart_discovery_tasks": len(segment),
            "holdout_opened": False,
        },
        "search": {
            "candidate_initializations": len(rows),
            "maximum_fixed_hidden_bounded_draws": max(0, args.max_gap),
            "method": args.method,
            "candidates_tested": tested,
            "discovery_exact_candidates": len(exact),
            "families": by_engine,
            "exact_candidates": exact,
        },
        "interpretation": (
            "A miss rejects only a constant count of hidden calls in the selected "
            "generation API between every triplet for the uint32 initializers "
            "surviving the prelude."
        ),
        "safety": {
            "discovery_only": True,
            "holdout_opened": False,
            "network_requests": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "candidate_initializations": len(rows),
                "candidates_tested": tested,
                "discovery_exact_candidates": len(exact),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
