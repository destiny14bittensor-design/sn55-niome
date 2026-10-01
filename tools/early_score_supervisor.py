#!/usr/bin/env python3
"""Launch one authorized, read-only early-score monitor per owned task.

The supervisor discovers only local request envelopes, passes an explicit list
of owned miner hotkeys to the monitor, and writes no submissions.  A task is
completed only when its monitor exits successfully after observing the public
batch (or reaching its configured timeout).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_state(path: Path, lookback_seconds: float) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    now = time.time()
    return {
        "schema_version": 1,
        "mode": "owned-artifact-public-score-observation-only",
        "not_before_epoch": now - max(0.0, lookback_seconds),
        "started_at": utc_now(),
        "completed_task_ids": [],
        "active_task_id": None,
        "safety": {"submission_writes": False, "authorized_surfaces_only": True},
    }


def discover_tasks(
    artifact_roots: Iterable[Path],
    not_before_epoch: float,
    completed_task_ids: set[str],
) -> list[tuple[float, str, Path]]:
    by_task: dict[str, tuple[float, str, Path]] = {}
    for root in artifact_roots:
        if not root.exists():
            continue
        for envelope_path in root.glob("*/request_envelope.json"):
            try:
                modified = envelope_path.stat().st_mtime
                if modified < not_before_epoch:
                    continue
                envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
                task_id = str((envelope.get("task") or {}).get("id") or "")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
                continue
            if not task_id or task_id in completed_task_ids:
                continue
            candidate = (modified, task_id, envelope_path)
            previous = by_task.get(task_id)
            if previous is None or modified > previous[0]:
                by_task[task_id] = candidate
    return sorted(by_task.values())


def record_child_result(
    state: dict[str, Any], completed: set[str], task_id: str, return_code: int
) -> None:
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


def _deadline_from_presigned_url(url: Any) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    query = parse_qs(urlparse(url).query)
    try:
        if query.get("Expires"):
            return datetime.fromtimestamp(
                float(query["Expires"][0]), tz=timezone.utc
            ).isoformat(timespec="microseconds")
        if query.get("X-Amz-Date") and query.get("X-Amz-Expires"):
            issued = datetime.strptime(
                query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ"
            ).replace(tzinfo=timezone.utc)
            return datetime.fromtimestamp(
                issued.timestamp() + float(query["X-Amz-Expires"][0]),
                tz=timezone.utc,
            ).isoformat(timespec="microseconds")
    except (ValueError, TypeError, OverflowError):
        return None
    return None


def build_submission_channel_evidence(
    envelope_path: Path, output_path: Path
) -> dict[str, Any]:
    """Sanitize owned S3/bridge state into the monitor's strict channel schema."""
    task_dir = envelope_path.parent
    try:
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        envelope = {}
    task_id = str((envelope.get("task") or {}).get("id") or "")
    deadline = _deadline_from_presigned_url(envelope.get("presigned_url"))

    def load(name: str) -> dict[str, Any]:
        try:
            value = json.loads((task_dir / name).read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}

    bridge = load("seed_bridge_status.json")
    submission = load("status.json")
    overwrite_enabled = bridge.get("same_round_overwrite_enabled") is True
    deadline_dt = datetime.fromisoformat(deadline) if deadline else None
    writable = bool(
        overwrite_enabled
        and deadline_dt
        and deadline_dt > datetime.now(timezone.utc)
        and bridge.get("state") not in {"complete", "failed", "disabled_same_round_overwrite"}
    )
    build_seconds = submission.get("upload_start_elapsed_seconds")
    try:
        build_seconds = max(0.0, float(build_seconds))
    except (TypeError, ValueError):
        build_seconds = None
    try:
        total = float(submission.get("upload_elapsed_seconds"))
        upload_seconds = max(0.0, total - float(build_seconds or 0))
    except (TypeError, ValueError):
        upload_seconds = None
    evidence = {
        "schema_version": 1,
        "task_id": task_id,
        "status": "open" if writable else (
            "policy_disabled" if not overwrite_enabled else "closed_or_expired"
        ),
        "writable": writable,
        "independent_of_score_source": bool(envelope.get("presigned_url")),
        "deadline_at": deadline,
        "build_seconds": build_seconds,
        "upload_seconds": upload_seconds,
        "safety_seconds": 15.0,
        "derived_at": utc_now(),
        "source": "owned-request-envelope-and-bridge-status",
        "stores_presigned_url": False,
    }
    atomic_json(output_path, evidence)
    return evidence


def monitor_command(
    *,
    script: Path,
    task_id: str,
    envelope_path: Path,
    output_root: Path,
    targets: list[str],
    interval: float,
    timeout: float,
    wandb_public_run: str | None = None,
) -> list[str]:
    task_dir = envelope_path.parent
    output_dir = output_root / task_id
    output_dir.mkdir(parents=True, exist_ok=True)
    channel_path = output_dir / "submission_channel_status.json"
    build_submission_channel_evidence(envelope_path, channel_path)
    command = [
        sys.executable,
        str(script),
        "--task-id",
        task_id,
        "--request-envelope",
        str(envelope_path),
        "--task-dir",
        str(output_dir),
        "--interval",
        str(max(0.2, interval)),
        "--timeout",
        str(max(60.0, timeout)),
        "--stop-after-batch",
        "--submission-status",
        str(channel_path),
    ]
    for target in targets:
        command.extend(("--target", target))
    if wandb_public_run:
        command.extend(("--wandb-public-run", wandb_public_run))
    for name in ("bridge_status.json", "status.json", "validation_status.json"):
        candidate = task_dir / name
        if candidate.exists():
            command.extend(("--owned-artifact", str(candidate)))
    authorized = task_dir / "authorized_score_events.jsonl"
    if authorized.exists():
        command.extend(("--authorized-events", str(authorized)))
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, action="append", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--target", action="append", required=True, metavar="ALIAS=HOTKEY")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--monitor-interval", type=float, default=0.5)
    parser.add_argument("--monitor-timeout", type=float, default=4 * 60 * 60)
    parser.add_argument("--lookback-seconds", type=float, default=3 * 60 * 60)
    parser.add_argument(
        "--wandb-public-run",
        help="Optional public ENTITY/PROJECT/RUN passed to each monitor",
    )
    args = parser.parse_args()

    roots = [path.resolve() for path in args.artifact_root]
    output_root = args.output_root.resolve()
    state_path = args.state.resolve()
    monitor_script = Path(__file__).with_name("early_score_monitor.py").resolve()
    state = load_state(state_path, args.lookback_seconds)
    state.pop("stopped_at", None)
    state["updated_at"] = utc_now()
    atomic_json(state_path, state)
    completed = {str(value) for value in state.get("completed_task_ids") or []}
    stopping = False
    child: subprocess.Popen[Any] | None = None

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while not stopping:
        candidates = discover_tasks(
            roots, float(state.get("not_before_epoch") or 0), completed
        )
        if not candidates:
            state["updated_at"] = utc_now()
            atomic_json(state_path, state)
            time.sleep(max(0.5, args.poll_interval))
            continue
        _modified, task_id, envelope_path = candidates[0]
        state.update(
            {
                "active_task_id": task_id,
                "active_started_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        state.pop("stopped_at", None)
        atomic_json(state_path, state)
        command = monitor_command(
            script=monitor_script,
            task_id=task_id,
            envelope_path=envelope_path,
            output_root=output_root,
            targets=list(args.target),
            interval=args.monitor_interval,
            timeout=args.monitor_timeout,
            wandb_public_run=args.wandb_public_run,
        )
        print(json.dumps({"event": "early_score_monitor_started", "task_id": task_id}), flush=True)
        child = subprocess.Popen(command)
        while child.poll() is None and not stopping:
            state["updated_at"] = utc_now()
            atomic_json(state_path, state)
            time.sleep(max(0.5, args.poll_interval))
        if stopping and child.poll() is None:
            child.terminate()
        return_code = child.wait()
        effective_return_code = -15 if stopping else return_code
        child = None
        record_child_result(state, completed, task_id, effective_return_code)
        atomic_json(state_path, state)
        print(
            json.dumps(
                {
                    "event": "early_score_monitor_finished",
                    "task_id": task_id,
                    "return_code": effective_return_code,
                }
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
