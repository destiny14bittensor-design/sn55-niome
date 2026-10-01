#!/usr/bin/env python3
"""Search Substrate collective-flip public randomness seed hypotheses.

The chain stores an 81-parent-hash ring used by
``RandomnessCollectiveFlip::random(subject)``.  Because the pallet algorithm is
public, the exact output can be reconstructed from the public block hashes
without issuing thousands of historical state queries.
"""

from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from tools.preseed_generator_lab import atomic_json, load_tasks
    from tools.preseed_hypothesis_search import (
        Ranking,
        _block_scalar,
        _digest_triplet,
        _numpy_triplet,
        _python_triplet,
        collect_hashes,
        evaluate_predictions,
        required_heights,
        utc_now,
    )
except ModuleNotFoundError:
    from preseed_generator_lab import atomic_json, load_tasks
    from preseed_hypothesis_search import (
        Ranking,
        _block_scalar,
        _digest_triplet,
        _numpy_triplet,
        _python_triplet,
        collect_hashes,
        evaluate_predictions,
        required_heights,
        utc_now,
    )


def _compact_u32(value: int) -> bytes:
    if value < 0:
        raise ValueError("SCALE compact value cannot be negative")
    if value < 1 << 6:
        return bytes([value << 2])
    if value < 1 << 14:
        return ((value << 2) | 1).to_bytes(2, "little")
    if value < 1 << 30:
        return ((value << 2) | 2).to_bytes(4, "little")
    width = max(4, (value.bit_length() + 7) // 8)
    return bytes([((width - 4) << 2) | 3]) + value.to_bytes(width, "little")


def _triplet_mix(values: Sequence[bytes]) -> bytes:
    if not values:
        return bytes(32)
    current = list(values)
    # The pallet uses exactly 81 == 3**4 inputs.  Keep the generic truncation
    # behavior of safe_mix for tests and defensive reuse.
    power = 1
    while power * 3 <= len(current):
        power *= 3
    current = current[:power]
    while len(current) > 1:
        mixed = []
        for index in range(0, len(current), 3):
            left, middle, right = current[index : index + 3]
            mixed.append(
                bytes(
                    (a & b) | (b & c) | (a & c)
                    for a, b, c in zip(left, middle, right)
                )
            )
        current = mixed
    return current[0]


def _created_block(record: Mapping[str, Any]) -> int:
    number = (((record.get("block_context") or {}).get("created") or {}).get("number"))
    if not isinstance(number, int):
        raise ValueError(f"missing created block for {record.get('task_id')}")
    return number


def collective_flip_output(
    block_number: int, subject: bytes, hashes: Mapping[str, str]
) -> bytes | None:
    # After on_initialize(N), ring slot (N-1)%81 contains hash(N-1).
    # Iteration begins at that slot, then wraps over hash(N-81)..hash(N-2).
    heights = [block_number - 1, *range(block_number - 81, block_number - 1)]
    values = [hashes.get(str(height)) for height in heights]
    if any(value is None for value in values):
        return None
    encoded_subject = _compact_u32(len(subject)) + subject
    leaves = [
        hashlib.blake2b(
            bytes([index])
            + encoded_subject
            + bytes.fromhex((block_hash or "").removeprefix("0x")),
            digest_size=32,
        ).digest()
        for index, block_hash in enumerate(values)
    ]
    return _triplet_mix(leaves)


def _raw_chunk_triplet(value: bytes, width: int, endian: str) -> list[int]:
    result: list[int] = []
    offset = 0
    while len(result) < 3:
        if offset + width > len(value):
            value = hashlib.blake2b(value, digest_size=32).digest()
            offset = 0
        candidate = 100 + int.from_bytes(value[offset : offset + width], endian) % 900
        offset += width
        if candidate not in result:
            result.append(candidate)
    return result


def run_search(
    discovery: Sequence[Mapping[str, Any]],
    holdout: Sequence[Mapping[str, Any]],
    cache: Mapping[str, Any],
    *,
    offset_start: int,
    offset_stop: int,
    top_limit: int,
) -> dict[str, Any]:
    ranking = Ranking(limit=top_limit)
    holdout_results: list[dict[str, Any]] = []
    records = {str(record["task_id"]): record for record in [*discovery, *holdout]}
    hashes = cache.get("hashes") or {}

    def subject(record: Mapping[str, Any], name: str) -> bytes:
        if name == "empty":
            return b""
        if name in {"random", "seed", "niome"}:
            return name.encode()
        return str(record.get(name) or "").encode()

    subject_names = (
        "empty",
        "random",
        "seed",
        "niome",
        "task_id",
        "created_at",
        "contract_material",
    )

    @lru_cache(maxsize=None)
    def output(task_id: str, offset: int, subject_name: str) -> bytes | None:
        record = records[task_id]
        return collective_flip_output(
            _created_block(record) + offset,
            subject(record, subject_name),
            hashes,
        )

    def add(
        model_id: str,
        family: str,
        predict: Callable[[Mapping[str, Any]], Sequence[int] | None],
    ) -> None:
        result = evaluate_predictions(model_id, family, discovery, predict)
        ranking.add(result)
        if result.exact_ordered == len(discovery):
            holdout_results.append(
                evaluate_predictions(model_id, family, holdout, predict).as_dict()
            )

    scalar_variants = []
    for source in ("raw", "hash"):
        algorithms = ("sha256", "sha512", "blake2b") if source == "hash" else ("sha256",)
        for algorithm in algorithms:
            for endian in ("big", "little"):
                for width in ("full", "4", "8", "16"):
                    scalar_variants.append((source, algorithm, endian, width))
    for algorithm in ("sha256", "sha512", "blake2b"):
        for endian in ("big", "little"):
            scalar_variants.append(("official-counter", algorithm, endian, "full"))

    for offset in range(offset_start, offset_stop - 1):
        for subject_name in subject_names:
            for source, algorithm, endian, width in scalar_variants:
                model_id = f"collective-consecutive:o{offset}:{subject_name}:{source}:{algorithm}:{endian}:{width}"

                def predict(record: Mapping[str, Any], o=offset, sn=subject_name, s=source, a=algorithm, e=endian, w=width):
                    values = [output(str(record["task_id"]), o + index, sn) for index in range(3)]
                    if any(value is None for value in values):
                        return None
                    result: list[int] = []
                    for value in values:
                        result.append(
                            _block_scalar(
                                "0x" + (value or b"").hex(),
                                source=s,
                                algorithm=a,
                                endian=e,
                                width=w,
                                exclude=result,
                            )
                        )
                    return result

                add(model_id, "collective-consecutive", predict)

    extractions = (
        "counter-big",
        "counter-little",
        "chunks-4-big",
        "chunks-4-little",
        "chunks-8-big",
        "chunks-8-little",
        "chunks-16-big",
        "chunks-16-little",
    )
    for offset in range(offset_start, offset_stop + 1):
        for subject_name in subject_names:
            for width in (4, 8, 16):
                for endian in ("big", "little"):
                    add(
                        f"collective-raw:o{offset}:{subject_name}:w{width}:{endian}",
                        "collective-raw-chunks",
                        lambda record, o=offset, sn=subject_name, w=width, e=endian: (
                            None
                            if (value := output(str(record["task_id"]), o, sn)) is None
                            else _raw_chunk_triplet(value, w, e)
                        ),
                    )
            for algorithm in ("sha256", "sha512", "blake2b"):
                for extraction in extractions:
                    add(
                        f"collective-digest:o{offset}:{subject_name}:{algorithm}:{extraction}",
                        "collective-digest",
                        lambda record, o=offset, sn=subject_name, a=algorithm, x=extraction: (
                            None
                            if (value := output(str(record["task_id"]), o, sn)) is None
                            else _digest_triplet(value, a, x)
                        ),
                    )
            for seed_name, seed_fn in (
                ("raw", lambda value: value),
                ("hex", lambda value: value.hex()),
                ("int-big", lambda value: int.from_bytes(value, "big")),
                ("int-little", lambda value: int.from_bytes(value, "little")),
                ("first32-big", lambda value: int.from_bytes(value[:4], "big")),
                ("last32-big", lambda value: int.from_bytes(value[-4:], "big")),
            ):
                for method in ("sample", "randint-unique"):
                    add(
                        f"collective-python:o{offset}:{subject_name}:{seed_name}:{method}",
                        "collective-python",
                        lambda record, o=offset, sn=subject_name, sf=seed_fn, m=method: (
                            None
                            if (value := output(str(record["task_id"]), o, sn)) is None
                            else _python_triplet(sf(value), m)
                        ),
                    )
            for integer_name, integer_fn in (
                ("int-big", lambda value: int.from_bytes(value, "big")),
                ("int-little", lambda value: int.from_bytes(value, "little")),
                ("first32-big", lambda value: int.from_bytes(value[:4], "big")),
                ("last32-big", lambda value: int.from_bytes(value[-4:], "big")),
            ):
                for generator in ("random-state", "default-rng"):
                    for method in ("choice", "integers-unique"):
                        add(
                            f"collective-numpy:o{offset}:{subject_name}:{integer_name}:{generator}:{method}",
                            "collective-numpy",
                            lambda record, o=offset, sn=subject_name, sf=integer_fn, g=generator, m=method: (
                                None
                                if (value := output(str(record["task_id"]), o, sn)) is None
                                else _numpy_triplet(sf(value), g, m)
                            ),
                        )

    return {
        "version": 1,
        "generated_at": utc_now(),
        "mode": "offline-public-collective-flip-discovery-search",
        "safety": {
            "public_chain_only": True,
            "credentials_accepted": False,
            "private_endpoints": False,
            "submission_writes": False,
        },
        "split": {
            "discovery_records": len(discovery),
            "holdout_records_reserved": len(holdout),
            "holdout_gate": "closed" if not ranking.discovery_exact else "opened-after-20-of-20",
        },
        "search": {
            "offset_start": offset_start,
            "offset_stop": offset_stop,
            "candidates_tested": ranking.tested,
            "discovery_exact_candidates": len(ranking.discovery_exact),
            "families": {
                key: {**value, "best_rank": list(value["best_rank"])}
                for key, value in sorted(ranking.by_family.items())
            },
            "top_candidates": [row.as_dict() for row in ranking.top()],
            "discovery_exact": [row.as_dict() for row in ranking.discovery_exact],
            "holdout": holdout_results,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--holdout", type=Path, required=True)
    parser.add_argument("--hash-cache", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--network", default="finney")
    parser.add_argument("--offset-start", type=int, default=0)
    parser.add_argument("--offset-stop", type=int, default=719)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--top-limit", type=int, default=50)
    parser.add_argument("--no-collect", action="store_true")
    args = parser.parse_args()
    if args.offset_stop <= args.offset_start:
        raise SystemExit("invalid offset range")

    discovery = load_tasks(args.discovery.resolve())
    holdout = load_tasks(args.holdout.resolve())
    if len(discovery) != 20 or len(holdout) < 5:
        raise SystemExit("requires the fixed 20 discovery and at least 5 holdout tasks")
    cache: dict[str, Any] = {"version": 1, "network": args.network, "hashes": {}}
    if args.hash_cache.exists():
        cache = json.loads(args.hash_cache.read_text(encoding="utf-8"))
    heights = required_heights(
        [*discovery, *holdout], args.offset_start - 81, args.offset_stop + 2
    )
    if not args.no_collect:
        cache = collect_hashes(
            cache,
            heights,
            network=args.network,
            batch_size=args.batch_size,
            on_progress=lambda value: atomic_json(args.hash_cache.resolve(), value),
        )
        atomic_json(args.hash_cache.resolve(), cache)
    missing = [height for height in heights if str(height) not in (cache.get("hashes") or {})]
    if missing:
        raise SystemExit(f"hash cache is missing {len(missing)} required public blocks")
    report = run_search(
        discovery,
        holdout,
        cache,
        offset_start=args.offset_start,
        offset_stop=args.offset_stop,
        top_limit=args.top_limit,
    )
    report["chain_cache"] = {
        "network": args.network,
        "required_blocks": len(heights),
        "cached_blocks": len(cache.get("hashes") or {}),
        "minimum_block": min(heights),
        "maximum_block": max(heights),
    }
    atomic_json(args.report.resolve(), report)
    print(
        json.dumps(
            {
                "event": "preseed_collective_flip_search_complete",
                "candidates_tested": report["search"]["candidates_tested"],
                "discovery_exact_candidates": report["search"]["discovery_exact_candidates"],
                "best": (report["search"]["top_candidates"] or [None])[0],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
