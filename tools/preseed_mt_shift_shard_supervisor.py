#!/usr/bin/env python3
"""Run an exact union of fixed missing-entry shift shards sequentially.

Each child starts from the same public CEGAR checkpoint and fixes one
``round:position:shift`` value.  The shifts are disjoint and exhaustive over
the requested range; no SAT/UNKNOWN shard is treated as a rejection of the
generator family.  Holdout labels, MT state, and raw streams are never read or
persisted by this supervisor.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

try:
    from tools.preseed_mt_cpsat_joint import atomic_json
except ModuleNotFoundError:
    from preseed_mt_cpsat_joint import atomic_json


def shift_values(encoded: str) -> list[int]:
    parts = str(encoded).split(":")
    if len(parts) != 2:
        raise ValueError("shift range must be LOW:HIGH")
    low, high = (int(value) for value in parts)
    if low < 0 or high < low:
        raise ValueError("shift range is invalid")
    return list(range(low, high + 1))


def shard_paths(output_dir: Path, prefix: str, shift: int) -> tuple[Path, Path]:
    stem = f"{prefix}_shift{int(shift)}"
    return output_dir / f"{stem}.json", output_dir / f"{stem}_checkpoint.json"


def build_shard_command(
    *,
    constraints: Path,
    tuple_report: Path,
    corridor_plan: Path,
    resume: Path,
    output: Path,
    checkpoint: Path,
    corridor_rank: int,
    rounds: int,
    target_round: int,
    target_position: int,
    shift: int,
    time_limit: float,
    threads: int,
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).with_name("preseed_mt_fixed_profile.py")),
        "--constraints",
        str(constraints),
        "--tuple-report",
        str(tuple_report),
        "--corridor-plan",
        str(corridor_plan),
        "--corridor-rank",
        str(int(corridor_rank)),
        "--rounds",
        str(int(rounds)),
        "--validate-next-discovery",
        "--omit-singleton-traces",
        "--omit-prefix-tuples",
        "--cegar-iterations",
        "1024",
        "--cegar-batch-size",
        "8",
        "--cegar-low-position-threshold",
        "31",
        "--cegar-low-batch-size",
        "1",
        "--cegar-domain-batch-size",
        "4",
        "--cegar-domain-low-position-threshold",
        "128",
        "--cegar-domain-low-batch-size",
        "1",
        "--cegar-strategy",
        "singleton-trace",
        "--cegar-resume",
        str(resume),
        "--fixed-trace-shift",
        f"{int(target_round)}:{int(target_position)}:{int(shift)}",
        "--checkpoint-output",
        str(checkpoint),
        "--time-limit",
        str(max(1.0, float(time_limit))),
        "--threads",
        str(max(1, int(threads))),
        "--output",
        str(output),
    ]


def summarize_output(
    path: Path,
    shift: int,
    returncode: int | None,
    *,
    target_round: int | None = None,
    target_position: int | None = None,
) -> dict[str, Any]:
    if not path.exists():
        return {
            "shift": int(shift),
            "returncode": returncode,
            "artifact": str(path),
            "terminal": False,
            "status": "missing",
            "candidate_promoted": False,
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    summary = payload.get("summary") or {}
    expected_mapping = {
        "round": int(target_round),
        "position": int(target_position),
        "shift": int(shift),
    } if target_round is not None and target_position is not None else None
    recorded_mappings = summary.get("fixed_trace_shifts") or []
    mapping_present = expected_mapping is None or expected_mapping in recorded_mappings
    # This field was introduced with the pre-solve binding fix.  Its absence
    # deliberately invalidates old artifacts whose first solve could time out
    # before the advertised fixed trace ever entered the formula.
    prebound = int(summary.get("fixed_trace_positions_prebound") or 0)
    exact_shift_certified = bool(mapping_present and prebound >= 1)
    return {
        "shift": int(shift),
        "returncode": returncode,
        "artifact": str(path),
        "terminal": True,
        "status": summary.get("status"),
        "exact_shift_certified": exact_shift_certified,
        "fixed_trace_positions_prebound": prebound,
        "public_replay_rows_passed": int(
            summary.get("public_replay_rows_passed") or 0
        ),
        "included_public_fit": bool(summary.get("included_public_fit")),
        "excluded_discovery_predicted": bool(
            summary.get("excluded_discovery_predicted")
        ),
        "candidate_promoted": bool(
            summary.get("candidate_promoted") and exact_shift_certified
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--tuple-report", type=Path, required=True)
    parser.add_argument("--corridor-plan", type=Path, required=True)
    parser.add_argument("--resume", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prefix", default="preseed_mt_shift_shard")
    parser.add_argument("--corridor-rank", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--target-round", type=int, default=1)
    parser.add_argument("--target-position", type=int, required=True)
    parser.add_argument("--shifts", default="0:5")
    parser.add_argument("--time-limit", type=float, default=3600.0)
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    shifts = shift_values(args.shifts)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for shift in shifts:
        output, checkpoint = shard_paths(args.output_dir, args.prefix, shift)
        if args.skip_existing and output.exists():
            result = summarize_output(
                output,
                shift,
                None,
                target_round=args.target_round,
                target_position=args.target_position,
            )
        else:
            command = build_shard_command(
                constraints=args.constraints,
                tuple_report=args.tuple_report,
                corridor_plan=args.corridor_plan,
                resume=args.resume,
                output=output,
                checkpoint=checkpoint,
                corridor_rank=args.corridor_rank,
                rounds=args.rounds,
                target_round=args.target_round,
                target_position=args.target_position,
                shift=shift,
                time_limit=args.time_limit,
                threads=args.threads,
            )
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    timeout=max(1.0, float(args.time_limit)) + 120.0,
                )
                returncode: int | None = int(completed.returncode)
            except subprocess.TimeoutExpired:
                returncode = 124
            result = summarize_output(
                output,
                shift,
                returncode,
                target_round=args.target_round,
                target_position=args.target_position,
            )
        results.append(result)
        report = {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "numpy-mt19937-fixed-trace-shift-supervisor",
            "summary": {
                "target_round": int(args.target_round),
                "target_position": int(args.target_position),
                "shifts_planned": shifts,
                "shifts_terminal": sum(item["terminal"] for item in results),
                "shifts_unsat": sum(item.get("status") == "unsat" for item in results),
                "shifts_exactly_certified": sum(
                    bool(item.get("exact_shift_certified")) for item in results
                ),
                "shifts_unknown_or_missing": sum(
                    item.get("status") in {"unknown", "missing"} for item in results
                ),
                "candidate_promoted": any(
                    item.get("candidate_promoted") for item in results
                ),
                "all_shards_terminal": len(results) == len(shifts)
                and all(item["terminal"] for item in results),
                "exact_profile_rejected": len(results) == len(shifts)
                and all(
                    item.get("status") == "unsat"
                    and item.get("exact_shift_certified")
                    for item in results
                ),
            },
            "shards": results,
            "safety": {
                "holdout_labels_opened": False,
                "state_persisted": False,
                "raw_stream_persisted": False,
                "submission_writes": False,
            },
        }
        atomic_json(args.output.resolve(), report)
        if result.get("candidate_promoted"):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
