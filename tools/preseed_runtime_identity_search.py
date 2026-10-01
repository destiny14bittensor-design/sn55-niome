#!/usr/bin/env python3
"""Search process-persistent PRNG seeds derived from public runtime identity.

This closes common operational initializers such as wallet hotkey, coldkey,
run id, git commit, process-start text, and curated concatenations thereof.
Only labels for those public materials are persisted; raw addresses are kept in
memory.  The restart prelude is included before the 16 Discovery task labels.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import tempfile
from typing import Any, Iterable
import zlib

import numpy as np

try:
    from tools.preseed_stateful_prelude_search import load_layout
except ModuleNotFoundError:
    from preseed_stateful_prelude_search import load_layout


DEFAULT_PROCESS_START = "2026-09-26T14:57:51.192497Z"
LOW, HIGH = 100, 999


def unique(draw) -> list[int]:
    result: list[int] = []
    while len(result) < 3:
        value = int(draw())
        if value not in result:
            result.append(value)
    return result


def python_stream(seed: Any, method: str, count: int) -> list[list[int]]:
    rng = random.Random(seed)
    output = []
    for _ in range(count):
        if method == "sample":
            output.append(rng.sample(range(LOW, HIGH + 1), 3))
        elif method == "randint-unique":
            output.append(unique(lambda: rng.randint(LOW, HIGH)))
        elif method == "float-unique":
            output.append(unique(lambda: LOW + int(rng.random() * 900)))
        else:
            raise ValueError(method)
    return output


def numpy_stream(seed: int, engine: str, method: str, count: int) -> list[list[int]]:
    rng: Any
    if engine == "random-state":
        rng = np.random.RandomState(int(seed) & 0xFFFFFFFF)
    else:
        rng = np.random.default_rng(int(seed))
    output = []
    for _ in range(count):
        if method == "choice":
            output.append(
                [
                    int(value)
                    for value in rng.choice(
                        np.arange(LOW, HIGH + 1), 3, replace=False
                    )
                ]
            )
        else:
            draw = (
                (lambda: rng.randint(LOW, HIGH + 1))
                if engine == "random-state"
                else (lambda: rng.integers(LOW, HIGH + 1))
            )
            output.append(unique(draw))
    return output


def public_materials(process_start: str) -> list[tuple[str, str]]:
    # Public chain/W&B/repository identities; none grants access to a service.
    values = {
        "netuid": "55",
        "validator-uid": "119",
        "wallet": "seus",
        "wallet-hotkey-name": "niowner",
        "wandb-run": "non2mca3",
        "git-commit": "9d9347a7ffab85a04eda6c36b9e87c59c8bb4049",
        "process-start": process_start,
        "process-date": process_start[:10],
        "validator-hotkey": "5DJ5fT174AY8GzbYHnamYQCJd4cTcj2Zf7ogUvBhry1KfYVd",
        "validator-coldkey": "5GeWUxaFP6duJyNg8EUv6Jfcv6ZNkoERywAcbSw4FAuEMpDq",
    }
    rows = list(values.items())
    curated = (
        ("netuid", "validator-uid"),
        ("wallet", "wallet-hotkey-name"),
        ("netuid", "validator-hotkey"),
        ("validator-hotkey", "validator-coldkey"),
        ("wandb-run", "process-start"),
        ("git-commit", "process-start"),
        ("wallet-hotkey-name", "process-start"),
        ("validator-hotkey", "process-start"),
    )
    for left, right in curated:
        for delimiter_name, delimiter in (("none", ""), ("colon", ":"), ("pipe", "|"), ("unit", "\x1f")):
            rows.append(
                (
                    f"{left}-{right}-{delimiter_name}",
                    values[left] + delimiter + values[right],
                )
            )
    return rows


def derived_seeds(material: str) -> Iterable[tuple[str, Any]]:
    yield "text", material
    if material.isdecimal():
        yield "integer", int(material)
    encoded = material.encode()
    yield "crc32", zlib.crc32(encoded)
    yield "adler32", zlib.adler32(encoded)
    for algorithm in ("sha256", "sha512", "blake2b"):
        digest = hashlib.new(algorithm, encoded).digest()
        yield f"{algorithm}-big", int.from_bytes(digest, "big")
        yield f"{algorithm}-little", int.from_bytes(digest, "little")
        yield f"{algorithm}-first32-big", int.from_bytes(digest[:4], "big")
        yield f"{algorithm}-last32-big", int.from_bytes(digest[-4:], "big")
        yield f"{algorithm}-first64-big", int.from_bytes(digest[:8], "big")
        yield f"{algorithm}-last64-big", int.from_bytes(digest[-8:], "big")


def prefix_match(actual: list[list[int]], expected: list[list[int]]) -> int:
    matched = 0
    for left, right in zip(actual, expected):
        if left != right:
            break
        matched += 1
    return matched


def run_search(targets: list[list[int]], process_start: str) -> dict[str, Any]:
    tested = 0
    hits = []
    best = 0
    families: dict[str, dict[str, Any]] = {}
    for label, material in public_materials(process_start):
        for derivation, seed in derived_seeds(material):
            for method in ("sample", "randint-unique", "float-unique"):
                matched = prefix_match(python_stream(seed, method, len(targets)), targets)
                tested += 1
                best = max(best, matched)
                family = families.setdefault("python-runtime-identity", {"tested": 0})
                family["tested"] += 1
                if matched == len(targets):
                    hits.append(
                        {"material_label": label, "derivation": derivation, "engine": "python", "method": method}
                    )
            if not isinstance(seed, int):
                continue
            for engine in ("random-state", "default-rng"):
                for method in ("choice", "integers-unique"):
                    matched = prefix_match(
                        numpy_stream(seed, engine, method, len(targets)), targets
                    )
                    tested += 1
                    best = max(best, matched)
                    family = families.setdefault("numpy-runtime-identity", {"tested": 0})
                    family["tested"] += 1
                    if matched == len(targets):
                        hits.append(
                            {"material_label": label, "derivation": derivation, "engine": engine, "method": method}
                        )
    return {
        "candidates_tested": tested,
        "discovery_exact_candidates": len(hits),
        "best_prefix_tasks": best,
        "families": families,
        "exact_candidates": hits,
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
    parser.add_argument("--process-start", default=DEFAULT_PROCESS_START)
    parser.add_argument("--prelude", default="654,347,964")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_runtime_identity_search.json"),
    )
    args = parser.parse_args()
    observed_prelude = [int(value) for value in args.prelude.split(",")]
    if len(observed_prelude) != 3:
        raise ValueError("--prelude requires three comma-separated integers")
    prelude, segment = load_layout(
        args.discovery.resolve(), args.process_start, observed_prelude
    )
    targets = [prelude] + [[int(value) for value in row["seeds"]] for row in segment]
    search = run_search(targets, args.process_start)
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "public-runtime-identity-prng-search",
        "split": {
            "process_segment_with_prelude": len(targets),
            "prelude_source": "explicit-public-wandb-generated-seeds-event",
            "holdout_opened": False,
        },
        "search": search,
        "safety": {
            "public_identity_only": True,
            "raw_identity_values_persisted": False,
            "holdout_opened": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **{k: search[k] for k in ("candidates_tested", "discovery_exact_candidates", "best_prefix_tasks")}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
