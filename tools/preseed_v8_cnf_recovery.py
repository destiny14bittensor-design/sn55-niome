#!/usr/bin/env python3
"""Recover/reject a consecutive V8 xorshift128+ stream with a CNF SAT model.

The existing bit-vector solvers time out on the carry in ``state0 + state1``.
This implementation independently bit-blasts the xorshift transition, the
64-bit ripple-carry adder, and the exact ``floor(Math.random() * 900)`` bucket
bounds into CNF.  Discovery output constraints are added incrementally.  No
Holdout label, credential, network endpoint, or submission channel is read.

Run through ``uv run --with python-sat`` because PySAT is intentionally not a
production dependency.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

try:
    from tools.preseed_v8_state_recovery import (
        DEFAULT_PRELUDE,
        DEFAULT_PROCESS_START,
        bin_bounds,
        generate_bins,
        load_segment,
    )
except ModuleNotFoundError:
    from preseed_v8_state_recovery import (
        DEFAULT_PRELUDE,
        DEFAULT_PROCESS_START,
        bin_bounds,
        generate_bins,
        load_segment,
    )


class CnfCircuit:
    """Small Tseitin circuit builder using positive variable identifiers."""

    def __init__(self) -> None:
        self.top = 0
        self.clauses: list[list[int]] = []
        self.xor_clauses: list[tuple[list[int], bool]] = []
        self.false = self.variable()
        self.clauses.append([-self.false])

    def variable(self) -> int:
        self.top += 1
        return self.top

    def variables(self, count: int) -> list[int]:
        return [self.variable() for _ in range(count)]

    def xor2(self, left: int, right: int) -> int:
        output = self.variable()
        self.xor_clauses.append(([left, right, output], False))
        return output

    def xor3(self, left: int, middle: int, right: int) -> int:
        output = self.variable()
        self.xor_clauses.append(([left, middle, right, output], False))
        return output

    def majority(self, left: int, middle: int, right: int) -> int:
        output = self.variable()
        self.clauses.extend(
            (
                [-left, -middle, output],
                [-left, -right, output],
                [-middle, -right, output],
                [left, middle, -output],
                [left, right, -output],
                [middle, right, -output],
            )
        )
        return output


def xor_optional(circuit: CnfCircuit, values: list[int]) -> int:
    if not values:
        return circuit.false
    result = values[0]
    for value in values[1:]:
        result = circuit.xor2(result, value)
    return result


def v8_step(circuit: CnfCircuit, state0: list[int], state1: list[int]) -> tuple[list[int], list[int]]:
    """Return the next xorshift128 state; words are LSB first."""
    shifted_left = [
        xor_optional(circuit, [state0[index], state0[index - 23]] if index >= 23 else [state0[index]])
        for index in range(64)
    ]
    shifted_right = [
        xor_optional(
            circuit,
            [shifted_left[index], shifted_left[index + 17]]
            if index + 17 < 64
            else [shifted_left[index]],
        )
        for index in range(64)
    ]
    with_new0 = [circuit.xor2(shifted_right[index], state1[index]) for index in range(64)]
    new1 = [
        xor_optional(
            circuit,
            [with_new0[index], state1[index + 26]]
            if index + 26 < 64
            else [with_new0[index]],
        )
        for index in range(64)
    ]
    return state1, new1


def add_words(circuit: CnfCircuit, left: list[int], right: list[int]) -> list[int]:
    carry = circuit.false
    output = []
    for left_bit, right_bit in zip(left, right):
        output.append(circuit.xor3(left_bit, right_bit, carry))
        carry = circuit.majority(left_bit, right_bit, carry)
    return output


def mismatch_literal(variable: int, expected: int) -> int:
    return variable if expected == 0 else -variable


def greater_equal_constant(bits: list[int], constant: int) -> list[list[int]]:
    """CNF for the unsigned LSB-first word being >= a constant."""
    clauses = []
    for index in range(len(bits) - 1, -1, -1):
        if (constant >> index) & 1:
            prefix = [
                mismatch_literal(bits[higher], (constant >> higher) & 1)
                for higher in range(len(bits) - 1, index, -1)
            ]
            clauses.append([*prefix, bits[index]])
    return clauses


def less_equal_constant(bits: list[int], constant: int) -> list[list[int]]:
    """CNF for the unsigned LSB-first word being <= a constant."""
    clauses = []
    for index in range(len(bits) - 1, -1, -1):
        if ((constant >> index) & 1) == 0:
            prefix = [
                mismatch_literal(bits[higher], (constant >> higher) & 1)
                for higher in range(len(bits) - 1, index, -1)
            ]
            clauses.append([*prefix, -bits[index]])
    return clauses


def bucket_clauses(numerator_bits: list[int], observed: int) -> list[list[int]]:
    low, high = bin_bounds(observed, len(numerator_bits))
    return [
        *greater_equal_constant(numerator_bits, low),
        *less_equal_constant(numerator_bits, high - 1),
    ]


def build_stream_circuit(output_count: int) -> tuple[CnfCircuit, list[int], list[int], list[list[int]]]:
    circuit = CnfCircuit()
    initial0 = circuit.variables(64)
    initial1 = circuit.variables(64)
    state0, state1 = initial0, initial1
    numerators = []
    for _ in range(output_count):
        state0, state1 = v8_step(circuit, state0, state1)
        summed = add_words(circuit, state0, state1)
        numerators.append(summed[11:64])
    # V8's all-zero state is invalid.
    circuit.clauses.append([*initial0, *initial1])
    return circuit, initial0, initial1, numerators


def word_from_model(model: tuple[Any, ...], bits: list[int]) -> int:
    return sum((1 << index) for index, variable in enumerate(bits) if model[variable])


def solve_incremental(
    observed: list[int], *, start_outputs: int = 8, time_limit: float = 120.0
) -> dict[str, Any]:
    try:
        from pycryptosat import Solver
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with pycryptosat") from error

    if not observed:
        raise ValueError("at least one output is required")
    start_outputs = min(len(observed), max(1, int(start_outputs)))
    checkpoints = []
    started = time.monotonic()
    for constrained in range(start_outputs, len(observed) + 1):
        circuit, initial0, initial1, numerators = build_stream_circuit(constrained)
        bucket_constraints = [
            clause
            for numerator, value in zip(numerators, observed[:constrained])
            for clause in bucket_clauses(numerator, value)
        ]
        solver = Solver(threads=4)
        solver.add_clauses(circuit.clauses)
        solver.add_clauses(bucket_constraints)
        for variables, rhs in circuit.xor_clauses:
            solver.add_xor_clause(variables, rhs)
        outcome, model = solver.solve(time_limit=max(0.001, float(time_limit)))
        elapsed = time.monotonic() - started
        checkpoint = {
            "constrained_outputs": constrained,
            "status": "sat" if outcome is True else "unsat" if outcome is False else "unknown",
            "elapsed_seconds": round(elapsed, 6),
        }
        checkpoints.append(checkpoint)
        if outcome is None:
            return {
                "status": "unknown",
                "reason": "time-limit-exhausted",
                "constrained_outputs": constrained,
                "variables": circuit.top,
                "clauses": len(circuit.clauses) + len(bucket_constraints),
                "xor_clauses": len(circuit.xor_clauses),
                "checkpoints": checkpoints,
            }
        if outcome is False:
            return {
                "status": "unsat",
                "constrained_outputs": constrained,
                "variables": circuit.top,
                "clauses": len(circuit.clauses) + len(bucket_constraints),
                "xor_clauses": len(circuit.xor_clauses),
                "checkpoints": checkpoints,
            }
        assert model is not None
        recovered0 = word_from_model(model, initial0)
        recovered1 = word_from_model(model, initial1)
        replay, _ = generate_bins(recovered0, recovered1, len(observed), "current-sum53")
        checkpoint["constrained_prefix_exact"] = replay[:constrained] == observed[:constrained]
        checkpoint["unseen_suffix_exact"] = replay[constrained:] == observed[constrained:]
        checkpoint["unseen_suffix_position_matches"] = sum(
            left == right for left, right in zip(replay[constrained:], observed[constrained:])
        )
        if replay == observed:
            return {
                "status": "sat",
                "constrained_outputs": constrained,
                "full_stream_exact": True,
                "variables": circuit.top,
                "clauses": len(circuit.clauses) + len(bucket_constraints),
                "xor_clauses": len(circuit.xor_clauses),
                "checkpoints": checkpoints,
            }
    raise AssertionError("unreachable")


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
    parser.add_argument("--start-outputs", type=int, default=8)
    parser.add_argument("--time-limit", type=float, default=120.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_v8_cnf_recovery.json"),
    )
    args = parser.parse_args()
    segment = load_segment(args.discovery.resolve(), args.process_start)
    prelude = [int(value) for value in args.prelude.split(",") if value.strip()]
    observed = prelude + [int(seed) for row in segment for seed in row["seeds"]]
    result = solve_incremental(
        observed,
        start_outputs=args.start_outputs,
        time_limit=args.time_limit,
    )
    exact = result.get("status") == "sat" and result.get("full_stream_exact") is True
    conclusive = result.get("status") in {"sat", "unsat"}
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "v8-xorshift128plus-cnf-state-recovery",
        "split": {
            "fixed_discovery_total": 20,
            "process_segment_tasks": len(segment),
            "observed_outputs": len(observed),
            "holdout_opened": False,
        },
        "search": {
            "candidates_tested": int(conclusive),
            "discovery_exact_candidates": int(exact),
            "families": {
                "v8-current-sum53-exact-buckets-cnf": {
                    "tested": int(conclusive),
                    "status": result.get("status"),
                }
            },
            "result": result,
        },
        "assumptions": [
            "one Math.random call per published seed",
            "no hidden duplicate draw or interleaved Math.random consumer",
            "one persistent V8 state across restart prelude and 16 Discovery tasks",
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
                "status": result.get("status"),
                "constrained_outputs": result.get("constrained_outputs"),
                "discovery_exact_candidates": int(exact),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
