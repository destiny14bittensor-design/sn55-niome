#!/usr/bin/env python3
"""Launch one read-only seed-signal race for each newly captured task."""

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
        "not_before_epoch": time.time(),
        "started_at": utc_now(),
        "completed_task_ids": [],
        "active_task_id": None,
    }


def discover_tasks(
    artifact_root: Path, not_before_epoch: float, completed_task_ids: set[str]
) -> list[tuple[float, str, Path]]:
    candidates = []
    for envelope_path in artifact_root.glob("*/request_envelope.json"):
        try:
            modified = envelope_path.stat().st_mtime
            if modified < not_before_epoch:
                continue
            envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
            task_id = str((envelope.get("task") or {}).get("id") or "")
        except (OSError, ValueError, TypeError):
            continue
        if not task_id or task_id in completed_task_ids:
            continue
        candidates.append((modified, task_id, envelope_path.parent))
    return sorted(candidates)


def record_child_result(
    state: dict[str, Any],
    completed: set[str],
    task_id: str,
    return_code: int,
) -> None:
    """Complete only successful races so interrupted tasks remain retryable."""
    state["active_task_id"] = None
    state["last_return_code"] = return_code
    state["updated_at"] = utc_now()
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
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--race-interval", type=float, default=3.0)
    parser.add_argument("--race-timeout", type=float, default=4 * 60 * 60)
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()

    artifact_root = args.artifact_root.resolve()
    state_path = (args.state or artifact_root / "seed_signal_supervisor.json").resolve()
    race_script = Path(__file__).with_name("seed_signal_race.py").resolve()
    state = load_state(state_path)
    completed = set(str(value) for value in state.get("completed_task_ids") or [])
    stopping = False
    child: subprocess.Popen | None = None

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

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
        state["active_task_id"] = task_id
        state["active_started_at"] = utc_now()
        state["updated_at"] = utc_now()
        state.pop("stopped_at", None)
        atomic_json(state_path, state)
        print(json.dumps({"event": "race_started", "task_id": task_id}), flush=True)
        child = subprocess.Popen(
            [
                sys.executable,
                str(race_script),
                str(task_dir),
                "--interval",
                str(max(0.5, args.race_interval)),
                "--timeout",
                str(max(60.0, args.race_timeout)),
                "--settle-seconds",
                "30",
            ]
        )
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
                {"event": "race_completed", "task_id": task_id, "return_code": return_code}
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
