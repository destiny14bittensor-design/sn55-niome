#!/usr/bin/env python3
"""Project public partial shuffles to Fisher--Yates bits forced in every completion.

Each row is solved independently.  A bit is reported only when the partial
endpoint-domain order makes the opposite polarity UNSAT.  The output contains
no endpoint strings, no MT state, and no sealed seed labels.  It is intended as
a compact, sound input to the joint MT/rejection-alignment solver.
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
    from tools.preseed_mt_xorsat_joint import (
        Cnf,
        add_observed_domain_subsequence,
        add_observed_subsequence,
        add_shuffle_network,
        interval_bits,
        matches_task_selector,
    )
    from tools.preseed_shuffle_prefix_domains import uid_aware_sequences
except ModuleNotFoundError:
    from preseed_mt_cpsat_joint import atomic_json
    from preseed_mt_xorsat_joint import (
        Cnf,
        add_observed_domain_subsequence,
        add_observed_subsequence,
        add_shuffle_network,
        interval_bits,
        matches_task_selector,
    )
    from preseed_shuffle_prefix_domains import uid_aware_sequences


def project_row(
    row: dict[str, Any],
    *,
    initial_time_limit: float = 30.0,
    per_query_time_limit: float,
    threads: int,
    max_bits: int = 0,
    enumeration_models: int = 0,
) -> dict[str, Any]:
    try:
        from pycryptosat import Solver
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with pycryptosat") from error

    initial, observed, _exact = uid_aware_sequences(row)
    size = len(initial)
    initial_solver = Solver(
        verbose=0,
        time_limit=max(0.1, float(initial_time_limit)),
        threads=max(1, int(threads)),
    )
    query_solver = Solver(
        verbose=0,
        time_limit=max(0.1, float(per_query_time_limit)),
        threads=max(1, int(threads)),
    )

    class MirroredSolver:
        def add_clause(self, values: list[int]) -> None:
            initial_solver.add_clause(values)
            query_solver.add_clause(values)

        def add_xor_clause(self, values: list[int], rhs: bool) -> None:
            initial_solver.add_xor_clause(values, rhs)
            query_solver.add_xor_clause(values, rhs)

    cnf = Cnf(MirroredSolver())
    choices = [
        [cnf.new() for _ in range(interval_bits(maximum))]
        for maximum in range(size - 1, 0, -1)
    ]
    output = add_shuffle_network(cnf, choices, initial)
    if size - len(observed) <= 12:
        add_observed_subsequence(cnf, output, initial, observed)
        automaton_encoding = "omitted-token-subset"
        automaton_states = automaton_edges = 0
    else:
        automaton_states, automaton_edges = add_observed_domain_subsequence(
            cnf, output, initial, observed
        )
        automaton_encoding = "linear-domain-subsequence"
    build_seconds = time.monotonic()
    status, model = initial_solver.solve()
    build_seconds = time.monotonic() - build_seconds
    if status is not True:
        return {
            "task_id": str(row["task_id"]),
            "status": "unsat" if status is False else "unknown",
            "shuffle_size": size,
            "observed_positions": len(observed),
            "missing_positions": size - len(observed),
            "forced_bits": [],
            "bits_tested": 0,
            "bits_unknown": 0,
            "build_and_initial_solve_seconds": round(build_seconds, 6),
            "automaton_states": automaton_states,
            "automaton_edges": automaton_edges,
            "automaton_encoding": automaton_encoding,
            "variables": cnf.next_variable - 1,
            "cnf_clauses": cnf.clauses,
        }

    selected_bits = [
        (draw, bit_offset, variable)
        for draw, bits in enumerate(choices)
        for bit_offset, variable in enumerate(bits)
    ]
    stop = max(0, int(max_bits))
    if stop:
        selected_bits = selected_bits[:stop]
    seen = {
        variable: {1 if bool(model[variable]) else 0}
        for _draw, _bit_offset, variable in selected_bits
    }
    models_enumerated = 1
    for _ in range(max(0, int(enumeration_models)) - 1):
        block = [
            -variable if bool(model[variable]) else variable
            for bits in choices
            for variable in bits
        ]
        initial_solver.add_clause(block)
        alternate_status, alternate_model = initial_solver.solve()
        if alternate_status is not True:
            break
        model = alternate_model
        models_enumerated += 1
        for _draw, _bit_offset, variable in selected_bits:
            seen[variable].add(1 if bool(model[variable]) else 0)

    forced: list[dict[str, int]] = []
    tested = unknown = queries_attempted = 0
    started = time.monotonic()
    for draw, bit_offset, variable in selected_bits:
        tested += 1
        if len(seen[variable]) > 1:
            continue
        witness_value = next(iter(seen[variable]))
        opposite = -variable if witness_value else variable
        alternate, _alternate_model = query_solver.solve(assumptions=[opposite])
        queries_attempted += 1
        if alternate is False:
            forced.append(
                {"draw": draw, "bit": bit_offset, "value": witness_value}
            )
        elif alternate is None:
            unknown += 1
    return {
        "task_id": str(row["task_id"]),
        "status": "complete" if not (stop and tested >= stop) else "partial",
        "shuffle_size": size,
        "observed_positions": len(observed),
        "missing_positions": size - len(observed),
        "forced_bits": forced,
        "bits_tested": tested,
        "bits_forced": len(forced),
        "bits_unknown": unknown,
        "queries_attempted": queries_attempted,
        "models_enumerated": models_enumerated,
        "bits_varied_by_enumeration": sum(
            len(seen[variable]) > 1 for _draw, _offset, variable in selected_bits
        ),
        "projection_seconds": round(time.monotonic() - started, 6),
        "build_and_initial_solve_seconds": round(build_seconds, 6),
        "automaton_states": automaton_states,
        "automaton_edges": automaton_edges,
        "automaton_encoding": automaton_encoding,
        "variables": cnf.next_variable - 1,
        "cnf_clauses": cnf.clauses,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--rounds", type=int, default=0, help="0 keeps all selected rows")
    parser.add_argument("--initial-time-limit", type=float, default=30.0)
    parser.add_argument("--per-query-time-limit", type=float, default=1.0)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--max-bits", type=int, default=0, help="Diagnostic cap; 0 tests all")
    parser.add_argument(
        "--enumeration-models",
        type=int,
        default=0,
        help="Enumerate up to N distinct shuffle completions before UNSAT queries.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_shuffle_forced_bits.json"),
    )
    args = parser.parse_args()
    payload = json.loads(args.constraints.read_text(encoding="utf-8"))
    rows = [
        row
        for row in payload.get("rounds") or []
        if row.get("seed_label_partition") == "discovery"
        and matches_task_selector(str(row["task_id"]), args.task)
    ]
    if args.rounds > 0:
        rows = rows[: int(args.rounds)]
    results = [
        project_row(
            row,
            initial_time_limit=args.initial_time_limit,
            per_query_time_limit=args.per_query_time_limit,
            threads=args.threads,
            max_bits=args.max_bits,
            enumeration_models=args.enumeration_models,
        )
        for row in rows
    ]
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "public-partial-shuffle-forced-choice-bits",
        "summary": {
            "rows": len(results),
            "complete_rows": sum(row.get("status") == "complete" for row in results),
            "bits_tested": sum(int(row.get("bits_tested") or 0) for row in results),
            "bits_forced": sum(int(row.get("bits_forced") or 0) for row in results),
            "bits_unknown": sum(int(row.get("bits_unknown") or 0) for row in results),
        },
        "rounds": results,
        "safety": {
            "discovery_seed_labels_read": False,
            "holdout_opened": False,
            "endpoint_strings_persisted": False,
            "network_reads": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
