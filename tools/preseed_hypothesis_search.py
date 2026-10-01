#!/usr/bin/env python3
"""Exhaustive, public-input-only search over plausible NIOME seed families.

The primary pre-seed laboratory intentionally keeps a small preregistered model
set.  This companion performs broad *discovery* searches without weakening the
holdout boundary: every candidate is ranked on the fixed 20-task discovery set,
and the five holdout labels are read only for candidates that reproduce all 20
discovery triplets exactly.

No wallet, API key, signed URL, score endpoint, or submission route is used.
The only network input is finalized public Finney block hashes.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import heapq
import json
from pathlib import Path
import random
import zlib
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import UUID

import numpy as np

try:
    from tools.preseed_generator_lab import atomic_json, load_tasks
except ModuleNotFoundError:
    from preseed_generator_lab import atomic_json, load_tasks


SEED_LOW = 100
SEED_HIGH = 999
SEED_SPAN = SEED_HIGH - SEED_LOW + 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _raw_hash(value: str) -> bytes:
    raw = bytes.fromhex(str(value).removeprefix("0x"))
    if len(raw) != 32:
        raise ValueError("expected a 32-byte public block hash")
    return raw


def _unique(draw: Callable[[], int]) -> list[int]:
    result: list[int] = []
    attempts = 0
    while len(result) < 3:
        value = int(draw())
        attempts += 1
        if value not in result:
            result.append(value)
        if attempts > 10_000:
            raise RuntimeError("candidate generator did not produce three unique seeds")
    return result


def _python_triplet(seed: Any, method: str) -> list[int]:
    rng = random.Random(seed)
    if method == "sample":
        return rng.sample(range(SEED_LOW, SEED_HIGH + 1), 3)
    if method == "randint-unique":
        return _unique(lambda: rng.randint(SEED_LOW, SEED_HIGH))
    if method == "randrange-unique":
        return _unique(lambda: rng.randrange(SEED_LOW, SEED_HIGH + 1))
    raise ValueError(f"unknown Python draw method: {method}")


def _numpy_triplet(seed: int, generator: str, method: str) -> list[int]:
    # RandomState accepts only uint32; default_rng accepts arbitrary-size ints.
    if generator == "random-state":
        rng: Any = np.random.RandomState(int(seed) & 0xFFFFFFFF)
    elif generator == "default-rng":
        rng = np.random.default_rng(int(seed))
    else:
        raise ValueError(f"unknown NumPy generator: {generator}")
    if method == "choice":
        return [int(value) for value in rng.choice(
            np.arange(SEED_LOW, SEED_HIGH + 1), size=3, replace=False
        )]
    if method == "integers-unique":
        high = SEED_HIGH + 1
        draw = (
            (lambda: rng.randint(SEED_LOW, high))
            if generator == "random-state"
            else (lambda: rng.integers(SEED_LOW, high))
        )
        return _unique(draw)
    raise ValueError(f"unknown NumPy draw method: {method}")


def _digest_triplet(material: bytes, algorithm: str, extraction: str) -> list[int]:
    digest = hashlib.new(algorithm, material).digest()
    result: list[int] = []
    counter = 0
    offset = 0
    while len(result) < 3:
        if extraction.startswith("counter-"):
            endian = extraction.removeprefix("counter-")
            digest = hashlib.new(
                algorithm, material + counter.to_bytes(4, endian)
            ).digest()
            counter += 1
            integer = int.from_bytes(digest, endian)
        else:
            _, width_text, endian = extraction.split("-")
            width = int(width_text)
            if offset + width > len(digest):
                digest = hashlib.new(algorithm, digest).digest()
                offset = 0
            integer = int.from_bytes(digest[offset : offset + width], endian)
            offset += width
        candidate = SEED_LOW + integer % SEED_SPAN
        if candidate not in result:
            result.append(candidate)
    return result


def _block_scalar(
    block_hash: str,
    *,
    source: str,
    algorithm: str,
    endian: str,
    width: str,
    exclude: Sequence[int],
) -> int:
    raw = _raw_hash(block_hash)
    if source == "raw":
        digest = raw
    elif source == "hash":
        digest = hashlib.new(algorithm, raw).digest()
    elif source == "official-counter":
        counter = 0
        while True:
            digest = hashlib.new(algorithm, raw + counter.to_bytes(4, endian)).digest()
            candidate = SEED_LOW + int.from_bytes(digest, endian) % SEED_SPAN
            counter += 1
            if candidate not in exclude:
                return candidate
    else:
        raise ValueError(f"unknown scalar source: {source}")
    selected = digest if width == "full" else digest[: int(width)]
    candidate = SEED_LOW + int.from_bytes(selected, endian) % SEED_SPAN
    if candidate in exclude:
        # This deterministic collision rule mirrors the counter form while
        # keeping the initially selected scalar family unchanged.
        counter = 1
        while candidate in exclude:
            retry = hashlib.new(algorithm, digest + counter.to_bytes(4, endian)).digest()
            candidate = SEED_LOW + int.from_bytes(retry, endian) % SEED_SPAN
            counter += 1
    return candidate


@dataclass(frozen=True)
class CandidateResult:
    model_id: str
    family: str
    exact_ordered: int
    exact_unordered: int
    position_matches: int
    task_any_position: int
    records: int

    @property
    def rank(self) -> tuple[int, int, int, int]:
        return (
            self.exact_ordered,
            self.exact_unordered,
            self.position_matches,
            self.task_any_position,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "family": self.family,
            "records": self.records,
            "exact_ordered": self.exact_ordered,
            "exact_unordered": self.exact_unordered,
            "position_matches": self.position_matches,
            "task_any_position": self.task_any_position,
        }


def evaluate_predictions(
    model_id: str,
    family: str,
    records: Sequence[Mapping[str, Any]],
    predict: Callable[[Mapping[str, Any]], Sequence[int] | None],
) -> CandidateResult:
    exact_ordered = exact_unordered = position_matches = task_any_position = 0
    for record in records:
        expected = [int(value) for value in record.get("seeds") or []]
        predicted_raw = predict(record)
        if predicted_raw is None:
            continue
        predicted = [int(value) for value in predicted_raw]
        exact_ordered += int(predicted == expected)
        exact_unordered += int(sorted(predicted) == sorted(expected))
        matches = sum(left == right for left, right in zip(predicted, expected))
        position_matches += matches
        task_any_position += int(matches > 0)
    return CandidateResult(
        model_id=model_id,
        family=family,
        exact_ordered=exact_ordered,
        exact_unordered=exact_unordered,
        position_matches=position_matches,
        task_any_position=task_any_position,
        records=len(records),
    )


class Ranking:
    def __init__(self, limit: int = 50) -> None:
        self.limit = limit
        self.tested = 0
        self._serial = 0
        self._heap: list[tuple[tuple[int, int, int, int], int, CandidateResult]] = []
        self.discovery_exact: list[CandidateResult] = []
        self.by_family: dict[str, dict[str, Any]] = {}

    def add(self, result: CandidateResult) -> None:
        self.tested += 1
        family = self.by_family.setdefault(
            result.family,
            {"tested": 0, "best_rank": (0, 0, 0, 0), "best_model_id": None},
        )
        family["tested"] += 1
        if result.rank > tuple(family["best_rank"]):
            family["best_rank"] = result.rank
            family["best_model_id"] = result.model_id
        if result.exact_ordered == result.records:
            self.discovery_exact.append(result)
        self._serial += 1
        item = (result.rank, self._serial, result)
        if len(self._heap) < self.limit:
            heapq.heappush(self._heap, item)
        elif item[:2] > self._heap[0][:2]:
            heapq.heapreplace(self._heap, item)

    def top(self) -> list[CandidateResult]:
        return [item[2] for item in sorted(self._heap, reverse=True)]


def _created_block(record: Mapping[str, Any]) -> int:
    material = ((record.get("block_context") or {}).get("created") or {})
    number = material.get("number")
    if not isinstance(number, int):
        raise ValueError(f"missing created block for {record.get('task_id')}")
    return number


def _hash_for(
    cache: Mapping[str, Any], record: Mapping[str, Any], offset: int
) -> str | None:
    return (cache.get("hashes") or {}).get(str(_created_block(record) + offset))


def required_heights(
    records: Sequence[Mapping[str, Any]], offset_start: int, offset_stop: int
) -> list[int]:
    return sorted(
        {
            _created_block(record) + offset
            for record in records
            for offset in range(offset_start, offset_stop + 1)
        }
    )


def collect_hashes(
    cache: dict[str, Any],
    heights: Sequence[int],
    *,
    network: str,
    batch_size: int,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    missing = [height for height in heights if str(height) not in (cache.get("hashes") or {})]
    if not missing:
        return cache
    import bittensor as bt

    subtensor = bt.Subtensor(network=network)
    hashes = dict(cache.get("hashes") or {})
    try:
        for start in range(0, len(missing), max(1, batch_size)):
            batch = missing[start : start + max(1, batch_size)]

            async def fetch() -> list[str | None]:
                # Public nodes enforce a per-connection queue budget.  A small
                # semaphore plus bounded retry gives us high throughput without
                # turning a historical backfill into a burst against the RPC.
                semaphore = asyncio.Semaphore(48)

                async def one(height: int) -> str | None:
                    for attempt in range(7):
                        try:
                            async with semaphore:
                                return await subtensor._client._block_hash(height)
                        except Exception:
                            if attempt == 6:
                                return None
                            await asyncio.sleep(min(0.5, 0.005 * (2**attempt)))
                    return None

                return await asyncio.gather(*(one(height) for height in batch))

            values = subtensor._call(fetch())
            for height, block_hash in zip(batch, values):
                if isinstance(block_hash, str) and block_hash.startswith("0x"):
                    hashes[str(height)] = block_hash
            if on_progress is not None:
                on_progress(
                    {
                        "version": 1,
                        "network": network,
                        "updated_at": utc_now(),
                        "hashes": hashes,
                    }
                )
    finally:
        subtensor.close()
    return {
        "version": 1,
        "network": network,
        "updated_at": utc_now(),
        "hashes": hashes,
    }


def _metadata_materials(record: Mapping[str, Any]) -> Iterable[tuple[str, Any]]:
    task_id = str(record["task_id"])
    uuid = UUID(task_id)
    created = str(record.get("created_at") or "")
    timestamp = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    words32 = [int.from_bytes(uuid.bytes[index : index + 4], "big") for index in range(0, 16, 4)]
    words64 = [int.from_bytes(uuid.bytes[index : index + 8], "big") for index in range(0, 16, 8)]
    yield "uuid-str", task_id
    yield "uuid-hex", uuid.hex
    yield "uuid-bytes", uuid.bytes
    yield "uuid-int-big", uuid.int
    yield "uuid-int-little", int.from_bytes(uuid.bytes, "little")
    yield "uuid-xor32", words32[0] ^ words32[1] ^ words32[2] ^ words32[3]
    yield "uuid-sum32", sum(words32)
    yield "uuid-xor64", words64[0] ^ words64[1]
    yield "uuid-sum64", sum(words64)
    yield "uuid-crc32", zlib.crc32(uuid.bytes)
    yield "created-str", created
    yield "created-sec-int", int(timestamp)
    yield "created-ms-int", int(timestamp * 1_000)
    yield "created-us-int", int(timestamp * 1_000_000)
    yield "created-sec-float", timestamp
    yield "created-block", _created_block(record)
    yield "round-index", (_created_block(record) - 8_843_300) // 720


def _metadata_value(record: Mapping[str, Any], name: str) -> Any:
    return dict(_metadata_materials(record))[name]


def run_search(
    discovery: Sequence[Mapping[str, Any]],
    holdout: Sequence[Mapping[str, Any]],
    cache: Mapping[str, Any],
    *,
    offset_start: int = 0,
    offset_stop: int = 719,
    top_limit: int = 50,
) -> dict[str, Any]:
    ranking = Ranking(limit=top_limit)

    def add(model_id: str, family: str, fn: Callable[[Mapping[str, Any]], Sequence[int] | None]) -> None:
        ranking.add(evaluate_predictions(model_id, family, discovery, fn))

    # 1. Three consecutive public block hashes, one output per block.
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
        for source, algorithm, endian, width in scalar_variants:
            model_id = f"chain-consecutive:o{offset}:{source}:{algorithm}:{endian}:{width}"

            def predict(record: Mapping[str, Any], o=offset, s=source, a=algorithm, e=endian, w=width):
                hashes = [_hash_for(cache, record, o + index) for index in range(3)]
                if any(value is None for value in hashes):
                    return None
                result: list[int] = []
                for value in hashes:
                    result.append(_block_scalar(value or "", source=s, algorithm=a, endian=e, width=w, exclude=result))
                return result

            add(model_id, "chain-consecutive", predict)

    # 2. One block drives all three draws through digest or common PRNG APIs.
    extractions = (
        "counter-big", "counter-little", "chunks-4-big", "chunks-4-little",
        "chunks-8-big", "chunks-8-little", "chunks-16-big", "chunks-16-little",
    )
    for offset in range(offset_start, offset_stop + 1):
        for material_name, material_fn in (
            ("raw", lambda value: _raw_hash(value)),
            ("hex", lambda value: value.removeprefix("0x").encode()),
            ("0xhex", lambda value: value.encode()),
        ):
            for algorithm in ("sha256", "sha512", "blake2b"):
                for extraction in extractions:
                    model_id = f"chain-one-digest:o{offset}:{material_name}:{algorithm}:{extraction}"

                    def predict(record: Mapping[str, Any], o=offset, mf=material_fn, a=algorithm, x=extraction):
                        value = _hash_for(cache, record, o)
                        return None if value is None else _digest_triplet(mf(value), a, x)

                    add(model_id, "chain-one-digest", predict)
        for seed_name, seed_fn in (
            ("hex", lambda value: value.removeprefix("0x")),
            ("0xhex", lambda value: value),
            ("raw", lambda value: _raw_hash(value)),
            ("int-big", lambda value: int.from_bytes(_raw_hash(value), "big")),
            ("int-little", lambda value: int.from_bytes(_raw_hash(value), "little")),
            ("first32-big", lambda value: int.from_bytes(_raw_hash(value)[:4], "big")),
            ("last32-big", lambda value: int.from_bytes(_raw_hash(value)[-4:], "big")),
        ):
            for method in ("sample", "randint-unique"):
                model_id = f"chain-one-python:o{offset}:{seed_name}:{method}"

                def predict(record: Mapping[str, Any], o=offset, sf=seed_fn, m=method):
                    value = _hash_for(cache, record, o)
                    return None if value is None else _python_triplet(sf(value), m)

                add(model_id, "chain-one-python", predict)
        for integer_name, integer_fn in (
            ("int-big", lambda value: int.from_bytes(_raw_hash(value), "big")),
            ("int-little", lambda value: int.from_bytes(_raw_hash(value), "little")),
            ("first32-big", lambda value: int.from_bytes(_raw_hash(value)[:4], "big")),
            ("last32-big", lambda value: int.from_bytes(_raw_hash(value)[-4:], "big")),
        ):
            for generator in ("random-state", "default-rng"):
                for method in ("choice", "integers-unique"):
                    model_id = f"chain-one-numpy:o{offset}:{integer_name}:{generator}:{method}"

                    def predict(record: Mapping[str, Any], o=offset, sf=integer_fn, g=generator, m=method):
                        value = _hash_for(cache, record, o)
                        return None if value is None else _numpy_triplet(sf(value), g, m)

                    add(model_id, "chain-one-numpy", predict)

    # 2b. The public block *number* itself (or a fixed phase offset) drives a
    # common PRNG/digest API.  Hash searches do not cover this family: seeding
    # Random with height 9150470 is unrelated to hashing that block's header.
    number_extractions = (
        "counter-big",
        "counter-little",
        "chunks-8-big",
        "chunks-8-little",
    )
    for offset in range(offset_start, offset_stop + 1):
        for material_name in ("int", "decimal-string"):
            for method in ("sample", "randint-unique"):
                add(
                    f"chain-number-python:o{offset}:{material_name}:{method}",
                    "chain-number-python",
                    lambda record, o=offset, mn=material_name, m=method: _python_triplet(
                        (_created_block(record) + o)
                        if mn == "int"
                        else str(_created_block(record) + o),
                        m,
                    ),
                )
        for generator in ("random-state", "default-rng"):
            for method in ("choice", "integers-unique"):
                add(
                    f"chain-number-numpy:o{offset}:{generator}:{method}",
                    "chain-number-numpy",
                    lambda record, o=offset, g=generator, m=method: _numpy_triplet(
                        _created_block(record) + o, g, m
                    ),
                )
        for encoding in ("decimal", "u64-big", "u64-little"):
            for algorithm in ("sha256", "blake2b"):
                for extraction in number_extractions:
                    def number_material(record: Mapping[str, Any], o=offset, e=encoding) -> bytes:
                        value = _created_block(record) + o
                        if e == "decimal":
                            return str(value).encode()
                        return value.to_bytes(8, e.removeprefix("u64-"))

                    add(
                        f"chain-number-digest:o{offset}:{encoding}:{algorithm}:{extraction}",
                        "chain-number-digest",
                        lambda record, fn=number_material, a=algorithm, x=extraction: _digest_triplet(
                            fn(record), a, x
                        ),
                    )

    # 3. Block hash plus task ID/contract/creation metadata, with common serialization.
    for offset in range(offset_start, offset_stop + 1):
        for field in ("task_id", "contract_material", "created_at"):
            for order in ("block-field", "field-block"):
                for delimiter in (b"", b":", b"|", b"\x1f"):
                    for algorithm in ("sha256", "blake2b"):
                        model_id = f"chain-task:o{offset}:{field}:{order}:d{delimiter.hex()}:{algorithm}"

                        def predict(record: Mapping[str, Any], o=offset, f=field, order_name=order, d=delimiter, a=algorithm):
                            value = _hash_for(cache, record, o)
                            if value is None:
                                return None
                            block = _raw_hash(value)
                            other = str(record.get(f) or "").encode()
                            material = block + d + other if order_name == "block-field" else other + d + block
                            return _digest_triplet(material, a, "counter-big")

                        add(model_id, "chain-task-composite", predict)

    # 4. UUID components, creation time and block/round number through common PRNGs.
    metadata_names = [name for name, _ in _metadata_materials(discovery[0])]
    for name in metadata_names:
        for method in ("sample", "randint-unique", "randrange-unique"):
            add(
                f"metadata-python:{name}:{method}",
                "metadata-python",
                lambda record, n=name, m=method: _python_triplet(_metadata_value(record, n), m),
            )
        sample_value = _metadata_value(discovery[0], name)
        if isinstance(sample_value, int) and sample_value >= 0:
            for generator in ("random-state", "default-rng"):
                for method in ("choice", "integers-unique"):
                    add(
                        f"metadata-numpy:{name}:{generator}:{method}",
                        "metadata-numpy",
                        lambda record, n=name, g=generator, m=method: _numpy_triplet(
                            int(_metadata_value(record, n)), g, m
                        ),
                    )

    # Holdout is deliberately evaluated only after a full 20/20 discovery hit.
    holdout_rows = []
    # The broad search stores descriptors, so reproduce exact candidates through
    # a second run would add complexity and risks accidental holdout peeking.
    # Until a discovery-perfect candidate exists, the correct holdout result is
    # therefore an explicit unopened gate.
    if ranking.discovery_exact:
        holdout_rows = [
            {
                **row.as_dict(),
                "status": "discovery-perfect-requires-preregistered-replay",
            }
            for row in ranking.discovery_exact
        ]

    return {
        "version": 1,
        "generated_at": utc_now(),
        "mode": "offline-public-input-discovery-search",
        "safety": {
            "public_chain_only": True,
            "credentials_accepted": False,
            "private_endpoints": False,
            "submission_writes": False,
            "holdout_opened_without_discovery_perfect_candidate": False,
        },
        "split": {
            "discovery_records": len(discovery),
            "holdout_records_reserved": len(holdout),
            "holdout_gate": "closed" if not ranking.discovery_exact else "replay-required",
        },
        "search": {
            "offset_start": offset_start,
            "offset_stop": offset_stop,
            "candidates_tested": ranking.tested,
            "discovery_exact_candidates": len(ranking.discovery_exact),
            "families": {
                key: {
                    **value,
                    "best_rank": list(value["best_rank"]),
                }
                for key, value in sorted(ranking.by_family.items())
            },
            "top_candidates": [row.as_dict() for row in ranking.top()],
            "discovery_exact": [row.as_dict() for row in ranking.discovery_exact],
            "holdout": holdout_rows,
        },
        "interpretation": (
            "A candidate is accepted only after 20/20 ordered discovery matches, "
            "a separately replayed 5/5 holdout match, and future shadow confirmation."
        ),
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
    if args.offset_start < -1 or args.offset_stop <= args.offset_start:
        raise SystemExit("invalid offset range")

    discovery = load_tasks(args.discovery.resolve())
    holdout = load_tasks(args.holdout.resolve())
    if len(discovery) != 20 or len(holdout) < 5:
        raise SystemExit("requires the fixed 20 discovery and at least 5 holdout tasks")
    cache: dict[str, Any] = {"version": 1, "network": args.network, "hashes": {}}
    if args.hash_cache.exists():
        cache = json.loads(args.hash_cache.read_text(encoding="utf-8"))
    heights = required_heights(
        [*discovery, *holdout], args.offset_start, args.offset_stop + 2
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
    print(json.dumps({
        "event": "preseed_extended_search_complete",
        "candidates_tested": report["search"]["candidates_tested"],
        "discovery_exact_candidates": report["search"]["discovery_exact_candidates"],
        "best": (report["search"]["top_candidates"] or [None])[0],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
