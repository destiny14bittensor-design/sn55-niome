#!/usr/bin/env python3
"""Sample UID permutations consistent with public endpoint-domain subsequences.

This is a heuristic branch-ordering tool, not a proof engine.  It randomly
assigns repeated domain observations to distinct initial UID positions, inserts
the unobserved positions, and converts every completed permutation to its
unique descending Fisher--Yates choices.  Stable sampled bits may prioritize a
joint solver branch, but can never justify UNSAT or candidate promotion.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import random
from typing import Any, Sequence

try:
    from tools.preseed_mt_cpsat_joint import atomic_json
    from tools.preseed_mt_xorsat_joint import interval_bits, matches_task_selector
    from tools.preseed_shuffle_prefix_domains import uid_aware_sequences
except ModuleNotFoundError:
    from preseed_mt_cpsat_joint import atomic_json
    from preseed_mt_xorsat_joint import interval_bits, matches_task_selector
    from preseed_shuffle_prefix_domains import uid_aware_sequences


def sample_completion(
    initial_tokens: Sequence[str], observed_tokens: Sequence[str], rng: random.Random
) -> list[int]:
    positions: dict[str, list[int]] = defaultdict(list)
    for position, token in enumerate(initial_tokens):
        positions[str(token)].append(position)
    for values in positions.values():
        rng.shuffle(values)
    observed_positions = []
    for token in observed_tokens:
        values = positions.get(str(token)) or []
        if not values:
            raise ValueError("observed domain multiset exceeds initial multiset")
        observed_positions.append(values.pop())
    omitted = [value for values in positions.values() for value in values]
    rng.shuffle(omitted)
    omitted_slots = set(rng.sample(range(len(initial_tokens)), len(omitted)))
    observed_iter = iter(observed_positions)
    omitted_iter = iter(omitted)
    return [
        next(omitted_iter) if slot in omitted_slots else next(observed_iter)
        for slot in range(len(initial_tokens))
    ]


def recover_choices(permutation: Sequence[int]) -> list[int]:
    """Return descending Fisher--Yates choices yielding ``permutation``."""
    pool = list(range(len(permutation)))
    positions = list(range(len(permutation)))
    choices = []
    for index in range(len(permutation) - 1, 0, -1):
        target = int(permutation[index])
        choice = positions[target]
        choices.append(choice)
        displaced = pool[index]
        pool[choice], pool[index] = pool[index], pool[choice]
        positions[target] = index
        positions[displaced] = choice
    return choices


def replay_choices(size: int, choices: Sequence[int]) -> list[int]:
    values = list(range(size))
    for index, choice in zip(range(size - 1, 0, -1), choices):
        values[index], values[int(choice)] = values[int(choice)], values[index]
    return values


def sample_row(row: dict[str, Any], samples: int, seed: int) -> dict[str, Any]:
    initial, observed, _exact = uid_aware_sequences(row)
    rng = random.Random(int(seed))
    one_counts = [
        [0] * interval_bits(maximum)
        for maximum in range(len(initial) - 1, 0, -1)
    ]
    value_sets = [set() for _ in one_counts]
    for _ in range(max(1, int(samples))):
        completed = sample_completion(initial, observed, rng)
        choices = recover_choices(completed)
        if replay_choices(len(initial), choices) != completed:
            raise AssertionError("choice inversion failed")
        for draw, value in enumerate(choices):
            value_sets[draw].add(int(value))
            for bit in range(len(one_counts[draw])):
                one_counts[draw][bit] += (int(value) >> bit) & 1
    total = max(1, int(samples))
    stable_bits = []
    biased_bits = []
    for draw, counts in enumerate(one_counts):
        for bit, ones in enumerate(counts):
            rate = ones / total
            if ones in (0, total):
                stable_bits.append(
                    {"draw": draw, "bit": bit, "value": int(ones == total)}
                )
            if rate <= 0.01 or rate >= 0.99:
                biased_bits.append(
                    {
                        "draw": draw,
                        "bit": bit,
                        "preferred": int(rate >= 0.5),
                        "confidence": round(max(rate, 1.0 - rate), 8),
                    }
                )
    return {
        "task_id": str(row["task_id"]),
        "shuffle_size": len(initial),
        "observed_positions": len(observed),
        "missing_positions": len(initial) - len(observed),
        "samples": total,
        "stable_bits": stable_bits,
        "stable_bit_count": len(stable_bits),
        "biased_bits_99pct": biased_bits,
        "biased_bit_count_99pct": len(biased_bits),
        "mean_choice_domain_size": round(
            sum(len(values) for values in value_sets) / max(1, len(value_sets)), 6
        ),
        "maximum_choice_domain_size": max(map(len, value_sets), default=0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--rounds", type=int, default=0)
    parser.add_argument("--samples", type=int, default=100000)
    parser.add_argument("--seed", type=int, default=20261001)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_shuffle_sampled_bits.json"),
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
        sample_row(row, args.samples, args.seed + index)
        for index, row in enumerate(rows)
    ]
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "heuristic-partial-shuffle-completion-sampling",
        "summary": {
            "rows": len(results),
            "samples_per_row": int(args.samples),
            "stable_bits": sum(row["stable_bit_count"] for row in results),
            "biased_bits_99pct": sum(row["biased_bit_count_99pct"] for row in results),
        },
        "rounds": results,
        "interpretation": (
            "Sample-stable bits prioritize branches only. They are not proven forced and "
            "cannot support UNSAT or candidate promotion."
        ),
        "safety": {
            "holdout_opened": False,
            "network_reads": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
