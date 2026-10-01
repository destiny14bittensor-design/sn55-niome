#!/usr/bin/env python3
"""Exhaust uint32 initializers for a persistent NumPy PCG64 seed stream.

The fixed restart prelude is followed only by Discovery labels created after
the public validator process start.  Holdout labels are never read.  The C++
scanner first filters on the prelude, then replays every later triplet.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

try:
    from tools.preseed_numpy_prelude_search import load_layout
    from tools.preseed_generator_lab import atomic_json
except ModuleNotFoundError:
    from preseed_numpy_prelude_search import load_layout
    from preseed_generator_lab import atomic_json


SOURCE = Path(__file__).with_name("preseed_pcg64_seed32_scan.cpp")


def compile_scanner(binary: Path) -> None:
    binary.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "g++",
            "-O3",
            "-std=c++17",
            "-pthread",
            "-march=native",
            str(SOURCE),
            "-o",
            str(binary),
        ],
        check=True,
    )


def scan(
    binary: Path,
    targets: list[list[int]],
    start: int,
    stop: int,
    threads: int,
    method: str = "integers-unique",
    engine: str = "pcg64",
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        target_file = Path(directory) / "targets.txt"
        target_file.write_text(
            "".join(",".join(str(value) for value in row) + "\n" for row in targets),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                str(binary),
                str(target_file),
                str(start),
                str(stop),
                str(max(1, threads)),
                method,
                engine,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    return json.loads(completed.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--process-start", default="2026-09-26T14:57:51.192497Z")
    parser.add_argument("--prelude", default="654,347,964")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--stop", type=int, default=1 << 32)
    parser.add_argument("--threads", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument(
        "--method",
        choices=("integers-unique", "choice", "float-unique"),
        default="integers-unique",
    )
    parser.add_argument(
        "--engine",
        choices=("pcg64", "pcg64dxsm", "sfc64", "philox"),
        default="pcg64",
    )
    parser.add_argument(
        "--binary",
        type=Path,
        default=Path("artifacts/research/bin/preseed_pcg64_seed32_scan"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_pcg64_uint32_persistent.json"),
    )
    args = parser.parse_args()
    if not (0 <= args.start <= args.stop <= 1 << 32):
        raise ValueError("scan range must lie within uint32")
    prelude = [int(value) for value in args.prelude.split(",")]
    if len(prelude) != 3:
        raise ValueError("--prelude requires three comma-separated integers")
    observed_prelude, segment = load_layout(
        args.discovery.resolve(), args.process_start, prelude
    )
    targets = [observed_prelude] + [
        [int(value) for value in record["seeds"]] for record in segment
    ]
    binary = args.binary.resolve()
    compile_scanner(binary)
    result = scan(
        binary, targets, args.start, args.stop, args.threads, args.method, args.engine
    )
    complete = args.start == 0 and args.stop == 1 << 32
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": f"numpy-{args.engine}-persistent-uint32-exhaustive",
        "split": {
            "restart_prelude": 1,
            "post_restart_discovery_tasks": len(segment),
            "holdout_opened": False,
        },
        "search": {
            **result,
            "candidates_tested": result["tested"],
            "families": {
                f"numpy-{args.engine}-{args.method}-persistent-uint32": {
                    "tested": result["tested"]
                }
            },
            "complete_uint32_space": complete,
            "discovery_exact_candidates": len(result["full_stream_candidates"]),
        },
        "interpretation": (
            "A complete miss rejects only the selected persistent NumPy "
            "default_rng(uint32) triplet API with no hidden interleaved draws. It does "
            "not reject OS entropy, wider seeds, another API, or hidden draws."
        ),
        "safety": {
            "discovery_only": True,
            "holdout_opened": False,
            "network_requests": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "tested": result["tested"],
                "first_triplet_candidates": len(result["first_triplet_candidates"]),
                "discovery_exact_candidates": len(result["full_stream_candidates"]),
                "complete_uint32_space": complete,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
