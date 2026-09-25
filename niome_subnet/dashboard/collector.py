"""Low-overhead asynchronous collectors for the dashboard."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

import httpx

from .state import build_dashboard_snapshot, parse_time, safe_json


logger = logging.getLogger(__name__)

CONFIG_RE = re.compile(
    r"(?:builder_)?guide_variants=(?P<variants>\d+)\s+primary_cas_share=(?P<share>\d+(?:\.\d+)?)"
)


def _tail_text(path: str | None, limit: int = 256 * 1024) -> str:
    if not path:
        return ""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def collect_pm2(
    *,
    miner_process_name: str = "niome-dollar1",
    bridge_process_name: str = "niome-seed-bridge",
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    result = subprocess.run(
        ["pm2", "jlist"],
        check=True,
        capture_output=True,
        text=True,
        timeout=8,
    )
    raw = json.loads(result.stdout)
    processes: dict[str, dict[str, Any]] = {}
    loaded: dict[str, dict[str, Any]] = {}
    canonical_by_name = {
        miner_process_name: "niome-dollar1",
        bridge_process_name: "niome-seed-bridge",
    }
    for item in raw:
        name = item.get("name")
        canonical_name = canonical_by_name.get(name)
        if canonical_name is None:
            continue
        env = item.get("pm2_env") or {}
        monit = item.get("monit") or {}
        uptime_ms = env.get("pm_uptime")
        started_at = None
        if isinstance(uptime_ms, (int, float)):
            started_at = datetime.fromtimestamp(uptime_ms / 1000, timezone.utc).isoformat()
        processes[canonical_name] = {
            "name": name,
            "pid": item.get("pid"),
            "status": env.get("status"),
            "restarts": env.get("restart_time"),
            "started_at": started_at,
            "cpu_percent": monit.get("cpu"),
            "memory_bytes": monit.get("memory"),
        }
        # Python logging writes to stderr by default; scan both PM2 streams and
        # extract only the two non-secret configuration values.
        config_text = "\n".join(
            (
                _tail_text(env.get("pm_out_log_path")),
                _tail_text(env.get("pm_err_log_path")),
            )
        )
        matches = list(CONFIG_RE.finditer(config_text))
        if matches:
            match = matches[-1]
            loaded[canonical_name] = {
                "guide_variants": int(match.group("variants")),
                "primary_cas_share": float(match.group("share")),
            }
    return processes, loaded


class DashboardCollector:
    def __init__(
        self,
        *,
        artifact_root: Path,
        miner_hotkey: str,
        score_url: str,
        network: str = "finney",
        miner_process_name: str = "niome-dollar1",
        bridge_process_name: str = "niome-seed-bridge",
        expected_variants: int = 72,
        expected_primary_share: float = 0.60,
    ) -> None:
        self.artifact_root = artifact_root
        self.miner_hotkey = miner_hotkey
        self.score_url = score_url
        self.network = network
        self.miner_process_name = miner_process_name
        self.bridge_process_name = bridge_process_name
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
        self._snapshot: dict[str, Any] = build_dashboard_snapshot(artifact_root)
        self._version = 0
        self._fingerprint = ""
        self._last_pm2 = 0.0
        self._last_chain = 0.0
        self._last_score = 0.0

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
        await self.refresh(force=True)
        self._task = asyncio.create_task(self._run(), name="niome-dashboard-collector")

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
                logger.exception("dashboard refresh failed")
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
                    collect_pm2,
                    miner_process_name=self.miner_process_name,
                    bridge_process_name=self.bridge_process_name,
                )
            except Exception as error:
                logger.warning("PM2 collection failed: %s", error)

        if force or monotonic - self._last_chain >= 12:
            self._last_chain = monotonic
            try:
                self.current_block = await asyncio.wait_for(
                    asyncio.to_thread(self._query_block), timeout=10
                )
                self.chain_source = "finney"
            except Exception as error:
                logger.warning("chain block collection failed: %s", error)
                estimated = self._estimated_block()
                if estimated is not None:
                    self.current_block = estimated
                    self.chain_source = "artifact_estimate"

        snapshot = build_dashboard_snapshot(
            self.artifact_root,
            processes=self.processes,
            current_block=self.current_block,
            chain_source=self.chain_source,
            scoreboards=self.scoreboards,
            miner_hotkey=self.miner_hotkey,
            expected_config=self.expected_config,
            loaded_config=self.loaded_config,
        )

        if force or monotonic - self._last_score >= 15:
            task_id = self._next_score_task(snapshot, monotonic)
            if task_id:
                self._last_score = monotonic
                self._score_attempted_at[task_id] = monotonic
                try:
                    scores = await self._fetch_scores(task_id)
                    if scores:
                        self.scoreboards[task_id] = scores
                        snapshot = build_dashboard_snapshot(
                            self.artifact_root,
                            processes=self.processes,
                            current_block=self.current_block,
                            chain_source=self.chain_source,
                            scoreboards=self.scoreboards,
                            miner_hotkey=self.miner_hotkey,
                            expected_config=self.expected_config,
                            loaded_config=self.loaded_config,
                        )
                except Exception as error:
                    logger.warning("official score collection failed for %s: %s", task_id, error)

        comparable = dict(snapshot)
        comparable.pop("generated_at", None)
        fingerprint = json.dumps(comparable, sort_keys=True, separators=(",", ":"), default=str)
        if fingerprint != self._fingerprint:
            self._fingerprint = fingerprint
            self._version += 1
        snapshot["version"] = self._version
        self._snapshot = snapshot

    def _query_block(self) -> int:
        if self._subtensor is None:
            import bittensor as bt

            self._subtensor = bt.Subtensor(network=self.network)
        return int(self._subtensor.block)

    def _estimated_block(self) -> int | None:
        latest: tuple[datetime, int] | None = None
        if not self.artifact_root.exists():
            return None
        for task_dir in self.artifact_root.iterdir():
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

    def _next_score_task(self, snapshot: dict[str, Any], now_mono: float) -> str | None:
        history = snapshot.get("history") or []
        if not history:
            return None
        current = snapshot.get("current") or {}
        current_id = current.get("task_id")
        validation = (current.get("bridge") or {}).get("validation_block")
        if (
            current_id
            and current_id not in self.scoreboards
            and isinstance(validation, int)
            and self.current_block is not None
            and self.current_block >= validation
        ):
            last = self._score_attempted_at.get(current_id, 0.0)
            if now_mono - last >= 15:
                return str(current_id)

        for item in history[1:5]:
            task_id = item.get("task_id")
            if not task_id or task_id in self.scoreboards:
                continue
            last = self._score_attempted_at.get(task_id, 0.0)
            if now_mono - last >= 300:
                return str(task_id)
        return None

    async def _fetch_scores(self, task_id: str) -> list[dict[str, Any]]:
        timeout = httpx.Timeout(15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(self.score_url, params={"task_id": task_id})
            response.raise_for_status()
            payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        return [item for item in (items or []) if isinstance(item, dict)]
