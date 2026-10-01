#!/usr/bin/env python3
"""Test PostgreSQL xoroshiro128** seed-generation hypotheses on Discovery only.

Models the official PostgreSQL PRNG transition and both plausible SQL shapes:
``floor(random() * 900) + 100`` and ``random(100, 999)``.  The latter uses
PostgreSQL's exact top-bit rejection sampler.  No recovered state is persisted.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

try:
    from tools.preseed_mt_cpsat_joint import atomic_json
except ModuleNotFoundError:
    from preseed_mt_cpsat_joint import atomic_json


MASK64 = (1 << 64) - 1


def discovery_triplets(path: Path) -> list[tuple[str, list[int]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = []
    for row in sorted(payload.get("records") or [], key=lambda item: str(item.get("created_at") or "")):
        seeds = [int(value) for value in row.get("seeds") or []]
        if len(seeds) == 3 and all(100 <= value <= 999 for value in seeds):
            result.append((str(row["task_id"]), seeds))
    return result


def concrete_step(s0: int, s1: int) -> tuple[int, int, int]:
    def rol(value: int, amount: int) -> int:
        return ((value << amount) | (value >> (64 - amount))) & MASK64

    sx = (s1 ^ s0) & MASK64
    output = (rol((s0 * 5) & MASK64, 7) * 9) & MASK64
    return output, (rol(s0, 24) ^ sx ^ ((sx << 16) & MASK64)) & MASK64, rol(sx, 37)


def symbolic_step(z3: Any, s0: Any, s1: Any) -> tuple[Any, Any, Any]:
    sx = s1 ^ s0
    output = z3.RotateLeft(s0 * 5, 7) * 9
    return output, z3.RotateLeft(s0, 24) ^ sx ^ (sx << 16), z3.RotateLeft(sx, 37)


def select_state(z3: Any, selector: Any, states: list[tuple[Any, Any]]) -> tuple[Any, Any]:
    left, right = states[-1]
    for index in range(len(states) - 2, -1, -1):
        left = z3.If(selector == index, states[index][0], left)
        right = z3.If(selector == index, states[index][1], right)
    return left, right


def advance_hidden(z3: Any, solver: Any, s0: Any, s1: Any, maximum: int, name: str):
    if maximum <= 0:
        return s0, s1
    states = [(s0, s1)]
    for _ in range(maximum):
        _output, s0, s1 = symbolic_step(z3, s0, s1)
        states.append((s0, s1))
    skipped = z3.Int(name)
    solver.add(skipped >= 0, skipped <= maximum)
    return select_state(z3, skipped, states)


def add_double_floor(z3: Any, solver: Any, s0: Any, s1: Any, seed: int):
    output, following0, following1 = symbolic_step(z3, s0, s1)
    top52 = z3.LShR(output, 12)
    scaled = top52 * 900
    bucket = int(seed) - 100
    solver.add(z3.UGE(scaled, bucket << 52))
    solver.add(z3.ULT(scaled, (bucket + 1) << 52))
    return following0, following1


def add_integer_range(
    z3: Any,
    solver: Any,
    s0: Any,
    s1: Any,
    seed: int,
    max_rejections: int,
    name: str,
):
    bucket = int(seed) - 100
    branches = []
    states = []
    rejected = []
    current0, current1 = s0, s1
    for attempt in range(max_rejections + 1):
        output, current0, current1 = symbolic_step(z3, current0, current1)
        top10 = z3.Extract(63, 54, output)
        states.append((current0, current1))
        conditions = [top10 == bucket]
        conditions.extend(value > 899 for value in rejected)
        branches.append(z3.And(*conditions))
        rejected.append(top10)
    choice = z3.Int(name)
    solver.add(choice >= 0, choice <= max_rejections)
    solver.add(z3.Or(*[z3.And(choice == index, branch) for index, branch in enumerate(branches)]))
    return select_state(z3, choice, states)


def solve_mode(
    z3: Any,
    triplets: list[tuple[str, list[int]]],
    *,
    mode: str,
    max_rejections: int,
    between_task_hidden_max: int,
    timeout_ms: int,
) -> dict[str, Any]:
    solver = z3.Solver()
    solver.set(timeout=max(1, timeout_ms))
    s0 = z3.BitVec(f"{mode}_s0", 64)
    s1 = z3.BitVec(f"{mode}_s1", 64)
    solver.add(z3.Or(s0 != 0, s1 != 0))
    checked_values = 0
    consistent_tasks = 0
    first_failure = None
    checks = []
    started = time.monotonic()
    for task_index, (task_id, seeds) in enumerate(triplets):
        if task_index:
            s0, s1 = advance_hidden(
                z3,
                solver,
                s0,
                s1,
                between_task_hidden_max,
                f"{mode}_hidden_{task_index}",
            )
        for seed_index, seed in enumerate(seeds):
            if mode == "double_floor":
                s0, s1 = add_double_floor(z3, solver, s0, s1, seed)
            else:
                s0, s1 = add_integer_range(
                    z3,
                    solver,
                    s0,
                    s1,
                    seed,
                    max_rejections,
                    f"{mode}_reject_{task_index}_{seed_index}",
                )
            checked_values += 1
        result = solver.check()
        status = str(result)
        checks.append({"tasks": task_index + 1, "values": checked_values, "status": status})
        if result == z3.sat:
            consistent_tasks = task_index + 1
            continue
        first_failure = {"task_index": task_index, "task_id": task_id, "status": status}
        break
    final_status = checks[-1]["status"] if checks else "no-data"
    return {
        "mode": mode,
        "status": final_status,
        "tasks_available": len(triplets),
        "consistent_prefix_tasks": consistent_tasks,
        "values_constrained": checked_values,
        "max_rejections_per_integer_draw": max_rejections if mode == "integer_range" else 0,
        "between_task_hidden_max": between_task_hidden_max,
        "first_failure": first_failure,
        "checks": checks,
        "solve_seconds": round(time.monotonic() - started, 6),
        "state_recovered": False,
        "state_persisted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--mode", choices=("double_floor", "integer_range", "both"), default="both")
    parser.add_argument("--max-rejections", type=int, default=5)
    parser.add_argument("--between-task-hidden-max", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_postgres_prng_recovery.json"),
    )
    args = parser.parse_args()
    try:
        import z3
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with z3-solver") from error

    triplets = discovery_triplets(args.discovery.resolve())
    modes = ("double_floor", "integer_range") if args.mode == "both" else (args.mode,)
    results = [
        solve_mode(
            z3,
            triplets,
            mode=mode,
            max_rejections=max(0, args.max_rejections),
            between_task_hidden_max=max(0, args.between_task_hidden_max),
            timeout_ms=int(max(1.0, args.timeout) * 1000),
        )
        for mode in modes
    ]
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "postgresql-xoroshiro128ss-state-recovery",
        "source_model": {
            "state_bits": 128,
            "output": "rotl(s0 * 5, 7) * 9",
            "transition": "PostgreSQL pg_prng.c xoroshiro128ss",
            "double": "top 52 bits / 2^52",
            "integer_range": "top 10 bits with rejection above 899",
        },
        "results": results,
        "summary": {
            "discovery_tasks": len(triplets),
            "full_discovery_compatible_modes": [
                item["mode"]
                for item in results
                if item["status"] == "sat" and item["consistent_prefix_tasks"] == len(triplets)
            ],
            "longest_consistent_prefix_tasks": max(
                (item["consistent_prefix_tasks"] for item in results), default=0
            ),
            "state_recovered": False,
        },
        "safety": {
            "holdout_opened": False,
            "state_persisted": False,
            "network_reads": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
