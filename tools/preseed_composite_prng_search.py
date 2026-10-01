#!/usr/bin/env python3
"""Search public block+task composites used as per-round PRNG initializers.

Direct digest-to-triplet and block-only PRNG searches leave a common coding
pattern uncovered: hash a block identifier together with task metadata, reduce
that digest to an integer, and pass it to ``random.Random`` or a NumPy RNG.
This discovery-only search covers common serializations, digest algorithms,
integer extraction rules, and draw APIs across the complete round offset.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

try:
    from tools.preseed_block_initializer_stream_search import (
        numpy_triplet,
        python_triplet,
    )
    from tools.preseed_generator_lab import atomic_json, load_tasks
except ModuleNotFoundError:
    from preseed_block_initializer_stream_search import numpy_triplet, python_triplet
    from preseed_generator_lab import atomic_json, load_tasks

import numpy as np
import random


_RECORDS: list[dict[str, Any]] = []
_HASHES: dict[str, str] = {}

BLOCK_REPRESENTATIONS = ("raw", "hex", "height-decimal")
FIELD_REPRESENTATIONS = ("task-id", "uuid-bytes", "created-at", "contract")
ORDERS = ("block-field", "field-block")
DELIMITERS = (b"", b":", b"|", b"\x1f")
DIGESTS = ("sha256", "sha512", "blake2b", "blake2s")
VIEWS = (
    "full-big",
    "full-little",
    "first4-big",
    "first4-little",
    "last4-big",
    "last4-little",
)
ENGINES = {
    "python": ("sample", "randint-unique", "float-unique"),
    "random-state": ("choice", "integers-unique"),
    "default-rng": ("choice", "integers-unique"),
}


def created_block(record: Mapping[str, Any]) -> int:
    return int(((record.get("block_context") or {}).get("created") or {})["number"])


def raw_hash(value: str) -> bytes:
    raw = bytes.fromhex(str(value).removeprefix("0x"))
    if len(raw) != 32:
        raise ValueError("expected a 32-byte block hash")
    return raw


def composite_material(
    record: Mapping[str, Any],
    block_hash: str,
    height: int,
    block_representation: str,
    field_representation: str,
    order: str,
    delimiter: bytes,
) -> bytes:
    raw = raw_hash(block_hash)
    if block_representation == "raw":
        block = raw
    elif block_representation == "hex":
        block = str(block_hash).encode()
    elif block_representation == "height-decimal":
        block = str(int(height)).encode()
    else:
        raise ValueError(block_representation)
    if field_representation == "task-id":
        field = str(record["task_id"]).encode()
    elif field_representation == "uuid-bytes":
        field = UUID(str(record["task_id"])).bytes
    elif field_representation == "created-at":
        field = str(record.get("created_at") or "").encode()
    elif field_representation == "contract":
        field = str(record.get("contract_material") or "").encode()
    else:
        raise ValueError(field_representation)
    return (
        block + delimiter + field
        if order == "block-field"
        else field + delimiter + block
    )


def digest_integer(material: bytes, algorithm: str, view: str) -> int:
    digest = hashlib.new(algorithm, material).digest()
    location, endian = view.rsplit("-", 1)
    if location == "full":
        selected = digest
    elif location == "first4":
        selected = digest[:4]
    elif location == "last4":
        selected = digest[-4:]
    else:
        raise ValueError(view)
    return int.from_bytes(selected, endian)


def seeded_triplet(initializer: Any, engine: str, method: str) -> list[int]:
    if engine == "python":
        return python_triplet(random.Random(initializer), method)
    if engine == "random-state":
        rng: Any = np.random.RandomState(int(initializer) & 0xFFFFFFFF)
    elif engine == "default-rng":
        rng = np.random.default_rng(int(initializer))
    else:
        raise ValueError(engine)
    return numpy_triplet(rng, engine, method)


def _predict(
    record: Mapping[str, Any],
    offset: int,
    block_representation: str,
    field_representation: str,
    order: str,
    delimiter: bytes,
    algorithm: str | None,
    view: str | None,
    engine: str,
    method: str,
) -> list[int]:
    height = created_block(record) + int(offset)
    block_hash = _HASHES[str(height)]
    material = composite_material(
        record,
        block_hash,
        height,
        block_representation,
        field_representation,
        order,
        delimiter,
    )
    initializer: Any = (
        material
        if algorithm is None
        else digest_integer(material, algorithm, str(view))
    )
    return seeded_triplet(initializer, engine, method)


def _evaluate(
    offset: int,
    block_representation: str,
    field_representation: str,
    order: str,
    delimiter: bytes,
    algorithm: str | None,
    view: str | None,
    engine: str,
    method: str,
) -> int:
    matched = 0
    for record in _RECORDS:
        predicted = _predict(
            record,
            offset,
            block_representation,
            field_representation,
            order,
            delimiter,
            algorithm,
            view,
            engine,
            method,
        )
        if predicted != [int(value) for value in record["seeds"]]:
            break
        matched += 1
    return matched


def _descriptor(
    offset: int,
    block_representation: str,
    field_representation: str,
    order: str,
    delimiter: bytes,
    algorithm: str | None,
    view: str | None,
    engine: str,
    method: str,
    matched: int,
) -> dict[str, Any]:
    return {
        "offset": int(offset),
        "block_representation": block_representation,
        "field_representation": field_representation,
        "order": order,
        "delimiter_hex": delimiter.hex(),
        "digest": algorithm or "direct-material",
        "integer_view": view,
        "engine": engine,
        "method": method,
        "matched_discovery_prefix": int(matched),
    }


def _init_worker(records: list[dict[str, Any]], hashes: dict[str, str]) -> None:
    global _RECORDS, _HASHES
    _RECORDS = records
    _HASHES = hashes


def _search_offsets(offsets: list[int]) -> dict[str, Any]:
    tested = best = 0
    first_hits: list[dict[str, Any]] = []
    exact: list[dict[str, Any]] = []
    for offset in offsets:
        for block_representation in BLOCK_REPRESENTATIONS:
            for field_representation in FIELD_REPRESENTATIONS:
                for order in ORDERS:
                    for delimiter in DELIMITERS:
                        # CPython accepts the composed bytes directly and applies
                        # its documented version-2 bytes seeding transform.
                        for method in ENGINES["python"]:
                            tested += 1
                            matched = _evaluate(
                                offset,
                                block_representation,
                                field_representation,
                                order,
                                delimiter,
                                None,
                                None,
                                "python",
                                method,
                            )
                            if matched:
                                row = _descriptor(
                                    offset,
                                    block_representation,
                                    field_representation,
                                    order,
                                    delimiter,
                                    None,
                                    None,
                                    "python",
                                    method,
                                    matched,
                                )
                                first_hits.append(row)
                                best = max(best, matched)
                                if matched == len(_RECORDS):
                                    exact.append(row)
                        for algorithm in DIGESTS:
                            for view in VIEWS:
                                for engine, methods in ENGINES.items():
                                    for method in methods:
                                        tested += 1
                                        matched = _evaluate(
                                            offset,
                                            block_representation,
                                            field_representation,
                                            order,
                                            delimiter,
                                            algorithm,
                                            view,
                                            engine,
                                            method,
                                        )
                                        if not matched:
                                            continue
                                        row = _descriptor(
                                            offset,
                                            block_representation,
                                            field_representation,
                                            order,
                                            delimiter,
                                            algorithm,
                                            view,
                                            engine,
                                            method,
                                            matched,
                                        )
                                        first_hits.append(row)
                                        best = max(best, matched)
                                        if matched == len(_RECORDS):
                                            exact.append(row)
    return {"tested": tested, "best": best, "first_hits": first_hits, "exact": exact}


def chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--hash-cache", type=Path, required=True)
    parser.add_argument("--offset-start", type=int, default=0)
    parser.add_argument("--offset-stop", type=int, default=719)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--chunk-size", type=int, default=8)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_composite_prng_search.json"),
    )
    args = parser.parse_args()
    records = load_tasks(args.discovery.resolve())
    if len(records) != 20:
        raise SystemExit("requires exactly 20 fixed Discovery records")
    hashes = {
        str(key): str(value)
        for key, value in (
            json.loads(args.hash_cache.read_text(encoding="utf-8")).get("hashes")
            or {}
        ).items()
    }
    offsets = list(range(int(args.offset_start), int(args.offset_stop) + 1))
    required = {
        str(created_block(record) + offset)
        for record in records
        for offset in offsets
    }
    missing = sorted(required - set(hashes), key=int)
    if missing:
        raise SystemExit(f"hash cache is missing {len(missing)} required blocks")
    jobs = chunks(offsets, max(1, int(args.chunk_size)))
    workers = max(1, int(args.workers))
    if workers == 1:
        _init_worker(records, hashes)
        results = [_search_offsets(job) for job in jobs]
    else:
        with mp.Pool(
            workers,
            initializer=_init_worker,
            initargs=(records, hashes),
        ) as pool:
            results = pool.map(_search_offsets, jobs)
    first_hits = [row for result in results for row in result["first_hits"]]
    exact = [row for result in results for row in result["exact"]]
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "public-block-task-composite-prng-initializer-search",
        "split": {"discovery_records": len(records), "holdout_opened": False},
        "search": {
            "offset_start": offsets[0],
            "offset_stop": offsets[-1],
            "candidates_tested": sum(int(row["tested"]) for row in results),
            "first_triplet_hits": first_hits,
            "best_prefix_tasks": max((int(row["best"]) for row in results), default=0),
            "discovery_exact_candidates": len(exact),
            "exact_candidates": exact,
            "families": {
                "python-direct-composite": {},
                "digest-to-python-prng": {},
                "digest-to-numpy-randomstate": {},
                "digest-to-numpy-generator": {},
            },
        },
        "interpretation": (
            "A discovery-perfect descriptor must still pass the sealed Holdout and "
            "prospective prediction gates before it can be promoted."
        ),
        "safety": {
            "public_offline_inputs_only": True,
            "holdout_opened": False,
            "network_reads": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "candidates_tested": report["search"]["candidates_tested"],
                "first_triplet_hits": len(first_hits),
                "best_prefix_tasks": report["search"]["best_prefix_tasks"],
                "discovery_exact_candidates": len(exact),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
