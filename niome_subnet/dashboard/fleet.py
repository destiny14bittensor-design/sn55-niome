"""Read-only aggregation for the four local NIOME miner lanes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import statistics
import subprocess
import time
from typing import Any, Iterable

import httpx

from .collector import CONFIG_RE, _tail_text
from .state import build_dashboard_snapshot, parse_time, safe_json


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MinerLaneConfig:
    lane_id: str
    label: str
    uid: int
    hotkey: str
    artifact_root: Path
    miner_process: str
    bridge_process: str
    axon_port: int
    profile: str


def collect_fleet_pm2(
    lanes: Iterable[MinerLaneConfig],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Collect every configured miner/bridge from one ``pm2 jlist`` call."""
    configured_names = {
        name
        for lane in lanes
        for name in (lane.miner_process, lane.bridge_process)
    }
    result = subprocess.run(
        ["pm2", "jlist"],
        check=True,
        capture_output=True,
        text=True,
        timeout=8,
    )
    processes: dict[str, dict[str, Any]] = {}
    loaded: dict[str, dict[str, Any]] = {}
    for item in json.loads(result.stdout):
        name = item.get("name")
        if name not in configured_names:
            continue
        env = item.get("pm2_env") or {}
        monit = item.get("monit") or {}
        uptime_ms = env.get("pm_uptime")
        started_at = None
        if isinstance(uptime_ms, (int, float)):
            started_at = datetime.fromtimestamp(
                uptime_ms / 1000, timezone.utc
            ).isoformat()
        processes[str(name)] = {
            "name": name,
            "pid": item.get("pid"),
            "status": env.get("status"),
            "restarts": env.get("restart_time"),
            "started_at": started_at,
            "cpu_percent": monit.get("cpu"),
            "memory_bytes": monit.get("memory"),
        }
        config_text = "\n".join(
            (
                _tail_text(env.get("pm_out_log_path")),
                _tail_text(env.get("pm_err_log_path")),
            )
        )
        matches = list(CONFIG_RE.finditer(config_text))
        if matches:
            match = matches[-1]
            loaded[str(name)] = {
                "guide_variants": int(match.group("variants")),
                "primary_cas_share": float(match.group("share")),
            }
    return processes, loaded


def _role_maps(
    lane: MinerLaneConfig,
    processes: dict[str, dict[str, Any]],
    loaded: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    process_roles = {}
    config_roles = {}
    for actual, canonical in (
        (lane.miner_process, "niome-dollar1"),
        (lane.bridge_process, "niome-seed-bridge"),
    ):
        if actual in processes:
            process_roles[canonical] = processes[actual]
        if actual in loaded:
            config_roles[canonical] = loaded[actual]
    return process_roles, config_roles


def _score_for_comparison(current: dict[str, Any]) -> tuple[float | None, str | None]:
    official = current.get("official") or {}
    if official.get("published") and isinstance(official.get("score"), (int, float)):
        return float(official["score"]), "official"
    local = current.get("local") or {}
    if isinstance(local.get("score"), (int, float)):
        return float(local["score"]), "local"
    return None, None


def build_fleet_state(
    *,
    lanes: Iterable[MinerLaneConfig],
    lane_snapshots: dict[str, dict[str, Any]],
    processes: dict[str, dict[str, Any]],
    current_block: int | None,
    chain_source: str,
    version: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Combine independent miner snapshots without conflating task IDs."""
    now = now or datetime.now(timezone.utc)
    lane_configs = list(lanes)
    newest: tuple[datetime, str] | None = None
    for lane in lane_configs:
        current = (lane_snapshots.get(lane.lane_id) or {}).get("current") or {}
        received = parse_time(current.get("received_at"))
        task_id = current.get("task_id")
        if received and task_id and (newest is None or received > newest[0]):
            newest = (received, str(task_id))
    active_task_id = newest[1] if newest else None

    baseline_values: list[float] = []
    if active_task_id:
        for lane in lane_configs:
            if lane.profile != "baseline":
                continue
            current = (lane_snapshots.get(lane.lane_id) or {}).get("current") or {}
            if current.get("task_id") != active_task_id:
                continue
            score, _ = _score_for_comparison(current)
            if score is not None:
                baseline_values.append(score)
    baseline_score = statistics.median(baseline_values) if baseline_values else None

    miner_rows: list[dict[str, Any]] = []
    fleet_alerts: list[dict[str, Any]] = []
    online_count = 0
    coverage = 0
    severity = {"critical": 3, "warning": 2, "healthy": 1, "complete": 0}
    worst = 0
    for lane in lane_configs:
        snapshot = lane_snapshots.get(lane.lane_id) or {}
        current = snapshot.get("current")
        miner_process = processes.get(lane.miner_process)
        bridge_process = processes.get(lane.bridge_process)
        online = bool(
            miner_process
            and miner_process.get("status") == "online"
            and bridge_process
            and bridge_process.get("status") == "online"
        )
        if online:
            online_count += 1
        if current and current.get("task_id") == active_task_id:
            coverage += 1
        lane_overall = (current or {}).get("overall") or (
            "healthy" if online else "critical"
        )
        worst = max(worst, severity.get(lane_overall, 0))
        alerts = [dict(item) for item in ((current or {}).get("alerts") or [])]
        if not online:
            alerts.insert(
                0,
                {
                    "severity": "critical",
                    "code": "lane_process_offline",
                    "title": "프로세스 오프라인",
                    "detail": "miner 또는 seed bridge가 online 상태가 아닙니다.",
                },
            )
        for alert in alerts:
            fleet_alerts.append({"miner": lane.lane_id, **alert})

        score, score_source = _score_for_comparison(current or {})
        comparable = bool(
            active_task_id
            and current
            and current.get("task_id") == active_task_id
            and score is not None
            and baseline_score is not None
        )
        comparison = {
            "comparable": comparable,
            "task_id": active_task_id,
            "baseline_score": baseline_score,
            "score": score if comparable else None,
            "score_source": score_source if comparable else None,
            "delta": score - baseline_score if comparable else None,
        }
        cpu = sum(
            float((item or {}).get("cpu_percent") or 0)
            for item in (miner_process, bridge_process)
        )
        memory = sum(
            int((item or {}).get("memory_bytes") or 0)
            for item in (miner_process, bridge_process)
        )
        restarts = sum(
            int((item or {}).get("restarts") or 0)
            for item in (miner_process, bridge_process)
        )
        miner_rows.append(
            {
                "id": lane.lane_id,
                "label": lane.label,
                "uid": lane.uid,
                "hotkey": lane.hotkey,
                "hotkey_short": f"{lane.hotkey[:6]}…{lane.hotkey[-5:]}",
                "profile": lane.profile,
                "axon_port": lane.axon_port,
                "online": online,
                "overall": lane_overall,
                "processes": {
                    "miner": miner_process,
                    "bridge": bridge_process,
                },
                "resources": {
                    "cpu_percent": cpu,
                    "memory_bytes": memory,
                    "restarts": restarts,
                },
                "current": current,
                "history": snapshot.get("history") or [],
                "comparison": comparison,
                "alerts": alerts,
            }
        )

    overall = next(
        (name for name, value in severity.items() if value == worst),
        "healthy",
    )
    round_phase = (
        (current_block - 8_843_300) % 720 if current_block is not None else None
    )
    return {
        "generated_at": now.isoformat(),
        "version": version,
        "chain": {
            "block": current_block,
            "source": chain_source,
            "round_phase": round_phase,
        },
        "fleet": {
            "overall": overall,
            "online": online_count,
            "total": len(lane_configs),
            "active_task_id": active_task_id,
            "coverage": coverage,
            "baseline_score": baseline_score,
            "alerts": fleet_alerts,
        },
        "miners": miner_rows,
    }


class FleetDashboardCollector:
    def __init__(
        self,
        *,
        lanes: Iterable[MinerLaneConfig],
        score_url: str,
        network: str = "finney",
        expected_variants: int = 72,
        expected_primary_share: float = 0.60,
    ) -> None:
        self.lanes = tuple(lanes)
        self.score_url = score_url
        self.network = network
        self.expected_config = {
            "guide_variants": expected_variants,
            "primary_cas_share": expected_primary_share,
        }
        self.processes: dict[str, dict[str, Any]] = {}
        self.loaded_config: dict[str, dict[str, Any]] = {}
        self.current_block: int | None = None
        self.chain_source = "unavailable"
        self.scoreboards: dict[str, list[dict[str, Any]]] = {}
        self._score_attempted_at: dict[str, float] = {}
        self._subtensor: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._version = 0
        self._fingerprint = ""
        self._last_pm2 = 0.0
        self._last_chain = 0.0
        self._last_score = 0.0
        self._lane_snapshots: dict[str, dict[str, Any]] = {}
        self._snapshot = build_fleet_state(
            lanes=self.lanes,
            lane_snapshots={},
            processes={},
            current_block=None,
            chain_source="unavailable",
        )

    @property
    def snapshot(self) -> dict[str, Any]:
        return self._snapshot

    @property
    def version(self) -> int:
        return self._version

    def miner_snapshot(self, lane_id: str) -> dict[str, Any]:
        return self._lane_snapshots.get(lane_id) or {
            "generated_at": self._snapshot.get("generated_at"),
            "chain": self._snapshot.get("chain"),
            "processes": {},
            "current": None,
            "history": [],
        }

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        await self.refresh(force=True)
        self._task = asyncio.create_task(self._run(), name="niome-fleet-dashboard")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._subtensor is not None:
            close = getattr(self._subtensor, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.refresh()
            except Exception:
                logger.exception("fleet dashboard refresh failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def refresh(self, *, force: bool = False) -> None:
        monotonic = time.monotonic()
        if force or monotonic - self._last_pm2 >= 10:
            self._last_pm2 = monotonic
            try:
                self.processes, self.loaded_config = await asyncio.to_thread(
                    collect_fleet_pm2, self.lanes
                )
            except Exception as error:
                logger.warning("fleet PM2 collection failed: %s", error)

        if force or monotonic - self._last_chain >= 12:
            self._last_chain = monotonic
            try:
                self.current_block = await asyncio.wait_for(
                    asyncio.to_thread(self._query_block), timeout=10
                )
                self.chain_source = "finney"
            except Exception as error:
                logger.warning("fleet chain collection failed: %s", error)
                estimated = self._estimated_block()
                if estimated is not None:
                    self.current_block = estimated
                    self.chain_source = "artifact_estimate"

        self._lane_snapshots = self._build_lane_snapshots()
        if force or monotonic - self._last_score >= 15:
            task_id = self._next_score_task(monotonic)
            if task_id:
                self._last_score = monotonic
                self._score_attempted_at[task_id] = monotonic
                try:
                    scores = await self._fetch_scores(task_id)
                    if scores:
                        self.scoreboards[task_id] = scores
                        self._lane_snapshots = self._build_lane_snapshots()
                except Exception as error:
                    logger.warning(
                        "fleet official score collection failed for %s: %s",
                        task_id,
                        error,
                    )

        candidate = build_fleet_state(
            lanes=self.lanes,
            lane_snapshots=self._lane_snapshots,
            processes=self.processes,
            current_block=self.current_block,
            chain_source=self.chain_source,
            version=self._version,
        )
        comparable = dict(candidate)
        comparable.pop("generated_at", None)
        comparable.pop("version", None)
        fingerprint = json.dumps(
            comparable, sort_keys=True, separators=(",", ":"), default=str
        )
        if fingerprint != self._fingerprint:
            self._fingerprint = fingerprint
            self._version += 1
        candidate["version"] = self._version
        self._snapshot = candidate

    def _build_lane_snapshots(self) -> dict[str, dict[str, Any]]:
        snapshots = {}
        for lane in self.lanes:
            role_processes, role_config = _role_maps(
                lane, self.processes, self.loaded_config
            )
            snapshots[lane.lane_id] = build_dashboard_snapshot(
                lane.artifact_root,
                processes=role_processes,
                current_block=self.current_block,
                chain_source=self.chain_source,
                scoreboards=self.scoreboards,
                miner_hotkey=lane.hotkey,
                expected_config=self.expected_config,
                loaded_config=role_config,
            )
        return snapshots

    def _query_block(self) -> int:
        if self._subtensor is None:
            import bittensor as bt

            self._subtensor = bt.Subtensor(network=self.network)
        return int(self._subtensor.block)

    def _estimated_block(self) -> int | None:
        latest: tuple[datetime, int] | None = None
        for lane in self.lanes:
            if not lane.artifact_root.exists():
                continue
            for task_dir in lane.artifact_root.iterdir():
                if not task_dir.is_dir():
                    continue
                bridge = safe_json(task_dir / "seed_bridge_status.json") or {}
                observed = parse_time(bridge.get("last_observed_at"))
                block = bridge.get("current_block")
                if observed and isinstance(block, (int, float)):
                    if latest is None or observed > latest[0]:
                        latest = (observed, int(block))
        if latest is None:
            return self.current_block
        elapsed = max(0.0, (datetime.now(timezone.utc) - latest[0]).total_seconds())
        return latest[1] + int(elapsed // 12)

    def _next_score_task(self, now_mono: float) -> str | None:
        for snapshot in self._lane_snapshots.values():
            current = snapshot.get("current") or {}
            task_id = current.get("task_id")
            validation = (current.get("bridge") or {}).get("validation_block")
            if (
                task_id
                and task_id not in self.scoreboards
                and isinstance(validation, int)
                and self.current_block is not None
                and self.current_block >= validation
                and now_mono - self._score_attempted_at.get(str(task_id), 0.0) >= 15
            ):
                return str(task_id)
        for snapshot in self._lane_snapshots.values():
            for item in (snapshot.get("history") or [])[1:5]:
                task_id = item.get("task_id")
                if (
                    task_id
                    and task_id not in self.scoreboards
                    and now_mono - self._score_attempted_at.get(str(task_id), 0.0) >= 300
                ):
                    return str(task_id)
        return None

    async def _fetch_scores(self, task_id: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.get(self.score_url, params={"task_id": task_id})
            response.raise_for_status()
            payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        return [item for item in (items or []) if isinstance(item, dict)]
