#!/usr/bin/env python3
"""Build a historical, time-split local-score calibration artifact."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from niome_subnet.dashboard.calibration import build_calibration_model


DEFAULT_SCORE_URL = "https://niome-api.genomes.io/api/v3/miners/scores"


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def parse_source(raw: str) -> tuple[str, Path, str]:
    parts = raw.split("=", 2)
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError("source must be NAME=ARTIFACT_ROOT=HOTKEY")
    return parts[0], Path(parts[1]).resolve(), parts[2]


def candidate_rows(sources: list[tuple[str, Path, str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source, root, hotkey in sources:
        if not root.exists():
            continue
        for local_path in root.glob("*/local_validation.json"):
            local = load_json(local_path)
            if local.get("score_semantics") != "unknown-seed-holdout-estimate":
                continue
            if not isinstance(local.get("final_score"), (int, float)):
                continue
            task_dir = local_path.parent
            status = load_json(task_dir / "status.json")
            bridge = load_json(task_dir / "seed_bridge_status.json")
            # An optimized bridge upload changes the S3 object, so its official
            # result is not a label for the original unknown-seed estimate.
            if (task_dir / "seed_bridge_local_validation.json").exists():
                continue
            if bridge.get("state") == "complete":
                continue
            diagnostics = load_json(task_dir / "builder_diagnostics.json")
            builder_policy = diagnostics.get("builder_policy")
            if isinstance(builder_policy, dict):
                builder_policy = builder_policy.get("policy_id")
            rows.append({
                "source": source,
                "artifact_root": str(root),
                "hotkey": hotkey,
                "task_id": task_dir.name,
                "received_at": status.get("received_at"),
                "submission_sha256": status.get("submission_sha256"),
                "builder_policy": builder_policy,
                "local_score": float(local["final_score"]),
            })
    return rows


def fetch_scoreboard(score_url: str, task_id: str) -> tuple[str, list[dict[str, Any]]]:
    try:
        response = httpx.get(score_url, params={"task_id": task_id}, timeout=15.0)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        return task_id, [item for item in (items or []) if isinstance(item, dict)]
    except Exception:
        return task_id, []


def attach_official(rows: list[dict[str, Any]], score_url: str, workers: int) -> list[dict[str, Any]]:
    task_ids = sorted({str(item["task_id"]) for item in rows})
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        fetched = dict(pool.map(lambda task_id: fetch_scoreboard(score_url, task_id), task_ids))
    paired: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in sorted(rows, key=lambda row: str(row.get("received_at") or "")):
        match = next(
            (score for score in fetched.get(str(item["task_id"]), []) if score.get("miner_hotkey") == item["hotkey"]),
            None,
        )
        if not match or not isinstance(match.get("final_score"), (int, float)):
            continue
        dedupe = (str(item["task_id"]), str(item.get("submission_sha256") or item["source"]))
        if dedupe in seen:
            continue
        seen.add(dedupe)
        paired.append({
            "task_id": item["task_id"],
            "received_at": item.get("received_at"),
            "submission_sha256": item.get("submission_sha256"),
            "builder_policy": item.get("builder_policy"),
            "local_score": item["local_score"],
            "official_score": float(match["final_score"]),
            "source": item["source"],
        })
    return paired


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", action="append", type=parse_source, required=True)
    parser.add_argument("--score-url", default=DEFAULT_SCORE_URL)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()
    candidates = candidate_rows(args.source)
    paired = attach_official(candidates, args.score_url, args.workers)
    model = build_calibration_model(paired)
    model["build"] = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "candidate_records": len(candidates),
        "paired_records": len(paired),
        "sources": [name for name, _, _ in args.source],
    }
    atomic_write(args.output.resolve(), model)
    summary = model["global"]
    print(json.dumps({
        "output": str(args.output.resolve()),
        "records": summary["records"],
        "ready": summary["ready"],
        "point_correction_applied": summary["point_correction_applied"],
        "point_offset": summary["point_offset"],
        "backtest": summary["backtest"],
        "interval_residual": [summary["residual_p10"], summary["residual_p90"]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
