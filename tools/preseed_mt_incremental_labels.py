#!/usr/bin/env python3
"""Incrementally bind Discovery seed labels to one fixed-path MT19937 state.

The first Discovery shuffle is constrained exactly.  Later rounds initially
add only their exact rejection path and three public seed labels to the same
live solver, preserving learned clauses between rounds.  Concrete replay then
measures how many public shuffles the resulting model actually explains.  No
state, raw stream, sealed label, or predicted seed value is persisted.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

try:
    from tools.preseed_mt_cpsat_joint import atomic_json
    from tools.preseed_mt_fixed_profile import (
        add_fixed_profile,
        concrete_raw_values_from_model,
        corridor_profile,
        profile_raw_indices,
        replay_bounded_draws_with_forbidden,
        validate_model,
    )
    from tools.preseed_mt_xorsat_joint import (
        Cnf,
        SparseMtCnfStream,
        add_choice_tuple_trie,
        add_observed_singleton_traces,
        add_observed_subsequence,
        add_shuffle_network,
        build_draw_layout,
        seed_duplicate_forbidden_values,
    )
    from tools.preseed_shuffle_prefix_domains import uid_aware_sequences
except ModuleNotFoundError:
    from preseed_mt_cpsat_joint import atomic_json
    from preseed_mt_fixed_profile import (
        add_fixed_profile,
        concrete_raw_values_from_model,
        corridor_profile,
        profile_raw_indices,
        replay_bounded_draws_with_forbidden,
        validate_model,
    )
    from preseed_mt_xorsat_joint import (
        Cnf,
        SparseMtCnfStream,
        add_choice_tuple_trie,
        add_observed_singleton_traces,
        add_observed_subsequence,
        add_shuffle_network,
        build_draw_layout,
        seed_duplicate_forbidden_values,
    )
    from preseed_shuffle_prefix_domains import uid_aware_sequences


def seed_labels_replay(
    raw_values: list[int],
    maxima: list[int],
    expected: list[int | None],
    forbidden_values: dict[int, list[int]],
) -> dict[str, Any]:
    replay = replay_bounded_draws_with_forbidden(raw_values, maxima, forbidden_values)
    if replay is None:
        return {"valid": False, "reason": "raw-cap-exhausted", "labels_passed": 0}
    accepted, cursor, rejected = replay
    labels_passed = 0
    for draw, value in enumerate(expected):
        if value is None:
            continue
        if accepted[draw] != int(value):
            return {
                "valid": False,
                "reason": "seed-label-mismatch",
                "failed_draw": draw,
                "labels_passed": labels_passed,
            }
        labels_passed += 1
    return {
        "valid": True,
        "reason": "all-bound-seed-labels-replayed",
        "labels_passed": labels_passed,
        "raw_words_consumed": cursor,
        "rejections": rejected,
    }


def incremental_stage_indices(total_rounds: int, initial_rounds: int) -> list[int]:
    total = max(0, int(total_rounds))
    initial = min(total, max(1, int(initial_rounds))) if total else 0
    return list(range(max(0, initial - 1), total))


def tuple_round_selected(round_number: int, first: int, last: int) -> bool:
    return int(first) > 0 and int(round_number) >= int(first) and (
        int(last) <= 0 or int(round_number) <= int(last)
    )


def balanced_choice_prefix_shards(
    tuples: list[list[int]], shard_count: int, prefix_depth: int
) -> list[list[tuple[int, ...]]]:
    """Partition indivisible choice prefixes into tuple-count-balanced shards."""
    count = max(1, int(shard_count))
    depth = max(1, int(prefix_depth))
    frequencies = Counter(
        tuple(map(int, values[:depth])) for values in tuples if len(values) >= depth
    )
    bins: list[tuple[int, list[tuple[int, ...]]]] = [
        (0, []) for _ in range(count)
    ]
    for prefix, frequency in sorted(
        frequencies.items(), key=lambda item: (-item[1], item[0])
    ):
        index = min(range(count), key=lambda item: (bins[item][0], item))
        total, prefixes = bins[index]
        bins[index] = (total + int(frequency), [*prefixes, prefix])
    return [prefixes for _total, prefixes in bins]


def balanced_first_choice_shards(
    tuples: list[list[int]], shard_count: int
) -> list[list[int]]:
    """Backward-compatible depth-one view used by existing reports/tests."""
    return [
        [prefix[0] for prefix in shard]
        for shard in balanced_choice_prefix_shards(tuples, shard_count, 1)
    ]


def write_checkpoint(path: Path, report: dict[str, Any]) -> None:
    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "numpy-mt19937-incremental-label-checkpoint",
        **report,
        "safety": {
            "holdout_labels_opened": False,
            "sealed_seed_labels_opened": False,
            "state_persisted": False,
            "raw_stream_persisted": False,
            "network_reads": False,
            "submission_writes": False,
        },
    }
    atomic_json(path.resolve(), payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--corridor-plan", type=Path, required=True)
    parser.add_argument("--tuple-report", type=Path)
    parser.add_argument(
        "--tuple-from-round",
        type=int,
        default=0,
        help="Bind correlated shuffle-prefix tuples starting at this 1-based round; 0 disables.",
    )
    parser.add_argument(
        "--tuple-to-round",
        type=int,
        default=0,
        help="Last 1-based round receiving a tuple trie; 0 has no upper bound.",
    )
    parser.add_argument("--tuple-shard-count", type=int, default=1)
    parser.add_argument("--tuple-shard-index", type=int, default=0)
    parser.add_argument("--tuple-shard-prefix-depth", type=int, default=1)
    parser.add_argument(
        "--first-shuffle-encoding",
        choices=("network", "singleton-trace", "none"),
        default="network",
    )
    parser.add_argument("--max-singleton-traces", type=int, default=4)
    parser.add_argument("--corridor-rank", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=16)
    parser.add_argument(
        "--prebuild-rounds",
        type=int,
        default=1,
        help="Expose this many round prefixes before the first solve.",
    )
    parser.add_argument(
        "--initial-bind-rounds",
        type=int,
        default=1,
        help="Bind this many complete round prefixes before the first solve.",
    )
    parser.add_argument("--time-limit", type=float, default=600.0)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--prelude", default="654,347,964")
    parser.add_argument("--checkpoint-output", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_mt_incremental_labels.json"),
    )
    args = parser.parse_args()
    try:
        from pycryptosat import Solver
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with pycryptosat") from error

    payload = json.loads(args.constraints.read_text(encoding="utf-8"))
    plan = json.loads(args.corridor_plan.read_text(encoding="utf-8"))
    tuple_payload = (
        json.loads(args.tuple_report.read_text(encoding="utf-8"))
        if args.tuple_report
        else {}
    )
    tuple_tables = {
        str(row["task_id"]): [list(map(int, values)) for values in row.get("choice_tuples") or []]
        for row in tuple_payload.get("rounds") or []
    }
    discovery_rows = [
        row
        for row in payload.get("rounds") or []
        if row.get("seed_label_partition") == "discovery"
        and len(row.get("discovery_seed_label") or []) == 3
        and int(row.get("shuffle_size") or 0) == 256
    ]
    rows = discovery_rows[: max(1, int(args.rounds))]
    if not rows:
        raise ValueError("no Discovery rows are available")
    prelude = [int(value) for value in args.prelude.split(",") if value.strip()]
    maxima, expected, round_slices, seed_slices = build_draw_layout(
        rows, prelude, "integers-unique"
    )
    full_gaps = corridor_profile(plan, args.corridor_rank)
    if len(full_gaps) < len(maxima):
        raise ValueError("corridor rejection path is shorter than the selected rounds")
    gaps = full_gaps[: len(maxima)]
    accepted_indices, _rejected = profile_raw_indices(gaps)
    raw_cap = accepted_indices[-1] + 1
    forbidden = seed_duplicate_forbidden_values(seed_slices, expected)

    started = time.monotonic()
    solver = Solver(
        verbose=0,
        time_limit=max(1.0, args.time_limit),
        threads=max(1, args.threads),
    )
    cnf = Cnf(solver)
    initial_bind_rounds = min(len(rows), max(1, int(args.initial_bind_rounds)))
    first_stop = int(seed_slices[initial_bind_rounds][1])
    prebuild_rounds = min(len(rows), max(1, int(args.prebuild_rounds)))
    prebuild_stop = int(seed_slices[max(prebuild_rounds, initial_bind_rounds)][1])
    stream = SparseMtCnfStream(cnf)
    raw_bits = stream.ensure(accepted_indices[prebuild_stop - 1] + 1)
    accepted_bits = add_fixed_profile(
        cnf,
        raw_bits,
        maxima,
        expected,
        gaps,
        forbidden,
        draw_stop=first_stop,
    )
    initial, observed, _exact = uid_aware_sequences(rows[0])
    first_choices = accepted_bits[round_slices[0][0] : round_slices[0][1]]
    initial_singleton_traces = 0
    if args.first_shuffle_encoding == "network":
        output = add_shuffle_network(cnf, first_choices, initial)
        add_observed_subsequence(cnf, output, initial, observed)
    elif args.first_shuffle_encoding == "singleton-trace":
        initial_singleton_traces = add_observed_singleton_traces(
            cnf,
            first_choices,
            rows[0],
            max_traces=max(0, int(args.max_singleton_traces)),
        )
    build_seconds = time.monotonic() - started

    stages: list[dict[str, Any]] = []
    tuple_rows_bound = 0
    tuple_nodes_bound = 0
    sharded_tuple_rounds = 0
    selected_tuple_count = 0
    selected_first_choices: list[int] = []
    selected_choice_prefixes: list[list[int]] = []
    active_stop = first_stop
    satisfiable: bool | None = None
    model: list[Any] = []
    for round_index in incremental_stage_indices(len(rows), initial_bind_rounds):
        row = rows[round_index]
        if round_index >= initial_bind_rounds:
            next_stop = int(seed_slices[round_index + 1][1])
            stream.ensure(accepted_indices[next_stop - 1] + 1)
            accepted_bits.extend(
                add_fixed_profile(
                    cnf,
                    raw_bits,
                    maxima,
                    expected,
                    gaps,
                    forbidden,
                    draw_start=active_stop,
                    draw_stop=next_stop,
                )
            )
            active_stop = next_stop
        if (
            tuple_round_selected(
                round_index + 1, args.tuple_from_round, args.tuple_to_round
            )
        ):
            start, stop = round_slices[round_index]
            tuples = tuple_tables.get(str(row["task_id"])) or []
            if not tuples:
                raise ValueError(
                    f"no correlated prefix tuples for task {row['task_id']}"
                )
            if int(args.tuple_shard_count) > 1 and tuples:
                sharded_tuple_rounds += 1
                if sharded_tuple_rounds > 1:
                    raise ValueError(
                        "tuple sharding is exact only for one tuple-bound round; "
                        "use a separate product-shard implementation for multiple rounds"
                    )
                prefix_depth = max(1, int(args.tuple_shard_prefix_depth))
                shards = balanced_choice_prefix_shards(
                    tuples, int(args.tuple_shard_count), prefix_depth
                )
                if not 0 <= int(args.tuple_shard_index) < len(shards):
                    raise ValueError("tuple shard index is outside the shard count")
                allowed = set(shards[int(args.tuple_shard_index)])
                tuples = [
                    values
                    for values in tuples
                    if tuple(map(int, values[:prefix_depth])) in allowed
                ]
                if not tuples:
                    raise ValueError("selected tuple shard is empty")
                selected_choice_prefixes = [list(prefix) for prefix in sorted(allowed)]
                selected_first_choices = sorted({prefix[0] for prefix in allowed})
                selected_tuple_count = len(tuples)
            depth, nodes = add_choice_tuple_trie(
                cnf, accepted_bits[start:stop], tuples
            )
            if depth:
                tuple_rows_bound += len(tuples)
                tuple_nodes_bound += nodes
        solve_started = time.monotonic()
        satisfiable, model = solver.solve()
        solve_seconds = time.monotonic() - solve_started
        status = (
            "sat" if satisfiable is True else "unsat" if satisfiable is False else "unknown"
        )
        replay = {"valid": False, "reason": "no-sat-model", "labels_passed": 0}
        public_replay = {"valid": False, "reason": "no-sat-model", "rows_passed": 0}
        if satisfiable is True:
            raw_values = [
                int(sum((bool(model[var]) << bit) for bit, var in enumerate(bits)))
                for bits in raw_bits
            ]
            replay = seed_labels_replay(
                raw_values,
                maxima[:active_stop],
                expected[:active_stop],
                forbidden,
            )
            public_replay = validate_model(
                raw_values,
                rows[: round_index + 1],
                maxima[:active_stop],
                expected[:active_stop],
                round_slices[: round_index + 1],
                forbidden,
            )
        stages.append(
            {
                "rounds_bound": round_index + 1,
                "last_task_id": str(row["task_id"]),
                "status": status,
                "seed_labels_bound": 3 * (round_index + 1),
                "seed_labels_replayed": int(replay.get("labels_passed") or 0),
                "seed_replay_reason": replay.get("reason"),
                "public_shuffle_rows_passed": int(public_replay.get("rows_passed") or 0),
                "public_replay_reason": public_replay.get("reason"),
                "solve_seconds": round(solve_seconds, 6),
                "variables": cnf.next_variable - 1,
                "cnf_clauses": cnf.clauses,
                "xor_clauses": cnf.xor_clauses,
                "prefix_tuple_rows_bound": tuple_rows_bound,
                "prefix_tuple_trie_nodes": tuple_nodes_bound,
            }
        )
        checkpoint = {
            "summary": {
                "status": status,
                "corridor_rank": int(args.corridor_rank),
                "target_rounds": len(rows),
                "completed_rounds": round_index + 1,
                "seed_labels_bound": 3 * (round_index + 1),
                "public_shuffle_rows_passed": stages[-1]["public_shuffle_rows_passed"],
            },
            "stages": stages,
        }
        if args.checkpoint_output:
            write_checkpoint(args.checkpoint_output, checkpoint)
        if satisfiable is not True:
            break

    completed = int(stages[-1]["rounds_bound"]) if stages else 0
    last_sat = next(
        (stage for stage in reversed(stages) if stage.get("status") == "sat"),
        None,
    )
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "numpy-mt19937-incremental-discovery-labels",
        "summary": {
            "status": stages[-1]["status"] if stages else "not-run",
            "corridor_rank": int(args.corridor_rank),
            "target_rounds": len(rows),
            "completed_rounds": completed,
            "sat_rounds": int(last_sat["rounds_bound"]) if last_sat else 0,
            "seed_labels_bound": stages[-1]["seed_labels_bound"] if stages else 0,
            "sat_seed_labels_bound": last_sat["seed_labels_bound"] if last_sat else 0,
            "seed_labels_replayed": last_sat["seed_labels_replayed"] if last_sat else 0,
            "public_shuffle_rows_passed": (
                last_sat["public_shuffle_rows_passed"] if last_sat else 0
            ),
            "bounded_draws_horizon": len(maxima),
            "fixed_rejections_horizon": sum(gaps),
            "raw_cap": raw_cap,
            "prebuild_rounds": prebuild_rounds,
            "initial_bind_rounds": initial_bind_rounds,
            "first_shuffle_encoding": str(args.first_shuffle_encoding),
            "initial_singleton_traces": initial_singleton_traces,
            "tuple_from_round": int(args.tuple_from_round),
            "tuple_to_round": int(args.tuple_to_round),
            "tuple_shard_count": int(args.tuple_shard_count),
            "tuple_shard_index": int(args.tuple_shard_index),
            "tuple_shard_prefix_depth": int(args.tuple_shard_prefix_depth),
            "selected_tuple_count": selected_tuple_count,
            "selected_first_choices": selected_first_choices,
            "selected_choice_prefixes": selected_choice_prefixes,
            "prefix_tuple_rows_bound": tuple_rows_bound,
            "prefix_tuple_trie_nodes": tuple_nodes_bound,
            "build_seconds": round(build_seconds, 6),
            "total_seconds": round(time.monotonic() - started, 6),
            "mt19937_state_recovered": False,
            "excluded_discovery_predicted": False,
        },
        "stages": stages,
        "interpretation": (
            "SAT means the fixed rejection path can explain the bound Discovery labels. "
            "A generator is not promoted until excluded labels and public shuffles replay."
        ),
        "safety": {
            "holdout_labels_opened": False,
            "sealed_seed_labels_opened": False,
            "state_persisted": False,
            "raw_stream_persisted": False,
            "network_reads": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    if args.checkpoint_output:
        write_checkpoint(
            args.checkpoint_output,
            {"summary": {**report["summary"], "final": True}, "stages": stages},
        )
    print(json.dumps({"output": str(args.output.resolve()), **report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
