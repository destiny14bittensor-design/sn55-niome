#!/usr/bin/env python3
"""Extract exact high-index Fisher--Yates choice domains from partial shuffles.

For the first few descending swaps, missing observations and duplicate endpoint
groups can be enumerated exactly without assigning concrete UIDs. Each state
tracks the current group-labelled array, the remaining observed suffix, and
the small multiset of omitted groups. The resulting ``j_i`` domains are safe,
bounded inputs for a later MT19937/rejection-alignment solver.

No endpoint strings, IP addresses, Holdout labels, or submissions are read.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Sequence


Overrides = tuple[tuple[int, str], ...]
State = tuple[Overrides, int, tuple[str, ...]]


def missing_tokens(initial: Sequence[str], observed: Sequence[str]) -> tuple[str, ...]:
    remaining = Counter(initial)
    remaining.subtract(observed)
    if any(value < 0 for value in remaining.values()):
        raise ValueError("observed multiset exceeds initial multiset")
    return tuple(
        token
        for token in sorted(remaining)
        for _ in range(int(remaining[token]))
    )


def remove_one(values: tuple[str, ...], target: str) -> tuple[str, ...]:
    index = values.index(target)
    return values[:index] + values[index + 1 :]


def value_at(initial: tuple[str, ...], overrides: dict[int, str], position: int) -> str:
    return overrides.get(position, initial[position])


def positions_for(
    initial: tuple[str, ...],
    initial_positions: dict[str, tuple[int, ...]],
    overrides: dict[int, str],
    target: str,
    maximum: int,
) -> list[int]:
    positions = {position for position in initial_positions[target] if position <= maximum}
    for position, value in overrides.items():
        if position > maximum:
            continue
        if initial[position] == target and value != target:
            positions.discard(position)
        elif initial[position] != target and value == target:
            positions.add(position)
    return sorted(positions)


def swap_sparse(
    initial: tuple[str, ...], overrides: dict[int, str], left: int, right: int
) -> Overrides:
    if left == right:
        return tuple(sorted(overrides.items()))
    left_value = value_at(initial, overrides, left)
    right_value = value_at(initial, overrides, right)
    updated = dict(overrides)
    for position, value in ((left, right_value), (right, left_value)):
        if value == initial[position]:
            updated.pop(position, None)
        else:
            updated[position] = value
    return tuple(sorted(updated.items()))


def exact_prefix_domains(
    initial: Sequence[str],
    observed: Sequence[str],
    *,
    depth: int = 3,
    max_states: int = 1_000_000,
) -> dict[str, Any]:
    initial = tuple(initial)
    observed = tuple(observed)
    omitted = missing_tokens(initial, observed)
    initial_positions = {
        token: tuple(index for index, value in enumerate(initial) if value == token)
        for token in set(initial)
    }
    states: set[State] = {((), len(observed) - 1, omitted)}
    rows = []
    for offset in range(min(max(0, int(depth)), max(0, len(initial) - 1))):
        index = len(initial) - 1 - offset
        following: set[State] = set()
        choices: set[int] = set()
        for encoded_overrides, observed_index, remaining in states:
            overrides = dict(encoded_overrides)
            targets: list[tuple[str, int, tuple[str, ...]]] = []
            if observed_index >= 0:
                targets.append((observed[observed_index], observed_index - 1, remaining))
            for target in sorted(set(remaining)):
                targets.append((target, observed_index, remove_one(remaining, target)))
            for target, next_observed, next_remaining in targets:
                for choice in positions_for(
                    initial, initial_positions, overrides, target, index
                ):
                    swapped = swap_sparse(initial, overrides, index, choice)
                    following.add((swapped, next_observed, next_remaining))
                    choices.add(choice)
                    if len(following) > max_states:
                        return {
                            "status": "unknown",
                            "reason": "state-cap-exceeded",
                            "completed_depth": offset,
                            "requested_depth": depth,
                            "prefix": rows,
                        }
        if not following:
            return {
                "status": "unsat",
                "completed_depth": offset,
                "requested_depth": depth,
                "prefix": rows,
            }
        rows.append(
            {
                "shuffle_index": index,
                "choice_domain": sorted(choices),
                "choice_domain_size": len(choices),
                "states_after_step": len(following),
                "domain_reduction_bits": round(
                    math.log2((index + 1) / max(1, len(choices))), 6
                ),
            }
        )
        states = following
    return {
        "status": "sat",
        "completed_depth": len(rows),
        "requested_depth": depth,
        "omitted_group_tokens": len(omitted),
        "final_state_count": len(states),
        "prefix": rows,
    }


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


def uid_aware_sequences(row: dict[str, Any]) -> tuple[list[str], list[str], int]:
    # The corpus represents a unique endpoint as a one-member UID domain
    # (``gN: [uid]``).  Preserve that information instead of collapsing it
    # back to an endpoint-equivalence token.  Exact error-log ``uUID`` tokens
    # and singleton domains then share one canonical representation.
    singleton_domains = {
        str(token): f"u{int(values[0])}"
        for token, values in (row.get("uid_domains") or {}).items()
        if len(values) == 1
    }
    observed = [
        singleton_domains.get(str(value), str(value))
        for value in row.get("ordered_uid_domains") or []
    ]
    initial_groups = [
        singleton_domains.get(str(value), str(value))
        for value in row["initial_domain_sequence"]
    ]
    initial_uids = [int(value) for value in row.get("initial_uids") or []]
    exact_tokens = {value for value in observed if value.startswith("u")}
    if len(initial_uids) != len(initial_groups) or not observed:
        return initial_groups, [str(value) for value in row["ordered_group_domains"]], 0
    initial = [
        token if token in exact_tokens else group
        for uid, group in zip(initial_uids, initial_groups)
        for token in (f"u{uid}",)
    ]
    return initial, observed, len(exact_tokens)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--max-states", type=int, default=1_000_000)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_shuffle_prefix_domains.json"),
    )
    args = parser.parse_args()
    payload = json.loads(args.constraints.read_text(encoding="utf-8"))
    rounds = []
    for row in payload.get("rounds") or []:
        initial, observed, exact_uid_tokens = uid_aware_sequences(row)
        result = exact_prefix_domains(
            initial,
            observed,
            depth=max(0, args.depth),
            max_states=max(1, args.max_states),
        )
        rounds.append(
            {
                "task_id": row["task_id"],
                "seed_label_partition": row.get("seed_label_partition"),
                "exact_uid_tokens": exact_uid_tokens,
                **result,
            }
        )
    completed = [row for row in rounds if row["status"] == "sat"]
    partial = [row for row in rounds if row["status"] == "unknown" and row["prefix"]]
    # Prefix rows emitted before a state cap are complete: the cap is checked
    # while constructing the *next* depth, whose partial domain is discarded.
    prefix_rows = [step for row in rounds for step in row["prefix"]]
    summary = {
        "rounds": len(rounds),
        "sat_rounds": len(completed),
        "partial_rounds": len(partial),
        "depth": max(0, args.depth),
        "maximum_completed_depth": max(
            (int(row.get("completed_depth") or 0) for row in rounds), default=0
        ),
        "choice_domains": len(prefix_rows),
        "singleton_choice_domains": sum(
            step["choice_domain_size"] == 1 for step in prefix_rows
        ),
        "domain_reduction_bits": round(
            sum(float(step["domain_reduction_bits"]) for step in prefix_rows), 6
        ),
        "mt19937_state_recovered": False,
    }
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "fisher-yates-exact-prefix-choice-domains",
        "summary": summary,
        "rounds": rounds,
        "next_gate": (
            "Bind these exact high-index choice domains and explicit rejection choices to "
            "one symbolic MT19937 stream; do not treat domain reduction as recovered raw bits."
        ),
        "safety": {
            "public_read_only": True,
            "holdout_labels_opened": False,
            "endpoint_data_stored": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
