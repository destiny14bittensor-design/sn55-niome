#!/usr/bin/env python3
"""Sparse-event MT compatibility model for NIOME seed/shuffle observations.

Unlike the full alignment lattice, this model binds only observed seed values
and exact correlated shuffle-prefix choices.  Unobserved accepted draws and
their rejections are collapsed into a monotone cumulative offset constrained
by a planned corridor.  The collapse is a sound over-approximation: SAT is only
a compatibility witness, while UNSAT rejects the selected corridor.
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
        add_choice_tuple_trie,
        add_sparse_mt_raw_bits,
        add_symbolic_raw_bits,
        interval_bits,
    )
except ModuleNotFoundError:
    from preseed_mt_cpsat_joint import atomic_json
    from preseed_mt_xorsat_joint import (
        Cnf,
        add_choice_tuple_trie,
        add_sparse_mt_raw_bits,
        add_symbolic_raw_bits,
        interval_bits,
    )


def corridor_for_rank(plan: dict[str, Any], rank: int) -> dict[int, tuple[int, int]]:
    for item in plan.get("corridors") or []:
        if int(item.get("rank") or 0) != int(rank):
            continue
        return {
            int(draw): (int(bounds[0]), int(bounds[1]))
            for draw, bounds in (item.get("checkpoints") or {}).items()
        }
    raise ValueError(f"corridor rank {rank} is not present in the plan")


def event_rejection_bounds(
    draw: int,
    checkpoints: dict[int, tuple[int, int]],
    rejection_budget: int,
) -> tuple[int, int]:
    """Conservative cumulative-rejection range at an arbitrary accepted draw."""
    previous_lows = [low for point, (low, _high) in checkpoints.items() if point <= draw]
    following_highs = [high for point, (_low, high) in checkpoints.items() if point >= draw]
    low = max(previous_lows, default=0)
    high = min(following_highs, default=int(rejection_budget))
    return max(0, int(low)), min(int(rejection_budget), int(high))


def exactly_one_sequential(cnf: Cnf, literals: list[int]) -> None:
    """Linear-size exactly-one encoding for positive literals."""
    if not literals:
        cnf.add([])
        return
    cnf.add(list(literals))
    if len(literals) == 1:
        return
    prefix = [cnf.new() for _ in range(len(literals) - 1)]
    cnf.add([-literals[0], prefix[0]])
    for index in range(1, len(literals) - 1):
        cnf.add([-literals[index], prefix[index]])
        cnf.add([-prefix[index - 1], prefix[index]])
        cnf.add([-literals[index], -prefix[index - 1]])
    cnf.add([-literals[-1], -prefix[-1]])


def build_sparse_events(
    rows: list[dict[str, Any]],
    tuple_tables: dict[str, list[list[int]]],
    prelude: list[int],
    *,
    include_prefix_tuples: bool = True,
) -> tuple[list[dict[str, Any]], list[tuple[list[int], list[list[int]]]]]:
    """Return sparse raw events and tuple groups over their accepted bit vectors."""
    events: list[dict[str, Any]] = []
    tuple_groups: list[tuple[list[int], list[list[int]]]] = []
    for offset, value in enumerate(prelude):
        events.append(
            {"base": offset, "maximum": 899, "expected": int(value) - 100, "kind": "prelude"}
        )
    for round_index, row in enumerate(rows):
        round_base = len(prelude) + round_index * 258
        tuples = (
            tuple_tables.get(str(row["task_id"])) or []
            if include_prefix_tuples
            else []
        )
        depth = len(tuples[0]) if tuples else 0
        group_indices: list[int] = []
        for prefix_offset in range(depth):
            group_indices.append(len(events))
            events.append(
                {
                    "base": round_base + prefix_offset,
                    "maximum": 255 - prefix_offset,
                    "expected": None,
                    "kind": "shuffle-prefix",
                    "task_id": str(row["task_id"]),
                }
            )
        if group_indices:
            tuple_groups.append((group_indices, tuples))
        labels = row.get("discovery_seed_label") or []
        if len(labels) == 3:
            for seed_offset, value in enumerate(labels):
                events.append(
                    {
                        "base": round_base + 255 + seed_offset,
                        "maximum": 899,
                        "expected": int(value) - 100,
                        "kind": "discovery-seed",
                        "task_id": str(row["task_id"]),
                    }
                )
    events.sort(key=lambda item: int(item["base"]))
    # Sorting does not change order for generated events, but rebuild group
    # indices defensively from task/base identity.
    identity_to_index = {
        (item.get("task_id"), int(item["base"])): index
        for index, item in enumerate(events)
    }
    rebuilt_groups = []
    for old_indices, tuples in tuple_groups:
        original = [
            (events[index].get("task_id"), int(events[index]["base"]))
            for index in old_indices
        ]
        rebuilt_groups.append(([identity_to_index[value] for value in original], tuples))
    return events, rebuilt_groups


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--tuple-report", type=Path, required=True)
    parser.add_argument("--corridor-plan", type=Path, required=True)
    parser.add_argument("--corridor-rank", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--include-sealed-shuffles", action="store_true")
    parser.add_argument("--raw-cap", type=int, default=7600)
    parser.add_argument("--rejection-budget", type=int, default=2200)
    parser.add_argument("--time-limit", type=float, default=120.0)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--mt-encoding", choices=("dense", "sparse"), default="sparse")
    parser.add_argument("--prelude", default="654,347,964")
    parser.add_argument("--omit-prefix-tuples", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_mt_sparse_events.json"),
    )
    args = parser.parse_args()
    try:
        from pycryptosat import Solver
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with pycryptosat") from error

    payload = json.loads(args.constraints.read_text(encoding="utf-8"))
    tuple_payload = json.loads(args.tuple_report.read_text(encoding="utf-8"))
    plan = json.loads(args.corridor_plan.read_text(encoding="utf-8"))
    all_rows = [
        row
        for row in payload.get("rounds") or []
        if int(row.get("shuffle_size") or 0) == 256
    ]
    discovery_rows = [
        row
        for row in all_rows
        if row.get("seed_label_partition") == "discovery"
        and len(row.get("discovery_seed_label") or []) == 3
    ]
    rows = (
        all_rows[: max(1, args.rounds)]
        if args.include_sealed_shuffles
        else discovery_rows[: max(1, args.rounds)]
    )
    tuple_tables = {
        str(row["task_id"]): [list(map(int, values)) for values in row.get("choice_tuples") or []]
        for row in tuple_payload.get("rounds") or []
    }
    prelude = [int(value) for value in args.prelude.split(",") if value.strip()]
    events, tuple_groups = build_sparse_events(
        rows,
        tuple_tables,
        prelude,
        include_prefix_tuples=not args.omit_prefix_tuples,
    )
    checkpoints = corridor_for_rank(plan, args.corridor_rank)
    maximum_raw = max(
        int(event["base"])
        + event_rejection_bounds(
            int(event["base"]) + 1, checkpoints, args.rejection_budget
        )[1]
        for event in events
    )
    raw_cap = max(int(args.raw_cap), maximum_raw + 1)

    started = time.monotonic()
    solver = Solver(verbose=0, time_limit=max(1.0, args.time_limit), threads=max(1, args.threads))
    cnf = Cnf(solver)
    raw_bits = (
        add_sparse_mt_raw_bits(cnf, raw_cap)
        if args.mt_encoding == "sparse"
        else add_symbolic_raw_bits(cnf, raw_cap)
    )
    event_states: list[dict[int, int]] = []
    accepted_bits: list[list[int]] = []
    for event in events:
        base = int(event["base"])
        low, high = event_rejection_bounds(base + 1, checkpoints, args.rejection_budget)
        states = {value: cnf.new() for value in range(low, high + 1)}
        exactly_one_sequential(cnf, list(states.values()))
        bits = [cnf.new() for _ in range(interval_bits(int(event["maximum"])))]
        for rejected, state in states.items():
            raw_index = base + rejected
            if raw_index >= len(raw_bits):
                cnf.add([-state])
                continue
            for bit, output in enumerate(bits):
                cnf.imply_equal(state, output, raw_bits[raw_index][bit])
        expected = event.get("expected")
        if expected is not None:
            for bit, output in enumerate(bits):
                cnf.add([output if (int(expected) >> bit) & 1 else -output])
        event_states.append(states)
        accepted_bits.append(bits)
    monotone_clauses = 0
    for previous, following in zip(event_states, event_states[1:]):
        following_items = sorted(following.items())
        for rejected, state in previous.items():
            allowed = [literal for value, literal in following_items if value >= rejected]
            cnf.add([-state, *allowed])
            monotone_clauses += 1
    tuple_rows = tuple_nodes = 0
    for indices, tuples in tuple_groups:
        depth, nodes = add_choice_tuple_trie(
            cnf, [accepted_bits[index] for index in indices], tuples
        )
        if depth:
            tuple_rows += len(tuples)
            tuple_nodes += nodes
    build_seconds = time.monotonic() - started
    satisfiable, _model = solver.solve()
    solve_seconds = time.monotonic() - started - build_seconds
    status = "sat" if satisfiable is True else "unsat" if satisfiable is False else "unknown"
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "numpy-mt19937-sparse-observed-events",
        "summary": {
            "status": status,
            "rounds_modelled": len(rows),
            "discovery_seed_labels_bound": sum(
                len(row.get("discovery_seed_label") or []) for row in rows
            ),
            "sealed_shuffle_rounds_modelled": sum(
                row.get("seed_label_partition") != "discovery" for row in rows
            ),
            "sealed_seed_labels_opened": 0,
            "sparse_events": len(events),
            "shuffle_prefix_events": sum(event["kind"] == "shuffle-prefix" for event in events),
            "tuple_groups": len(tuple_groups),
            "prefix_tuples_omitted": bool(args.omit_prefix_tuples),
            "prefix_tuple_rows_bound": tuple_rows,
            "prefix_tuple_trie_nodes": tuple_nodes,
            "corridor_rank": args.corridor_rank,
            "corridor_checkpoints": len(checkpoints),
            "raw_cap": raw_cap,
            "variables": cnf.next_variable - 1,
            "cnf_clauses": cnf.clauses,
            "xor_clauses": cnf.xor_clauses,
            "monotone_offset_clauses": monotone_clauses,
            "build_seconds": round(build_seconds, 6),
            "solve_seconds": round(solve_seconds, 6),
            "mt19937_state_recovered": False,
            "excluded_discovery_predicted": False,
        },
        "interpretation": (
            "SAT is a compatibility witness for a sound over-approximation. UNSAT rejects "
            "only this corridor. Promotion requires a unique excluded-row prediction."
        ),
        "safety": {
            "holdout_labels_opened": False,
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
