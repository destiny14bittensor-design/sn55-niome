#!/usr/bin/env python3
"""Race NIOME's read-only seed signals against the first published score.

The monitor deliberately uses only public endpoints plus a contract URL already
delivered to this miner.  It never prints or persists that URL, request headers,
W&B credentials, or full score rows.  Results are an append-only JSONL timeline
and a compact JSON summary in the selected task artifact directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TASKS_URL = "https://niome-api.genomes.io/api/v3/tasks"
SCORES_URL = "https://niome-api.genomes.io/api/v3/miners/scores"
WANDB_GRAPHQL_URL = "https://api.wandb.ai/graphql"
WANDB_ENTITY = "genomes"
WANDB_PROJECT = "niome"
WANDB_RUN = "non2mca3"
SEED_RE = re.compile(r"Generated seeds:\s*([0-9][0-9, ]*)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_seeds(raw: Any) -> list[int]:
    if raw in (None, "", 0, "0"):
        return []
    try:
        values = [int(part.strip()) for part in str(raw).split(",") if part.strip()]
    except (TypeError, ValueError):
        return []
    return values if values and all(value != 0 for value in values) else []


def read_json(url: str, *, method: str = "GET", body: dict | None = None) -> Any:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {
        "Accept": "application/json",
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
        "User-Agent": "niome-seed-signal-race/1",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def task_history_seed(task_id: str) -> list[int]:
    query = urlencode({"page": 1, "per_page": 100, "_": time.time_ns()})
    payload = read_json(f"{TASKS_URL}?{query}")
    for task in payload.get("items", []):
        if task.get("id") == task_id:
            contract = ((task.get("content") or {}).get("contract") or {})
            return parse_seeds(contract.get("seed"))
    return []


def first_score_time(task_id: str) -> str | None:
    query = urlencode(
        {"task_id": task_id, "page": 1, "per_page": 1, "_": time.time_ns()}
    )
    payload = read_json(f"{SCORES_URL}?{query}")
    items = payload.get("items", [])
    if not items:
        return None
    item = items[0]
    # Do not trust the query filter implicitly.  A proxy/backend regression that
    # ignores task_id must not turn a previous round's score into a false signal.
    if str(item.get("task_id") or "") != task_id:
        return None
    return str(item.get("created_at") or "") or None


def signed_contract_seed(contract_url: str | None) -> list[int]:
    if not contract_url:
        return []
    payload = read_json(contract_url)
    return parse_seeds(payload.get("seed") if isinstance(payload, dict) else None)


def wandb_tail() -> tuple[list[int], str | None, int, int]:
    query = """
      query SeedTail($entity: String!, $project: String!, $run: String!) {
        project(name: $project, entityName: $entity) {
          run(name: $run) {
            eventsLineCount
            eventsTail
            logLineCount
            logLines(last: 30) {
              edges { node { line timestamp } }
            }
          }
        }
      }
    """
    payload = read_json(
        WANDB_GRAPHQL_URL,
        method="POST",
        body={
            "query": query,
            "variables": {
                "entity": WANDB_ENTITY,
                "project": WANDB_PROJECT,
                "run": WANDB_RUN,
            },
        },
    )
    run = (((payload.get("data") or {}).get("project") or {}).get("run") or {})
    match_values: list[int] = []
    match_time = None
    for edge in ((run.get("logLines") or {}).get("edges") or []):
        node = edge.get("node") or {}
        match = SEED_RE.search(str(node.get("line") or ""))
        if match:
            match_values = parse_seeds(match.group(1))
            match_time = str(node.get("timestamp") or "") or None
    events_tail = run.get("eventsTail")
    try:
        event_count = len(json.loads(events_tail)) if events_tail else 0
    except (TypeError, ValueError, json.JSONDecodeError):
        event_count = -1
    return (
        match_values,
        match_time,
        int(run.get("logLineCount") or 0),
        event_count,
    )


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_envelope(task_dir: Path) -> tuple[str, str | None]:
    envelope = json.loads((task_dir / "request_envelope.json").read_text())
    task = envelope.get("task") or {}
    task_id = str(task.get("id") or task_dir.name)
    contract_url = task.get("contract_url")
    return task_id, str(contract_url) if contract_url else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_dir", type=Path)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=4 * 60 * 60)
    parser.add_argument("--settle-seconds", type=float, default=30.0)
    args = parser.parse_args()

    task_dir = args.task_dir.resolve()
    task_id, contract_url = load_envelope(task_dir)
    timeline_path = task_dir / "seed_signal_race.jsonl"
    summary_path = task_dir / "seed_signal_race_summary.json"
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    first: dict[str, dict[str, Any]] = {}
    last_errors: dict[str, str] = {}
    terminal_sources: dict[str, dict[str, Any]] = {}
    last_counts: tuple[int, int] | None = None
    all_seed_sources_at = None
    wandb_baseline: tuple[tuple[int, ...], str | None] | None = None

    # The run is long-lived, so logLines initially contains the previous task's
    # seed.  Establish a marker before polling and only accept a different log
    # record as a signal for this task.  The current public task must still be
    # seedless when the baseline is taken; otherwise we accept matching values.
    try:
        public_seed_at_start = task_history_seed(task_id)
        baseline_seeds, baseline_at, line_count, event_count = wandb_tail()
        last_counts = (line_count, event_count)
        if not public_seed_at_start:
            wandb_baseline = (tuple(baseline_seeds), baseline_at)
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        public_seed_at_start = []

    # Baseline network latency is setup time, not part of the requested watch
    # duration.  Start the timeout clock only after the marker is established.
    started = time.monotonic()

    append_jsonl(
        timeline_path,
        {"at": utc_now(), "event": "monitor_started", "task_id": task_id},
    )

    while not stopping and time.monotonic() - started < args.timeout:
        observations: dict[str, tuple[list[int], str | None]] = {}
        try:
            observations["task-history-cache-bust"] = (
                task_history_seed(task_id),
                None,
            )
            last_errors.pop("task-history-cache-bust", None)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
            last_errors["task-history-cache-bust"] = type(error).__name__

        try:
            score_time = first_score_time(task_id)
            if score_time and "score" not in first:
                first["score"] = {"observed_at": utc_now(), "source_at": score_time}
                append_jsonl(
                    timeline_path,
                    {
                        "at": first["score"]["observed_at"],
                        "event": "first_score_observed",
                        "source_at": score_time,
                        "task_id": task_id,
                    },
                )
            last_errors.pop("score", None)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
            last_errors["score"] = type(error).__name__

        if contract_url:
            try:
                observations["signed-contract"] = (
                    signed_contract_seed(contract_url),
                    None,
                )
                last_errors.pop("signed-contract", None)
            except HTTPError as error:
                observed_at = utc_now()
                terminal_sources["signed-contract"] = {
                    "observed_at": observed_at,
                    "http_status": error.code,
                }
                append_jsonl(
                    timeline_path,
                    {
                        "at": observed_at,
                        "event": "source_unavailable",
                        "source": "signed-contract",
                        "http_status": error.code,
                        "task_id": task_id,
                    },
                )
                last_errors.pop("signed-contract", None)
                contract_url = None
            except (URLError, TimeoutError, ValueError, OSError) as error:
                # Network failures are transient; keep the URL in memory and retry.
                last_errors["signed-contract"] = type(error).__name__

        try:
            seeds, source_at, line_count, event_count = wandb_tail()
            marker = (tuple(seeds), source_at)
            if wandb_baseline is None or marker != wandb_baseline:
                observations["wandb-logLines"] = (seeds, source_at)
            counts = (line_count, event_count)
            if counts != last_counts:
                last_counts = counts
                append_jsonl(
                    timeline_path,
                    {
                        "at": utc_now(),
                        "event": "wandb_counts",
                        "log_line_count": line_count,
                        "events_tail_items": event_count,
                    },
                )
            last_errors.pop("wandb-logLines", None)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
            last_errors["wandb-logLines"] = type(error).__name__

        for source, (seeds, source_at) in observations.items():
            if not seeds or source in first:
                continue
            first[source] = {
                "observed_at": utc_now(),
                "source_at": source_at,
                "seeds": seeds,
            }
            append_jsonl(
                timeline_path,
                {
                    "at": first[source]["observed_at"],
                    "event": "seed_observed",
                    "source": source,
                    "source_at": source_at,
                    "seeds": seeds,
                    "task_id": task_id,
                },
            )

        public_seed_sources = {
            source for source in ("task-history-cache-bust", "wandb-logLines")
            if source in first
        }
        if len(public_seed_sources) == 2:
            all_seed_sources_at = all_seed_sources_at or time.monotonic()
            if time.monotonic() - all_seed_sources_at >= args.settle_seconds:
                break

        summary = {
            "task_id": task_id,
            "started_at": datetime.fromtimestamp(
                time.time() - (time.monotonic() - started), timezone.utc
            ).isoformat(),
            "updated_at": utc_now(),
            "first": first,
            "last_errors": last_errors,
            "terminal_sources": terminal_sources,
            "wandb_counts": {
                "log_lines": last_counts[0] if last_counts else None,
                "events_tail_items": last_counts[1] if last_counts else None,
            },
        }
        atomic_json(summary_path, summary)
        time.sleep(max(0.5, args.interval))

    append_jsonl(
        timeline_path,
        {"at": utc_now(), "event": "monitor_stopped", "task_id": task_id},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
