#!/usr/bin/env python3
"""Run one planned rejection corridor through the exact MT XOR-SAT model."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import resource
import subprocess
import sys


def corridor_arguments(plan: dict, rank: int) -> list[str]:
    for corridor in plan.get("corridors") or []:
        if int(corridor.get("rank") or 0) == int(rank):
            return [str(value) for value in corridor.get("solver_arguments") or []]
    raise ValueError(f"corridor rank {rank} is not present in the plan")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--rank", type=int, required=True)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--tuple-report", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument(
        "--shuffle-mode",
        choices=("trace", "prefix-only", "none"),
        default="trace",
    )
    parser.add_argument("--max-singleton-traces", type=int, default=8)
    parser.add_argument("--combine-prefix-tuples-with-traces", action="store_true")
    parser.add_argument("--raw-cap", type=int, default=7600)
    parser.add_argument("--rejection-budget", type=int, default=2200)
    parser.add_argument("--time-limit", type=float, default=120.0)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--memory-limit-gib", type=float, default=12.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    command = [
        sys.executable,
        str(Path(__file__).with_name("preseed_mt_xorsat_joint.py")),
        "--constraints",
        str(args.constraints),
        "--tuple-report",
        str(args.tuple_report),
        "--rounds",
        str(max(1, args.rounds)),
        "--include-sealed-shuffles",
        "--raw-cap",
        str(max(1, args.raw_cap)),
        "--alignment-encoding",
        "lattice",
        "--rejection-budget",
        str(max(0, args.rejection_budget)),
        "--mt-encoding",
        "sparse",
        "--time-limit",
        str(max(1.0, args.time_limit)),
        "--threads",
        str(max(1, args.threads)),
        "--output",
        str(args.output),
    ]
    if args.shuffle_mode == "trace":
        command.extend(
            [
                "--shuffle-encoding",
                "singleton-trace",
                "--max-singleton-traces",
                str(max(0, args.max_singleton_traces)),
            ]
        )
        if args.combine_prefix_tuples_with_traces:
            command.append("--combine-prefix-tuples-with-traces")
    elif args.shuffle_mode == "prefix-only":
        # A selector that matches no UUID makes every row take the exact tuple
        # trie path without constructing a full permutation network.
        command.extend(["--shuffle-encoding", "network", "--full-shuffle-task", "__none__"])
    else:
        command.extend(["--shuffle-encoding", "none"])
    command.extend(corridor_arguments(plan, args.rank))

    memory_bytes = int(max(1.0, args.memory_limit_gib) * (1024**3))

    def limits() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        os.nice(10)

    completed = subprocess.run(command, check=False, preexec_fn=limits)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
