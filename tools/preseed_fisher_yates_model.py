#!/usr/bin/env python3
"""Convert sanitized shuffle observations into Fisher-Yates swap constraints.

This stage deliberately solves only the permutation layer.  It proves that the
sanitized endpoint-equivalence sequence can be represented as the validator's
``np.random.shuffle`` calls and exports one witness swap vector per selected
round.  The next stage links those bounded swap indices to symbolic MT19937
outputs, including rejection sampling; an arbitrary witness is not a recovered
RNG state and is never promoted as a seed candidate.

The default witness is constructed directly.  An optional Z3 helper remains
for experiments, but no solver dependency is added until the full state model
is demonstrated.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Sequence


def replay_shuffle(values: Sequence[Any], descending_choices: Sequence[int]) -> list[Any]:
    result = list(values)
    if len(descending_choices) != max(0, len(result) - 1):
        raise ValueError("one choice is required for every i=n-1..1")
    for i, choice in zip(range(len(result) - 1, 0, -1), descending_choices):
        if choice < 0 or choice > i:
            raise ValueError(f"choice {choice} is outside 0..{i}")
        result[i], result[choice] = result[choice], result[i]
    return result


def invert_exact_shuffle(initial: Sequence[Any], final: Sequence[Any]) -> list[int]:
    """Recover the unique Fisher-Yates choice vector for distinct values."""
    if len(initial) != len(final) or len(set(initial)) != len(initial):
        raise ValueError("exact inversion requires equal-length distinct values")
    active = list(initial)
    choices = []
    for i in range(len(active) - 1, 0, -1):
        try:
            choice = active.index(final[i], 0, i + 1)
        except ValueError as error:
            raise ValueError("final is not a permutation of initial") from error
        choices.append(choice)
        active[i], active[choice] = active[choice], active[i]
    if active != list(final):
        raise ValueError("final is not a permutation of initial")
    return choices


def is_subsequence(needle: Sequence[Any], haystack: Sequence[Any]) -> bool:
    iterator = iter(haystack)
    return all(any(value == candidate for candidate in iterator) for value in needle)


def construct_group_shuffle(
    initial: Sequence[str], observed: Sequence[str]
) -> dict[str, Any]:
    """Construct one valid witness without mistaking it for the real shuffle.

    The observed order is a subsequence.  Append the missing multiset, attach a
    stable occurrence index to repeated labels, and use exact inversion on the
    now-distinct tokens.  This is linear/quadratic and avoids asking SMT to
    rediscover a witness that is not yet constrained by an RNG.
    """
    remaining = Counter(initial)
    for token in observed:
        remaining[token] -= 1
        if remaining[token] < 0:
            return {"status": "unsat", "reason": "observed-multiset-exceeds-initial"}
    completed = list(observed)
    for token in sorted(remaining):
        completed.extend([token] * remaining[token])

    occurrences: dict[str, int] = defaultdict(int)
    tagged_initial = []
    queues: dict[str, deque[tuple[str, int]]] = defaultdict(deque)
    for token in initial:
        tagged = (token, occurrences[token])
        occurrences[token] += 1
        tagged_initial.append(tagged)
        queues[token].append(tagged)
    tagged_final = [queues[token].popleft() for token in completed]
    witness = invert_exact_shuffle(tagged_initial, tagged_final)
    final = replay_shuffle(initial, witness)
    if not is_subsequence(observed, final):
        raise AssertionError("constructed witness failed local replay")
    return {
        "status": "sat",
        "shuffle_size": len(initial),
        "observed_positions": len(observed),
        "missing_positions": len(initial) - len(observed),
        "descending_choices": witness,
        "witness_verified": True,
        "witness_strategy": "append-missing-labels-and-stably-tag-duplicates",
    }


def solve_group_shuffle(
    initial: Sequence[str], observed: Sequence[str], timeout_ms: int
) -> dict[str, Any]:
    try:
        import z3
    except ModuleNotFoundError as error:
        raise RuntimeError("install ephemeral solver with --with z3-solver") from error

    if len(observed) > len(initial):
        return {"status": "unsat", "reason": "observation-longer-than-shuffle"}
    labels = {value: index for index, value in enumerate(sorted(set(initial) | set(observed)))}
    solver = z3.Solver()
    solver.set(timeout=max(1, timeout_ms))
    array = z3.K(z3.IntSort(), z3.IntVal(-1))
    for index, token in enumerate(initial):
        array = z3.Store(array, index, labels[token])
    choices = []
    for i in range(len(initial) - 1, 0, -1):
        choice = z3.Int(f"j_{i}")
        solver.add(choice >= 0, choice <= i)
        left = z3.Select(array, i)
        right = z3.Select(array, choice)
        array = z3.Store(z3.Store(array, i, right), choice, left)
        choices.append(choice)

    positions = [z3.Int(f"p_{index}") for index in range(len(observed))]
    for index, (position, token) in enumerate(zip(positions, observed)):
        solver.add(position >= 0, position < len(initial))
        solver.add(z3.Select(array, position) == labels[token])
        if index:
            solver.add(positions[index - 1] < position)
    outcome = solver.check()
    if outcome == z3.unknown:
        return {"status": "unknown", "reason": solver.reason_unknown()}
    if outcome != z3.sat:
        return {"status": "unsat"}
    model = solver.model()
    witness = [model.eval(choice).as_long() for choice in choices]
    final = replay_shuffle(initial, witness)
    if not is_subsequence(observed, final):
        raise AssertionError("solver witness failed local replay")
    return {
        "status": "sat",
        "shuffle_size": len(initial),
        "observed_positions": len(observed),
        "missing_positions": len(initial) - len(observed),
        "descending_choices": witness,
        "witness_verified": True,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_fisher_yates_model.json"),
    )
    args = parser.parse_args()
    payload = json.loads(args.constraints.read_text(encoding="utf-8"))
    ranked = sorted(
        payload.get("rounds") or [],
        key=lambda row: (
            int(row.get("group_sequence_missing_positions") or 10**9),
            -int(row.get("group_order_constraints") or 0),
        ),
    )[: max(1, args.max_rounds)]
    rounds = []
    for row in ranked:
        solved = construct_group_shuffle(
            row["initial_domain_sequence"], row["ordered_group_domains"]
        )
        rounds.append({"task_id": row["task_id"], **solved})
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "fisher-yates-group-constraint-witness",
        "summary": {
            "rounds_attempted": len(rounds),
            "sat_rounds": sum(row["status"] == "sat" for row in rounds),
            "unknown_rounds": sum(row["status"] == "unknown" for row in rounds),
            "mt19937_state_recovered": False,
        },
        "rounds": rounds,
        "next_gate": (
            "Replace unconstrained j_i witnesses with NumPy random_interval outputs tied to one "
            "MT19937 state and explicit rejection draws; then predict excluded Discovery seeds."
        ),
        "safety": {
            "public_read_only": True,
            "holdout_opened": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
