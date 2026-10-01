#!/usr/bin/env python3
"""Search previous-published-seed recurrence hypotheses on Discovery only.

This family is deliberately separate from stateless task/block hashing and
from a process-global PRNG: each task may deterministically derive its triplet
from the preceding task's already published seed plus public current context.
Holdout labels are never loaded by this tool.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import random
import tempfile
from typing import Any, Callable, Iterable

import numpy as np


SEED_LOW = 100
SEED_SPAN = 900


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def previous_material(seeds: list[int], encoding: str) -> bytes:
    if encoding == "csv":
        return ",".join(str(value) for value in seeds).encode()
    if encoding == "json":
        return json.dumps(seeds, separators=(",", ":")).encode()
    if encoding == "concat":
        return "".join(f"{value:03d}" for value in seeds).encode()
    if encoding in {"u16-big", "u16-little"}:
        order = encoding.removeprefix("u16-")
        return b"".join(value.to_bytes(2, order) for value in seeds)
    raise ValueError(encoding)


def context_material(record: dict[str, Any], name: str) -> bytes:
    if name == "none":
        return b""
    if name == "task-id":
        return str(record["task_id"]).encode()
    if name == "contract":
        return str(record.get("contract_material") or "").encode()
    if name == "created-at":
        return str(record["created_at"]).encode()
    if name.startswith("block-"):
        _, role, field = name.split("-", 2)
        value = ((record.get("block_context") or {}).get(role) or {}).get(field)
        return str(value or "").encode()
    raise ValueError(name)


def digest_triplet(material: bytes, algorithm: str, extraction: str) -> list[int]:
    unique = extraction.endswith("-unique")
    base = extraction.removesuffix("-unique")
    values: list[int] = []
    counter = 0
    digest = hashlib.new(algorithm, material).digest()
    offset = 0
    while len(values) < 3:
        if base.startswith("counter"):
            order = base.split("-")[-1]
            digest = hashlib.new(algorithm, material + counter.to_bytes(4, "big")).digest()
            raw = int.from_bytes(digest[:8], order)
            counter += 1
        else:
            _, width_text, order = base.split("-")
            width = int(width_text)
            if offset + width > len(digest):
                digest = hashlib.new(algorithm, digest).digest()
                offset = 0
            raw = int.from_bytes(digest[offset : offset + width], order)
            offset += width
        value = SEED_LOW + raw % SEED_SPAN
        if not unique or value not in values:
            values.append(value)
    return values


def hmac_triplet(key: bytes, message: bytes, algorithm: str, extraction: str) -> list[int]:
    unique = extraction.endswith("-unique")
    base = extraction.removesuffix("-unique")
    values: list[int] = []
    counter = 0
    digest = hmac.new(key, message, algorithm).digest()
    offset = 0
    while len(values) < 3:
        if base.startswith("counter"):
            order = base.split("-")[-1]
            digest = hmac.new(
                key, message + counter.to_bytes(4, "big"), algorithm
            ).digest()
            raw = int.from_bytes(digest[:8], order)
            counter += 1
        else:
            _, width_text, order = base.split("-")
            width = int(width_text)
            if offset + width > len(digest):
                digest = hmac.new(key, digest, algorithm).digest()
                offset = 0
            raw = int.from_bytes(digest[offset : offset + width], order)
            offset += width
        value = SEED_LOW + raw % SEED_SPAN
        if not unique or value not in values:
            values.append(value)
    return values


def python_triplet(seed: Any, method: str) -> list[int]:
    rng = random.Random(seed)
    if method == "sample":
        return rng.sample(range(SEED_LOW, SEED_LOW + SEED_SPAN), 3)
    values: list[int] = []
    while len(values) < 3:
        value = rng.randrange(SEED_LOW, SEED_LOW + SEED_SPAN)
        if value not in values:
            values.append(value)
    return values


def numpy_triplet(seed: int, engine: str, method: str) -> list[int]:
    rng: Any = (
        np.random.RandomState(seed & 0xFFFFFFFF)
        if engine == "random-state"
        else np.random.default_rng(seed)
    )
    if method == "choice":
        return [
            int(value)
            for value in rng.choice(
                np.arange(SEED_LOW, SEED_LOW + SEED_SPAN), 3, replace=False
            )
        ]
    values: list[int] = []
    while len(values) < 3:
        value = int(
            rng.randint(SEED_LOW, SEED_LOW + SEED_SPAN)
            if engine == "random-state"
            else rng.integers(SEED_LOW, SEED_LOW + SEED_SPAN)
        )
        if value not in values:
            values.append(value)
    return values


@dataclass(frozen=True)
class Hypothesis:
    model_id: str
    family: str
    predict: Callable[[list[int], dict[str, Any]], list[int]]


def hypotheses() -> Iterable[Hypothesis]:
    previous_encodings = ("csv", "json", "concat", "u16-big", "u16-little")
    contexts = ["none", "task-id", "contract", "created-at"]
    for role in ("created", "created_minus_1", "round_start", "validation"):
        for field in ("hash", "header"):
            contexts.append(f"block-{role}-{field}")
    extractions = (
        "counter-big", "counter-little",
        "chunks-4-big", "chunks-4-little",
        "chunks-8-big", "chunks-8-little",
    )
    for prev_encoding in previous_encodings:
        for context in contexts:
            orders = ("prev-only",) if context == "none" else ("prev-context", "context-prev")
            for order in orders:
                delimiters = (b"",) if context == "none" else (b"", b":", b"|", b"\x1f")
                for delimiter in delimiters:
                    def material(
                        previous: list[int], record: dict[str, Any],
                        pe=prev_encoding, ctx=context, ordering=order, delim=delimiter,
                    ) -> bytes:
                        prior = previous_material(previous, pe)
                        current = context_material(record, ctx)
                        if ordering == "prev-only":
                            return prior
                        return prior + delim + current if ordering == "prev-context" else current + delim + prior

                    for algorithm in ("sha256", "sha512", "blake2b"):
                        for extraction in extractions:
                            for unique in (False, True):
                                mode = extraction + ("-unique" if unique else "")
                                yield Hypothesis(
                                    f"digest:{prev_encoding}:{context}:{order}:d{delimiter.hex()}:{algorithm}:{mode}",
                                    "previous-seed-digest",
                                    lambda previous, record, fn=material, a=algorithm, x=mode: digest_triplet(fn(previous, record), a, x),
                                )
                    for seed_form in ("bytes", "sha256-big", "sha256-little"):
                        for method in ("sample", "randrange-unique"):
                            def predict_python(
                                previous: list[int], record: dict[str, Any], fn=material,
                                form=seed_form, m=method,
                            ) -> list[int]:
                                raw = fn(previous, record)
                                seed: Any = raw
                                if form != "bytes":
                                    seed = int.from_bytes(hashlib.sha256(raw).digest(), form.split("-")[-1])
                                return python_triplet(seed, m)

                            yield Hypothesis(
                                f"python:{prev_encoding}:{context}:{order}:d{delimiter.hex()}:{seed_form}:{method}",
                                "previous-seed-python-reseed",
                                predict_python,
                            )
                    for endian in ("big", "little"):
                        for engine in ("random-state", "default-rng"):
                            for method in ("choice", "integers-unique"):
                                yield Hypothesis(
                                    f"numpy:{prev_encoding}:{context}:{order}:d{delimiter.hex()}:sha256-{endian}:{engine}:{method}",
                                    "previous-seed-numpy-reseed",
                                    lambda previous, record, fn=material, e=endian, g=engine, m=method: numpy_triplet(
                                        int.from_bytes(hashlib.sha256(fn(previous, record)).digest(), e), g, m
                                    ),
                                )
                    if context != "none" and order == "prev-context" and delimiter == b"":
                        for algorithm in ("sha256", "sha512", "blake2b"):
                            for extraction in ("counter-big-unique", "chunks-8-big-unique"):
                                yield Hypothesis(
                                    f"hmac:{prev_encoding}:{context}:{order}:d{delimiter.hex()}:{algorithm}:{extraction}",
                                    "previous-seed-hmac",
                                    lambda previous, record, pe=prev_encoding, ctx=context, a=algorithm, x=extraction: hmac_triplet(
                                        previous_material(previous, pe), context_material(record, ctx), a, x
                                    ),
                                )


def evaluate(model: Hypothesis, records: list[dict[str, Any]]) -> dict[str, Any]:
    exact = 0
    positions = 0
    longest = current = 0
    for previous_record, record in zip(records, records[1:]):
        predicted = model.predict(
            [int(value) for value in previous_record["seeds"]], record
        )
        expected = [int(value) for value in record["seeds"]]
        positions += sum(left == right for left, right in zip(predicted, expected))
        if predicted == expected:
            exact += 1
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return {
        "model_id": model.model_id,
        "family": model.family,
        "exact_transitions": exact,
        "position_matches": positions,
        "longest_exact_streak": longest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--discovery", type=Path,
        default=Path("artifacts/research/preseed_datasets/discovery.json"),
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path("artifacts/research/preseed_previous_seed_recurrence.json"),
    )
    args = parser.parse_args()
    payload = json.loads(args.discovery.read_text(encoding="utf-8"))
    records = sorted(payload.get("records") or [], key=lambda row: row["created_at"])
    if len(records) != 20 or any(len(row.get("seeds") or []) != 3 for row in records):
        raise ValueError("requires the fixed 20-task labeled Discovery set")
    results = [evaluate(model, records) for model in hypotheses()]
    ranking = sorted(
        results,
        key=lambda row: (
            row["exact_transitions"], row["longest_exact_streak"], row["position_matches"]
        ),
        reverse=True,
    )
    exact = [row for row in ranking if row["exact_transitions"] == len(records) - 1]
    families: dict[str, int] = {}
    for row in results:
        families[row["family"]] = families.get(row["family"], 0) + 1
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "previous-published-seed-recurrence-discovery-only",
        "split": {"discovery_tasks": 20, "transitions": 19, "holdout_opened": False},
        "search": {
            "candidates_tested": len(results),
            "families": families,
            "exact_candidates": exact,
            "top": ranking[:50],
        },
        "interpretation": (
            "A miss rejects only the explicit previous-seed encodings, public contexts, "
            "and reseeding/extraction APIs enumerated here."
        ),
        "safety": {
            "discovery_only": True,
            "holdout_opened": False,
            "network_requests": False,
            "submission_writes": False,
            "predicted_triplets_persisted": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({
        "output": str(args.output.resolve()),
        "candidates_tested": len(results),
        "exact_candidates": len(exact),
        "best": ranking[0],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
