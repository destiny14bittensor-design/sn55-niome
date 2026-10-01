#!/usr/bin/env python3
"""Observe authorized early-score signals without submitting or changing state.

The monitor uses public NIOME score endpoints and operator-owned files only.  A
signed contract may be read only when its URL is already present in the owned
request envelope.  Persisted output is deliberately allow-listed: URLs,
headers, credentials, score values, and complete API rows are never written.

The output consists of an append-only, sanitized JSONL timeline and an atomic
summary.  ``actionable`` is a deliberately strict evidence gate, not an action:
this program contains no submission code.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import time
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SCORES_URL = "https://niome-api.genomes.io/api/v3/miners/scores"
WANDB_GRAPHQL_URL = "https://api.wandb.ai/graphql"
USER_AGENT = "niome-authorized-early-score-monitor/1"
TIMESTAMP_KEYS = (
    "score_created_at",
    "validation_completed_at",
    "validated_at",
    "validation_started_at",
    "validation_at",
    "source_at",
    "created_at",
    "updated_at",
)
SCORE_KEYS = ("final_score", "validation_score", "score")
WANDB_FETCHED_TASK = re.compile(r"Fetched task ([0-9a-f-]{36})")
WANDB_SCORE_ROW = re.compile(
    r"MinerScore\(uid=(\d+),.*?final_score=([^,\)]+)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_time(value: Any) -> str | None:
    parsed = parse_time(value)
    return parsed.isoformat(timespec="microseconds") if parsed else None


def read_json_url(url: str) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
            "User-Agent": USER_AGENT,
        },
    )
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def read_public_wandb_logs(
    entity: str, project: str, run: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    """Read a public W&B console tail without credentials or persisted raw logs."""
    query = """
    query PublicRunLogs(
      $entity: String!, $project: String!, $run: String!, $limit: Int!
    ) {
      project(name: $project, entityName: $entity) {
        run(name: $run) {
          logLines(last: $limit) { edges { node { timestamp line } } }
        }
      }
    }
    """
    request = Request(
        WANDB_GRAPHQL_URL,
        data=json.dumps(
            {
                "query": query,
                "variables": {
                    "entity": entity,
                    "project": project,
                    "run": run,
                    "limit": max(1, min(500, int(limit))),
                },
            }
        ).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
            "User-Agent": USER_AGENT,
        },
    )
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read())
    if payload.get("errors"):
        raise RuntimeError("public W&B GraphQL returned errors")
    run_payload = (((payload.get("data") or {}).get("project") or {}).get("run") or {})
    connection = run_payload.get("logLines") or {}
    return [
        node
        for edge in connection.get("edges") or []
        if isinstance(edge, dict)
        and isinstance((node := edge.get("node")), dict)
    ]


def public_wandb_score_observations(
    lines: Iterable[dict[str, Any]],
    *,
    task_id: str,
    observed_at: str,
    armed_at: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Extract score-presence facts for the task active in a public log.

    Score values are used only as a parse gate and are never returned. The
    association is armed by an exact ``Fetched task`` log; a later fetch for a
    different task disarms it.
    """
    ordered = sorted(
        (line for line in lines if isinstance(line, dict)),
        key=lambda line: canonical_time(line.get("timestamp")) or "",
    )
    active_task = task_id if armed_at else None
    active_since = canonical_time(armed_at)
    observations: list[dict[str, Any]] = []
    for item in ordered:
        timestamp = canonical_time(item.get("timestamp"))
        if not timestamp:
            continue
        if active_since and parse_time(timestamp) < parse_time(active_since):
            continue
        line = item.get("line")
        if not isinstance(line, str):
            continue
        fetched = WANDB_FETCHED_TASK.search(line)
        if fetched:
            active_task = fetched.group(1)
            active_since = timestamp if active_task == task_id else None
            continue
        if active_task != task_id or "Scores:" not in line:
            continue
        seen_uids: set[int] = set()
        for match in WANDB_SCORE_ROW.finditer(line):
            try:
                uid = int(match.group(1))
                float(match.group(2))  # Parse gate only; value is never retained.
            except (TypeError, ValueError):
                continue
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            observations.append(
                {
                    "kind": "individual_score",
                    "source": "public-wandb-scores",
                    "target": f"uid:{uid}",
                    "first_seen": observed_at,
                    "source_at": timestamp,
                }
            )
        if observations:
            break
    return observations, active_since


def parse_public_wandb_run(value: str | None) -> tuple[str, str, str] | None:
    if not value:
        return None
    parts = value.strip().split("/")
    if len(parts) != 3 or not all(parts):
        raise ValueError("--wandb-public-run must be ENTITY/PROJECT/RUN")
    return parts[0], parts[1], parts[2]


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_request_envelope(path: Path | None, expected_task: str) -> str | None:
    """Return an in-memory signed URL only when an owned envelope matches task."""
    if path is None:
        return None
    payload = load_json(path)
    task = payload.get("task") or {}
    envelope_task = str(task.get("id") or payload.get("task_id") or "")
    if envelope_task != expected_task:
        raise ValueError("request envelope task_id does not match --task-id")
    candidate = task.get("contract_url") or payload.get("contract_url")
    return str(candidate) if candidate else None


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("items") or []
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def public_individual_rows(task_id: str, hotkey: str) -> list[dict[str, Any]]:
    query = urlencode(
        {
            "task_id": task_id,
            "miner_hotkey": hotkey,
            "page": 1,
            "per_page": 100,
            "_": time.time_ns(),
        }
    )
    # Never trust server-side filtering alone.
    return [
        row
        for row in _rows(read_json_url(f"{SCORES_URL}?{query}"))
        if str(row.get("task_id") or "") == task_id
        and str(row.get("miner_hotkey") or "") == hotkey
    ]


def public_cluster_rows(task_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(1, 11):
        query = urlencode(
            {
                "task_id": task_id,
                "page": page,
                "per_page": 100,
                "_": time.time_ns(),
            }
        )
        batch = _rows(read_json_url(f"{SCORES_URL}?{query}"))
        rows.extend(row for row in batch if str(row.get("task_id") or "") == task_id)
        if len(batch) < 100:
            break
    # Deduplicate without retaining IDs in output.
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("id") or ""), str(row.get("miner_hotkey") or ""))
        unique[key] = row
    return list(unique.values())


def row_source_at(row: dict[str, Any]) -> str | None:
    for key in ("created_at", "scored_at", "validated_at", "updated_at"):
        value = canonical_time(row.get(key))
        if value:
            return value
    return None


def summarize_cluster(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    timestamps = [parsed for row in rows if (parsed := parse_time(row_source_at(row)))]
    count = len(rows)
    first = min(timestamps) if timestamps else None
    last = max(timestamps) if timestamps else None
    return {
        "cohort_size": count,
        "first_source_at": first.isoformat(timespec="microseconds") if first else None,
        "last_source_at": last.isoformat(timespec="microseconds") if last else None,
        "timestamp_spread_seconds": (last - first).total_seconds() if first and last else None,
    }


def _walk_mappings(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mappings(child)


def owned_timestamp_observations(
    payload: Any, *, source: str, task_id: str, observed_at: str
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for mapping in _walk_mappings(payload):
        embedded_task = mapping.get("task_id")
        if embedded_task not in (None, "", task_id):
            continue
        for key in TIMESTAMP_KEYS:
            source_at = canonical_time(mapping.get(key))
            identity = (key, source_at or "")
            if not source_at or identity in seen:
                continue
            seen.add(identity)
            observations.append(
                {
                    "kind": "owned_timestamp",
                    "source": source,
                    "event": key,
                    "first_seen": observed_at,
                    "source_at": source_at,
                }
            )
    return observations


def signed_contract_observations(
    payload: Any, *, observed_at: str, task_id: str
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    source_at = next(
        (
            normalized
            for key in TIMESTAMP_KEYS
            if (normalized := canonical_time(payload.get(key)))
        ),
        None,
    )
    result = owned_timestamp_observations(
        payload, source="owned-signed-contract", task_id=task_id, observed_at=observed_at
    )
    if any(key in payload and payload.get(key) is not None for key in SCORE_KEYS):
        result.append(
            {
                "kind": "individual_score",
                "source": "owned-signed-contract",
                "target": "owned-miner",
                "first_seen": observed_at,
                "source_at": source_at,
            }
        )
    return result


def read_authorized_jsonl(path: Path, task_id: str, observed_at: str) -> list[dict[str, Any]]:
    """Read allow-listed event facts from an operator-owned JSONL adapter."""
    output: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        try:
            raw = json.loads(raw_line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(raw, dict) or str(raw.get("task_id") or "") != task_id:
            continue
        kind = str(raw.get("kind") or raw.get("event") or "")
        source = f"authorized-jsonl:{path.name}"
        source_at = canonical_time(raw.get("source_at") or raw.get("created_at") or raw.get("at"))
        if kind in {"individual_score", "miner_score", "score_observed"}:
            output.append(
                {
                    "kind": "individual_score",
                    "source": source,
                    "target": str(raw.get("target") or raw.get("miner") or "owned-miner")[:80],
                    "first_seen": observed_at,
                    "source_at": source_at,
                }
            )
        elif kind in {"validation", "validation_started", "validation_completed"}:
            output.append(
                {
                    "kind": "owned_timestamp",
                    "source": source,
                    "event": kind,
                    "first_seen": observed_at,
                    "source_at": source_at,
                }
            )
        elif kind in {"batch_publication", "score_cluster"}:
            try:
                size = max(0, int(raw.get("cohort_size") or 0))
            except (TypeError, ValueError):
                size = 0
            output.append(
                {
                    "kind": "batch_publication",
                    "source": source,
                    "first_seen": observed_at,
                    "source_at": source_at,
                    "cohort_size": size,
                }
            )
    return output


def load_submission_channel(path: Path | None, expected_task: str | None = None) -> dict[str, Any]:
    """Load only allow-listed status from an operator-owned channel artifact."""
    if path is None:
        return {
            "provided": False,
            "status": "not_provided",
            "writable": False,
            "independent": False,
            "deadline_at": None,
            "build_seconds": None,
            "upload_seconds": None,
            "safety_seconds": None,
        }
    raw = load_json(path)
    artifact_task = str(raw.get("task_id") or "")
    if expected_task and artifact_task and artifact_task != expected_task:
        raise ValueError("submission channel task_id does not match monitored task")
    return {
        "provided": True,
        "status": str(raw.get("status") or "unknown")[:80],
        "writable": raw.get("writable") is True,
        "independent": raw.get("independent_of_score_source") is True,
        "deadline_at": canonical_time(raw.get("deadline_at")),
        "build_seconds": _nonnegative_float(raw.get("build_seconds")),
        "upload_seconds": _nonnegative_float(raw.get("upload_seconds")),
        "safety_seconds": _nonnegative_float(raw.get("safety_seconds"), default=0.0),
    }


def _nonnegative_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


@dataclass
class MonitorState:
    task_id: str
    batch_spread_seconds: float = 1.0
    minimum_early_lead_seconds: float = 1.0
    batch_settle_seconds: float = 0.0
    started_at: str = field(default_factory=utc_now)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    individual_scores: list[dict[str, Any]] = field(default_factory=list)
    cluster: dict[str, Any] = field(default_factory=lambda: summarize_cluster([]))
    cluster_first_seen: str | None = None
    cluster_last_changed_at: str | None = None
    cluster_complete_at: str | None = None
    cluster_revision: int = 0
    cluster_snapshots: list[dict[str, Any]] = field(default_factory=list)
    last_errors: dict[str, str] = field(default_factory=dict)

    def add_observation(self, observation: dict[str, Any]) -> bool:
        source = str(observation.get("source") or "unknown")
        kind = str(observation.get("kind") or "unknown")
        event_name = str(observation.get("event") or "")[:80]
        source_key = f"{source}:{kind}" + (f":{event_name}" if event_name else "")
        first_seen = canonical_time(observation.get("first_seen")) or utc_now()
        source_at = canonical_time(observation.get("source_at"))
        changed = False
        if source_key not in self.sources:
            self.sources[source_key] = {
                "kind": kind,
                "first_seen": first_seen,
                "source_at": source_at,
            }
            if event_name:
                self.sources[source_key]["event"] = event_name
            changed = True
        if kind == "individual_score":
            target = str(observation.get("target") or "owned-miner")[:80]
            identity = (source, target)
            if not any(
                (item["source"], item["target"]) == identity
                for item in self.individual_scores
            ):
                self.individual_scores.append(
                    {
                        "source": source,
                        "target": target,
                        "first_seen": first_seen,
                        "source_at": source_at,
                        "cluster_revision_at_first_seen": self.cluster_revision,
                    }
                )
                changed = True
        return changed

    def set_cluster(self, cluster: dict[str, Any], observed_at: str) -> bool:
        observed_at = canonical_time(observed_at) or utc_now()
        changed = cluster != self.cluster
        if int(cluster.get("cohort_size") or 0) > 0 and self.cluster_first_seen is None:
            self.cluster_first_seen = observed_at
            self.sources["public-task-cluster:batch_publication"] = {
                "kind": "batch_publication",
                "first_seen": observed_at,
                "source_at": cluster.get("first_source_at"),
            }
            changed = True
        if changed:
            self.cluster = dict(cluster)
            self.cluster_revision += 1
            self.cluster_last_changed_at = observed_at
            self.cluster_complete_at = None
            self.cluster_snapshots.append(
                {
                    "observed_at": observed_at,
                    "cohort_size": int(cluster.get("cohort_size") or 0),
                    "first_source_at": cluster.get("first_source_at"),
                    "last_source_at": cluster.get("last_source_at"),
                    "timestamp_spread_seconds": cluster.get("timestamp_spread_seconds"),
                }
            )
            self.cluster_snapshots = self.cluster_snapshots[-50:]
        self.mark_cluster_stable(observed_at)
        return changed

    def mark_cluster_stable(self, observed_at: str) -> bool:
        """Confirm batch completion only after the cohort stops changing."""
        if int(self.cluster.get("cohort_size") or 0) <= 0 or self.cluster_complete_at:
            return False
        last_change = parse_time(self.cluster_last_changed_at)
        current = parse_time(observed_at)
        if last_change is None or current is None:
            return False
        if (current - last_change).total_seconds() < max(0.0, self.batch_settle_seconds):
            return False
        self.cluster_complete_at = current.isoformat(timespec="microseconds")
        self.sources["public-task-cluster:batch_complete"] = {
            "kind": "batch_completion",
            "first_seen": self.cluster_complete_at,
            "source_at": self.cluster.get("last_source_at"),
        }
        return True

    def summary(self, channel: dict[str, Any], updated_at: str) -> dict[str, Any]:
        updated_at = canonical_time(updated_at) or utc_now()
        earliest = min(
            self.individual_scores,
            key=lambda item: parse_time(item["first_seen"])
            or datetime.max.replace(tzinfo=timezone.utc),
            default=None,
        )
        individual_at = parse_time(earliest["first_seen"]) if earliest else None
        cluster_at = parse_time(self.cluster_complete_at)
        cohort_progressed_after_individual = bool(
            earliest
            and self.cluster_revision
            > int(earliest.get("cluster_revision_at_first_seen") or 0)
        )
        if not earliest:
            lead_status, lead_seconds, early = "no_individual_score", None, False
        elif cluster_at is None:
            reference_at = parse_time(updated_at) or datetime.now(timezone.utc)
            lead_seconds = (reference_at - individual_at).total_seconds()
            early = lead_seconds >= self.minimum_early_lead_seconds
            lead_status = "provisional_before_batch" if early else "within_observation_skew"
        else:
            lead_seconds = (cluster_at - individual_at).total_seconds()
            early = (
                cohort_progressed_after_individual
                and lead_seconds >= self.minimum_early_lead_seconds
            )
            if early:
                lead_status = "confirmed_before_batch_completion"
            elif not cohort_progressed_after_individual:
                lead_status = "same_cohort_snapshot"
            else:
                lead_status = "not_early"

        deadline = parse_time(channel.get("deadline_at"))
        build = channel.get("build_seconds")
        upload = channel.get("upload_seconds")
        safety = channel.get("safety_seconds")
        complete_budget = deadline is not None and None not in (build, upload, safety)
        available = (
            (deadline - individual_at).total_seconds()
            if deadline and individual_at
            else None
        )
        required = build + upload + safety if complete_budget else None
        net_budget = (
            available - required
            if available is not None and required is not None
            else None
        )
        positive_budget = net_budget is not None and net_budget > 0
        channel_view = dict(channel)
        channel_view.update(
            {
                "available_seconds_at_individual_detection": available,
                "required_seconds": required,
                "net_budget_seconds": net_budget,
            }
        )

        spread = self.cluster.get("timestamp_spread_seconds")
        cohort_size = int(self.cluster.get("cohort_size") or 0)
        if cohort_size == 0:
            classification = "pending"
        elif cohort_size == 1:
            classification = "single-row-insufficient"
        elif spread is None:
            classification = "timestamp-unavailable"
        elif float(spread) <= self.batch_spread_seconds:
            classification = "batch"
        else:
            classification = "staggered"

        gates = {
            "early_individual_score": early,
            "owned_channel_writable": channel.get("writable") is True,
            "channel_independent_of_score_source": channel.get("independent") is True,
            "positive_build_upload_budget": positive_budget,
        }
        actionable = all(gates.values())
        reasons = [name for name, passed in gates.items() if not passed]
        return {
            "schema_version": 1,
            "task_id": self.task_id,
            "started_at": self.started_at,
            "updated_at": updated_at,
            "mode": "observation-only",
            "sources": dict(sorted(self.sources.items())),
            "score_publication": {
                **self.cluster,
                "first_seen": self.cluster_first_seen,
                "complete_first_seen": self.cluster_complete_at,
                "settle_seconds": self.batch_settle_seconds,
                "classification": classification,
                "batch_spread_threshold_seconds": self.batch_spread_seconds,
                "snapshots": self.cluster_snapshots,
            },
            "earliest_authorized_individual_score": earliest,
            "lead_time": {
                "status": lead_status,
                "seconds": lead_seconds,
                "minimum_evidence_seconds": self.minimum_early_lead_seconds,
                "reference": "public-task-cluster-stable-completion",
                "cohort_progressed_after_individual": cohort_progressed_after_individual,
            },
            "submission_channel": channel_view,
            "decision": {
                "actionable": actionable,
                "gates": gates,
                "failed_gates": reasons,
                "note": "Evidence only; this monitor never submits or mutates a submission.",
            },
            "last_errors": dict(sorted(self.last_errors.items())),
            "safety": {
                "public_and_operator_owned_inputs_only": True,
                "uses_wandb_credentials": False,
                "stores_urls": False,
                "stores_headers_or_credentials": False,
                "stores_full_score_rows": False,
                "performs_submissions": False,
            },
        }


def public_observations(
    task_id: str, targets: dict[str, str], observed_at: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = public_cluster_rows(task_id)
    observations: list[dict[str, Any]] = []
    for target, hotkey in targets.items():
        matches = [row for row in rows if str(row.get("miner_hotkey") or "") == hotkey]
        if not matches:
            continue
        source_at = min(
            (row_source_at(row) for row in matches if row_source_at(row)), default=None
        )
        observations.append(
            {
                "kind": "individual_score",
                "source": "public-exact-task-hotkey",
                "target": target,
                "first_seen": observed_at,
                "source_at": source_at,
            }
        )
    return observations, summarize_cluster(rows)


def fixture_frames(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    frames = payload.get("frames") if isinstance(payload, dict) else payload
    if not isinstance(frames, list):
        raise ValueError("fixture must contain a frames list")
    return [frame for frame in frames if isinstance(frame, dict)]


def fixture_observations(
    frame: dict[str, Any], task_id: str, targets: dict[str, str], observed_at: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    individuals = frame.get("individual_rows") or {}
    if isinstance(individuals, dict):
        for target in targets:
            rows = _rows(individuals.get(target) or [])
            rows = [row for row in rows if str(row.get("task_id") or "") == task_id]
            if rows:
                timestamps = [value for row in rows if (value := row_source_at(row))]
                observations.append(
                    {
                        "kind": "individual_score",
                        "source": "public-exact-task-hotkey",
                        "target": target,
                        "first_seen": observed_at,
                        "source_at": min(timestamps, default=None),
                    }
                )
    cluster = [
        row
        for row in _rows(frame.get("cluster_rows") or [])
        if str(row.get("task_id") or "") == task_id
    ]
    return observations, summarize_cluster(cluster)


def _parse_targets(values: list[str]) -> dict[str, str]:
    targets: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--target must be ALIAS=HOTKEY")
        alias, hotkey = value.split("=", 1)
        if not alias.strip() or not hotkey.strip():
            raise ValueError("--target must be ALIAS=HOTKEY")
        targets[alias.strip()[:80]] = hotkey.strip()
    if not targets:
        raise ValueError("at least one --target is required")
    return targets


def run(args: argparse.Namespace) -> dict[str, Any]:
    targets = _parse_targets(args.target)
    contract_url = load_request_envelope(args.request_envelope, args.task_id)
    wandb_public_run = parse_public_wandb_run(
        getattr(args, "wandb_public_run", None)
    )
    state = MonitorState(
        args.task_id,
        batch_spread_seconds=args.batch_spread_seconds,
        minimum_early_lead_seconds=args.minimum_early_lead_seconds,
        batch_settle_seconds=getattr(args, "batch_settle_seconds", 0.0),
    )
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    append_jsonl(
        args.timeline,
        {
            "at": state.started_at,
            "event": "monitor_started",
            "mode": "observation-only",
            "task_id": args.task_id,
            "target_count": len(targets),
        },
    )

    frames = fixture_frames(args.fixture) if args.fixture else None
    started = time.monotonic()
    index = 0
    wandb_task_armed_at: str | None = None
    last_wandb_poll = float("-inf")
    while not stopping and time.monotonic() - started <= args.timeout:
        frame = frames[index] if frames is not None and index < len(frames) else None
        if frames is not None and frame is None:
            break
        observed_at = canonical_time(frame.get("observed_at")) if frame else None
        observed_at = observed_at or utc_now()
        try:
            if frame is None:
                observations, cluster = public_observations(args.task_id, targets, observed_at)
            else:
                observations, cluster = fixture_observations(
                    frame, args.task_id, targets, observed_at
                )
            state.last_errors.pop("public-scores", None)
        except Exception as error:  # sanitized type only; monitor must survive transient I/O
            state.last_errors["public-scores"] = type(error).__name__
            observations, cluster = [], state.cluster

        if (
            frame is None
            and wandb_public_run
            and time.monotonic() - last_wandb_poll
            >= max(0.2, float(getattr(args, "wandb_interval", 1.0)))
        ):
            last_wandb_poll = time.monotonic()
            try:
                entity, project, run_id = wandb_public_run
                wandb_lines = read_public_wandb_logs(
                    entity,
                    project,
                    run_id,
                    limit=100 if wandb_task_armed_at else 500,
                )
                wandb_observations, wandb_task_armed_at = (
                    public_wandb_score_observations(
                        wandb_lines,
                        task_id=args.task_id,
                        observed_at=observed_at,
                        armed_at=wandb_task_armed_at,
                    )
                )
                observations.extend(wandb_observations)
                state.last_errors.pop("public-wandb", None)
            except Exception as error:
                state.last_errors["public-wandb"] = type(error).__name__

        if state.set_cluster(cluster, observed_at):
            append_jsonl(
                args.timeline,
                {
                    "at": observed_at,
                    "event": "score_cohort_changed",
                    "task_id": args.task_id,
                    "cohort_size": cluster.get("cohort_size"),
                    "first_source_at": cluster.get("first_source_at"),
                    "last_source_at": cluster.get("last_source_at"),
                    "timestamp_spread_seconds": cluster.get("timestamp_spread_seconds"),
                },
            )

        if frame is not None and isinstance(frame.get("signed_contract"), dict):
            # Fixture parity still requires an owned envelope to authorize this source.
            if contract_url:
                observations.extend(
                    signed_contract_observations(
                        frame["signed_contract"], observed_at=observed_at, task_id=args.task_id
                    )
                )
        elif frame is None and contract_url:
            try:
                observations.extend(
                    signed_contract_observations(
                        read_json_url(contract_url), observed_at=observed_at, task_id=args.task_id
                    )
                )
                state.last_errors.pop("owned-signed-contract", None)
            except Exception as error:
                state.last_errors["owned-signed-contract"] = type(error).__name__

        for artifact in args.owned_artifact:
            try:
                observations.extend(
                    owned_timestamp_observations(
                        load_json(artifact),
                        source=f"owned-artifact:{artifact.name}",
                        task_id=args.task_id,
                        observed_at=observed_at,
                    )
                )
            except Exception as error:
                state.last_errors[f"owned-artifact:{artifact.name}"] = type(error).__name__
        for adapter in args.authorized_events:
            try:
                observations.extend(read_authorized_jsonl(adapter, args.task_id, observed_at))
            except Exception as error:
                state.last_errors[f"authorized-jsonl:{adapter.name}"] = type(error).__name__

        if frame is not None:
            for raw in frame.get("authorized_observations") or []:
                if isinstance(raw, dict):
                    observations.append({**raw, "first_seen": raw.get("first_seen") or observed_at})

        for observation in observations:
            if state.add_observation(observation):
                event = {
                    "at": observation.get("first_seen") or observed_at,
                    "event": "authorized_signal_first_seen",
                    "task_id": args.task_id,
                    "kind": observation.get("kind"),
                    "source": observation.get("source"),
                    "source_at": observation.get("source_at"),
                }
                if observation.get("target"):
                    event["target"] = observation.get("target")
                append_jsonl(args.timeline, event)

        if frame is not None and isinstance(frame.get("submission_channel"), dict):
            raw_channel = frame["submission_channel"]
            channel = {
                "provided": True,
                "status": str(raw_channel.get("status") or "unknown")[:80],
                "writable": raw_channel.get("writable") is True,
                "independent": raw_channel.get("independent_of_score_source") is True,
                "deadline_at": canonical_time(raw_channel.get("deadline_at")),
                "build_seconds": _nonnegative_float(raw_channel.get("build_seconds")),
                "upload_seconds": _nonnegative_float(raw_channel.get("upload_seconds")),
                "safety_seconds": _nonnegative_float(
                    raw_channel.get("safety_seconds"), default=0.0
                ),
            }
        else:
            try:
                channel = load_submission_channel(args.submission_status, args.task_id)
            except Exception as error:
                state.last_errors["submission-channel"] = type(error).__name__
                channel = load_submission_channel(None)
        summary = state.summary(channel, observed_at)
        atomic_json(args.summary, summary)

        index += 1
        if (
            args.once
            or getattr(args, "stop_after_batch", False)
            and summary["score_publication"].get("complete_first_seen") is not None
            or frames is not None
            and index >= len(frames)
        ):
            break
        time.sleep(max(0.1, args.interval))

    append_jsonl(
        args.timeline,
        {"at": utc_now(), "event": "monitor_stopped", "task_id": args.task_id},
    )
    final_channel = channel if "channel" in locals() else load_submission_channel(None)
    return state.summary(final_channel, utc_now())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--target", action="append", default=[], metavar="ALIAS=HOTKEY")
    parser.add_argument("--request-envelope", type=Path)
    parser.add_argument("--owned-artifact", type=Path, action="append", default=[])
    parser.add_argument("--authorized-events", type=Path, action="append", default=[])
    parser.add_argument(
        "--wandb-public-run",
        help="Optional public ENTITY/PROJECT/RUN console source; no credentials used",
    )
    parser.add_argument("--wandb-interval", type=float, default=1.0)
    parser.add_argument("--submission-status", type=Path)
    parser.add_argument(
        "--task-dir",
        type=Path,
        help=(
            "Per-task output directory; defaults to early_score_timeline.jsonl "
            "and early_score_summary.json within it"
        ),
    )
    parser.add_argument("--timeline", type=Path, help="Explicit append-only JSONL output")
    parser.add_argument("--summary", type=Path, help="Explicit atomic summary JSON output")
    parser.add_argument(
        "--fixture", type=Path, help="Offline JSON frames; disables network score polling"
    )
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=4 * 60 * 60)
    parser.add_argument("--batch-spread-seconds", type=float, default=1.0)
    parser.add_argument(
        "--batch-settle-seconds",
        type=float,
        default=3.0,
        help="Unchanged cohort duration required before batch completion is confirmed",
    )
    parser.add_argument("--minimum-early-lead-seconds", type=float, default=1.0)
    parser.add_argument(
        "--stop-after-batch",
        action="store_true",
        help="Stop once the public task cohort exists; no later signal can precede it",
    )
    parser.add_argument("--once", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.timeline is None:
            if args.task_dir is None:
                raise ValueError("provide --task-dir or --timeline")
            args.timeline = args.task_dir / "early_score_timeline.jsonl"
        if args.summary is None:
            if args.task_dir is None:
                raise ValueError("provide --task-dir or --summary")
            args.summary = args.task_dir / "early_score_summary.json"
        run(args)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
