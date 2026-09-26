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
FLEET_LANES = (
    MinerLaneConfig(
        lane_id="bitcoin1",
        label="bitcoin1",
        uid=155,
        hotkey="5Ge44ZvzfkxGYXwnbPmnCGfzatGUtfiXSXUcTWGNcv6DMLXz",
        artifact_root=REPO_ROOT / "artifacts" / "live",
        miner_process="niome-dollar1",
        bridge_process="niome-seed-bridge",
        axon_port=8091,
        profile="baseline",
    ),
    MinerLaneConfig(
        lane_id="bitcoin2",
        label="bitcoin2",
        uid=223,
        hotkey="5H1jPksvzJuak6P63VAp7QttcRpQ1PuT9BNDyMT3qGEYovdQ",
        artifact_root=REPO_ROOT / "artifacts" / "miners" / "dollar2",
        miner_process="niome-dollar2",
        bridge_process="niome-seed-bridge-dollar2",
        axon_port=8092,
        profile="exploration",
    ),
    MinerLaneConfig(
        lane_id="hype1",
        label="hype1",
        uid=96,
        hotkey="5EeqkTcDzGg7Ge89N1DJzQv5ehyCEPMBreCHqcxfEpB3WU21",
        artifact_root=REPO_ROOT / "artifacts" / "miners" / "dollar3",
        miner_process="niome-dollar3",
        bridge_process="niome-seed-bridge-dollar3",
        axon_port=8093,
        profile="exploration",
    ),
    MinerLaneConfig(
        lane_id="hype2",
        label="hype2",
        uid=159,
        hotkey="5HKLhT3ie4VkW9hG3Vgn2MiYgZ9PQtVnFbJ18fmDh5ZkWY86",
        artifact_root=REPO_ROOT / "artifacts" / "miners" / "dollar4",
        miner_process="niome-dollar4",
        bridge_process="niome-seed-bridge-dollar4",
        axon_port=8094,
        profile="exploration",
    ),
)

collector = FleetDashboardCollector(
    lanes=FLEET_LANES,
    score_url=f"{BASE_URL}/api/v3/miners/scores",
    network=os.getenv("NIOME_DASH_NETWORK", "finney"),
    expected_variants=int(os.getenv("NIOME_DASH_GUIDE_VARIANTS", "72")),
    expected_primary_share=float(os.getenv("NIOME_DASH_PRIMARY_CAS_SHARE", "0.60")),
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
