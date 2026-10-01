#!/usr/bin/env python3
"""Bind exact shuffle-prefix domains and seed labels to one NumPy MT stream.

This is the lightweight counterpart to ``preseed_mt_cpsat_joint.py``.  It
still consumes every Fisher--Yates draw and models ``rk_interval`` rejection,
but it does not reconstruct a 256-element symbolic pool.  Only prefix choice
domains that were proved exact by ``preseed_shuffle_prefix_domains.py`` are
applied.  This makes multi-round feasibility tests practical while retaining
the stream alignment needed to reach each round's three seed outputs.

SAT is only a compatibility result.  A generator is not recovered unless one
state predicts an excluded Discovery suffix exactly and then passes sealed
Holdout.  This tool never reads Holdout labels or persists a state.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

try:
    from tools.preseed_mt_cpsat_joint import (
        SymbolicMtStream,
        add_bounded_draw,
        atomic_json,
    )
except ModuleNotFoundError:
    from preseed_mt_cpsat_joint import SymbolicMtStream, add_bounded_draw, atomic_json


def stream_rows(
    payload: dict[str, Any], limit: int, start: int = 0
) -> list[dict[str, Any]]:
    """Chronological public shuffle rows, with labels only where Discovery allows."""
    rows = [
        row
        for row in payload.get("rounds") or []
        if int(row.get("shuffle_size") or 0) > 1
    ]
    offset = max(0, int(start))
    return rows[offset : offset + max(1, int(limit))]


def prefix_by_task(payload: dict[str, Any]) -> dict[str, dict[int, list[int]]]:
    result: dict[str, dict[int, list[int]]] = {}
    for row in payload.get("rounds") or []:
        domains: dict[int, list[int]] = {}
        for step in row.get("prefix") or []:
            domains[int(step["shuffle_index"])] = [
                int(value) for value in step["choice_domain"]
            ]
        result[str(row["task_id"])] = domains
    return result


def tuples_by_task(
    payload: dict[str, Any] | None,
) -> dict[str, tuple[list[int], list[list[int]]]]:
    result: dict[str, tuple[list[int], list[list[int]]]] = {}
    for row in (payload or {}).get("rounds") or []:
        indices = [int(value) for value in row.get("choice_indices") or []]
        tuples = [
            [int(value) for value in values]
            for values in row.get("choice_tuples") or []
        ]
        if indices and tuples and all(len(values) == len(indices) for values in tuples):
            result[str(row["task_id"])] = (indices, tuples)
    return result


def build_model(
    cp_model: Any,
    rows: list[dict[str, Any]],
    prefixes: dict[str, dict[int, list[int]]],
    tuple_tables: dict[str, tuple[list[int], list[list[int]]]],
    *,
    prelude: list[int],
    raw_cap: int,
    max_rejections: int,
    min_round_rejections: int | None = None,
    max_round_rejections: int | None = None,
) -> tuple[Any, SymbolicMtStream, Any, int, int, float]:
    model = cp_model.CpModel()
    started = time.monotonic()
    mt = SymbolicMtStream(model, raw_cap, exposed_bits=10)
    low_values = {bits: mt.low_values(bits) for bits in range(1, 11)}
    pointer: Any = model.new_int_var(0, 0, "pointer_start")
    for index, value in enumerate(prelude):
        pointer, _ = add_bounded_draw(
            model,
            low_values,
            pointer,
            maximum=899,
            raw_cap=raw_cap,
            max_rejections=max_rejections,
            name=f"prelude_{index}",
            expected=value - 100,
        )

    domain_count = 0
    tuple_count = 0
    for round_index, row in enumerate(rows):
        domains = prefixes.get(str(row["task_id"]), {})
        tuple_indices, allowed_tuples = tuple_tables.get(str(row["task_id"]), ([], []))
        tuple_index_set = set(tuple_indices)
        tuple_choices: dict[int, Any] = {}
        shuffle_size = int(row.get("shuffle_size") or 256)
        round_rejection_gaps = []
        for index in range(shuffle_size - 1, 0, -1):
            allowed = domains.get(index)
            choice = (
                model.new_int_var(0, index, f"round_{round_index}_tuple_choice_{index}")
                if index in tuple_index_set
                else None
            )
            if choice is not None:
                tuple_choices[index] = choice
            pointer, gap = add_bounded_draw(
                model,
                low_values,
                pointer,
                maximum=index,
                raw_cap=raw_cap,
                max_rejections=max_rejections,
                name=f"round_{round_index}_shuffle_{index}",
                choice=choice,
                allowed_values=allowed,
            )
            round_rejection_gaps.append(gap)
            domain_count += int(allowed is not None)
        if min_round_rejections is not None:
            model.add(sum(round_rejection_gaps) >= int(min_round_rejections))
        if max_round_rejections is not None:
            model.add(sum(round_rejection_gaps) <= int(max_round_rejections))
        if tuple_indices:
            model.add_allowed_assignments(
                [tuple_choices[index] for index in tuple_indices], allowed_tuples
            )
            tuple_count += len(allowed_tuples)
        labels = row.get("discovery_seed_label") or [None, None, None]
        for seed_index, value in enumerate(labels):
            pointer, _ = add_bounded_draw(
                model,
                low_values,
                pointer,
                maximum=899,
                raw_cap=raw_cap,
                max_rejections=max_rejections,
                name=f"round_{round_index}_seed_{seed_index}",
                expected=int(value) - 100 if value is not None else None,
            )
    return model, mt, pointer, domain_count, tuple_count, time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--prefix-report", type=Path, required=True)
    parser.add_argument("--tuple-report", type=Path)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--start-round", type=int, default=0)
    parser.add_argument("--raw-cap", type=int, default=1_200)
    parser.add_argument("--max-rejections", type=int, default=5)
    parser.add_argument("--min-round-rejections", type=int)
    parser.add_argument("--max-round-rejections", type=int)
    parser.add_argument("--time-limit", type=float, default=300.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--prelude", default="654,347,964")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_mt_prefix_cpsat.json"),
    )
    args = parser.parse_args()
    try:
        from ortools.sat.python import cp_model
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with ortools") from error

    constraints = json.loads(args.constraints.read_text(encoding="utf-8"))
    prefix_report = json.loads(args.prefix_report.read_text(encoding="utf-8"))
    rows = stream_rows(constraints, args.rounds, args.start_round)
    prefixes = prefix_by_task(prefix_report)
    tuple_tables = tuples_by_task(
        json.loads(args.tuple_report.read_text(encoding="utf-8"))
        if args.tuple_report
        else None
    )
    # A nonzero window starts from an arbitrary post-twist state immediately
    # before that round's shuffle; the process-restart prelude belongs only to
    # the chronological window beginning at round zero.
    prelude = (
        [int(value) for value in args.prelude.split(",") if value.strip()]
        if max(0, args.start_round) == 0
        else []
    )
    model, mt, pointer, domain_count, tuple_count, build_seconds = build_model(
        cp_model,
        rows,
        prefixes,
        tuple_tables,
        prelude=prelude,
        raw_cap=max(1, args.raw_cap),
        max_rejections=max(0, args.max_rejections),
        min_round_rejections=args.min_round_rejections,
        max_round_rejections=args.max_round_rejections,
    )
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(1.0, args.time_limit)
    solver.parameters.num_search_workers = max(1, args.workers)
    status_code = solver.solve(model)
    status = solver.status_name(status_code).lower()
    consumed = int(solver.value(pointer)) if status in {"optimal", "feasible"} else None
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "numpy-mt19937-prefix-domain-seed-cpsat",
        "summary": {
            "status": status,
            "rounds_modelled": len(rows),
            "start_round_index": max(0, args.start_round),
            "discovery_seed_labels_bound": sum(
                len(row.get("discovery_seed_label") or []) for row in rows
            ),
            "sealed_shuffle_rounds_modelled": sum(
                row.get("seed_label_partition") != "discovery" for row in rows
            ),
            "prefix_choice_domains_bound": domain_count,
            "correlated_choice_tuples_bound": tuple_count,
            "raw_cap": args.raw_cap,
            "max_rejections_per_draw": args.max_rejections,
            "minimum_shuffle_rejections_per_round": args.min_round_rejections,
            "maximum_shuffle_rejections_per_round": args.max_round_rejections,
            "raw_words_consumed_witness": consumed,
            "mt_twists_modelled": mt.twists,
            "build_seconds": round(build_seconds, 6),
            "solve_seconds": round(solver.wall_time, 6),
            "mt19937_state_recovered": False,
            "excluded_discovery_predicted": False,
        },
        "interpretation": (
            "SAT only means the bounded common-stream hypothesis is compatible with the "
            "exact prefix domains and included Discovery labels; it is not a seed prediction."
        ),
        "next_gate": (
            "Increase Discovery rounds and exact choice constraints, then require one state "
            "to predict an excluded Discovery suffix before opening Holdout."
        ),
        "safety": {
            "discovery_only": True,
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
