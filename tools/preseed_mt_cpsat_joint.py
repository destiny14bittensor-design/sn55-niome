#!/usr/bin/env python3
"""Joint CP-SAT prototype for NumPy MT19937, rejection draws, and shuffles.

Unlike the prefix-domain audit, this model keeps one symbolic MT19937 stream,
models ``rk_interval``'s rejected words explicitly, and drives the observed
group-labelled Fisher--Yates shuffles with those accepted values.  It also
binds the restart prelude and per-round Discovery triplets under the
``RandomState.randint(100, 1000)`` hypothesis.

The default bounded run intentionally starts with two Discovery rounds.  SAT
only proves that the joint encoding is feasible; it is not a generator
candidate until an excluded Discovery suffix is predicted exactly.  Holdout
labels are never loaded and no recovered state is persisted.

Run through ``uv run --with ortools``.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any

try:
    from tools.preseed_mt19937_rank_audit import MATRIX_A, M, N
    from tools.preseed_shuffle_prefix_domains import uid_aware_sequences
except ModuleNotFoundError:
    from preseed_mt19937_rank_audit import MATRIX_A, M, N
    from preseed_shuffle_prefix_domains import uid_aware_sequences


def xor_expression(left: list[set[int]], right: list[set[int]]) -> list[set[int]]:
    return [a.symmetric_difference(b) for a, b in zip(left, right)]


def right_shift_expression(word: list[set[int]], amount: int) -> list[set[int]]:
    return word[amount:] + [set() for _ in range(amount)]


def left_shift_mask_expression(
    word: list[set[int]], amount: int, mask: int
) -> list[set[int]]:
    return [
        set(word[bit - amount]) if bit >= amount and ((mask >> bit) & 1) else set()
        for bit in range(32)
    ]


def tempered_low_expressions(word: list[Any], bits: int) -> list[set[int]]:
    value = [{int(variable.index)} for variable in word]
    value = xor_expression(value, right_shift_expression(value, 11))
    value = xor_expression(value, left_shift_mask_expression(value, 7, 0x9D2C5680))
    value = xor_expression(value, left_shift_mask_expression(value, 15, 0xEFC60000))
    value = xor_expression(value, right_shift_expression(value, 18))
    return value[:bits]


def add_xor_equality(model: Any, output: Any, input_indices: set[int], variables: dict[int, Any]) -> None:
    literals = [variables[index] for index in sorted(input_indices)]
    # XOR(inputs) == output  <=>  XOR(inputs, NOT output) == true.
    model.add_bool_xor([*literals, output.Not()])


class SymbolicMtStream:
    def __init__(self, model: Any, raw_count: int, exposed_bits: int = 10) -> None:
        self.model = model
        self.variables: dict[int, Any] = {}
        self.state = [
            [self._bool(f"mt0_{word}_{bit}") for bit in range(32)]
            for word in range(N)
        ]
        self.initial_state = self.state
        self.position = 0
        self.twists = 0
        self.raw_bits: list[list[Any]] = []
        for raw_index in range(raw_count):
            if self.position >= N:
                self.state = self._twist(self.state)
                self.position = 0
                self.twists += 1
            expressions = tempered_low_expressions(
                self.state[self.position], exposed_bits
            )
            exposed = []
            for bit, expression in enumerate(expressions):
                output = self._bool(f"raw_{raw_index}_{bit}")
                add_xor_equality(model, output, expression, self.variables)
                exposed.append(output)
            self.raw_bits.append(exposed)
            self.position += 1

    def _bool(self, name: str) -> Any:
        variable = self.model.new_bool_var(name)
        self.variables[int(variable.index)] = variable
        return variable

    def _xor_bit(self, values: list[Any], name: str) -> Any:
        parity: dict[int, Any] = {}
        for value in values:
            index = int(value.index)
            if index in parity:
                parity.pop(index)
            else:
                parity[index] = value
        output = self._bool(name)
        self.model.add_bool_xor([*parity.values(), output.Not()])
        return output

    def _twist(self, state: list[list[Any]]) -> list[list[Any]]:
        result = list(state)

        def replace(index: int, source_index: int, following_index: int) -> None:
            following = result[following_index]
            mixed = following[:31] + [result[index][31]]
            shifted: list[Any | None] = mixed[1:] + [None]
            word = []
            for bit in range(32):
                values = [result[source_index][bit]]
                if shifted[bit] is not None:
                    values.append(shifted[bit])
                if (MATRIX_A >> bit) & 1:
                    values.append(mixed[0])
                word.append(
                    self._xor_bit(values, f"mt{self.twists + 1}_{index}_{bit}")
                )
            result[index] = word

        for index in range(N - M):
            replace(index, index + M, index + 1)
        for index in range(N - M, N - 1):
            replace(index, index + M - N, index + 1)
        replace(N - 1, M - 1, 0)
        return result

    def low_values(self, bits: int) -> list[Any]:
        maximum = (1 << bits) - 1
        values = []
        for raw_index, word in enumerate(self.raw_bits):
            value = self.model.new_int_var(0, maximum, f"low{bits}_{raw_index}")
            self.model.add(
                value == sum((1 << bit) * word[bit] for bit in range(bits))
            )
            values.append(value)
        return values


def interval_bits(maximum: int) -> int:
    mask = int(maximum)
    mask |= mask >> 1
    mask |= mask >> 2
    mask |= mask >> 4
    mask |= mask >> 8
    return max(1, mask.bit_length())


def add_bounded_draw(
    model: Any,
    low_values: dict[int, list[Any]],
    pointer: Any,
    *,
    maximum: int,
    raw_cap: int,
    max_rejections: int,
    name: str,
    expected: int | None = None,
    choice: Any | None = None,
    allowed_values: list[int] | None = None,
) -> tuple[Any, Any]:
    bits = interval_bits(maximum)
    gap = model.new_int_var(0, max_rejections, f"gap_{name}")
    accepted_value = model.new_int_var(0, (1 << bits) - 1, f"accepted_{name}")
    accepted_index = model.new_int_var(0, raw_cap - 1, f"accepted_index_{name}")
    model.add(accepted_index == pointer + gap)
    model.add_element(accepted_index, low_values[bits], accepted_value)
    model.add(accepted_value <= maximum)
    if expected is not None:
        model.add(accepted_value == int(expected))
    if choice is not None:
        model.add(choice == accepted_value)
    if allowed_values is not None:
        allowed = sorted({int(value) for value in allowed_values})
        if not allowed or allowed[0] < 0 or allowed[-1] > maximum:
            raise ValueError(f"invalid accepted-value domain for {name}")
        model.add_allowed_assignments(
            [accepted_value], [(value,) for value in allowed]
        )

    for attempt in range(max_rejections):
        rejected = model.new_bool_var(f"rejected_{name}_{attempt}")
        model.add(gap > attempt).only_enforce_if(rejected)
        model.add(gap <= attempt).only_enforce_if(~rejected)
        attempt_index = model.new_int_var(0, raw_cap - 1, f"attempt_index_{name}_{attempt}")
        model.add(attempt_index == pointer + attempt)
        attempt_value = model.new_int_var(
            0, (1 << bits) - 1, f"attempt_value_{name}_{attempt}"
        )
        model.add_element(attempt_index, low_values[bits], attempt_value)
        model.add(attempt_value > maximum).only_enforce_if(rejected)

    following = model.new_int_var(0, raw_cap, f"pointer_after_{name}")
    model.add(following == pointer + gap + 1)
    model.add(following <= raw_cap - max_rejections - 1)
    return following, gap


def add_round_targets(model: Any, row: dict[str, Any], name: str) -> tuple[list[int], list[Any]]:
    initial, observed, _exact = uid_aware_sequences(row)
    remaining = Counter(initial)
    remaining.subtract(observed)
    if any(value < 0 for value in remaining.values()):
        raise ValueError("observed multiset exceeds initial multiset")
    missing = [token for token in sorted(remaining) for _ in range(remaining[token])]
    labels = {token: index for index, token in enumerate(sorted(set(initial)))}
    size = len(initial)
    missing_positions = [
        model.new_int_var(0, size - 1, f"{name}_missing_{index}")
        for index in range(len(missing))
    ]
    if missing_positions:
        model.add_all_different(missing_positions)
    observed_labels = [labels[token] for token in observed]
    targets = []
    for position in range(size):
        at_position = []
        before = []
        for index, missing_position in enumerate(missing_positions):
            here = model.new_bool_var(f"{name}_missing_here_{position}_{index}")
            model.add(missing_position == position).only_enforce_if(here)
            model.add(missing_position != position).only_enforce_if(~here)
            at_position.append(here)
            earlier = model.new_bool_var(f"{name}_missing_before_{position}_{index}")
            model.add(missing_position < position).only_enforce_if(earlier)
            model.add(missing_position >= position).only_enforce_if(~earlier)
            before.append(earlier)
        no_missing = model.new_bool_var(f"{name}_not_missing_{position}")
        model.add(sum(at_position) == 0).only_enforce_if(no_missing)
        model.add(sum(at_position) >= 1).only_enforce_if(~no_missing)
        observed_index = model.new_int_var(
            0, max(0, len(observed_labels) - 1), f"{name}_observed_index_{position}"
        )
        # A missing slot has no corresponding observed-array index.  Keep the
        # element index arbitrary there instead of forcing an out-of-range
        # equality at a leading/trailing missing position.
        model.add(observed_index == position - sum(before)).only_enforce_if(
            no_missing
        )
        observed_value = model.new_int_var(
            0, len(labels) - 1, f"{name}_observed_value_{position}"
        )
        model.add_element(observed_index, observed_labels, observed_value)
        target = model.new_int_var(0, len(labels) - 1, f"{name}_target_{position}")
        model.add(target == observed_value).only_enforce_if(no_missing)
        for flag, token in zip(at_position, missing):
            model.add(target == labels[token]).only_enforce_if(flag)
        targets.append(target)
    return [labels[token] for token in initial], targets


def add_shuffle_round(
    model: Any,
    row: dict[str, Any],
    low_values: dict[int, list[Any]],
    pointer: Any,
    *,
    raw_cap: int,
    max_rejections: int,
    name: str,
) -> Any:
    current, targets = add_round_targets(model, row, name)
    for index in range(len(current) - 1, 0, -1):
        choice = model.new_int_var(0, index, f"{name}_j_{index}")
        pointer, _gap = add_bounded_draw(
            model,
            low_values,
            pointer,
            maximum=index,
            raw_cap=raw_cap,
            max_rejections=max_rejections,
            name=f"{name}_shuffle_{index}",
            choice=choice,
        )
        selected = model.new_int_var(
            0, max(current) if all(isinstance(value, int) for value in current) else 255,
            f"{name}_selected_{index}",
        )
        model.add_element(choice, current, selected)
        model.add(selected == targets[index])
        following = []
        for position in range(index):
            value = model.new_int_var(0, 255, f"{name}_pool_{index}_{position}")
            selected_position = model.new_bool_var(f"{name}_is_j_{index}_{position}")
            model.add(choice == position).only_enforce_if(selected_position)
            model.add(choice != position).only_enforce_if(~selected_position)
            model.add(value == current[index]).only_enforce_if(selected_position)
            model.add(value == current[position]).only_enforce_if(~selected_position)
            following.append(value)
        current = following
    model.add(current[0] == targets[0])
    return pointer


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
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--raw-cap", type=int, default=1_200)
    parser.add_argument("--max-rejections", type=int, default=5)
    parser.add_argument("--time-limit", type=float, default=300.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--prelude", default="654,347,964")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_mt_cpsat_joint.json"),
    )
    args = parser.parse_args()
    try:
        from ortools.sat.python import cp_model
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with ortools") from error

    payload = json.loads(args.constraints.read_text(encoding="utf-8"))
    rows = [
        row
        for row in payload.get("rounds") or []
        if row.get("seed_label_partition") == "discovery"
        and len(row.get("discovery_seed_label") or []) == 3
    ][: max(1, args.rounds)]
    model = cp_model.CpModel()
    started = time.monotonic()
    mt = SymbolicMtStream(model, max(1, args.raw_cap), exposed_bits=10)
    low_values = {bits: mt.low_values(bits) for bits in range(1, 11)}
    pointer: Any = model.new_int_var(0, 0, "pointer_start")
    for index, value in enumerate(
        int(item) for item in args.prelude.split(",") if item.strip()
    ):
        pointer, _gap = add_bounded_draw(
            model,
            low_values,
            pointer,
            maximum=899,
            raw_cap=args.raw_cap,
            max_rejections=max(0, args.max_rejections),
            name=f"prelude_{index}",
            expected=value - 100,
        )
    for round_index, row in enumerate(rows):
        pointer = add_shuffle_round(
            model,
            row,
            low_values,
            pointer,
            raw_cap=args.raw_cap,
            max_rejections=max(0, args.max_rejections),
            name=f"round_{round_index}",
        )
        for seed_index, value in enumerate(row["discovery_seed_label"]):
            pointer, _gap = add_bounded_draw(
                model,
                low_values,
                pointer,
                maximum=899,
                raw_cap=args.raw_cap,
                max_rejections=max(0, args.max_rejections),
                name=f"round_{round_index}_seed_{seed_index}",
                expected=int(value) - 100,
            )

    build_seconds = time.monotonic() - started
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(1.0, args.time_limit)
    solver.parameters.num_search_workers = max(1, args.workers)
    status_code = solver.solve(model)
    status = solver.status_name(status_code).lower()
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "numpy-mt19937-shuffle-seed-joint-cpsat",
        "summary": {
            "status": status,
            "rounds_modelled": len(rows),
            "raw_cap": args.raw_cap,
            "max_rejections_per_draw": args.max_rejections,
            "mt_twists_modelled": mt.twists,
            "build_seconds": round(build_seconds, 6),
            "solve_seconds": round(solver.wall_time, 6),
            "mt19937_state_recovered": False,
            "excluded_discovery_predicted": False,
        },
        "assumptions": [
            "one persistent NumPy RandomState stream",
            "restart prelude and per-round seeds use randint(100, 1000)",
            "each bounded draw has at most the configured rejection cap",
            "no other interleaved NumPy global RNG consumer",
        ],
        "next_gate": (
            "Increase training rounds only after SAT on the bounded model, then recover a "
            "unique state and predict an excluded Discovery suffix before opening Holdout."
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
