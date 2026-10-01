#!/usr/bin/env python3
"""Recover PostgreSQL ``random(100,999)`` xoroshiro128** state with XOR-SAT.

PostgreSQL's integer range sampler consumes the top ten output bits and rejects
values 900..1023.  This model jointly solves the 128-bit state and bounded
rejection alignment using Discovery labels only.
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
    from tools.preseed_postgres_cnf_recovery import postgres_step, solve_circuit
    from tools.preseed_postgres_prng_recovery import concrete_step, discovery_triplets
    from tools.preseed_v8_cnf_recovery import CnfCircuit, greater_equal_constant, word_from_model
except ModuleNotFoundError:
    from preseed_mt_cpsat_joint import atomic_json
    from preseed_postgres_cnf_recovery import postgres_step, solve_circuit
    from preseed_postgres_prng_recovery import concrete_step, discovery_triplets
    from preseed_v8_cnf_recovery import CnfCircuit, greater_equal_constant, word_from_model


def add_at_most_one(circuit: CnfCircuit, values: list[int]) -> None:
    for offset, left in enumerate(values):
        for right in values[offset + 1 :]:
            circuit.clauses.append([-left, -right])


def build_raw_outputs(count: int):
    circuit = CnfCircuit()
    initial0 = circuit.variables(64)
    initial1 = circuit.variables(64)
    state0, state1 = initial0, initial1
    top10_outputs = []
    for _ in range(count):
        output, state0, state1 = postgres_step(circuit, state0, state1)
        top10_outputs.append(output[54:64])
    circuit.clauses.append([*initial0, *initial1])
    return circuit, initial0, initial1, top10_outputs


def add_rejection_alignment(
    circuit: CnfCircuit,
    raw_outputs: list[list[int]],
    observed: list[int],
    *,
    max_rejections: int,
    rejection_budget: int,
) -> None:
    start = circuit.variable()
    circuit.clauses.append([start])
    states: dict[int, int] = {0: start}
    rejected_clauses = [greater_equal_constant(bits, 900) for bits in raw_outputs]
    for draw_index, seed in enumerate(observed):
        following: dict[int, int] = {}
        incoming: dict[int, list[int]] = {}
        for rejected, source in states.items():
            edges = []
            for gap in range(max_rejections + 1):
                next_rejected = rejected + gap
                raw_index = draw_index + next_rejected
                if next_rejected > rejection_budget or raw_index >= len(raw_outputs):
                    continue
                edge = circuit.variable()
                destination = following.setdefault(next_rejected, circuit.variable())
                edges.append(edge)
                incoming.setdefault(next_rejected, []).append(edge)
                circuit.clauses.extend(([-edge, source], [-edge, destination]))
                for attempt in range(gap):
                    rejected_index = draw_index + rejected + attempt
                    for clause in rejected_clauses[rejected_index]:
                        circuit.clauses.append([-edge, *clause])
                bucket = int(seed) - 100
                for bit_index, bit in enumerate(raw_outputs[raw_index]):
                    circuit.clauses.append(
                        [-edge, bit if (bucket >> bit_index) & 1 else -bit]
                    )
            circuit.clauses.append([-source, *edges])
            add_at_most_one(circuit, edges)
        for rejected, destination in following.items():
            edges = incoming[rejected]
            circuit.clauses.append([-destination, *edges])
            for edge in edges:
                circuit.clauses.append([-edge, destination])
        states = following
    circuit.clauses.append(list(states.values()))


def replay_range(state0: int, state1: int, count: int) -> list[int]:
    result = []
    while len(result) < count:
        output, state0, state1 = concrete_step(state0, state1)
        value = output >> 54
        if value <= 899:
            result.append(100 + value)
    return result


def solve_incremental(
    observed: list[int],
    *,
    start_outputs: int,
    max_rejections: int,
    rejection_budget: int,
    time_limit: float,
    threads: int,
    backend: str = "cryptosat",
) -> dict[str, Any]:
    checkpoints = []
    started = time.monotonic()
    for constrained in range(min(len(observed), max(1, start_outputs)), len(observed) + 1):
        budget = min(rejection_budget, max(0, constrained * max_rejections))
        circuit, initial0, initial1, raw_outputs = build_raw_outputs(constrained + budget)
        add_rejection_alignment(
            circuit,
            raw_outputs,
            observed[:constrained],
            max_rejections=max_rejections,
            rejection_budget=budget,
        )
        outcome, model, clause_count = solve_circuit(
            circuit,
            [],
            backend=backend,
            time_limit=time_limit,
            threads=threads,
        )
        status = "sat" if outcome is True else "unsat" if outcome is False else "unknown"
        checkpoint = {
            "constrained_outputs": constrained,
            "rejection_budget": budget,
            "status": status,
            "elapsed_seconds": round(time.monotonic() - started, 6),
        }
        checkpoints.append(checkpoint)
        common = {
            "status": status,
            "constrained_outputs": constrained,
            "variables": circuit.top,
            "clauses": clause_count,
            "xor_clauses": len(circuit.xor_clauses),
            "checkpoints": checkpoints,
        }
        if outcome is not True or not model:
            return common
        state0 = word_from_model(model, initial0)
        state1 = word_from_model(model, initial1)
        replay = replay_range(state0, state1, len(observed))
        checkpoint["constrained_prefix_exact"] = replay[:constrained] == observed[:constrained]
        checkpoint["unseen_suffix_exact"] = replay[constrained:] == observed[constrained:]
        checkpoint["unseen_suffix_position_matches"] = sum(
            left == right for left, right in zip(replay[constrained:], observed[constrained:])
        )
        if replay == observed:
            return {**common, "full_stream_exact": True, "state_recovered": True}
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--start-outputs", type=int, default=12)
    parser.add_argument("--max-rejections", type=int, default=5)
    parser.add_argument("--rejection-budget", type=int, default=30)
    parser.add_argument("--time-limit", type=float, default=300.0)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--backend", choices=("cryptosat", "glucose"), default="cryptosat")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_postgres_range_cnf.json"),
    )
    args = parser.parse_args()
    triplets = discovery_triplets(args.discovery.resolve())
    observed = [seed for _task_id, seeds in triplets for seed in seeds]
    result = solve_incremental(
        observed,
        start_outputs=max(1, args.start_outputs),
        max_rejections=max(0, args.max_rejections),
        rejection_budget=max(0, args.rejection_budget),
        time_limit=max(1.0, args.time_limit),
        threads=max(1, args.threads),
        backend=args.backend,
    )
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "postgresql-xoroshiro128ss-integer-range-cnf",
        "split": {
            "discovery_tasks": len(triplets),
            "discovery_outputs": len(observed),
            "holdout_opened": False,
        },
        "result": result,
        "summary": {
            "status": result.get("status"),
            "constrained_outputs": result.get("constrained_outputs"),
            "full_stream_exact": result.get("full_stream_exact") is True,
            "state_recovered": result.get("state_recovered") is True,
        },
        "assumptions": [
            "one persistent PostgreSQL backend PRNG state",
            "one random(100, 999) call per published seed",
            "no hidden consumer except range-sampler rejection",
        ],
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
