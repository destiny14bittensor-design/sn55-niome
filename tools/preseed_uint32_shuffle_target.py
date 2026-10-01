#!/usr/bin/env python3
"""Exhaust explicit uint32 RandomState seeds at the first Discovery shuffle.

The AVX2 worker first checks the exact four-choice tuple table derived from the
public partial Fisher--Yates result.  Only surviving initializers replay the
remaining 251 bounded draws and the first Discovery seed triplet.  A complete
zero-hit scan therefore closes fresh ``RandomState(uint32)`` initialization
immediately before that shuffle, without using the restart prelude.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile

import numpy as np

try:
    from tools.preseed_generator_lab import atomic_json
    from tools.preseed_mt_xorsat_joint import is_exact_observed_subsequence
    from tools.preseed_shuffle_prefix_domains import uid_aware_sequences
    from tools.preseed_uint32_hidden_gap import tuple_keys
except ModuleNotFoundError:
    from preseed_generator_lab import atomic_json
    from preseed_mt_xorsat_joint import is_exact_observed_subsequence
    from preseed_shuffle_prefix_domains import uid_aware_sequences
    from preseed_uint32_hidden_gap import tuple_keys


def compatible_shuffle_candidates(
    candidates: list[int], initial: list[str], observed: list[str]
) -> list[int]:
    compatible = []
    for seed in candidates:
        shuffled = np.asarray(initial, dtype=object)
        np.random.RandomState(int(seed) & 0xFFFFFFFF).shuffle(shuffled)
        if is_exact_observed_subsequence(shuffled.tolist(), observed):
            compatible.append(int(seed))
    return compatible


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--tuple-report", type=Path, required=True)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--task-prefix", default="f05ef562")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--stop", type=int, default=1 << 32)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    keys = tuple_keys(args.tuple_report.resolve(), args.task_prefix)
    with tempfile.NamedTemporaryFile("w", encoding="ascii") as handle:
        handle.write("\n".join(str(int(value)) for value in keys))
        handle.write("\n")
        handle.flush()
        completed = subprocess.run(
            [
                str(args.binary.resolve()),
                "--shuffle-target",
                handle.name,
                str(int(args.start)),
                str(int(args.stop)),
                str(max(1, int(args.threads))),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
    worker = json.loads(completed.stdout.strip().splitlines()[-1])
    tested = int(worker["tested"])
    complete = int(args.start) == 0 and int(args.stop) == 1 << 32
    prefix_candidates = [
        int(value) for value in worker.get("prelude_candidates") or []
    ]
    constraints = json.loads(args.constraints.read_text(encoding="utf-8"))
    row = next(
        row for row in constraints.get("rounds") or []
        if str(row.get("task_id") or "").startswith(args.task_prefix)
    )
    initial, observed, _exact = uid_aware_sequences(row)
    full_shuffle_candidates = compatible_shuffle_candidates(
        prefix_candidates, initial, observed
    )
    full_set = set(full_shuffle_candidates)
    exact = [
        int(value) for value in worker.get("first_discovery_hits") or []
        if int(value) in full_set
    ]
    choice_exact = [
        int(value) for value in worker.get("choice_first_discovery_hits") or []
        if int(value) in full_set
    ]
    all_exact = sorted(set(exact + choice_exact))
    integers_status = "candidate" if exact else "unsat" if complete else "partial"
    choice_status = "candidate" if choice_exact else "unsat" if complete else "partial"
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "exhaustive-uint32-randomstate-first-discovery-shuffle-search",
        "split": {
            "fixed_discovery_total": 20,
            "rounds_constrained": 1,
            "task_id_prefix": args.task_prefix,
            "holdout_opened": False,
        },
        "search": {
            "start": int(args.start),
            "stop_exclusive": int(args.stop),
            "candidates_tested": tested,
            "correlated_prefix_tuple_count": int(keys.size),
            "shuffle_prefix_hits": int(worker["prelude_hits"]),
            "shuffle_prefix_candidates": prefix_candidates,
            "full_public_shuffle_candidates": full_shuffle_candidates,
            "exact_candidates": exact,
            "choice_exact_candidates": choice_exact,
            "discovery_exact_candidates": len(all_exact),
            "families": {
                "numpy-randomstate-uint32-fresh-before-first-discovery-integers-unique": {
                    "status": integers_status,
                    "tested": tested,
                },
                "numpy-randomstate-uint32-fresh-before-first-discovery-choice": {
                    "status": choice_status,
                    "tested": tested,
                },
            },
            "complete": complete,
        },
        "safety": {
            "discovery_only": True,
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
