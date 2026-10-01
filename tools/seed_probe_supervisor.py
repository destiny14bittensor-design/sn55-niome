#!/usr/bin/env python3
"""Start a read-only score-fingerprint pipeline for each new NIOME task."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any


REQUIRED_FILES = (
    "contract.json",
    "hbb_reference.json",
    "cell_types.json",
    "submission.json",
    "status.json",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "started_at": utc_now(),
        "not_before_epoch": time.time(),
        "completed_task_ids": [],
        "active_task_id": None,
    }


def discover_tasks(
    artifact_root: Path,
    not_before_epoch: float,
    completed_task_ids: set[str],
) -> list[tuple[float, str, Path]]:
    candidates = []
    for status_path in artifact_root.glob("*/status.json"):
        try:
            modified = status_path.stat().st_mtime
            if modified < not_before_epoch:
                continue
            status = json.loads(status_path.read_text(encoding="utf-8"))
            task_id = str(status.get("task_id") or status_path.parent.name)
            if str(status.get("state") or "") != "complete":
                continue
            if not all((status_path.parent / name).is_file() for name in REQUIRED_FILES):
                continue
        except (OSError, TypeError, ValueError):
            continue
        if not task_id or task_id in completed_task_ids:
            continue
        candidates.append((modified, task_id, status_path.parent))
    return sorted(candidates)


def record_child_result(
    state: dict[str, Any],
    completed: set[str],
    task_id: str,
    return_code: int,
) -> None:
    """Persist success, while leaving interrupted/failed tasks retryable."""
    state.update(
        {
            "active_task_id": None,
            "last_return_code": return_code,
            "updated_at": utc_now(),
        }
    )
    if return_code == 0:
        completed.add(task_id)
        state["last_completed_task_id"] = task_id
        state.pop("last_failed_task_id", None)
    else:
        state["last_failed_task_id"] = task_id
    state["completed_task_ids"] = sorted(completed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_root", type=Path)
    parser.add_argument("--miner-hotkey", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--chromosome", type=Path, default=Path("data/chr11.fa"))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--seed-min", type=int, default=100)
    parser.add_argument("--seed-max", type=int, default=999)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--score-timeout", type=float, default=3 * 60 * 60)
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()

    artifact_root = args.artifact_root.resolve()
    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    state_path = (args.state or output_root / "seed_probe_supervisor.json").resolve()
    state = load_state(state_path)
    completed = set(str(value) for value in state.get("completed_task_ids") or [])
    inverter = Path(__file__).with_name("seed_probe_inverter.py").resolve()
    stopping = False
    child: subprocess.Popen | None = None

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while not stopping:
        candidates = discover_tasks(
            artifact_root,
            float(state["not_before_epoch"]),
            completed,
        )
        if not candidates:
            state["updated_at"] = utc_now()
            atomic_json(state_path, state)
            time.sleep(max(1.0, args.poll_interval))
            continue

        _modified, task_id, task_dir = candidates[0]
        database = output_root / f"{task_id}.probe.sqlite3"
        status = output_root / f"{task_id}.probe.status.json"
        solution = output_root / f"{task_id}.probe.solution.json"
        command = [
            sys.executable,
            str(inverter),
            "build",
            "--task-dir",
            str(task_dir),
            "--miner-hotkey",
            args.miner_hotkey,
            "--chromosome",
            str(args.chromosome.resolve()),
            "--database",
            str(database),
            "--status",
            str(status),
            "--seed-min",
            str(args.seed_min),
            "--seed-max",
            str(args.seed_max),
            "--workers",
            str(max(1, args.workers)),
            "--solve-output",
            str(solution),
            "--wait-score-seconds",
            str(max(0.0, args.score_timeout)),
            "--poll-interval",
            str(max(1.0, args.poll_interval)),
        ]
        state.update(
            {
                "active_task_id": task_id,
                "active_started_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        state.pop("stopped_at", None)
        atomic_json(state_path, state)
        print(json.dumps({"event": "probe_started", "task_id": task_id}), flush=True)
        child = subprocess.Popen(command)
        while child.poll() is None and not stopping:
            state["updated_at"] = utc_now()
            atomic_json(state_path, state)
            time.sleep(max(1.0, args.poll_interval))
        if stopping and child.poll() is None:
            child.terminate()
        return_code = child.wait()
        child = None
        record_child_result(state, completed, task_id, return_code)
        atomic_json(state_path, state)
        print(
            json.dumps(
                {"event": "probe_completed", "task_id": task_id, "return_code": return_code}
            ),
            flush=True,
        )

    state["active_task_id"] = None
    state["stopped_at"] = utc_now()
    state["updated_at"] = utc_now()
    atomic_json(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
