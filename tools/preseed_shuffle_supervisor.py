#!/usr/bin/env python3
"""Continuously promote completed public shuffle rounds into solver artifacts.

The W&B console is sequential: a newly fetched task appears long before all
miner queries finish.  This supervisor refreshes the lightweight leak report,
but runs the expensive historical-metagraph constraint build only when the
number of *complete* rounds increases.  The constraint builder independently
enforces the same observation floor, so a supervisor bug cannot promote a
partial round.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


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


def completed_round_count(leak_report: dict[str, Any]) -> int:
    return int((leak_report.get("summary") or {}).get("round_count") or 0)


def should_refresh_constraints(
    leak_report: dict[str, Any], state: dict[str, Any]
) -> bool:
    completed = completed_round_count(leak_report)
    promoted = int(state.get("constraint_rounds") or 0)
    return completed > promoted


def run_checked(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, timeout=900)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--poll-seconds", type=float, default=120.0)
    parser.add_argument("--minimum-observations", type=int, default=200)
    parser.add_argument(
        "--leak-report",
        type=Path,
        default=Path("artifacts/research/preseed_shuffle_leak_audit.json"),
    )
    parser.add_argument(
        "--constraints-report",
        type=Path,
        default=Path("artifacts/research/preseed_numpy_shuffle_constraints.json"),
    )
    parser.add_argument(
        "--fisher-report",
        type=Path,
        default=Path("artifacts/research/preseed_fisher_yates_model.json"),
    )
    parser.add_argument(
        "--prefix-report",
        type=Path,
        default=Path("artifacts/research/preseed_shuffle_prefix_domains.json"),
    )
    parser.add_argument(
        "--prefix-tuple-report",
        type=Path,
        default=Path("artifacts/research/preseed_shuffle_prefix_tuples.json"),
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("artifacts/research/preseed_shuffle_supervisor.json"),
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while not stopping:
        state = read_json((root / args.state).resolve(), {})
        try:
            run_checked(
                [
                    sys.executable,
                    "tools/preseed_shuffle_leak_audit.py",
                    "--minimum-observations",
                    str(max(1, args.minimum_observations)),
                    "--output",
                    str(args.leak_report),
                ],
                root,
            )
            leak = read_json((root / args.leak_report).resolve(), {})
            if should_refresh_constraints(leak, state):
                command = [
                    sys.executable,
                    "tools/preseed_numpy_shuffle_constraints.py",
                    "--discovery",
                    str(args.discovery),
                    "--minimum-observations",
                    str(max(1, args.minimum_observations)),
                    "--output",
                    str(args.constraints_report),
                ]
                for dataset in args.dataset:
                    command.extend(("--dataset", str(dataset)))
                run_checked(command, root)
                run_checked(
                    [
                        sys.executable,
                        "tools/preseed_fisher_yates_model.py",
                        "--constraints",
                        str(args.constraints_report),
                        "--max-rounds",
                        "8",
                        "--output",
                        str(args.fisher_report),
                    ],
                    root,
                )
                run_checked(
                    [
                        sys.executable,
                        "tools/preseed_shuffle_prefix_tuples.py",
                        "--constraints",
                        str(args.constraints_report),
                        "--depth",
                        "4",
                        "--max-states",
                        "50000",
                        "--output",
                        str(args.prefix_tuple_report),
                    ],
                    root,
                )
                run_checked(
                    [
                        sys.executable,
                        "tools/preseed_shuffle_prefix_domains.py",
                        "--constraints",
                        str(args.constraints_report),
                        "--depth",
                        "8",
                        "--max-states",
                        "250000",
                        "--output",
                        str(args.prefix_report),
                    ],
                    root,
                )
                constraints = read_json((root / args.constraints_report).resolve(), {})
                state["constraint_rounds"] = int(
                    (constraints.get("summary") or {}).get("rounds") or 0
                )
                state["last_promotion_at"] = datetime.now(timezone.utc).isoformat()
            state.update(
                {
                    "status": "healthy",
                    "completed_rounds": completed_round_count(leak),
                    "incomplete_rounds": int(
                        (leak.get("summary") or {}).get("incomplete_round_count") or 0
                    ),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "last_error": None,
                }
            )
        except Exception as error:
            state.update(
                {
                    "status": "error",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "last_error": type(error).__name__,
                }
            )
        atomic_json((root / args.state).resolve(), state)
        print(json.dumps({"event": "shuffle_supervisor", **state}, sort_keys=True), flush=True)
        if args.once:
            break
        time.sleep(max(10.0, args.poll_seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
