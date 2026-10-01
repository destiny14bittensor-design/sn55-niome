#!/usr/bin/env python3
"""Compact Z3 model for the public shuffle trace and one NumPy MT stream.

The CP-SAT prototypes either retain only a short exact prefix or materialise a
large integer pool at every Fisher--Yates step.  This model uses Z3 arrays for
the pool and for symbolic raw-word lookup.  It therefore binds the complete
public endpoint-group subsequence, bounded-draw rejection alignment, and the
Discovery seed labels to one legacy NumPy MT19937 stream.

SAT is compatibility, not recovery.  A state is deliberately never written.
Holdout labels are not read; sealed rounds can only contribute public shuffle
constraints.  Run through ``uv run --with z3-solver``.
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
    from tools.preseed_mt19937_rank_audit import MATRIX_A, M, N
except ModuleNotFoundError:
    from preseed_mt_cpsat_joint import atomic_json
    from preseed_mt19937_rank_audit import MATRIX_A, M, N


def chronological_rows(payload: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    return [
        row
        for row in payload.get("rounds") or []
        if int(row.get("shuffle_size") or 0) > 1
    ][: max(1, int(limit))]


def missing_group_counts(row: dict[str, Any]) -> Counter[str]:
    remaining = Counter(str(value) for value in row["initial_domain_sequence"])
    remaining.subtract(str(value) for value in row["ordered_group_domains"])
    if any(value < 0 for value in remaining.values()):
        raise ValueError("observed group sequence exceeds initial multiset")
    return +remaining


class Z3MtStream:
    def __init__(self, z3: Any, raw_cap: int) -> None:
        self.z3 = z3
        self.state = [z3.BitVec(f"mt0_{index}", 32) for index in range(N)]
        self.position = 0
        self.twists = 0
        self.words = []
        for _ in range(raw_cap):
            self.words.append(self._temper(self._next_state_word()))

    def _twist(self) -> None:
        z3 = self.z3
        result = list(self.state)

        def replace(index: int, source: int, following: int) -> None:
            mixed = (result[index] & z3.BitVecVal(0x80000000, 32)) | (
                result[following] & z3.BitVecVal(0x7FFFFFFF, 32)
            )
            conditional = z3.If(
                (mixed & z3.BitVecVal(1, 32)) == 1,
                z3.BitVecVal(MATRIX_A, 32),
                z3.BitVecVal(0, 32),
            )
            result[index] = result[source] ^ z3.LShR(mixed, 1) ^ conditional

        for index in range(N - M):
            replace(index, index + M, index + 1)
        for index in range(N - M, N - 1):
            replace(index, index + M - N, index + 1)
        replace(N - 1, M - 1, 0)
        self.state = result
        self.position = 0
        self.twists += 1

    def _next_state_word(self) -> Any:
        if self.position >= N:
            self._twist()
        word = self.state[self.position]
        self.position += 1
        return word

    def _temper(self, word: Any) -> Any:
        z3 = self.z3
        value = word ^ z3.LShR(word, 11)
        value = value ^ ((value << 7) & z3.BitVecVal(0x9D2C5680, 32))
        value = value ^ ((value << 15) & z3.BitVecVal(0xEFC60000, 32))
        return value ^ z3.LShR(value, 18)


def interval_mask(maximum: int) -> int:
    mask = int(maximum)
    mask |= mask >> 1
    mask |= mask >> 2
    mask |= mask >> 4
    mask |= mask >> 8
    mask |= mask >> 16
    return mask


def build_model(
    z3: Any,
    rows: list[dict[str, Any]],
    *,
    raw_cap: int,
    max_rejections: int,
    min_round_rejections: int | None,
    max_round_rejections: int | None,
    prelude: list[int],
) -> tuple[Any, Any, Z3MtStream, dict[str, int]]:
    solver = z3.Solver()
    mt = Z3MtStream(z3, raw_cap)
    raw_at = z3.Function("raw_at", z3.IntSort(), z3.BitVecSort(32))
    for index, word in enumerate(mt.words):
        solver.add(raw_at(index) == word)

    pointer: Any = z3.IntVal(0)
    counters = {"draws": 0, "full_group_positions": 0, "missing_positions": 0}

    def bounded_draw(maximum: int, name: str, expected: int | None = None):
        nonlocal pointer
        mask = interval_mask(maximum)
        gap = z3.Int(f"gap_{name}")
        solver.add(gap >= 0, gap <= max_rejections)
        accepted_index = pointer + gap
        solver.add(accepted_index >= 0, accepted_index < raw_cap)
        accepted = raw_at(accepted_index) & z3.BitVecVal(mask, 32)
        solver.add(z3.ULE(accepted, z3.BitVecVal(maximum, 32)))
        if expected is not None:
            solver.add(accepted == z3.BitVecVal(int(expected), 32))
        for attempt in range(max_rejections):
            rejected = raw_at(pointer + attempt) & z3.BitVecVal(mask, 32)
            solver.add(
                z3.Implies(
                    gap > attempt,
                    z3.UGT(rejected, z3.BitVecVal(maximum, 32)),
                )
            )
        pointer = pointer + gap + 1
        solver.add(pointer <= raw_cap)
        counters["draws"] += 1
        return accepted, gap

    for index, value in enumerate(prelude):
        bounded_draw(899, f"prelude_{index}", int(value) - 100)

    for round_index, row in enumerate(rows):
        size = int(row["shuffle_size"])
        if size > 256:
            raise ValueError("8-bit pool encoding supports at most 256 UIDs")
        uid_sort = z3.BitVecSort(8)
        pool = z3.K(uid_sort, z3.BitVecVal(0, 8))
        for uid in range(size):
            pool = z3.Store(pool, z3.BitVecVal(uid, 8), z3.BitVecVal(uid, 8))
        output: list[Any] = [None] * size
        rejection_gaps = []
        for shuffle_index in range(size - 1, 0, -1):
            accepted, gap = bounded_draw(
                shuffle_index, f"round_{round_index}_shuffle_{shuffle_index}"
            )
            rejection_gaps.append(gap)
            choice = z3.Extract(7, 0, accepted)
            selected = z3.Select(pool, choice)
            output[shuffle_index] = selected
            pool = z3.Store(
                pool, choice, z3.Select(pool, z3.BitVecVal(shuffle_index, 8))
            )
        output[0] = z3.Select(pool, z3.BitVecVal(0, 8))
        if min_round_rejections is not None:
            solver.add(z3.Sum(rejection_gaps) >= int(min_round_rejections))
        if max_round_rejections is not None:
            solver.add(z3.Sum(rejection_gaps) <= int(max_round_rejections))

        initial_groups = [str(value) for value in row["initial_domain_sequence"]]
        observed_groups = [str(value) for value in row["ordered_group_domains"]]
        group_names = sorted(set(initial_groups))
        group_id = {name: index for index, name in enumerate(group_names)}
        uid_group = z3.K(uid_sort, z3.IntVal(0))
        for uid, group in enumerate(initial_groups):
            uid_group = z3.Store(
                uid_group, z3.BitVecVal(uid, 8), z3.IntVal(group_id[group])
            )
        observed_at = z3.K(z3.IntSort(), z3.IntVal(0))
        for position, group in enumerate(observed_groups):
            observed_at = z3.Store(
                observed_at, z3.IntVal(position), z3.IntVal(group_id[group])
            )

        missing = missing_group_counts(row)
        consumed: Any = z3.IntVal(0)
        missing_flags = []
        output_group_exprs = []
        for position, uid in enumerate(output):
            is_missing = z3.Bool(f"round_{round_index}_missing_{position}")
            missing_flags.append(is_missing)
            group_value = z3.Select(uid_group, uid)
            output_group_exprs.append(group_value)
            solver.add(
                z3.Implies(
                    z3.Not(is_missing),
                    group_value == z3.Select(observed_at, consumed),
                )
            )
            consumed = consumed + z3.If(is_missing, 0, 1)
        solver.add(consumed == len(observed_groups))
        solver.add(z3.Sum([z3.If(flag, 1, 0) for flag in missing_flags]) == sum(missing.values()))
        for group, count in missing.items():
            gid = z3.IntVal(group_id[group])
            solver.add(
                z3.Sum(
                    [
                        z3.If(z3.And(flag, value == gid), 1, 0)
                        for flag, value in zip(missing_flags, output_group_exprs)
                    ]
                )
                == int(count)
            )
        counters["full_group_positions"] += len(observed_groups)
        counters["missing_positions"] += sum(missing.values())

        labels = row.get("discovery_seed_label") or [None, None, None]
        for seed_index, value in enumerate(labels):
            bounded_draw(
                899,
                f"round_{round_index}_seed_{seed_index}",
                int(value) - 100 if value is not None else None,
            )
    return solver, pointer, mt, counters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--raw-cap", type=int, default=700)
    parser.add_argument("--max-rejections", type=int, default=5)
    parser.add_argument("--min-round-rejections", type=int)
    parser.add_argument("--max-round-rejections", type=int)
    parser.add_argument("--time-limit", type=float, default=300.0)
    parser.add_argument("--prelude", default="654,347,964")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_mt_z3_joint.json"),
    )
    args = parser.parse_args()
    try:
        import z3
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with z3-solver") from error

    payload = json.loads(args.constraints.read_text(encoding="utf-8"))
    rows = chronological_rows(payload, args.rounds)
    started = time.monotonic()
    solver, pointer, mt, counters = build_model(
        z3,
        rows,
        raw_cap=max(1, args.raw_cap),
        max_rejections=max(0, args.max_rejections),
        min_round_rejections=args.min_round_rejections,
        max_round_rejections=args.max_round_rejections,
        prelude=[int(value) for value in args.prelude.split(",") if value.strip()],
    )
    build_seconds = time.monotonic() - started
    solver.set(timeout=max(1, int(args.time_limit * 1000)))
    solve_started = time.monotonic()
    status_value = solver.check()
    solve_seconds = time.monotonic() - solve_started
    status = str(status_value)
    consumed = None
    if status == "sat":
        consumed = solver.model().eval(pointer, model_completion=True).as_long()
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "numpy-mt19937-full-shuffle-z3",
        "summary": {
            "status": status,
            "rounds_modelled": len(rows),
            "discovery_seed_labels_bound": sum(
                len(row.get("discovery_seed_label") or []) for row in rows
            ),
            "full_group_positions_bound": counters["full_group_positions"],
            "missing_positions_modelled": counters["missing_positions"],
            "bounded_draws_modelled": counters["draws"],
            "raw_cap": args.raw_cap,
            "max_rejections_per_draw": args.max_rejections,
            "minimum_shuffle_rejections_per_round": args.min_round_rejections,
            "maximum_shuffle_rejections_per_round": args.max_round_rejections,
            "raw_words_consumed_witness": consumed,
            "mt_twists_modelled": mt.twists,
            "build_seconds": round(build_seconds, 6),
            "solve_seconds": round(solve_seconds, 6),
            "mt19937_state_recovered": False,
            "excluded_discovery_predicted": False,
        },
        "interpretation": (
            "SAT only establishes compatibility of the bounded common-stream model; "
            "no generator is accepted until an excluded Discovery suffix is predicted."
        ),
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
