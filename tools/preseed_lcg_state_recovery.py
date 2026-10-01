#!/usr/bin/env python3
"""Recover or reject common small-state LCG seed streams on Discovery data.

This tests state directly instead of guessing an initialization seed.  It
covers Java ``Random.nextInt``, common ANSI/MSVC high-bit LCGs, and full-word
LCG modulo output, plus Park--Miller.  The validator is Python, so these are
secondary patch/backend hypotheses, but their 31--48 bit states are fully
testable from the restart prelude and Discovery segment.  Holdout is sealed.

Run through ``uv run --with z3-solver``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import numpy as np

try:
    from tools.preseed_v8_state_recovery import (
        DEFAULT_PRELUDE,
        DEFAULT_PROCESS_START,
        load_segment,
    )
except ModuleNotFoundError:
    from preseed_v8_state_recovery import (
        DEFAULT_PRELUDE,
        DEFAULT_PROCESS_START,
        load_segment,
    )


@dataclass(frozen=True)
class LcgSpec:
    name: str
    width: int
    multiplier: int
    increment: int
    output_shift: int


SPECS = (
    LcgSpec("java48-next31", 48, 25_214_903_917, 11, 17),
    LcgSpec("ansi31-high15", 31, 1_103_515_245, 12_345, 16),
    LcgSpec("msvc32-high15", 32, 214_013, 2_531_011, 16),
    LcgSpec("ansi31-fullword", 31, 1_103_515_245, 12_345, 0),
    LcgSpec("nr32-fullword", 32, 1_664_525, 1_013_904_223, 0),
)


def generate_lcg(initial_state: int, count: int, spec: LcgSpec) -> list[int]:
    mask = (1 << spec.width) - 1
    state = initial_state & mask
    result = []
    for _ in range(count):
        state = (state * spec.multiplier + spec.increment) & mask
        result.append(100 + ((state >> spec.output_shift) % 900))
    return result


def generate_erand48(initial_state: int, count: int) -> list[int]:
    state = int(initial_state) & ((1 << 48) - 1)
    result = []
    for _ in range(count):
        state = (state * 0x5DEECE66D + 0xB) & ((1 << 48) - 1)
        result.append(100 + (state * 900) // (1 << 48))
    return result


def solve_erand48(observed: list[int], timeout_ms: int) -> dict[str, Any]:
    try:
        import z3
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with z3-solver") from error
    initial = z3.BitVec("postgres_erand48_initial", 48)
    state = initial
    solver = z3.Solver()
    solver.set(timeout=max(1, timeout_ms))
    denominator = 1 << 48
    for value in observed:
        state = state * z3.BitVecVal(0x5DEECE66D, 48) + z3.BitVecVal(0xB, 48)
        bucket = int(value) - 100
        low = (bucket * denominator + 899) // 900
        high = ((bucket + 1) * denominator + 899) // 900
        solver.add(z3.UGE(state, z3.BitVecVal(low, 48)))
        solver.add(z3.ULT(state, z3.BitVecVal(high, 48)))
    outcome = solver.check()
    if outcome == z3.unknown:
        return {"status": "unknown", "reason": solver.reason_unknown()}
    if outcome != z3.sat:
        return {"status": "unsat"}
    candidate = solver.model().eval(initial).as_long()
    replay = generate_erand48(candidate, len(observed))
    return {"status": "sat" if replay == observed else "model-replay-failed", "exact_bucket_replay": replay == observed}


def generate_mysql(initial1: int, initial2: int, count: int) -> list[int]:
    modulus = 0x3FFFFFFF
    seed1, seed2 = int(initial1) % modulus, int(initial2) % modulus
    result = []
    for _ in range(count):
        seed1 = (seed1 * 3 + seed2) % modulus
        seed2 = (seed1 + seed2 + 33) % modulus
        result.append(100 + (seed1 * 900) // modulus)
    return result


def solve_mysql(observed: list[int], timeout_ms: int) -> dict[str, Any]:
    try:
        import z3
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with z3-solver") from error
    modulus = 0x3FFFFFFF
    initial1, initial2 = z3.Ints("mysql_seed1 mysql_seed2")
    seed1, seed2 = initial1, initial2
    solver = z3.Solver()
    solver.set(timeout=max(1, timeout_ms))
    solver.add(initial1 >= 0, initial1 < modulus, initial2 >= 0, initial2 < modulus)
    for index, value in enumerate(observed):
        raw1 = seed1 * 3 + seed2
        quotient1 = z3.Int(f"mysql_q1_{index}")
        following1 = raw1 - quotient1 * modulus
        solver.add(quotient1 >= 0, quotient1 <= 3, following1 >= 0, following1 < modulus)
        raw2 = following1 + seed2 + 33
        quotient2 = z3.Int(f"mysql_q2_{index}")
        following2 = raw2 - quotient2 * modulus
        solver.add(quotient2 >= 0, quotient2 <= 2, following2 >= 0, following2 < modulus)
        bucket = int(value) - 100
        solver.add(following1 * 900 >= bucket * modulus)
        solver.add(following1 * 900 < (bucket + 1) * modulus)
        seed1, seed2 = following1, following2
    outcome = solver.check()
    if outcome == z3.unknown:
        return {"status": "unknown", "reason": solver.reason_unknown()}
    if outcome != z3.sat:
        return {"status": "unsat"}
    model = solver.model()
    candidate1, candidate2 = model.eval(initial1).as_long(), model.eval(initial2).as_long()
    replay = generate_mysql(candidate1, candidate2, len(observed))
    return {"status": "sat" if replay == observed else "model-replay-failed", "exact_bucket_replay": replay == observed}


def solve_lcg(observed: list[int], spec: LcgSpec, timeout_ms: int) -> dict[str, Any]:
    try:
        import z3
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with z3-solver") from error

    initial = z3.BitVec(f"initial_{spec.name.replace('-', '_')}", spec.width)
    state = initial
    solver = z3.Solver()
    solver.set(timeout=max(1, int(timeout_ms)))
    modulus = z3.BitVecVal(900, spec.width)
    for value in observed:
        state = (
            state * z3.BitVecVal(spec.multiplier, spec.width)
            + z3.BitVecVal(spec.increment, spec.width)
        )
        output = z3.LShR(state, spec.output_shift)
        solver.add(z3.URem(output, modulus) == int(value) - 100)
    outcome = solver.check()
    if outcome == z3.unknown:
        return {"status": "unknown", "reason": solver.reason_unknown()}
    if outcome != z3.sat:
        return {"status": "unsat"}
    candidate = solver.model().eval(initial).as_long()
    replay = generate_lcg(candidate, len(observed), spec)
    return {
        "status": "sat" if replay == observed else "model-replay-failed",
        "exact_bucket_replay": replay == observed,
    }


def solve_park_miller(observed: list[int]) -> dict[str, Any]:
    """Vectorized exact inversion from the first modulo-900 output."""
    modulus = 2_147_483_647
    target = int(observed[0]) - 100
    states = np.arange(target, modulus, 900, dtype=np.int64)
    states = states[(states > 0) & (states < modulus)]
    for value in observed[1:]:
        states = (states * 16_807) % modulus
        states = states[(states % 900) == int(value) - 100]
        if not len(states):
            return {"status": "unsat", "first_output_state_candidates": int((modulus - target) // 900)}
    return {
        "status": "sat" if len(states) else "unsat",
        "exact_state_candidates": int(len(states)),
        "state_persisted": False,
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
    parser.add_argument("--prelude", default=",".join(map(str, DEFAULT_PRELUDE)))
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_lcg_state_recovery.json"),
    )
    args = parser.parse_args()
    segment = load_segment(args.discovery.resolve(), args.process_start)
    discovery_payload = json.loads(args.discovery.resolve().read_text(encoding="utf-8"))
    all_records = sorted(discovery_payload.get("records") or [], key=lambda item: str(item.get("created_at") or ""))
    backend_observed = [int(seed) for row in all_records for seed in row.get("seeds") or []]
    prelude = [int(value) for value in args.prelude.split(",") if value.strip()]
    observed = prelude + [int(seed) for row in segment for seed in row["seeds"]]
    variants = {
        spec.name: solve_lcg(observed, spec, int(args.timeout_seconds * 1_000))
        for spec in SPECS
    }
    variants["park-miller31-fullword"] = solve_park_miller(observed)
    variants["postgres-erand48-double"] = solve_erand48(
        backend_observed, int(args.timeout_seconds * 1_000)
    )
    variants["mysql-rand-two-state"] = solve_mysql(
        backend_observed, int(args.timeout_seconds * 1_000)
    )
    exact = [name for name, result in variants.items() if result.get("status") == "sat"]
    conclusive = sum(result.get("status") in {"sat", "unsat"} for result in variants.values())
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "common-lcg-direct-state-recovery",
        "split": {
            "fixed_discovery_total": 20,
            "process_segment_tasks": len(segment),
            "observed_outputs": len(observed),
            "backend_discovery_outputs": len(backend_observed),
            "holdout_opened": False,
        },
        "search": {
            "candidates_tested": conclusive,
            "discovery_exact_candidates": len(exact),
            "exact_candidates": exact,
            "families": variants,
        },
        "assumptions": [
            "one generator output per published seed",
            "no hidden duplicate redraw or interleaved consumer",
            "Java nextInt rejection omitted; its per-draw probability at bound 900 is below 4e-7",
            "database variants use all 20 Discovery tasks without the validator restart prelude",
        ],
        "safety": {
            "discovery_only": True,
            "holdout_opened": False,
            "network_reads": False,
            "submission_writes": False,
            "recovered_state_persisted": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "conclusive_variants": conclusive,
                "discovery_exact_candidates": len(exact),
                "statuses": {name: value["status"] for name, value in variants.items()},
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
