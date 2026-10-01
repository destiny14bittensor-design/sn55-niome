#!/usr/bin/env python3
"""Search hidden raw-word consumption after exact uint32 prelude hits."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import random
from typing import Any

import numpy as np

try:
    from numba import njit
except ModuleNotFoundError:  # Unit tests can exercise the same reference loops.
    def njit(function):  # type: ignore[misc]
        return function

try:
    from tools.preseed_generator_lab import atomic_json
    from tools.preseed_numpy_interleaved_search import _seed_triplet
except ModuleNotFoundError:
    from preseed_generator_lab import atomic_json
    from preseed_numpy_interleaved_search import _seed_triplet


PRELUDE = [654, 347, 964]
FIRST_DISCOVERY = [491, 210, 379]


@njit
def scan_python_gaps(
    raw: np.ndarray, maximum_gap: int, target0: int = 491,
    target1: int = 210, target2: int = 379
) -> list[int]:
    hits: list[int] = []
    for gap in range(maximum_gap + 1):
        cursor = gap
        values = [-1, -1, -1]
        count = 0
        while count < 3 and cursor < raw.size:
            value = int(raw[cursor] >> np.uint32(22))
            cursor += 1
            if value >= 900:
                continue
            value += 100
            duplicate = False
            for index in range(count):
                duplicate = duplicate or values[index] == value
            if not duplicate:
                values[count] = value
                count += 1
        if count == 3 and values[0] == target0 and values[1] == target1 and values[2] == target2:
            hits.append(gap)
    return hits


@njit
def scan_python_float_gaps(
    raw: np.ndarray, maximum_gap: int, target0: int = 491,
    target1: int = 210, target2: int = 379
) -> list[int]:
    hits: list[int] = []
    targets = (target0, target1, target2)
    for gap in range(maximum_gap + 1):
        matched = True
        for index in range(3):
            cursor = gap + index * 2
            if cursor + 1 >= raw.size:
                matched = False
                break
            high = int(raw[cursor] >> np.uint32(5))
            low = int(raw[cursor + 1] >> np.uint32(6))
            value = (high * 67108864.0 + low) / 9007199254740992.0
            if 100 + int(value * 900.0) != targets[index]:
                matched = False
                break
        if matched:
            hits.append(gap)
    return hits


@njit
def scan_numpy_shuffle_prefix_gaps(
    raw: np.ndarray, allowed_keys: np.ndarray, maximum_gap: int
) -> list[int]:
    hits: list[int] = []
    maxima = (255, 254, 253, 252)
    for gap in range(maximum_gap + 1):
        cursor = gap
        key = np.uint64(0)
        complete = True
        for offset in range(4):
            maximum = maxima[offset]
            mask = 255
            accepted = -1
            while cursor < raw.size:
                value = int(raw[cursor] & np.uint32(mask))
                cursor += 1
                if value <= maximum:
                    accepted = value
                    break
            if accepted < 0:
                complete = False
                break
            key |= np.uint64(accepted) << np.uint64(offset * 8)
        if complete:
            position = np.searchsorted(allowed_keys, key)
            if position < allowed_keys.size and allowed_keys[position] == key:
                hits.append(gap)
    return hits


def raw_randomstate_after_prelude(seed: int, count: int) -> np.ndarray:
    rng = np.random.RandomState(int(seed) & 0xFFFFFFFF)
    if _seed_triplet(rng, "integers-unique") != PRELUDE:
        raise ValueError(f"NumPy candidate {seed} does not reproduce the prelude")
    return rng.randint(0, 2**32, size=count, dtype=np.uint32)


def raw_python_after_triplet(
    seed: int, count: int, source_triplet: list[int] | tuple[int, int, int] = PRELUDE,
    method: str = "bits",
) -> np.ndarray:
    rng = random.Random(int(seed))
    if method == "float":
        values = [100 + int(rng.random() * 900) for _ in range(3)]
    else:
        values = []
        while len(values) < 3:
            value = rng.randrange(100, 1000)
            if value not in values:
                values.append(value)
    if values != list(source_triplet):
        raise ValueError(
            f"Python candidate {seed} does not reproduce source triplet {source_triplet}"
        )
    return python_mt_raw_words(rng, count)


def python_mt_raw_words(rng: random.Random, count: int) -> np.ndarray:
    """Read Python's current MT state through NumPy without changing ``rng``.

    CPython and legacy ``RandomState`` use the same 624-word MT19937 state
    layout and position counter.  Bridging avoids millions of Python-level
    ``getrandbits`` calls while preserving their exact raw-word sequence.
    """
    internal = rng.getstate()[1]
    bridge = np.random.RandomState()
    bridge.set_state(
        (
            "MT19937",
            np.asarray(internal[:-1], dtype=np.uint32),
            int(internal[-1]),
            0,
            0.0,
        )
    )
    return bridge.randint(0, 2**32, size=count, dtype=np.uint32)


def tuple_keys(path: Path, task_prefix: str = "f05ef562") -> np.ndarray:
    payload = json.loads(path.read_text(encoding="utf-8"))
    row = next(
        row for row in payload.get("rounds") or []
        if str(row.get("task_id") or "").startswith(task_prefix)
    )
    keys = {
        int(values[0])
        | (int(values[1]) << 8)
        | (int(values[2]) << 16)
        | (int(values[3]) << 24)
        for values in row.get("choice_tuples") or []
        if len(values) >= 4
    }
    return np.asarray(sorted(keys), dtype=np.uint64)


def validate_numpy_gap(seed: int, gap: int) -> bool:
    rng = np.random.RandomState(int(seed) & 0xFFFFFFFF)
    if _seed_triplet(rng, "integers-unique") != PRELUDE:
        return False
    if gap:
        rng.randint(0, 2**32, size=int(gap), dtype=np.uint32)
    values = np.arange(256, dtype=np.int64)
    rng.shuffle(values)
    return _seed_triplet(rng, "integers-unique") == FIRST_DISCOVERY


def parse_candidates(encoded: str) -> list[int]:
    return sorted({int(value) for value in encoded.split(",") if value.strip()})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("numpy", "python"), required=True)
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--tuple-report", type=Path)
    parser.add_argument("--max-gap", type=int, default=10_000_000)
    parser.add_argument("--target", default="491,210,379")
    parser.add_argument("--source-triplet", default="654,347,964")
    parser.add_argument("--python-method", choices=("bits", "float"), default="bits")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    candidates = parse_candidates(args.candidates)
    maximum_gap = max(0, int(args.max_gap))
    target = [int(value) for value in args.target.split(",")]
    if len(target) != 3 or any(value < 100 or value > 999 for value in target):
        raise SystemExit("--target requires three values in 100..999")
    source_triplet = [int(value) for value in args.source_triplet.split(",")]
    if len(source_triplet) != 3 or any(value < 100 or value > 999 for value in source_triplet):
        raise SystemExit("--source-triplet requires three values in 100..999")
    raw_count = maximum_gap + 64
    rows: list[dict[str, Any]] = []
    if args.engine == "numpy":
        if not args.tuple_report:
            raise SystemExit("--tuple-report is required for the NumPy engine")
        allowed = tuple_keys(args.tuple_report.resolve())
        for seed in candidates:
            raw = raw_randomstate_after_prelude(seed, raw_count)
            prefix_hits = [
                int(value)
                for value in scan_numpy_shuffle_prefix_gaps(raw, allowed, maximum_gap)
            ]
            exact = [gap for gap in prefix_hits if validate_numpy_gap(seed, gap)]
            rows.append(
                {
                    "seed_u32": seed,
                    "shuffle_prefix_gap_hits": len(prefix_hits),
                    "exact_first_discovery_gaps": exact,
                }
            )
    else:
        for seed in candidates:
            raw = raw_python_after_triplet(
                seed, raw_count, source_triplet, args.python_method
            )
            scanner = (
                scan_python_float_gaps
                if args.python_method == "float"
                else scan_python_gaps
            )
            exact = [int(value) for value in scanner(raw, maximum_gap, *target)]
            rows.append({"seed_u32": seed, "exact_first_discovery_gaps": exact})
    exact_rows = [
        {"seed_u32": row["seed_u32"], "gap": gap}
        for row in rows
        for gap in row["exact_first_discovery_gaps"]
    ]
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "uint32-prelude-hit-hidden-raw-gap-search",
        "engine": args.engine,
        "target_triplet": target,
        "source_triplet": source_triplet,
        "python_method": args.python_method if args.engine == "python" else None,
        "search": {
            "candidate_initializers": len(candidates),
            "maximum_gap_words": maximum_gap,
            "candidates_tested": len(candidates) * (maximum_gap + 1),
            "families": {
                f"{args.engine}-{args.python_method if args.engine == 'python' else 'bounded'}-hidden-raw-gap": {}
            },
            # A one-transition hit is only a path candidate.  It must survive
            # every remaining fixed Discovery target before being counted as
            # a generator exact candidate by the campaign orchestrator.
            "transition_exact_candidates": len(exact_rows),
            "discovery_exact_candidates": 0,
            "exact_candidates": exact_rows,
            "rows": rows,
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
