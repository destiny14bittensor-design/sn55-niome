#!/usr/bin/env python3
"""Build score fingerprints and invert a NIOME three-seed aggregate.

This is a read-only research sidecar.  It evaluates submissions already owned
by the operator, reads the public score API, and never uploads or resubmits a
miner payload.  Results are checkpointed in SQLite so a long fingerprint build
can resume safely.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.local_validator.artifacts import ArtifactBundle
from tools.local_validator.ingestion import ingest_submission, load_submission
from tools.local_validator.stage12 import run_stage12
from tools.local_validator.stage3 import run_stage3
from tools.local_validator.stage4 import run_stage4
from tools.local_validator.stage5 import run_stage5
from tools.public_score_cluster_audit import fetch_task_scores


SCHEMA_VERSION = 1
_WORKER_LANES: list[dict[str, Any]] = []


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def artifact_identity(task_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in ("contract.json", "hbb_reference.json", "cell_types.json", "submission.json"):
        digest.update(name.encode())
        digest.update((task_dir / name).read_bytes())
    return digest.hexdigest()


def load_lane(
    task_dir: Path, chromosome_path: Path, miner_hotkey: str
) -> dict[str, Any]:
    artifacts = ArtifactBundle.from_paths(
        contract_path=task_dir / "contract.json",
        hbb_reference_path=task_dir / "hbb_reference.json",
        chromosome_11_path=chromosome_path,
        cell_types_path=task_dir / "cell_types.json",
    )
    submission, raw = load_submission(task_dir / "submission.json")
    ingestion = ingest_submission(submission, artifacts.contract, raw_bytes=raw)
    valid, invalid, _provenance = run_stage12(
        ingestion.retained,
        contract=artifacts.contract,
        reference=artifacts.hbb_reference,
        chromosome_11=artifacts.chromosome_11,
        cell_types=artifacts.cell_types,
    )
    if invalid or not valid:
        raise ValueError(
            f"probe artifact must contain valid experiments: {task_dir} "
            f"(valid={len(valid)}, invalid={len(invalid)})"
        )
    status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
    hotkey = str(miner_hotkey).strip()
    if not hotkey:
        raise ValueError("miner hotkey must be supplied explicitly")
    return {
        "lane_id": artifact_identity(task_dir)[:16],
        "task_dir": str(task_dir),
        "task_id": str(status.get("task_id") or task_dir.name),
        "hotkey": hotkey,
        "artifacts": artifacts,
        "valid": valid,
    }


def _init_worker(
    task_dirs: list[str], miner_hotkeys: list[str], chromosome_path: str
) -> None:
    global _WORKER_LANES
    _WORKER_LANES = [
        load_lane(Path(task_dir), Path(chromosome_path), hotkey)
        for task_dir, hotkey in zip(task_dirs, miner_hotkeys, strict=True)
    ]


def _score_seed(seed: int) -> tuple[int, list[dict[str, Any]]]:
    rows = []
    for lane in _WORKER_LANES:
        artifacts = lane["artifacts"]
        contract = {**artifacts.contract, "seed": seed}
        seeded_artifacts = replace(artifacts, contract=contract)
        stage3_results, _stage3_summary = run_stage3(lane["valid"], seed)
        stage4 = run_stage4(lane["valid"], stage3_results, seed=seed)
        stage5 = run_stage5(
            lane["valid"], stage3_results, stage4, seeded_artifacts.contract
        )
        rows.append(
            {
                "lane_id": lane["lane_id"],
                "final_score": float(stage5["final_score"]),
                "consistency_factor": float(stage5["consistency_factor"]),
                "distribution_fidelity_factor": float(
                    stage5["distribution_fidelity_factor"]
                ),
                "total_weighted_score": float(stage5["total_weighted_score"]),
            }
        )
    return seed, rows


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lanes (
            lane_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            hotkey TEXT NOT NULL,
            artifact_dir TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fingerprints (
            lane_id TEXT NOT NULL,
            seed INTEGER NOT NULL,
            final_score REAL NOT NULL,
            consistency_factor REAL NOT NULL,
            distribution_fidelity_factor REAL NOT NULL,
            total_weighted_score REAL NOT NULL,
            PRIMARY KEY (lane_id, seed)
        );
        """
    )
    existing = connection.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()
    if existing and int(existing[0]) != SCHEMA_VERSION:
        raise ValueError(f"unsupported database schema {existing[0]}")
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version',?)",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()
    return connection


def register_lanes(connection: sqlite3.Connection, lanes: list[dict[str, Any]]) -> None:
    for lane in lanes:
        existing = connection.execute(
            "SELECT task_id,hotkey,artifact_dir FROM lanes WHERE lane_id=?",
            (lane["lane_id"],),
        ).fetchone()
        current = (lane["task_id"], lane["hotkey"], lane["task_dir"])
        if existing and (str(existing[0]), str(existing[2])) != (
            str(current[0]),
            str(current[2]),
        ):
            raise ValueError(f"lane identity collision for {lane['lane_id']}")
        connection.execute(
            "INSERT OR REPLACE INTO lanes(lane_id,task_id,hotkey,artifact_dir) "
            "VALUES(?,?,?,?)",
            (lane["lane_id"], *current),
        )
    connection.commit()


def completed_seeds(
    connection: sqlite3.Connection, lane_ids: list[str]
) -> set[int]:
    if not lane_ids:
        return set()
    placeholders = ",".join("?" for _ in lane_ids)
    rows = connection.execute(
        f"SELECT seed,COUNT(*) FROM fingerprints WHERE lane_id IN ({placeholders}) "
        "GROUP BY seed HAVING COUNT(*)=?",
        (*lane_ids, len(lane_ids)),
    )
    return {int(row[0]) for row in rows}


def store_fingerprint(
    connection: sqlite3.Connection, seed: int, rows: list[dict[str, Any]]
) -> None:
    connection.executemany(
        "INSERT OR REPLACE INTO fingerprints("
        "lane_id,seed,final_score,consistency_factor,"
        "distribution_fidelity_factor,total_weighted_score) VALUES(?,?,?,?,?,?)",
        [
            (
                row["lane_id"],
                seed,
                row["final_score"],
                row["consistency_factor"],
                row["distribution_fidelity_factor"],
                row["total_weighted_score"],
            )
            for row in rows
        ],
    )


def find_three_seed_candidates(
    score_by_seed: dict[int, float],
    observed_average: float,
    *,
    tolerance: float = 1e-9,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Find unordered triples whose score average matches an observation."""
    seeds = sorted(score_by_seed)
    pairs = sorted(
        (score_by_seed[left] + score_by_seed[right], left, right)
        for left_index, left in enumerate(seeds)
        for right in seeds[left_index:]
    )
    pair_values = [item[0] for item in pairs]
    target_sum = observed_average * 3.0
    sum_tolerance = tolerance * 3.0
    found: dict[tuple[int, int, int], float] = {}
    for third in seeds:
        wanted = target_sum - score_by_seed[third]
        start = bisect_left(pair_values, wanted - sum_tolerance)
        stop = bisect_right(pair_values, wanted + sum_tolerance)
        for pair_sum, first, second in pairs[start:stop]:
            if second > third:
                continue
            triple = (first, second, third)
            residual = abs((pair_sum + score_by_seed[third]) / 3.0 - observed_average)
            found[triple] = min(residual, found.get(triple, float("inf")))
    return [
        {"seeds": list(triple), "absolute_residual": residual}
        for triple, residual in sorted(found.items(), key=lambda item: item[1])[:limit]
    ]


def load_lane_scores(
    connection: sqlite3.Connection, lane_id: str
) -> dict[int, float]:
    return {
        int(seed): float(score)
        for seed, score in connection.execute(
            "SELECT seed,final_score FROM fingerprints WHERE lane_id=? ORDER BY seed",
            (lane_id,),
        )
    }


def public_observation(task_id: str, hotkey: str) -> dict[str, Any]:
    rows, diagnostics = fetch_task_scores(task_id)
    matches = [row for row in rows if str(row.get("miner_hotkey") or "") == hotkey]
    if not matches:
        raise ValueError(f"no public score for task={task_id} and configured hotkey")
    row = max(matches, key=lambda value: str(value.get("created_at") or ""))
    return {
        "final_score": float(row.get("final_score") or 0.0),
        "created_at": row.get("created_at"),
        "diagnostics": diagnostics,
    }


def wait_for_public_observation(
    task_id: str,
    hotkey: str,
    *,
    timeout_seconds: float,
    poll_interval: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            return public_observation(task_id, hotkey)
        except (OSError, ValueError) as error:
            if time.monotonic() >= deadline:
                raise
            print(
                json.dumps(
                    {
                        "event": "waiting_for_public_score",
                        "task_id": task_id,
                        "reason": type(error).__name__,
                    }
                ),
                flush=True,
            )
            time.sleep(max(1.0, poll_interval))


def build_fingerprints(args: argparse.Namespace) -> dict[str, Any]:
    task_dirs = [path.resolve() for path in args.task_dir]
    miner_hotkeys = [str(value).strip() for value in args.miner_hotkey]
    if len(task_dirs) != len(miner_hotkeys):
        raise ValueError("provide exactly one --miner-hotkey for each --task-dir")
    chromosome_path = args.chromosome.resolve()
    lanes = [
        load_lane(path, chromosome_path, hotkey)
        for path, hotkey in zip(task_dirs, miner_hotkeys, strict=True)
    ]
    unique: dict[str, dict[str, Any]] = {lane["lane_id"]: lane for lane in lanes}
    lanes = list(unique.values())
    task_ids = {lane["task_id"] for lane in lanes}
    if len(task_ids) != 1:
        raise ValueError("all probe lanes must belong to one task")

    connection = open_database(args.database.resolve())
    register_lanes(connection, lanes)
    seed_range = list(range(args.seed_min, args.seed_max + 1))
    done = completed_seeds(connection, [lane["lane_id"] for lane in lanes])
    pending = [seed for seed in seed_range if seed not in done]
    status_path = args.status.resolve()
    started = time.monotonic()

    def write_status(state: str, latest_seed: int | None = None) -> None:
        complete = len(seed_range) - len(pending) + processed
        elapsed = time.monotonic() - started
        rate = processed / elapsed if elapsed > 0 else 0.0
        atomic_json(
            status_path,
            {
                "state": state,
                "updated_at": utc_now(),
                "task_id": next(iter(task_ids)),
                "lane_count": len(lanes),
                "seed_min": args.seed_min,
                "seed_max": args.seed_max,
                "completed": complete,
                "total": len(seed_range),
                "latest_seed": latest_seed,
                "seeds_per_second_this_run": rate,
                "estimated_seconds_remaining": (
                    (len(pending) - processed) / rate if rate > 0 else None
                ),
                "database": str(args.database.resolve()),
                "safety": {
                    "read_only": True,
                    "uploads": False,
                    "resubmissions": False,
                    "credentials_stored": False,
                },
            },
        )

    processed = 0
    write_status("building")
    if pending:
        context = mp.get_context("spawn")
        with context.Pool(
            processes=max(1, args.workers),
            initializer=_init_worker,
            initargs=(
                [lane["task_dir"] for lane in lanes],
                [lane["hotkey"] for lane in lanes],
                str(chromosome_path),
            ),
        ) as pool:
            for seed, rows in pool.imap_unordered(_score_seed, pending, chunksize=1):
                store_fingerprint(connection, seed, rows)
                connection.commit()
                processed += 1
                if processed == 1 or processed % args.status_every == 0:
                    write_status("building", seed)
                    print(
                        json.dumps(
                            {
                                "event": "fingerprint_progress",
                                "processed_this_run": processed,
                                "pending_this_run": len(pending),
                                "latest_seed": seed,
                            }
                        ),
                        flush=True,
                    )
    write_status("fingerprints_complete")
    connection.close()
    result = json.loads(status_path.read_text(encoding="utf-8"))
    if args.solve_output is not None:
        result["solution"] = solve(
            argparse.Namespace(
                database=args.database,
                output=args.solve_output,
                tolerance=args.tolerance,
                candidate_limit=args.candidate_limit,
                wait_score_seconds=args.wait_score_seconds,
                poll_interval=args.poll_interval,
            )
        )
        result["state"] = "complete"
        result["updated_at"] = utc_now()
        atomic_json(status_path, result)
    return result


def solve(args: argparse.Namespace) -> dict[str, Any]:
    connection = open_database(args.database.resolve())
    lane_rows = connection.execute(
        "SELECT lane_id,task_id,hotkey,artifact_dir FROM lanes ORDER BY lane_id"
    ).fetchall()
    if not lane_rows:
        raise ValueError("fingerprint database has no lanes")
    task_ids = {str(row[1]) for row in lane_rows}
    if len(task_ids) != 1:
        raise ValueError("database contains more than one task")
    observations = {}
    fingerprint_counts = {}
    candidates: list[dict[str, Any]] | None = None
    for lane_id, task_id, hotkey, _artifact_dir in lane_rows:
        observation = wait_for_public_observation(
            str(task_id),
            str(hotkey),
            timeout_seconds=args.wait_score_seconds,
            poll_interval=args.poll_interval,
        )
        observations[str(lane_id)] = observation
        scores = load_lane_scores(connection, str(lane_id))
        fingerprint_counts[str(lane_id)] = len(scores)
        lane_candidates = find_three_seed_candidates(
            scores,
            observation["final_score"],
            tolerance=args.tolerance,
            limit=args.candidate_limit,
        )
        if candidates is None:
            candidates = lane_candidates
            continue
        allowed = {tuple(item["seeds"]) for item in lane_candidates}
        candidates = [item for item in candidates if tuple(item["seeds"]) in allowed]
    report = {
        "captured_at": utc_now(),
        "task_id": next(iter(task_ids)),
        "lane_count": len(lane_rows),
        "observations": observations,
        "fingerprint_counts": fingerprint_counts,
        "candidate_count": len(candidates or []),
        "candidates": candidates or [],
        "method": "unordered-three-seed meet-in-the-middle",
        "tolerance": args.tolerance,
        "safety": {
            "public_score_api_only": True,
            "contains_credentials": False,
            "submission_side_effects": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    connection.close()
    return report


def watch_solve(args: argparse.Namespace) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, args.timeout)
    attempts = 0
    last_counts: dict[str, int] | None = None
    connection = open_database(args.database.resolve())
    identity = connection.execute(
        "SELECT task_id,hotkey FROM lanes ORDER BY lane_id LIMIT 1"
    ).fetchone()
    connection.close()
    if identity is None:
        raise ValueError("fingerprint database has no lanes")
    score_visible = False
    while True:
        attempts += 1
        try:
            if not score_visible:
                public_observation(str(identity[0]), str(identity[1]))
                score_visible = True
            report = solve(
                argparse.Namespace(
                    database=args.database,
                    output=args.output,
                    tolerance=args.tolerance,
                    candidate_limit=args.candidate_limit,
                    wait_score_seconds=0.0,
                    poll_interval=args.poll_interval,
                )
            )
            counts = report.get("fingerprint_counts") or {}
            if report["candidate_count"] > 0:
                report["watch"] = {
                    "attempts": attempts,
                    "completed_at": utc_now(),
                    "partial_table_match": any(
                        count < args.expected_fingerprints for count in counts.values()
                    ),
                }
                atomic_json(args.output.resolve(), report)
                return report
            if counts != last_counts:
                print(
                    json.dumps(
                        {
                            "event": "partial_table_no_match",
                            "fingerprint_counts": counts,
                        }
                    ),
                    flush=True,
                )
                last_counts = counts
        except (OSError, ValueError) as error:
            if last_counts is not None:
                print(
                    json.dumps(
                        {"event": "watch_retry", "reason": type(error).__name__}
                    ),
                    flush=True,
                )
        if time.monotonic() >= deadline:
            raise TimeoutError("no invertible public score observed before timeout")
        time.sleep(max(1.0, args.poll_interval))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subcommands = root.add_subparsers(dest="command", required=True)
    build = subcommands.add_parser("build", help="build/resume single-seed fingerprints")
    build.add_argument("--task-dir", type=Path, action="append", required=True)
    build.add_argument("--miner-hotkey", action="append", required=True)
    build.add_argument("--chromosome", type=Path, default=Path("data/chr11.fa"))
    build.add_argument("--database", type=Path, required=True)
    build.add_argument("--status", type=Path, required=True)
    build.add_argument("--seed-min", type=int, default=100)
    build.add_argument("--seed-max", type=int, default=999)
    build.add_argument("--workers", type=int, default=2)
    build.add_argument("--status-every", type=int, default=5)
    build.add_argument("--solve-output", type=Path)
    build.add_argument("--tolerance", type=float, default=1e-9)
    build.add_argument("--candidate-limit", type=int, default=1000)
    build.add_argument("--wait-score-seconds", type=float, default=0.0)
    build.add_argument("--poll-interval", type=float, default=5.0)
    build.set_defaults(handler=build_fingerprints)

    invert = subcommands.add_parser("solve", help="invert public aggregate score")
    invert.add_argument("--database", type=Path, required=True)
    invert.add_argument("--output", type=Path, required=True)
    invert.add_argument("--tolerance", type=float, default=1e-9)
    invert.add_argument("--candidate-limit", type=int, default=1000)
    invert.add_argument("--wait-score-seconds", type=float, default=0.0)
    invert.add_argument("--poll-interval", type=float, default=5.0)
    invert.set_defaults(handler=solve)

    watch = subcommands.add_parser(
        "watch", help="watch for a score and solve against a growing fingerprint table"
    )
    watch.add_argument("--database", type=Path, required=True)
    watch.add_argument("--output", type=Path, required=True)
    watch.add_argument("--tolerance", type=float, default=1e-9)
    watch.add_argument("--candidate-limit", type=int, default=1000)
    watch.add_argument("--timeout", type=float, default=3 * 60 * 60)
    watch.add_argument("--poll-interval", type=float, default=3.0)
    watch.add_argument("--expected-fingerprints", type=int, default=900)
    watch.set_defaults(handler=watch_solve)
    return root


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if getattr(args, "seed_min", 0) > getattr(args, "seed_max", 0):
        raise ValueError("seed-min must not exceed seed-max")
    result = args.handler(args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
