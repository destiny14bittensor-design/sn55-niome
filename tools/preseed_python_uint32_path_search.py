#!/usr/bin/env python3
"""Extend a uint32 Python RNG hit across the fixed Discovery sequence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import random
from typing import Any

import numpy as np

try:
    from tools.preseed_generator_lab import atomic_json
    from tools.preseed_uint32_hidden_gap import (
        scan_python_float_gaps,
        scan_python_gaps,
    )
except ModuleNotFoundError:
    from preseed_generator_lab import atomic_json
    from preseed_uint32_hidden_gap import (
        scan_python_float_gaps,
        scan_python_gaps,
    )


def bridge_after_first(seed: int, target: list[int], method: str) -> np.random.RandomState:
    source = random.Random(int(seed))
    if method == "float":
        actual = [100 + int(source.random() * 900) for _ in range(3)]
    else:
        actual = []
        while len(actual) < 3:
            value = source.randrange(100, 1000)
            if value not in actual:
                actual.append(value)
    if actual != target:
        raise ValueError(f"seed {seed} does not reproduce first target")
    internal = source.getstate()[1]
    bridge = np.random.RandomState()
    bridge.set_state(
        ("MT19937", np.asarray(internal[:-1], dtype=np.uint32), int(internal[-1]), 0, 0.0)
    )
    return bridge


def raw_word(rng: np.random.RandomState) -> int:
    return int(rng.randint(0, 2**32, dtype=np.uint32))


def consume_target(rng: np.random.RandomState, target: list[int], method: str) -> bool:
    if method == "float":
        actual = []
        for _ in range(3):
            high = raw_word(rng) >> 5
            low = raw_word(rng) >> 6
            value = (high * 67108864.0 + low) / 9007199254740992.0
            actual.append(100 + int(value * 900.0))
    else:
        actual = []
        while len(actual) < 3:
            value = raw_word(rng) >> 22
            if value >= 900:
                continue
            value += 100
            if value not in actual:
                actual.append(value)
    return actual == target


def load_targets(path: Path, start_prefix: str) -> list[dict[str, Any]]:
    records = json.loads(path.read_text(encoding="utf-8")).get("records") or []
    start = next(
        index for index, row in enumerate(records)
        if str(row.get("task_id") or "").startswith(start_prefix)
    )
    return [
        {"task_id": str(row["task_id"]), "seeds": [int(value) for value in row["seeds"]]}
        for row in records[start:]
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--start-prefix", default="f05ef562")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--method", choices=("bits", "float"), default="bits")
    parser.add_argument("--max-gap", type=int, default=10_000_000)
    parser.add_argument("--max-branches", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    targets = load_targets(args.dataset.resolve(), args.start_prefix)
    maximum_gap = max(0, int(args.max_gap))
    initial = bridge_after_first(args.seed, targets[0]["seeds"], args.method)
    branches: list[tuple[tuple[Any, ...], list[int]]] = [(initial.get_state(), [])]
    transitions = []
    for target_index, target in enumerate(targets[1:], start=1):
        following: list[tuple[tuple[Any, ...], list[int]]] = []
        target_hits = []
        for state, gaps in branches:
            probe = np.random.RandomState()
            probe.set_state(state)
            raw = probe.randint(
                0, 2**32, size=maximum_gap + 64, dtype=np.uint32
            )
            scanner = scan_python_float_gaps if args.method == "float" else scan_python_gaps
            hits = [int(value) for value in scanner(raw, maximum_gap, *target["seeds"])]
            for gap in hits:
                replay = np.random.RandomState()
                replay.set_state(state)
                if gap:
                    replay.randint(0, 2**32, size=gap, dtype=np.uint32)
                if not consume_target(replay, target["seeds"], args.method):
                    raise RuntimeError("gap scanner/replay disagreement")
                following.append((replay.get_state(), [*gaps, gap]))
                target_hits.append({"prior_gaps": gaps, "gap": gap})
                if len(following) >= max(1, int(args.max_branches)):
                    break
            if len(following) >= max(1, int(args.max_branches)):
                break
        transitions.append(
            {
                "task_id": target["task_id"],
                "target": target["seeds"],
                "incoming_branches": len(branches),
                "hits": target_hits,
                "outgoing_branches": len(following),
            }
        )
        branches = following
        if not branches:
            break

    matched_tasks = 1 + len([row for row in transitions if row["outgoing_branches"]])
    complete = matched_tasks == len(targets)
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "python-uint32-discovery-hidden-gap-path-search",
        "engine": args.method,
        "search": {
            "candidate_initializers": 1,
            "candidates_tested": sum(
                int(row["incoming_branches"]) * (maximum_gap + 1)
                for row in transitions
            ),
            "maximum_gap_words_per_transition": maximum_gap,
            "discovery_targets": len(targets),
            "matched_tasks": matched_tasks,
            "complete_discovery_path": complete,
            "discovery_exact_candidates": int(complete),
            "exact_candidates": [
                {"seed_u32": int(args.seed), "gaps": gaps} for _state, gaps in branches
            ] if complete else [],
            "families": {f"python-{args.method}-uint32-hidden-gap-path": {}},
            "transitions": transitions,
        },
        "safety": {
            "discovery_only": True,
            "fixed_discovery_total": 20,
            "holdout_opened": False,
            "network_requests": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **report["search"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
