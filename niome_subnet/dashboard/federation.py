"""Read-only federation of independent NIOME fleet dashboards."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import re
import time
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

from .state import parse_time


logger = logging.getLogger(__name__)

FEDERATION_SCHEMA_VERSION = 1
MAX_SOURCE_RESPONSE_BYTES = 2 * 1024 * 1024
SOURCE_STALE_SECONDS = 15.0
SOURCE_UNAVAILABLE_SECONDS = 60.0
_SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_SECRET_QUERY_RE = re.compile(
    r"(?i)(X-Amz-(?:Signature|Credential|Security-Token)|Signature)=[^&\s]+"
)
_SS58_ADDRESS_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{47,48}\b")
_SEVERITY = {"complete": 0, "healthy": 1, "warning": 2, "critical": 3}


@dataclass(frozen=True)
class FederationSourceConfig:
    source_id: str
    label: str
    state_url: str | None = None
    local: bool = False


@dataclass
class SourceObservation:
    config: FederationSourceConfig
    snapshot: dict[str, Any] | None = None
    last_success_at: datetime | None = None
    last_checked_at: datetime | None = None
    latency_ms: float | None = None
    error: str | None = None


def parse_remote_sources(raw: str | None) -> tuple[FederationSourceConfig, ...]:
    """Parse a fixed startup allow-list; request parameters never select upstreams."""
    if not raw or not raw.strip():
        return ()
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("NIOME_FEDERATION_REMOTE_SOURCES must be a JSON list")
    sources: list[FederationSourceConfig] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("each federation source must be an object")
        source_id = str(item.get("id") or "").strip()
        label = str(item.get("label") or source_id).strip()
        state_url = str(item.get("url") or "").strip()
        if not _SOURCE_ID_RE.fullmatch(source_id):
            raise ValueError(f"invalid federation source id: {source_id!r}")
        if source_id in seen:
            raise ValueError(f"duplicate federation source id: {source_id}")
        parsed = urlparse(state_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"invalid federation state URL for {source_id}")
        if parsed.username or parsed.password:
            raise ValueError("federation URLs must not embed credentials")
        sources.append(
            FederationSourceConfig(
                source_id=source_id,
                label=label or source_id,
                state_url=state_url,
            )
        )
        seen.add(source_id)
    return tuple(sources)


def _redact_string(value: str) -> str:
    value = _SECRET_QUERY_RE.sub(lambda match: f"{match.group(1)}=<redacted>", value)
    value = _URL_RE.sub("<redacted-url>", value)
    return _SS58_ADDRESS_RE.sub("<redacted-address>", value)


def _sanitize(value: Any) -> Any:
    """Remove credentials and host-local network details from federated output."""
    if isinstance(value, dict):
        cleaned = {}
        for key, child in value.items():
            if key in {
                "hotkey",
                "local_address",
                "peer_address",
                "response_headers",
                "response_excerpt",
                "presigned_url",
                "contract_url",
            }:
                continue
            cleaned[key] = _sanitize(child)
        return cleaned
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _score_for_miner(miner: dict[str, Any]) -> tuple[float | None, str | None]:
    current = miner.get("current") or {}
    official = current.get("official") or {}
    if official.get("published") and isinstance(official.get("score"), (int, float)):
        return float(official["score"]), "official"
    local = current.get("local") or {}
    semantics = str(local.get("score_semantics") or "")
    if (
        isinstance(local.get("score"), (int, float))
        and local.get("comparable_to_official") is not False
        and semantics
        not in {"unknown-seed-holdout-estimate", "provisional-seed-estimate"}
    ):
        return float(local["score"]), "local-exact"
    return None, None


def _source_age(observation: SourceObservation, now: datetime) -> float | None:
    generated = parse_time((observation.snapshot or {}).get("generated_at"))
    reference = generated or observation.last_success_at
    if reference is None:
        return None
    return max(0.0, (now - reference).total_seconds())


def _transport_status(age: float | None) -> str:
    if age is None or age > SOURCE_UNAVAILABLE_SECONDS:
        return "unreachable"
    if age > SOURCE_STALE_SECONDS:
        return "stale"
    return "healthy"


def _worst_status(values: Iterable[str]) -> str:
    return max(values, key=lambda value: _SEVERITY.get(value, 0), default="healthy")


def _official_task_records(miner: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    current = miner.get("current") or {}
    official = current.get("official") or {}
    current_task_id = current.get("task_id")
    if current_task_id and (
        official.get("rank") is not None or official.get("score") is not None
    ):
        records[str(current_task_id)] = {
            "received_at": current.get("received_at"),
            "rank": official.get("rank"),
            "score": official.get("score"),
        }
    for item in miner.get("history") or []:
        if not isinstance(item, dict):
            continue
        task_id = item.get("task_id")
        if not task_id or (
            item.get("official_rank") is None
            and item.get("official_score") is None
        ):
            continue
        records.setdefault(
            str(task_id),
            {
                "received_at": item.get("received_at"),
                "rank": item.get("official_rank"),
                "score": item.get("official_score"),
            },
        )
    return records


def _build_ranking_summary(miners: list[dict[str, Any]]) -> dict[str, Any]:
    records_by_miner: dict[str, dict[str, dict[str, Any]]] = {}
    task_times: dict[str, datetime] = {}
    for miner in miners:
        records = _official_task_records(miner)
        records_by_miner[str(miner.get("id"))] = records
        for task_id, record in records.items():
            received = parse_time(record.get("received_at"))
            if received and (
                task_id not in task_times or received > task_times[task_id]
            ):
                task_times[task_id] = received

    ranked_tasks = sorted(
        task_times,
        key=lambda task_id: task_times[task_id],
        reverse=True,
    )
    task_id = ranked_tasks[0] if ranked_tasks else None
    previous_task_id = ranked_tasks[1] if len(ranked_tasks) > 1 else None
    rows: list[dict[str, Any]] = []
    for miner in miners:
        records = records_by_miner.get(str(miner.get("id")), {})
        current_record = records.get(task_id or "", {})
        previous_record = records.get(previous_task_id or "", {})
        rank = current_record.get("rank")
        previous_rank = previous_record.get("rank")
        movement = None
        if isinstance(rank, (int, float)) and isinstance(
            previous_rank, (int, float)
        ):
            movement = int(previous_rank) - int(rank)
        rows.append(
            {
                "miner_id": miner.get("id"),
                "source_id": miner.get("source_id"),
                "source_label": miner.get("source_label"),
                "label": miner.get("label"),
                "uid": miner.get("uid"),
                "online": bool(miner.get("online")),
                "rank": rank,
                "score": current_record.get("score"),
                "previous_rank": previous_rank,
                "movement": movement,
            }
        )
    rows.sort(
        key=lambda row: (
            row["rank"] is None,
            row["rank"] if row["rank"] is not None else float("inf"),
            -(row["score"] or 0),
            str(row["label"] or ""),
        )
    )
    return {
        "task_id": task_id,
        "task_received_at": task_times[task_id].isoformat() if task_id else None,
        "previous_task_id": previous_task_id,
        "previous_task_received_at": (
            task_times[previous_task_id].isoformat()
            if previous_task_id
            else None
        ),
        "ranked_miners": sum(row["rank"] is not None for row in rows),
        "total_miners": len(rows),
        "rows": rows,
    }


def build_federated_state(
    observations: Iterable[SourceObservation],
    *,
    version: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Normalize source snapshots and compute task-safe cross-fleet comparisons."""
    now = now or datetime.now(timezone.utc)
    source_rows: list[dict[str, Any]] = []
    miners: list[dict[str, Any]] = []
    fleet_alerts: list[dict[str, Any]] = []
    blocks: list[int] = []

    for observation in observations:
        snapshot = observation.snapshot or {}
        source_fleet = snapshot.get("fleet") or {}
        source_chain = snapshot.get("chain") or {}
        age = _source_age(observation, now)
        transport = _transport_status(age)
        source_overall = str(source_fleet.get("overall") or "critical")
        reported_online = int(source_fleet.get("online") or 0)
        reported_total = int(source_fleet.get("total") or 0)
        source_rows.append(
            {
                "id": observation.config.source_id,
                "label": observation.config.label,
                "kind": "local" if observation.config.local else "remote",
                "transport": transport,
                "overall": source_overall,
                "reachable": transport != "unreachable",
                "generated_at": snapshot.get("generated_at"),
                "last_checked_at": (
                    observation.last_checked_at.isoformat()
                    if observation.last_checked_at
                    else None
                ),
                "snapshot_age_seconds": age,
                "latency_ms": observation.latency_ms,
                "error": observation.error,
                "version": snapshot.get("version"),
                "chain": {
                    "block": source_chain.get("block"),
                    "round_phase": source_chain.get("round_phase"),
                    "source": source_chain.get("source"),
                },
                "online": reported_online if transport != "unreachable" else 0,
                "reported_online": reported_online,
                "total": reported_total,
                "active_task_id": source_fleet.get("active_task_id"),
            }
        )
        block = source_chain.get("block")
        if transport != "unreachable" and isinstance(block, int):
            blocks.append(block)

        if transport != "healthy":
            severity = "critical" if transport == "unreachable" else "warning"
            fleet_alerts.append(
                {
                    "source_id": observation.config.source_id,
                    "miner": observation.config.label,
                    "severity": severity,
                    "code": f"source_{transport}",
                    "title": "원격 Fleet 연결 지연" if transport == "stale" else "원격 Fleet 연결 실패",
                    "detail": (
                        f"마지막 스냅샷이 {age:.0f}초 지연되었습니다."
                        if age is not None
                        else "정상 스냅샷을 아직 받지 못했습니다."
                    ),
                }
            )

        for raw_miner in snapshot.get("miners") or []:
            if not isinstance(raw_miner, dict):
                continue
            miner = _sanitize(deepcopy(raw_miner))
            lane_id = str(raw_miner.get("id") or raw_miner.get("label") or "unknown")
            global_id = f"{observation.config.source_id}:{lane_id}"
            miner.update(
                {
                    "id": global_id,
                    "global_id": global_id,
                    "lane_id": lane_id,
                    "source_id": observation.config.source_id,
                    "source_label": observation.config.label,
                    "source_transport": transport,
                    "source_comparison": miner.get("comparison"),
                }
            )
            if transport == "unreachable":
                miner["reported_online"] = bool(miner.get("online"))
                miner["online"] = False
                miner["overall"] = "critical"
            elif transport == "stale" and _SEVERITY.get(str(miner.get("overall")), 0) < 2:
                miner["overall"] = "warning"
            miners.append(miner)

        for raw_alert in source_fleet.get("alerts") or []:
            if not isinstance(raw_alert, dict):
                continue
            alert = _sanitize(raw_alert)
            lane_id = str(alert.get("miner") or "fleet")
            alert.update(
                {
                    "source_id": observation.config.source_id,
                    "miner": f"{observation.config.source_id}:{lane_id}",
                }
            )
            fleet_alerts.append(alert)

    task_groups: dict[str, list[dict[str, Any]]] = {}
    newest_task: tuple[datetime, str] | None = None
    for miner in miners:
        current = miner.get("current") or {}
        task_id = current.get("task_id")
        if not task_id:
            miner["federation_comparison"] = {"comparable": False}
            continue
        task_id = str(task_id)
        task_groups.setdefault(task_id, []).append(miner)
        received = parse_time(current.get("received_at"))
        if received and (newest_task is None or received > newest_task[0]):
            newest_task = (received, task_id)

    task_rows: list[dict[str, Any]] = []
    for task_id, task_miners in task_groups.items():
        scored: list[tuple[float, str, dict[str, Any]]] = []
        received_values: list[datetime] = []
        for miner in task_miners:
            score, score_source = _score_for_miner(miner)
            if score is not None and score_source is not None:
                scored.append((score, score_source, miner))
            received = parse_time((miner.get("current") or {}).get("received_at"))
            if received:
                received_values.append(received)
        scored.sort(key=lambda item: item[0], reverse=True)
        top_score = scored[0][0] if scored else None
        for score, score_source, miner in scored:
            rank = 1 + sum(other[0] > score for other in scored)
            miner["federation_comparison"] = {
                "comparable": True,
                "task_id": task_id,
                "score": score,
                "score_source": score_source,
                "top_score": top_score,
                "rank": rank,
                "participants": len(scored),
                "delta_to_top": score - top_score if top_score is not None else None,
            }
        for miner in task_miners:
            miner.setdefault("federation_comparison", {"comparable": False})
        task_rows.append(
            {
                "task_id": task_id,
                "received_at": max(received_values).isoformat() if received_values else None,
                "miners": len(task_miners),
                "scored_miners": len(scored),
                "sources": sorted({str(miner.get("source_id")) for miner in task_miners}),
                "top_score": top_score,
                "leader": scored[0][2].get("id") if scored else None,
            }
        )

    task_rows.sort(key=lambda item: item.get("received_at") or "", reverse=True)
    active_task_id = newest_task[1] if newest_task else (task_rows[0]["task_id"] if task_rows else None)
    active_task = next(
        (item for item in task_rows if item["task_id"] == active_task_id),
        None,
    )
    coverage = sum(
        (miner.get("current") or {}).get("task_id") == active_task_id
        for miner in miners
    ) if active_task_id else 0

    block = max(blocks) if blocks else None
    block_skew = max(blocks) - min(blocks) if len(blocks) > 1 else 0
    if block_skew > 3:
        fleet_alerts.append(
            {
                "source_id": "federation",
                "miner": "federation",
                "severity": "warning",
                "code": "chain_block_skew",
                "title": "Fleet 블록 높이 불일치",
                "detail": f"정상 source 사이에 {block_skew}블록 차이가 있습니다.",
            }
        )

    online = sum(bool(miner.get("online")) for miner in miners)
    source_statuses = [
        "critical" if row["transport"] == "unreachable"
        else "warning" if row["transport"] == "stale"
        else str(row.get("overall") or "healthy")
        for row in source_rows
    ]
    miner_statuses = [str(miner.get("overall") or "healthy") for miner in miners]
    overall = _worst_status((*source_statuses, *miner_statuses))
    ranking = _build_ranking_summary(miners)

    return {
        "schema_version": FEDERATION_SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "version": version,
        "chain": {
            "block": block,
            "source": "federation",
            "round_phase": (block - 8_843_300) % 720 if block is not None else None,
            "block_skew": block_skew,
        },
        "fleet": {
            "overall": overall,
            "online": online,
            "total": len(miners),
            "sources_online": sum(row["reachable"] for row in source_rows),
            "sources_total": len(source_rows),
            "active_task_id": active_task_id,
            "coverage": coverage,
            "task_top_score": (active_task or {}).get("top_score"),
            "alerts": fleet_alerts,
        },
        "sources": source_rows,
        "miners": miners,
        "tasks": task_rows,
        "ranking": ranking,
    }


def _stable_snapshot(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_snapshot(child)
            for key, child in value.items()
            if key not in {
                "generated_at",
                "version",
                "last_checked_at",
                "snapshot_age_seconds",
                "latency_ms",
            }
        }
    if isinstance(value, list):
        return [_stable_snapshot(item) for item in value]
    return value


class FederationCollector:
    def __init__(
        self,
        *,
        local_collector: Any,
        local_source: FederationSourceConfig,
        remote_sources: Iterable[FederationSourceConfig] = (),
        poll_seconds: float = 2.0,
        request_timeout_seconds: float = 2.0,
    ) -> None:
        self.local_collector = local_collector
        self.local_source = local_source
        self.remote_sources = tuple(remote_sources)
        self.poll_seconds = max(0.25, float(poll_seconds))
        self.request_timeout_seconds = max(0.25, float(request_timeout_seconds))
        self._observations = {
            source.source_id: SourceObservation(config=source)
            for source in self.remote_sources
        }
        self._version = 0
        self._fingerprint = ""
        self._snapshot = build_federated_state([], version=0)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._client: httpx.AsyncClient | None = None

    @property
    def snapshot(self) -> dict[str, Any]:
        return self._snapshot

    @property
    def version(self) -> int:
        return self._version

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.request_timeout_seconds),
            follow_redirects=False,
            headers={"User-Agent": "niome-federation/1"},
        )
        await self.refresh()
        self._task = asyncio.create_task(self._run(), name="niome-federation")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.refresh()
            except Exception:
                logger.exception("federation refresh failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _fetch_remote(self, source: FederationSourceConfig) -> None:
        observation = self._observations[source.source_id]
        checked_at = datetime.now(timezone.utc)
        started = time.monotonic()
        observation.last_checked_at = checked_at
        try:
            if self._client is None or source.state_url is None:
                raise RuntimeError("federation HTTP client is not running")
            response = await self._client.get(source.state_url)
            response.raise_for_status()
            if len(response.content) > MAX_SOURCE_RESPONSE_BYTES:
                raise ValueError("source response exceeds 2 MiB")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("source state must be a JSON object")
            if not isinstance(payload.get("miners"), list):
                raise ValueError("source state has no miners list")
            if not isinstance(payload.get("fleet"), dict):
                raise ValueError("source state has no fleet object")
        except Exception as error:
            observation.error = f"{type(error).__name__}: {_redact_string(str(error))}"
            logger.warning("federation source %s failed: %s", source.source_id, error)
        else:
            observation.snapshot = payload
            observation.last_success_at = checked_at
            observation.error = None
        finally:
            observation.latency_ms = (time.monotonic() - started) * 1000

    async def refresh(self) -> None:
        now = datetime.now(timezone.utc)
        local_observation = SourceObservation(
            config=self.local_source,
            snapshot=self.local_collector.snapshot,
            last_success_at=now,
            last_checked_at=now,
            latency_ms=0.0,
        )
        if self.remote_sources:
            await asyncio.gather(
                *(self._fetch_remote(source) for source in self.remote_sources)
            )
        observations = [local_observation, *self._observations.values()]
        candidate = build_federated_state(
            observations,
            version=self._version,
            now=now,
        )
        fingerprint = json.dumps(
            _stable_snapshot(candidate),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if fingerprint != self._fingerprint:
            self._fingerprint = fingerprint
            self._version += 1
        candidate["version"] = self._version
        self._snapshot = candidate
