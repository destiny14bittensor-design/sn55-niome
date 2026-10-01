#!/usr/bin/env python3
"""Enumerate correlated Fisher--Yates prefix choice tuples exactly.

Marginal choice domains discard the key fact that a particular first choice
changes which second and later choices remain possible.  This bounded search
retains the complete accepted-choice path.  Rows are emitted only after a
depth is fully enumerated; a state-cap hit discards the unfinished depth.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Sequence

try:
    from tools.preseed_shuffle_prefix_domains import (
        atomic_json,
        missing_tokens,
        positions_for,
        remove_one,
        swap_sparse,
        uid_aware_sequences,
    )
except ModuleNotFoundError:
    from preseed_shuffle_prefix_domains import (
        atomic_json,
        missing_tokens,
        positions_for,
        remove_one,
        swap_sparse,
        uid_aware_sequences,
    )


def exact_prefix_tuples(
    initial: Sequence[str],
    observed: Sequence[str],
    *,
    depth: int = 4,
    max_states: int = 50_000,
) -> dict[str, Any]:
    initial = tuple(initial)
    observed = tuple(observed)
    omitted = missing_tokens(initial, observed)
    positions = {
        token: tuple(index for index, value in enumerate(initial) if value == token)
        for token in set(initial)
    }
    # sparse overrides, next observed suffix index, omitted multiset, choices
    states = {((), len(observed) - 1, omitted, ())}
    completed = 0
    requested = min(max(0, int(depth)), max(0, len(initial) - 1))
    reason = None
    for offset in range(requested):
        index = len(initial) - 1 - offset
        following = set()
        overflow = False
        for encoded, observed_index, remaining, path in states:
            overrides = dict(encoded)
            targets: list[tuple[str, int, tuple[str, ...]]] = []
            if observed_index >= 0:
                targets.append((observed[observed_index], observed_index - 1, remaining))
            for target in sorted(set(remaining)):
                targets.append((target, observed_index, remove_one(remaining, target)))
            for target, next_observed, next_remaining in targets:
                for choice in positions_for(initial, positions, overrides, target, index):
                    following.add(
                        (
                            swap_sparse(initial, overrides, index, choice),
                            next_observed,
                            next_remaining,
                            path + (choice,),
                        )
                    )
                    if len(following) > max_states:
                        overflow = True
                        break
                if overflow:
                    break
            if overflow:
                break
        if overflow:
            reason = "state-cap-exceeded"
            break
        if not following:
            return {
                "status": "unsat",
                "completed_depth": completed,
                "requested_depth": depth,
                "choice_indices": [len(initial) - 1 - value for value in range(completed)],
                "choice_tuples": sorted({state[3] for state in states}),
            }
        states = following
        completed += 1
    tuples = sorted({state[3] for state in states})
    return {
        "status": "sat" if completed == requested else "unknown",
        "reason": reason,
        "completed_depth": completed,
        "requested_depth": depth,
        "choice_indices": [len(initial) - 1 - value for value in range(completed)],
        "choice_tuples": tuples,
        "choice_tuple_count": len(tuples),
        "state_count": len(states),
        "omitted_group_tokens": len(omitted),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument("--max-states", type=int, default=50_000)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_shuffle_prefix_tuples.json"),
    )
    args = parser.parse_args()
    payload = json.loads(args.constraints.read_text(encoding="utf-8"))
    rounds = []
    for row in payload.get("rounds") or []:
        initial, observed, exact = uid_aware_sequences(row)
        result = exact_prefix_tuples(
            initial,
            observed,
            depth=max(0, args.depth),
            max_states=max(1, args.max_states),
        )
        rounds.append(
            {
                "task_id": row["task_id"],
                "seed_label_partition": row.get("seed_label_partition"),
                "exact_uid_tokens": exact,
                **result,
            }
        )
    summary = {
        "rounds": len(rounds),
        "requested_depth": max(0, args.depth),
        "maximum_completed_depth": max(
            (int(row["completed_depth"]) for row in rounds), default=0
        ),
        "complete_rounds": sum(row["status"] == "sat" for row in rounds),
        "partial_rounds": sum(row["status"] == "unknown" for row in rounds),
        "choice_tuples": sum(int(row.get("choice_tuple_count") or 0) for row in rounds),
        "mt19937_state_recovered": False,
    }
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "fisher-yates-exact-correlated-prefix-tuples",
        "summary": summary,
        "rounds": rounds,
        "next_gate": (
            "Apply each tuple table to the corresponding accepted bounded draws in one "
            "symbolic MT19937 stream; an included-row SAT result is not a prediction."
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
