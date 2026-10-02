"""FastAPI application serving the NIOME operations dashboard."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from niome_subnet.utils.settings import BASE_URL

from .federation import (
    FederationCollector,
    FederationSourceConfig,
    parse_remote_sources,
)
from .fleet import FleetDashboardCollector, MinerLaneConfig


REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = REPO_ROOT / "dashboard"
SEED_RESEARCH_STATE = Path(
    os.getenv(
        "NIOME_SEED_RESEARCH_STATE",
        str(REPO_ROOT / "artifacts" / "research" / "seed_research_state.json"),
    )
)
SCORE_CALIBRATION_MODEL = Path(
    os.getenv(
        "NIOME_SCORE_CALIBRATION_MODEL",
        str(REPO_ROOT / "artifacts" / "research" / "score_calibration.json"),
    )
)
ALL_FLEET_LANES = (
    MinerLaneConfig(
        lane_id="tao1",
        label="tao1",
        uid=32,
        hotkey="5GbhpWKt2SYHaZMHNy2WAsm5pzkGL5sRa7DFnYu9zJY3qYGC",
        artifact_root=REPO_ROOT / "artifacts" / "miners" / "tao1",
        miner_process="niome-tao1",
        bridge_process=None,
        axon_port=8091,
        profile="baseline",
        builder_policy="champion-reservoir003-cas65-v3",
        expected_primary_cas_share=0.65,
    ),
    MinerLaneConfig(
        lane_id="tao2",
        label="tao2",
        uid=53,
        hotkey="5Ehx52VbhGyvmZVcvaRF2dG8JVJUMHHreRBLRsGMkJ695zih",
        artifact_root=REPO_ROOT / "artifacts" / "miners" / "tao2",
        miner_process="niome-tao2",
        bridge_process=None,
        axon_port=8092,
        profile="exploration",
        builder_policy="champion-reservoir005-v3",
        expected_primary_cas_share=0.60,
    ),
    MinerLaneConfig(
        lane_id="won1",
        label="won1",
        uid=80,
        hotkey="5CPsx7spR4FCr786VBTqxuNGaoWnMfe91fcQKEYig3eVbTbi",
        artifact_root=REPO_ROOT / "artifacts" / "miners" / "won1",
        miner_process="niome-won1",
        bridge_process=None,
        axon_port=8093,
        profile="baseline",
        builder_policy="champion-reservoir003-cas65-v3",
        expected_primary_cas_share=0.65,
    ),
    MinerLaneConfig(
        lane_id="won2",
        label="won2",
        uid=255,
        hotkey="5E6ttv44E9Ko8NAsYerT4atZummaYxYXGzkTNHNUgXmXEvb4",
        artifact_root=REPO_ROOT / "artifacts" / "miners" / "won2",
        miner_process="niome-won2",
        bridge_process=None,
        axon_port=8094,
        profile="exploration",
        builder_policy="champion-reservoir005-v3",
        expected_primary_cas_share=0.60,
    ),
)


def select_fleet_lanes(raw: str | None) -> tuple[MinerLaneConfig, ...]:
    """Select this host's explicit lane ownership without changing shared code."""

    available = {lane.lane_id: lane for lane in ALL_FLEET_LANES}
    requested = [
        item.strip()
        for item in (raw or ",".join(available)).split(",")
        if item.strip()
    ]
    unknown = sorted(set(requested) - set(available))
    if unknown:
        raise RuntimeError(f"unknown NIOME_FLEET_LANES: {', '.join(unknown)}")
    if not requested:
        raise RuntimeError("NIOME_FLEET_LANES must select at least one lane")
    return tuple(available[lane_id] for lane_id in requested)


FLEET_LANES = select_fleet_lanes(os.getenv("NIOME_FLEET_LANES"))
TARGET_RANK = max(
    1,
    int(os.getenv("NIOME_DASH_TARGET_RANK", os.getenv("NIOME_TARGET_RANK", "30"))),
)

collector = FleetDashboardCollector(
    lanes=FLEET_LANES,
    score_url=f"{BASE_URL}/api/v3/miners/scores",
    network=os.getenv("NIOME_DASH_NETWORK", "finney"),
    expected_variants=int(os.getenv("NIOME_DASH_GUIDE_VARIANTS", "72")),
    expected_primary_share=float(os.getenv("NIOME_DASH_PRIMARY_CAS_SHARE", "0.60")),
    calibration_model_path=SCORE_CALIBRATION_MODEL,
    target_rank=TARGET_RANK,
)
federation = FederationCollector(
    local_collector=collector,
    local_source=FederationSourceConfig(
        source_id=os.getenv("NIOME_FEDERATION_LOCAL_ID", "bitcoin-hype-fleet"),
        label=os.getenv("NIOME_FEDERATION_LOCAL_LABEL", "Bitcoin / Hype Fleet"),
        local=True,
    ),
    remote_sources=parse_remote_sources(
        os.getenv("NIOME_FEDERATION_REMOTE_SOURCES")
    ),
    poll_seconds=float(os.getenv("NIOME_FEDERATION_POLL_SECONDS", "2")),
    request_timeout_seconds=float(
        os.getenv("NIOME_FEDERATION_TIMEOUT_SECONDS", "2")
    ),
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await collector.start()
    await federation.start()
    try:
        yield
    finally:
        await federation.stop()
        await collector.stop()


app = FastAPI(
    title="NIOME Operations Dashboard",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; font-src 'self'"
    )
    return response


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_ROOT / "index.html", media_type="text/html")


@app.get("/api/state", include_in_schema=False)
async def state() -> JSONResponse:
    return JSONResponse(collector.miner_snapshot("bitcoin1"))


@app.get("/api/fleet/state", include_in_schema=False)
async def fleet_state() -> JSONResponse:
    return JSONResponse(collector.snapshot)


@app.get("/api/v1/federation/state", include_in_schema=False)
async def federation_state() -> JSONResponse:
    return JSONResponse(federation.snapshot)


@app.get("/api/v1/federation/sources", include_in_schema=False)
async def federation_sources() -> JSONResponse:
    snapshot = federation.snapshot
    return JSONResponse({
        "schema_version": snapshot.get("schema_version"),
        "generated_at": snapshot.get("generated_at"),
        "version": federation.version,
        "sources": snapshot.get("sources") or [],
    })


def load_seed_research_state() -> dict:
    """Read research status without coupling dashboard uptime to the worker."""
    try:
        value = json.loads(SEED_RESEARCH_STATE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if isinstance(value, dict):
        return value
    return {
        "schema_version": 1,
        "generated_at": None,
        "phase": "waiting_for_automation",
        "current_action": {
            "code": "waiting_for_automation",
            "title": "연구 자동화기 시작 대기",
            "detail": "상태 파일이 생성되면 단계별 진척이 자동으로 표시됩니다.",
            "task_id": None,
            "progress": 0.0,
        },
        "targets": {
            "discovery": 20,
            "holdout": 5,
            "probe_verification": 5,
            "forward_exact": 5,
        },
        "dataset": {},
        "probe": {},
        "hypothesis": {},
        "forward": {},
        "automation": {"supervisor_healthy": False},
        "recent_rounds": [],
        "safety": {"submission_writes": False},
    }


@app.get("/api/v1/seed-research/state", include_in_schema=False)
async def seed_research_state() -> JSONResponse:
    return JSONResponse(load_seed_research_state())


@app.get("/api/v1/tasks/{task_id}", include_in_schema=False)
async def federation_task(task_id: str) -> JSONResponse:
    snapshot = federation.snapshot
    task = next(
        (item for item in snapshot.get("tasks") or [] if item.get("task_id") == task_id),
        None,
    )
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    miners = [
        miner
        for miner in snapshot.get("miners") or []
        if (miner.get("current") or {}).get("task_id") == task_id
    ]
    return JSONResponse({
        "schema_version": snapshot.get("schema_version"),
        "generated_at": snapshot.get("generated_at"),
        "task": task,
        "miners": miners,
    })


@app.get("/api/health", include_in_schema=False)
async def health() -> JSONResponse:
    return await federation_health()


@app.get("/api/v1/federation/health", include_in_schema=False)
async def federation_health() -> JSONResponse:
    snapshot = federation.snapshot
    fleet = snapshot.get("fleet") or {}
    return JSONResponse({
        "ok": fleet.get("overall") != "critical",
        "schema_version": snapshot.get("schema_version"),
        "version": federation.version,
        "generated_at": snapshot.get("generated_at"),
        "task_id": fleet.get("active_task_id"),
        "online": fleet.get("online"),
        "total": fleet.get("total"),
        "sources_online": fleet.get("sources_online"),
        "sources_total": fleet.get("sources_total"),
    })


@app.get("/api/events", include_in_schema=False)
async def events(request: Request) -> StreamingResponse:
    async def stream():
        last_version = -1
        heartbeat = 0
        while not await request.is_disconnected():
            version = collector.version
            if version != last_version:
                payload = json.dumps(
                    collector.miner_snapshot("bitcoin1"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield f"event: state\ndata: {payload}\n\n"
                last_version = version
                heartbeat = 0
            else:
                heartbeat += 1
                if heartbeat >= 8:
                    yield ": heartbeat\n\n"
                    heartbeat = 0
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/fleet/events", include_in_schema=False)
async def fleet_events(request: Request) -> StreamingResponse:
    async def stream():
        last_version = -1
        heartbeat = 0
        while not await request.is_disconnected():
            version = collector.version
            if version != last_version:
                payload = json.dumps(
                    collector.snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield f"event: state\ndata: {payload}\n\n"
                last_version = version
                heartbeat = 0
            else:
                heartbeat += 1
                if heartbeat >= 8:
                    yield ": heartbeat\n\n"
                    heartbeat = 0
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.get("/api/v1/federation/events", include_in_schema=False)
async def federation_events(request: Request) -> StreamingResponse:
    async def stream():
        last_version = -1
        heartbeat = 0
        while not await request.is_disconnected():
            version = federation.version
            if version != last_version:
                payload = json.dumps(
                    federation.snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield f"event: state\ndata: {payload}\n\n"
                last_version = version
                heartbeat = 0
            else:
                heartbeat += 1
                if heartbeat >= 8:
                    yield ": heartbeat\n\n"
                    heartbeat = 0
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


app.mount("/assets", StaticFiles(directory=STATIC_ROOT), name="assets")
