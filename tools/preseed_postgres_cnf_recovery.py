#!/usr/bin/env python3
"""Recover or reject a strict PostgreSQL ``random()`` seed stream with CNF.

The circuit exactly bit-blasts PostgreSQL's xoroshiro128** output function,
linear transition, and ``floor(random() * 900) + 100`` bucket bounds.  Only
Discovery labels are read and a recovered state is never persisted.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any

try:
    from tools.preseed_mt_cpsat_joint import atomic_json
    from tools.preseed_postgres_prng_recovery import concrete_step, discovery_triplets
    from tools.preseed_v8_cnf_recovery import (
        CnfCircuit,
        add_words,
        bucket_clauses,
        word_from_model,
    )
except ModuleNotFoundError:
    from preseed_mt_cpsat_joint import atomic_json
    from preseed_postgres_prng_recovery import concrete_step, discovery_triplets
    from preseed_v8_cnf_recovery import CnfCircuit, add_words, bucket_clauses, word_from_model


def xor_word(circuit: CnfCircuit, *words: list[int]) -> list[int]:
    result = list(words[0])
    for word in words[1:]:
        result = [circuit.xor2(left, right) for left, right in zip(result, word)]
    return result


def shift_left(word: list[int], amount: int, false: int) -> list[int]:
    return [false] * amount + word[: 64 - amount]


def rotate_left(word: list[int], amount: int) -> list[int]:
    return [word[(index - amount) % 64] for index in range(64)]


def postgres_step(
    circuit: CnfCircuit, state0: list[int], state1: list[int]
) -> tuple[list[int], list[int], list[int]]:
    times5 = add_words(circuit, state0, shift_left(state0, 2, circuit.false))
    rotated = rotate_left(times5, 7)
    output = add_words(circuit, rotated, shift_left(rotated, 3, circuit.false))
    sx = xor_word(circuit, state1, state0)
    following0 = xor_word(
        circuit,
        rotate_left(state0, 24),
        sx,
        shift_left(sx, 16, circuit.false),
    )
    following1 = rotate_left(sx, 37)
    return output, following0, following1


def build_circuit(count: int):
    circuit = CnfCircuit()
    initial0 = circuit.variables(64)
    initial1 = circuit.variables(64)
    state0, state1 = initial0, initial1
    numerators = []
    for _ in range(count):
        output, state0, state1 = postgres_step(circuit, state0, state1)
        numerators.append(output[12:64])
    circuit.clauses.append([*initial0, *initial1])
    return circuit, initial0, initial1, numerators


def replay_bins(state0: int, state1: int, count: int) -> list[int]:
    result = []
    for _ in range(count):
        output, state0, state1 = concrete_step(state0, state1)
        result.append(100 + ((output >> 12) * 900) // (1 << 52))
    return result


def xor_to_cnf(variables: list[int], rhs: bool) -> list[list[int]]:
    clauses = []
    for assignment in range(1 << len(variables)):
        parity = assignment.bit_count() & 1
        if parity == int(rhs):
            continue
        clauses.append(
            [
                -variable if (assignment >> index) & 1 else variable
                for index, variable in enumerate(variables)
            ]
        )
    return clauses


def solve_circuit(
    circuit: CnfCircuit,
    extra_clauses: list[list[int]],
    *,
    backend: str,
    time_limit: float,
    threads: int,
) -> tuple[bool | None, list[bool] | None, int]:
    if backend == "cryptosat":
        try:
            from pycryptosat import Solver
        except ModuleNotFoundError as error:
            raise RuntimeError("run through uv with --with pycryptosat") from error
        solver = Solver(threads=max(1, threads), time_limit=max(0.001, time_limit))
        solver.add_clauses(circuit.clauses)
        solver.add_clauses(extra_clauses)
        for variables, rhs in circuit.xor_clauses:
            solver.add_xor_clause(variables, rhs)
        outcome, model = solver.solve()
        return outcome, model, len(circuit.clauses) + len(extra_clauses)

    try:
        from pysat.solvers import Solver
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with python-sat") from error
    parity_clauses = [
        clause
        for variables, rhs in circuit.xor_clauses
        for clause in xor_to_cnf(variables, rhs)
    ]
    clauses = [*circuit.clauses, *extra_clauses, *parity_clauses]
    with Solver(name="glucose4", bootstrap_with=clauses) as solver:
        timer = threading.Timer(max(0.001, time_limit), solver.interrupt)
        timer.start()
        try:
            outcome = solver.solve_limited(expect_interrupt=True)
            signed = solver.get_model() if outcome is True else None
        finally:
            timer.cancel()
    if signed is None:
        return outcome, None, len(clauses)
    model = [False] * (circuit.top + 1)
    for literal in signed:
        if abs(literal) <= circuit.top:
            model[abs(literal)] = literal > 0
    return outcome, model, len(clauses)


def solve_incremental(
    observed: list[int], *, start_outputs: int, time_limit: float, threads: int, backend: str = "cryptosat"
) -> dict[str, Any]:
    checkpoints = []
    started = time.monotonic()
    for constrained in range(min(len(observed), max(1, start_outputs)), len(observed) + 1):
        circuit, initial0, initial1, numerators = build_circuit(constrained)
        clauses = [
            clause
            for numerator, value in zip(numerators, observed[:constrained])
            for clause in bucket_clauses(numerator, value)
        ]
        outcome, model, clause_count = solve_circuit(
            circuit,
            clauses,
            backend=backend,
            time_limit=time_limit,
            threads=threads,
        )
        status = "sat" if outcome is True else "unsat" if outcome is False else "unknown"
        checkpoint = {
            "constrained_outputs": constrained,
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
        replay = replay_bins(state0, state1, len(observed))
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
    parser.add_argument("--start-outputs", type=int, default=5)
    parser.add_argument("--time-limit", type=float, default=300.0)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--backend", choices=("cryptosat", "glucose"), default="cryptosat")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_postgres_cnf_recovery.json"),
    )
    args = parser.parse_args()
    triplets = discovery_triplets(args.discovery.resolve())
    observed = [seed for _task_id, seeds in triplets for seed in seeds]
    result = solve_incremental(
        observed,
        start_outputs=args.start_outputs,
        time_limit=max(1.0, args.time_limit),
        threads=max(1, args.threads),
        backend=args.backend,
    )
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "postgresql-xoroshiro128ss-double-floor-cnf",
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
            "one floor(random() * 900) + 100 call per published seed",
            "no hidden or interleaved random() consumer",
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
