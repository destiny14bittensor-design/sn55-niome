#!/usr/bin/env python3
"""Search NumPy SeedSequence/BitGenerator constructions on fixed Discovery.

Application code commonly passes UUID or digest words to ``SeedSequence``
rather than reducing them to one Python integer.  This search covers that
missing family across NumPy's public BitGenerators and two plausible distinct
triplet APIs.  It reads only the fixed 20 Discovery labels and an existing
public block-hash cache; Holdout is never opened.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import heapq
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import UUID

import numpy as np

try:
    from tools.preseed_generator_lab import atomic_json, load_tasks
except ModuleNotFoundError:
    from preseed_generator_lab import atomic_json, load_tasks


LOW = 100
HIGH = 999


def unique_integers(rng: np.random.Generator) -> list[int]:
    result: list[int] = []
    while len(result) < 3:
        value = int(rng.integers(LOW, HIGH + 1))
        if value not in result:
            result.append(value)
    return result


def triplet(entropy: Sequence[int], engine: str, method: str) -> list[int]:
    seed_sequence = np.random.SeedSequence([int(value) for value in entropy])
    bit_generators = {
        "pcg64": np.random.PCG64,
        "pcg64dxsm": np.random.PCG64DXSM,
        "mt19937": np.random.MT19937,
        "philox": np.random.Philox,
        "sfc64": np.random.SFC64,
    }
    rng = np.random.Generator(bit_generators[engine](seed_sequence))
    if method == "choice":
        return [
            int(value)
            for value in rng.choice(np.arange(LOW, HIGH + 1), 3, replace=False)
        ]
    if method == "integers-unique":
        return unique_integers(rng)
    raise ValueError(method)


def words(raw: bytes, endian: str) -> list[int]:
    if len(raw) % 4:
        raise ValueError("word material must be a multiple of four bytes")
    return [
        int.from_bytes(raw[index : index + 4], endian)
        for index in range(0, len(raw), 4)
    ]


def block_number(record: Mapping[str, Any]) -> int:
    return int(((record.get("block_context") or {}).get("created") or {})["number"])


def hash_bytes(cache: Mapping[str, Any], height: int) -> bytes | None:
    value = (cache.get("hashes") or {}).get(str(height))
    if not value:
        return None
    raw = bytes.fromhex(str(value).removeprefix("0x"))
    return raw if len(raw) == 32 else None


def uuid_words(record: Mapping[str, Any], endian: str) -> list[int]:
    return words(UUID(str(record["task_id"])).bytes, endian)


def evaluate(
    records: Sequence[Mapping[str, Any]],
    predict: Callable[[Mapping[str, Any]], list[int] | None],
) -> tuple[int, int, int, int]:
    ordered = unordered = positions = any_position = 0
    for record in records:
        predicted = predict(record)
        if predicted is None:
            return (-1, -1, -1, -1)
        expected = [int(value) for value in record["seeds"]]
        ordered += predicted == expected
        unordered += sorted(predicted) == sorted(expected)
        matches = sum(a == b for a, b in zip(predicted, expected))
        positions += matches
        any_position += matches > 0
    return ordered, unordered, positions, any_position


def run_search(
    records: Sequence[Mapping[str, Any]],
    cache: Mapping[str, Any],
    offset_start: int,
    offset_stop: int,
    top_limit: int = 30,
) -> dict[str, Any]:
    engines = ("pcg64", "pcg64dxsm", "mt19937", "philox", "sfc64")
    methods = ("choice", "integers-unique")
    heap: list[tuple[tuple[int, int, int, int], int, dict[str, Any]]] = []
    serial = tested = 0
    exact: list[dict[str, Any]] = []
    families: dict[str, dict[str, Any]] = {}

    def add(model_id: str, family: str, predict: Callable[[Mapping[str, Any]], list[int] | None]) -> None:
        nonlocal serial, tested
        rank = evaluate(records, predict)
        if rank[0] < 0:
            return
        tested += 1
        serial += 1
        row = {
            "model_id": model_id,
            "family": family,
            "exact_ordered": rank[0],
            "exact_unordered": rank[1],
            "position_matches": rank[2],
            "task_any_position": rank[3],
            "records": len(records),
        }
        stats = families.setdefault(
            family, {"tested": 0, "best_rank": [0, 0, 0, 0], "best_model_id": None}
        )
        stats["tested"] += 1
        if list(rank) > stats["best_rank"]:
            stats["best_rank"] = list(rank)
            stats["best_model_id"] = model_id
        if rank[0] == len(records):
            exact.append(row)
        item = (rank, serial, row)
        if len(heap) < top_limit:
            heapq.heappush(heap, item)
        elif item[:2] > heap[0][:2]:
            heapq.heapreplace(heap, item)

    # UUID-only array entropy (offset-independent).
    for endian in ("big", "little"):
        for engine in engines:
            for method in methods:
                add(
                    f"seedsequence:uuid-words-{endian}:{engine}:{method}",
                    "seedsequence-uuid",
                    lambda record, e=endian, g=engine, m=method: triplet(
                        uuid_words(record, e), g, m
                    ),
                )

    for offset in range(offset_start, offset_stop + 1):
        for endian in ("big", "little"):
            for layout in (
                "hash",
                "hash-uuid",
                "uuid-hash",
                "height-hash",
                "height-uuid",
            ):
                for engine in engines:
                    for method in methods:
                        model_id = f"seedsequence:o{offset}:{layout}:{endian}:{engine}:{method}"

                        def predict(record: Mapping[str, Any], o=offset, lay=layout, e=endian, g=engine, m=method):
                            height = block_number(record) + o
                            raw = hash_bytes(cache, height)
                            if raw is None:
                                return None
                            block = words(raw, e)
                            uuid = uuid_words(record, e)
                            if lay == "hash":
                                entropy = block
                            elif lay == "hash-uuid":
                                entropy = block + uuid
                            elif lay == "uuid-hash":
                                entropy = uuid + block
                            elif lay == "height-hash":
                                entropy = [height & 0xFFFFFFFF, height >> 32] + block
                            else:
                                entropy = [height & 0xFFFFFFFF, height >> 32] + uuid
                            return triplet(entropy, g, m)

                        add(model_id, "seedsequence-block-material", predict)

    top = [item[2] for item in sorted(heap, reverse=True)]
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "numpy-seedsequence-bitgenerator-search",
        "split": {
            "discovery_records": len(records),
            "holdout_opened": False,
        },
        "search": {
            "offset_start": offset_start,
            "offset_stop": offset_stop,
            "candidates_tested": tested,
            "discovery_exact_candidates": len(exact),
            "families": families,
            "top_candidates": top,
            "discovery_exact": exact,
        },
        "safety": {
            "public_offline_inputs_only": True,
            "holdout_opened": False,
            "submission_writes": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--hash-cache", type=Path, required=True)
    parser.add_argument("--offset-start", type=int, default=0)
    parser.add_argument("--offset-stop", type=int, default=719)
    parser.add_argument("--top-limit", type=int, default=30)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_seedsequence_search.json"),
    )
    args = parser.parse_args()
    records = load_tasks(args.discovery.resolve())
    if len(records) != 20:
        raise SystemExit("requires exactly 20 fixed Discovery records")
    cache = json.loads(args.hash_cache.read_text(encoding="utf-8"))
    report = run_search(
        records,
        cache,
        args.offset_start,
        args.offset_stop,
        args.top_limit,
    )
    atomic_json(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "candidates_tested": report["search"]["candidates_tested"],
                "discovery_exact_candidates": report["search"][
                    "discovery_exact_candidates"
                ],
                "best": (report["search"]["top_candidates"] or [None])[0],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
