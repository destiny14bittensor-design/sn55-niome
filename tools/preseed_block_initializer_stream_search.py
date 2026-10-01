#!/usr/bin/env python3
"""Search public block material as a process-persistent PRNG initializer.

The per-round block/digest searches do not cover a distinct and plausible
deployment pattern: initialize a long-lived RNG once from a public block near
process start, then keep drawing from it.  This tool tests every block already
present in the public cache against the observed restart prelude and all fixed
post-restart Discovery labels.  NumPy variants are tested both as a seed-only
stream and with the public ``shuffle(256) -> seed triplet`` call order.

The first public triplet is a very strong prefilter, so only its rare matches
are replayed over the complete Discovery suffix.  Holdout data is never read.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import random
from typing import Any, Iterable

import numpy as np

try:
    from tools.preseed_generator_lab import atomic_json
    from tools.preseed_numpy_prelude_search import (
        DEFAULT_PROCESS_START,
        load_layout,
    )
    from tools.preseed_numpy_interleaved_search import shuffle_sizes_for_segment
except ModuleNotFoundError:
    from preseed_generator_lab import atomic_json
    from preseed_numpy_prelude_search import DEFAULT_PROCESS_START, load_layout
    from preseed_numpy_interleaved_search import shuffle_sizes_for_segment


LOW = 100
HIGH = 999
_PRELUDE: list[int] = []
_TARGETS: list[list[int]] = []
_SIZES: list[int] = []


def unique_draw(draw: Any) -> list[int]:
    values: list[int] = []
    while len(values) < 3:
        value = int(draw())
        if value not in values:
            values.append(value)
    return values


def python_triplet(rng: random.Random, method: str) -> list[int]:
    if method == "sample":
        return rng.sample(range(LOW, HIGH + 1), 3)
    if method == "randint-unique":
        return unique_draw(lambda: rng.randint(LOW, HIGH))
    if method == "float-unique":
        return unique_draw(lambda: LOW + int(rng.random() * (HIGH - LOW + 1)))
    raise ValueError(method)


def numpy_triplet(rng: Any, engine: str, method: str) -> list[int]:
    if method == "choice":
        return [
            int(value)
            for value in rng.choice(np.arange(LOW, HIGH + 1), 3, replace=False)
        ]
    if method == "integers-unique":
        draw = (
            (lambda: rng.randint(LOW, HIGH + 1))
            if engine == "random-state"
            else (lambda: rng.integers(LOW, HIGH + 1))
        )
        return unique_draw(draw)
    if method == "float-unique":
        return unique_draw(lambda: LOW + int(rng.random() * (HIGH - LOW + 1)))
    raise ValueError(method)


def public_initializers(height: int, block_hash: str) -> list[tuple[str, Any]]:
    """Return deduplicated common initializers derived from one public block."""
    text = str(block_hash)
    raw = bytes.fromhex(text.removeprefix("0x"))
    if len(raw) != 32:
        raise ValueError("block hash must contain exactly 32 bytes")
    candidates: list[tuple[str, Any]] = [
        ("raw-bytes", raw),
        ("reversed-raw-bytes", raw[::-1]),
        ("hex", text.removeprefix("0x")),
        ("0xhex", text),
        ("height-int", int(height)),
        ("height-decimal", str(int(height))),
        ("raw-int-big", int.from_bytes(raw, "big")),
        ("raw-int-little", int.from_bytes(raw, "little")),
    ]
    for location, part in (
        ("first4", raw[:4]),
        ("last4", raw[-4:]),
        ("first8", raw[:8]),
        ("last8", raw[-8:]),
    ):
        for endian in ("big", "little"):
            candidates.append(
                (f"raw-{location}-{endian}", int.from_bytes(part, endian))
            )
    for algorithm in ("sha256", "sha512", "blake2b", "blake2s", "sha3_256"):
        digest = hashlib.new(algorithm, raw).digest()
        for location, part in (
            ("full", digest),
            ("first4", digest[:4]),
            ("last4", digest[-4:]),
        ):
            for endian in ("big", "little"):
                candidates.append(
                    (
                        f"{algorithm}-{location}-{endian}",
                        int.from_bytes(part, endian),
                    )
                )
    # Keep the first descriptive route to a value.  Equivalent initializers do
    # not constitute independent candidates and should not inflate totals.
    output: list[tuple[str, Any]] = []
    seen: set[tuple[type, Any]] = set()
    for label, value in candidates:
        key = (type(value), value)
        if key in seen:
            continue
        seen.add(key)
        output.append((label, value))
    return output


def prefix_match(actual: Iterable[list[int]], expected: list[list[int]]) -> int:
    matched = 0
    for left, right in zip(actual, expected):
        if list(left) != list(right):
            break
        matched += 1
    return matched


def search_block_candidates(
    height: int,
    block_hash: str,
    prelude: list[int],
    targets: list[list[int]],
    sizes: list[int],
) -> dict[str, Any]:
    tested = 0
    first_hits: list[dict[str, Any]] = []
    exact: list[dict[str, Any]] = []
    best = 0
    for initializer_label, initializer in public_initializers(height, block_hash):
        # Python accepts integers, strings, and bytes directly.  This includes
        # CPython's version-2 string/bytes SHA-512 seeding path.
        for method in ("sample", "randint-unique", "float-unique"):
            tested += 1
            rng = random.Random(initializer)
            if python_triplet(rng, method) != prelude:
                continue
            generated = [python_triplet(rng, method) for _ in targets]
            matched = 1 + prefix_match(generated, targets)
            row = {
                "block_height": int(height),
                "initializer": initializer_label,
                "engine": "python-random",
                "method": method,
                "layout": "persistent-seed-only",
                "matched_triplets_including_prelude": matched,
            }
            first_hits.append(row)
            best = max(best, matched)
            if matched == len(targets) + 1:
                exact.append(row)

        if not isinstance(initializer, int) or initializer < 0:
            continue
        for engine in ("random-state", "default-rng"):
            for method in ("choice", "integers-unique", "float-unique"):
                for layout in ("persistent-seed-only", "prelude-shuffle-seed"):
                    tested += 1
                    rng: Any = (
                        np.random.RandomState(initializer & 0xFFFFFFFF)
                        if engine == "random-state"
                        else np.random.default_rng(initializer)
                    )
                    if numpy_triplet(rng, engine, method) != prelude:
                        continue
                    generated: list[list[int]] = []
                    for size in sizes:
                        if layout == "prelude-shuffle-seed":
                            values = np.arange(int(size), dtype=np.int64)
                            rng.shuffle(values)
                        generated.append(numpy_triplet(rng, engine, method))
                    matched = 1 + prefix_match(generated, targets)
                    row = {
                        "block_height": int(height),
                        "initializer": initializer_label,
                        "engine": engine,
                        "method": method,
                        "layout": layout,
                        "matched_triplets_including_prelude": matched,
                    }
                    first_hits.append(row)
                    best = max(best, matched)
                    if matched == len(targets) + 1:
                        exact.append(row)
    return {
        "tested": tested,
        "first_hits": first_hits,
        "exact": exact,
        "best": best,
    }


def _init_worker(prelude: list[int], targets: list[list[int]], sizes: list[int]) -> None:
    global _PRELUDE, _TARGETS, _SIZES
    _PRELUDE = prelude
    _TARGETS = targets
    _SIZES = sizes


def _search_job(job: list[tuple[int, str]]) -> dict[str, Any]:
    tested = best = 0
    first_hits: list[dict[str, Any]] = []
    exact: list[dict[str, Any]] = []
    for height, block_hash in job:
        result = search_block_candidates(
            height, block_hash, _PRELUDE, _TARGETS, _SIZES
        )
        tested += int(result["tested"])
        best = max(best, int(result["best"]))
        first_hits.extend(result["first_hits"])
        exact.extend(result["exact"])
    return {"tested": tested, "first_hits": first_hits, "exact": exact, "best": best}


def chunks(values: list[tuple[int, str]], size: int) -> list[list[tuple[int, str]]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--hash-cache", type=Path, required=True)
    parser.add_argument("--shuffle-constraints", type=Path)
    parser.add_argument("--process-start", default=DEFAULT_PROCESS_START)
    parser.add_argument("--prelude", default="654,347,964")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--chunk-size", type=int, default=64)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "artifacts/research/preseed_block_initializer_stream_search.json"
        ),
    )
    args = parser.parse_args()

    observed_prelude = [int(value) for value in args.prelude.split(",")]
    if len(observed_prelude) != 3:
        raise ValueError("--prelude requires three comma-separated integers")
    prelude, segment = load_layout(
        args.discovery.resolve(), args.process_start, observed_prelude
    )
    targets = [[int(value) for value in row["seeds"]] for row in segment]
    sizes = shuffle_sizes_for_segment(
        segment,
        args.shuffle_constraints.resolve() if args.shuffle_constraints else None,
    )
    cache = json.loads(args.hash_cache.read_text(encoding="utf-8"))
    blocks = sorted(
        (int(height), str(value))
        for height, value in (cache.get("hashes") or {}).items()
    )
    if not blocks:
        raise ValueError("public block hash cache is empty")
    jobs = chunks(blocks, max(1, int(args.chunk_size)))
    workers = max(1, int(args.workers))
    if workers == 1:
        _init_worker(prelude, targets, sizes)
        results = [_search_job(job) for job in jobs]
    else:
        with mp.Pool(
            workers,
            initializer=_init_worker,
            initargs=(prelude, targets, sizes),
        ) as pool:
            results = pool.map(_search_job, jobs)

    tested = sum(int(row["tested"]) for row in results)
    first_hits = [hit for row in results for hit in row["first_hits"]]
    exact = [hit for row in results for hit in row["exact"]]
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "public-block-process-persistent-prng-initializer-search",
        "split": {
            "fixed_discovery_total": 20,
            "prelude_tasks": 1,
            "post_restart_discovery_tasks": len(targets),
            "holdout_opened": False,
        },
        "search": {
            "blocks_tested": len(blocks),
            "minimum_block": blocks[0][0],
            "maximum_block": blocks[-1][0],
            "candidates_tested": tested,
            "prelude_hits": first_hits,
            "best_prefix_triplets_including_prelude": max(
                (int(row["best"]) for row in results), default=0
            ),
            "discovery_exact_candidates": len(exact),
            "exact_candidates": exact,
            "families": {
                "python-random-persistent": {},
                "numpy-randomstate-persistent": {},
                "numpy-generator-persistent": {},
            },
        },
        "interpretation": (
            "No exact hit excludes the tested cached public blocks and common "
            "initializer derivations; it does not exclude OS entropy or secret material."
        ),
        "safety": {
            "public_offline_inputs_only": True,
            "holdout_opened": False,
            "network_reads": False,
            "submission_writes": False,
            "prng_state_persisted": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "blocks_tested": len(blocks),
                "candidates_tested": tested,
                "prelude_hits": len(first_hits),
                "best_prefix_triplets_including_prelude": report["search"][
                    "best_prefix_triplets_including_prelude"
                ],
                "discovery_exact_candidates": len(exact),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
