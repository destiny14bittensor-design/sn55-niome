#!/usr/bin/env python3
"""Analyse exact uint32 PRNG preimages for independent per-round reseeding.

The companion AVX2 scanner enumerates every uint32 initializer whose *first*
CPython ``random.sample(range(100, 1000), 3)`` (and float-based triplet)
matches each fixed Discovery label.  This tool asks whether one candidate per
round is a simple public clock/block/round function.  It never reads Holdout
labels and does not make submissions.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from math import gcd
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence
from uuid import UUID
import zlib

try:
    from tools.preseed_event_clock_search import generated_seed_times
    from tools.preseed_shuffle_leak_audit import fetch_log_nodes
    from tools.preseed_validation_time_search import validation_times
except ModuleNotFoundError:
    from preseed_event_clock_search import generated_seed_times
    from preseed_shuffle_leak_audit import fetch_log_nodes
    from preseed_validation_time_search import validation_times


UINT32_MODULUS = 1 << 32


def parse_epoch_microseconds(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def public_sources(
    records: Sequence[Mapping[str, Any]],
    *,
    validation_us: Mapping[str, int] | None = None,
    generated_us: Mapping[str, int] | None = None,
) -> dict[str, list[int | None]]:
    validation_us = validation_us or {}
    generated_us = generated_us or {}
    result: dict[str, list[int | None]] = {
        "round-index": [],
        "created-block": [],
    }
    for label in (
        "uuid:word0-big",
        "uuid:word1-big",
        "uuid:word2-big",
        "uuid:word3-big",
        "uuid:xor32-big",
        "uuid:sum32-big",
        "uuid:crc32",
        "uuid:adler32",
        "uuid:sha256-first32-big",
        "uuid:sha256-last32-big",
        "created-block-hash:first32-big",
        "created-block-hash:last32-big",
        "created-block-hash:first32-little",
        "created-block-hash:last32-little",
    ):
        result[label] = []
    for anchor in ("created", "validation", "generated-seed-log"):
        for unit in ("seconds", "milliseconds", "microseconds", "nanoseconds"):
            result[f"{anchor}:{unit}"] = []
    for index, record in enumerate(records):
        task_id = str(record["task_id"])
        uuid = UUID(task_id)
        words = [
            int.from_bytes(uuid.bytes[offset : offset + 4], "big")
            for offset in range(0, 16, 4)
        ]
        digest = hashlib.sha256(uuid.bytes).digest()
        result["uuid:word0-big"].append(words[0])
        result["uuid:word1-big"].append(words[1])
        result["uuid:word2-big"].append(words[2])
        result["uuid:word3-big"].append(words[3])
        result["uuid:xor32-big"].append(words[0] ^ words[1] ^ words[2] ^ words[3])
        result["uuid:sum32-big"].append(sum(words) & 0xFFFFFFFF)
        result["uuid:crc32"].append(zlib.crc32(uuid.bytes))
        result["uuid:adler32"].append(zlib.adler32(uuid.bytes))
        result["uuid:sha256-first32-big"].append(int.from_bytes(digest[:4], "big"))
        result["uuid:sha256-last32-big"].append(int.from_bytes(digest[-4:], "big"))
        block_hash_text = str(
            (((record.get("block_context") or {}).get("created") or {}).get("hash") or "")
        ).removeprefix("0x")
        try:
            block_hash = bytes.fromhex(block_hash_text)
        except ValueError:
            block_hash = b""
        for endian in ("big", "little"):
            result[f"created-block-hash:first32-{endian}"].append(
                int.from_bytes(block_hash[:4], endian) if len(block_hash) == 32 else None
            )
            result[f"created-block-hash:last32-{endian}"].append(
                int.from_bytes(block_hash[-4:], endian) if len(block_hash) == 32 else None
            )
        created_us = parse_epoch_microseconds(str(record["created_at"]))
        anchors = {
            "created": created_us,
            "validation": validation_us.get(task_id),
            "generated-seed-log": generated_us.get(task_id),
        }
        result["round-index"].append(index)
        result["created-block"].append(
            int((((record.get("block_context") or {}).get("created") or {}).get("number") or 0))
        )
        for anchor, value in anchors.items():
            for unit, divisor in (
                ("seconds", 1_000_000),
                ("milliseconds", 1_000),
                ("microseconds", 1),
                ("nanoseconds", 0),
            ):
                result[f"{anchor}:{unit}"].append(
                    None
                    if value is None
                    else int(value) * 1_000
                    if divisor == 0
                    else int(value) // divisor
                )
    return result


def rank_constant_relations(
    candidate_sets: Sequence[Sequence[int]],
    sources: Mapping[str, Sequence[int | None]],
    *,
    top: int = 5,
) -> dict[str, dict[str, Any]]:
    """Rank additive and XOR constants by distinct-task support."""
    if any(len(values) != len(candidate_sets) for values in sources.values()):
        raise ValueError("every source must align with the candidate rows")
    ranked: dict[str, dict[str, Any]] = {}
    for label, values in sources.items():
        additive: Counter[int] = Counter()
        xor: Counter[int] = Counter()
        evaluable = 0
        for candidates, source in zip(candidate_sets, values):
            if source is None:
                continue
            evaluable += 1
            source32 = int(source) & 0xFFFFFFFF
            additive.update(
                {(int(candidate) - source32) % UINT32_MODULUS for candidate in candidates}
            )
            xor.update({int(candidate) ^ source32 for candidate in candidates})

        def top_rows(counter: Counter[int]) -> list[dict[str, int]]:
            return [
                {"constant": int(constant), "task_support": int(support)}
                for constant, support in counter.most_common(max(0, top))
            ]

        add_rows = top_rows(additive)
        xor_rows = top_rows(xor)
        ranked[label] = {
            "evaluable_tasks": evaluable,
            "additive_max_support": add_rows[0]["task_support"] if add_rows else 0,
            "xor_max_support": xor_rows[0]["task_support"] if xor_rows else 0,
            "additive_top": add_rows,
            "xor_top": xor_rows,
            "exact_constant_relation": bool(
                evaluable
                and (
                    (add_rows and add_rows[0]["task_support"] == evaluable)
                    or (xor_rows and xor_rows[0]["task_support"] == evaluable)
                )
            ),
        }
    return ranked


def _modular_linear_solutions(coefficient: int, target: int) -> list[int]:
    """Solve ``coefficient * x == target (mod 2**32)`` exactly."""
    coefficient %= UINT32_MODULUS
    target %= UINT32_MODULUS
    divisor = gcd(coefficient, UINT32_MODULUS)
    if target % divisor:
        return []
    reduced_modulus = UINT32_MODULUS // divisor
    if reduced_modulus == 1:
        return list(range(divisor))
    base = (
        (target // divisor)
        * pow(coefficient // divisor, -1, reduced_modulus)
    ) % reduced_modulus
    return [base + step * reduced_modulus for step in range(divisor)]


def exact_affine_relations(
    candidate_sets: Sequence[Sequence[int]],
    sources: Mapping[str, Sequence[int | None]],
    *,
    maximum_pair_solutions: int = 65_536,
) -> dict[str, dict[str, Any]]:
    """Find affine uint32 maps selecting a candidate in every evaluable task."""
    result: dict[str, dict[str, Any]] = {}
    for label, values in sources.items():
        if len(values) != len(candidate_sets):
            raise ValueError("every source must align with the candidate rows")
        rows = [
            (int(source) & 0xFFFFFFFF, {int(value) for value in candidates})
            for source, candidates in zip(values, candidate_sets)
            if source is not None
        ]
        maps: set[tuple[int, int]] = set()
        truncated = False
        unreachable = sum(not outputs for _source, outputs in rows)
        if len(rows) >= 2 and not unreachable:
            (first_source, first_outputs), (second_source, second_outputs) = rows[:2]
            delta_source = (second_source - first_source) % UINT32_MODULUS
            for first in first_outputs:
                for second in second_outputs:
                    solutions = _modular_linear_solutions(
                        delta_source, second - first
                    )
                    if len(solutions) > maximum_pair_solutions:
                        truncated = True
                        continue
                    for multiplier in solutions:
                        increment = (
                            first - multiplier * first_source
                        ) % UINT32_MODULUS
                        maps.add((multiplier, increment))
        exact = [
            {"multiplier": multiplier, "increment": increment}
            for multiplier, increment in sorted(maps)
            if all(
                ((multiplier * source + increment) % UINT32_MODULUS) in outputs
                for source, outputs in rows
            )
        ]
        result[label] = {
            "evaluable_tasks": len(rows),
            "tasks_without_uint32_preimage": unreachable,
            "pair_candidates_tested": len(maps),
            "pair_solution_limit_hit": truncated,
            "exact_affine_count": len(exact),
            "exact_affine": exact[:20],
        }
    return result


def modulo_window_coverage(
    candidate_sets: Sequence[Sequence[int]],
    sources: Mapping[str, Sequence[int | None]],
    *,
    radii: Sequence[int] = (5_000_000, 250_000_000),
) -> dict[str, dict[str, Any]]:
    """Count rows whose uint32 preimage is near a public nanosecond clock."""
    result: dict[str, dict[str, Any]] = {}
    for label, values in sources.items():
        if not label.endswith(":nanoseconds"):
            continue
        nearest: list[dict[str, int]] = []
        for index, (candidates, source) in enumerate(zip(candidate_sets, values)):
            if source is None or not candidates:
                continue
            source32 = int(source) & 0xFFFFFFFF
            offsets = [
                ((int(candidate) - source32 + (1 << 31)) % UINT32_MODULUS)
                - (1 << 31)
                for candidate in candidates
            ]
            closest = min(offsets, key=abs)
            nearest.append({"task_index": index, "offset_ns": closest})
        result[label] = {
            "evaluable_tasks": sum(source is not None for source in values),
            "tasks_with_preimage": len(nearest),
            "coverage": {
                str(int(radius)): sum(abs(row["offset_ns"]) <= radius for row in nearest)
                for radius in radii
            },
            "nearest_offsets_ns": nearest,
        }
    return result


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
    parser.add_argument("--scan", type=Path, required=True)
    parser.add_argument(
        "--engine",
        choices=("python", "numpy-randomstate"),
        default="python",
    )
    parser.add_argument("--entity", default="genomes")
    parser.add_argument("--project", default="niome")
    parser.add_argument("--run", default="non2mca3")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_python_uint32_round_reseed.json"),
    )
    args = parser.parse_args()

    discovery = json.loads(args.discovery.read_text(encoding="utf-8"))
    records = [dict(row) for row in discovery.get("records") or []]
    if len(records) != 20:
        raise ValueError("requires the fixed 20-task Discovery set")
    scan = json.loads(args.scan.read_text(encoding="utf-8"))
    if int(scan.get("start", -1)) != 0 or int(
        scan.get("stop_exclusive", -1)
    ) != UINT32_MODULUS:
        raise ValueError("scan must cover the complete uint32 initializer space")
    targets = scan.get("targets") or []
    if len(targets) != len(records):
        raise ValueError("scan target count does not match Discovery")
    for record, target in zip(records, targets):
        if [int(value) for value in target.get("target") or []] != [
            int(value) for value in record.get("seeds") or []
        ]:
            raise ValueError("scan target order does not match Discovery")

    nodes = fetch_log_nodes(args.entity, args.project, args.run)
    validation = {
        task_id: int(round(value * 1_000_000))
        for task_id, value in validation_times(nodes).items()
    }
    generated = {
        task_id: int(round(value * 1_000_000))
        for task_id, value in generated_seed_times(nodes).items()
    }
    sources = public_sources(records, validation_us=validation, generated_us=generated)
    methods: dict[str, Any] = {}
    method_fields = (
        (
            ("integer_candidates", "python-sample-or-randrange-unique"),
            ("float_candidates", "python-random-float-triplet"),
        )
        if args.engine == "python"
        else (("integer_candidates", "numpy-randomstate-randint-unique"),)
    )
    for field, method in method_fields:
        candidates = [
            [int(value) for value in target.get(field) or []] for target in targets
        ]
        relations = rank_constant_relations(candidates, sources)
        affine = exact_affine_relations(candidates, sources)
        modulo_windows = modulo_window_coverage(candidates, sources)
        exact = [label for label, row in relations.items() if row["exact_constant_relation"]]
        exact_affine = [
            label for label, row in affine.items() if row["exact_affine_count"]
        ]
        methods[method] = {
            "candidate_count": sum(map(len, candidates)),
            "tasks_with_candidate": sum(bool(values) for values in candidates),
            "minimum_candidates_per_task": min(map(len, candidates), default=0),
            "maximum_candidates_per_task": max(map(len, candidates), default=0),
            "exact_public_constant_relations": exact,
            "exact_public_affine_relations": exact_affine,
            "relations": relations,
            "affine_relations": affine,
            "modulo_nanosecond_windows": modulo_windows,
        }

    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": f"per-round-independent-{args.engine}-uint32-reseed-inversion",
        "split": {
            "fixed_discovery_total": len(records),
            "holdout_opened": False,
            "validation_clock_tasks": sum(str(row["task_id"]) in validation for row in records),
            "seed_log_clock_tasks": sum(str(row["task_id"]) in generated for row in records),
        },
        "search": {
            "candidates_tested": UINT32_MODULUS,
            "discovery_exact_candidates": sum(
                bool(
                    row["exact_public_constant_relations"]
                    or row["exact_public_affine_relations"]
                )
                for row in methods.values()
            ),
            "families": {
                method: {
                    "candidate_preimages": row["candidate_count"],
                    "exact_public_constant_relations": row[
                        "exact_public_constant_relations"
                    ],
                    "exact_public_affine_relations": row[
                        "exact_public_affine_relations"
                    ],
                }
                for method, row in methods.items()
            },
            "initializer_space_tested": UINT32_MODULUS,
            "complete": True,
            "methods": methods,
        },
        "interpretation": (
            "Each listed candidate is an exact uint32 initializer preimage for one Discovery "
            "triplet. A relation is promoted only if the same public additive/XOR constant "
            "selects a candidate in every evaluable round. This does not cover wider or "
            "string/byte seeds, OS entropy, a different engine/API, or hidden draws before "
            "the triplet."
        ),
        "safety": {
            "discovery_only": True,
            "holdout_opened": False,
            "public_read_only": True,
            "submission_writes": False,
            "credentials_stored": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    compact = {
        method: {
            "candidates": row["candidate_count"],
            "tasks": row["tasks_with_candidate"],
            "exact_relations": row["exact_public_constant_relations"],
            "exact_affine_relations": row["exact_public_affine_relations"],
        }
        for method, row in methods.items()
    }
    print(json.dumps({"output": str(args.output.resolve()), "methods": compact}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
