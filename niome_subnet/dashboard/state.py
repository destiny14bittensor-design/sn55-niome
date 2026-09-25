"""Pure state reduction for the NIOME operations dashboard.

The live artifact directory remains the source of truth.  This module only
selects and normalizes safe fields; it deliberately never reads the request
envelope because that file contains a presigned S3 URL.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


BASE_BLOCK_NUMBER = 8_843_300
INTERVAL_BLOCKS = 720
VALIDATION_OFFSET = 18
ACTIVE_BRIDGE_STATES = {
    "created",
    "opening",
    "streaming",
    "waiting_for_seeds",
    "building",
    "finishing_upload",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def safe_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _record_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _received_at(task_dir: Path, status: dict[str, Any], task: dict[str, Any]) -> datetime:
    parsed = parse_time(status.get("received_at")) or parse_time(task.get("received_at"))
    if parsed:
        return parsed
    return datetime.fromtimestamp(task_dir.stat().st_mtime, timezone.utc)


def _official_summary(
    scoreboard: list[dict[str, Any]] | None,
    miner_hotkey: str,
) -> dict[str, Any]:
    if not scoreboard:
        return {
            "published": False,
            "score": None,
            "rank": None,
            "top_score": None,
            "gap_to_first": None,
            "participants": 0,
            "published_at": None,
            "breakdown": {},
        }

    ordered = sorted(
        scoreboard,
        key=lambda item: float(item.get("final_score") or 0.0),
        reverse=True,
    )
    mine = next(
        (item for item in ordered if item.get("miner_hotkey") == miner_hotkey),
        None,
    )
    top_score = _number(ordered[0].get("final_score")) if ordered else None
    score = _number(mine.get("final_score")) if mine else None
    rank = ordered.index(mine) + 1 if mine else None
    return {
        "published": mine is not None,
        "score": score,
        "rank": rank,
        "top_score": top_score,
        "gap_to_first": (score - top_score) if score is not None and top_score is not None else None,
        "participants": len(ordered),
        "published_at": mine.get("created_at") if mine else None,
        "breakdown": dict(mine.get("breakdown") or {}) if mine else {},
    }


def _local_summary(task_dir: Path) -> dict[str, Any]:
    choices = (
        ("optimized_bridge", task_dir / "seed_bridge_local_validation.json"),
        ("safe_submission", task_dir / "local_validation.json"),
    )
    for source, path in choices:
        data = safe_json(path)
        if isinstance(data, dict):
            return {
                "available": True,
                "source": source,
                "score": _number(data.get("final_score")),
                "breakdown": dict(data.get("breakdown") or {}),
                # The validator artifacts contain the complete experiment lists.
                # The dashboard only needs their counts; returning the records
                # would add ~150 KiB to every live event.
                "valid_experiments": _record_count(data.get("valid_experiments")),
                "invalid_experiments": _record_count(data.get("invalid_experiments")),
            }
    return {
        "available": False,
        "source": None,
        "score": None,
        "breakdown": {},
        "valid_experiments": None,
        "invalid_experiments": None,
    }


def _stream_summary(stream: dict[str, Any], now: datetime) -> dict[str, Any]:
    total = int(stream.get("total_bytes") or 0)
    sent = int(stream.get("bytes_sent") or 0)
    last_send = parse_time(stream.get("last_successful_send_at"))
    return {
        "label": str(stream.get("label") or "unknown"),
        "state": str(stream.get("state") or "unknown"),
        "bytes_sent": sent,
        "total_bytes": total,
        "progress": min(1.0, sent / total) if total > 0 else 0.0,
        "send_count": int(stream.get("send_count") or 0),
        "started_at": stream.get("started_at"),
        "last_successful_send_at": stream.get("last_successful_send_at"),
        "last_send_age_seconds": max(0.0, (now - last_send).total_seconds()) if last_send else None,
        "elapsed_seconds": _number(stream.get("elapsed_seconds")),
        "http_status": stream.get("status"),
        "failure_stage": stream.get("failure_stage"),
        "failure_category": stream.get("failure_category"),
        "error_type": stream.get("error_type"),
    }


def _safe_lane(status: dict[str, Any]) -> dict[str, Any]:
    uploaded = bool(status.get("uploaded_at"))
    state = "complete" if uploaded else str(status.get("state") or "waiting")
    return {
        "state": state,
        "uploaded": uploaded,
        "uploaded_at": status.get("uploaded_at"),
        "submission_rows": status.get("submission_rows"),
        "submission_sha256": status.get("submission_sha256"),
        "upload_attempts": status.get("upload_attempts"),
        "upload_elapsed_seconds": _number(status.get("upload_elapsed_seconds")),
        "upload_start_target_missed": bool(status.get("upload_start_target_missed", False)),
        "deadline_source": status.get("deadline_source"),
        "url_seconds_remaining_at_receipt": _number(status.get("url_seconds_remaining_at_receipt")),
    }


def _bridge_lane(
    bridge: dict[str, Any],
    failure: dict[str, Any],
    now: datetime,
    current_block: int | None,
) -> dict[str, Any]:
    streams = [
        _stream_summary(item, now)
        for item in bridge.get("stream_results", [])
        if isinstance(item, dict)
    ]
    target = bridge.get("seed_target_block")
    target_block = int(target) if isinstance(target, (int, float)) else None
    blocks_remaining = (
        max(0, target_block - current_block)
        if target_block is not None and current_block is not None
        else None
    )
    put_result = bridge.get("put_result") if isinstance(bridge.get("put_result"), dict) else {}
    return {
        "state": str(bridge.get("state") or ("failed" if failure else "waiting")),
        "started_at": bridge.get("started_at"),
        "completed_at": bridge.get("completed_at"),
        "last_observed_at": bridge.get("last_observed_at"),
        "seed_target_block": target_block,
        "validation_block": target_block + VALIDATION_OFFSET if target_block is not None else None,
        "seed_read_block": bridge.get("seed_read_block"),
        "round_seeds": list(bridge.get("round_seeds") or []),
        "blocks_to_seeds": blocks_remaining,
        "seconds_to_seeds": blocks_remaining * 12 if blocks_remaining is not None else None,
        "build_elapsed_seconds": _number(bridge.get("build_elapsed_seconds")),
        "submission_rows": bridge.get("submission_rows"),
        "submission_sha256": bridge.get("submission_sha256"),
        "url_seconds_remaining_at_open": _number(bridge.get("url_seconds_remaining_at_open")),
        "live_stream_profiles": list(bridge.get("live_stream_profiles") or []),
        "stopped_streams": list(bridge.get("stopped_streams") or []),
        "streams": streams,
        "put": {
            "state": put_result.get("state"),
            "label": put_result.get("label"),
            "http_status": put_result.get("status"),
            "reason": put_result.get("reason"),
            "finished_at": put_result.get("finished_at"),
            "average_body_bytes_per_second": _number(put_result.get("average_body_bytes_per_second")),
        },
        "failure": {
            "stage": failure.get("failure_stage") or failure.get("stage"),
            "category": failure.get("failure_category") or failure.get("category"),
            "error_type": failure.get("error_type"),
            "error": failure.get("error"),
            "failed_at": failure.get("failed_at"),
        } if failure else None,
    }


def _config_summary(
    expected: dict[str, Any],
    loaded: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    expected_variants = int(expected.get("guide_variants", 72))
    expected_share = float(expected.get("primary_cas_share", 0.60))
    evidence = []
    for process_name in ("niome-dollar1", "niome-seed-bridge"):
        value = loaded.get(process_name) or {}
        variants = value.get("guide_variants")
        share = value.get("primary_cas_share")
        matches = variants == expected_variants and share is not None and abs(float(share) - expected_share) < 1e-9
        evidence.append({
            "process": process_name,
            "guide_variants": variants,
            "primary_cas_share": share,
            "confirmed": bool(value),
            "matches_expected": matches,
        })
    return {
        "expected": {
            "guide_variants": expected_variants,
            "primary_cas_share": expected_share,
        },
        "evidence": evidence,
        "all_confirmed": all(item["confirmed"] for item in evidence),
        "all_match": all(item["matches_expected"] for item in evidence),
    }


def _alerts(
    received_at: datetime,
    safe_lane: dict[str, Any],
    bridge_lane: dict[str, Any],
    local: dict[str, Any],
    official: dict[str, Any],
    processes: dict[str, dict[str, Any]],
    config: dict[str, Any],
    current_block: int | None,
    now: datetime,
) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []

    for name in ("niome-dollar1", "niome-seed-bridge"):
        process = processes.get(name)
        if not process or process.get("status") != "online":
            alerts.append({
                "severity": "critical",
                "code": "process_offline",
                "title": f"{name} 중단",
                "detail": "프로세스가 온라인 상태가 아닙니다.",
            })
            continue
        uptime_at = parse_time(process.get("started_at"))
        restart_is_unsafe = (
            not bool(safe_lane.get("uploaded"))
            if name == "niome-dollar1"
            else bridge_lane.get("state") in ACTIVE_BRIDGE_STATES
        )
        if uptime_at and uptime_at > received_at and restart_is_unsafe:
            alerts.append({
                "severity": "critical",
                "code": "restart_during_task",
                "title": f"{name} 작업 중 재시작",
                "detail": (
                    f"현재 작업의 안전 구간 완료 전 {uptime_at.isoformat()}에 "
                    "시작됐습니다."
                ),
            })

    if not config["all_match"]:
        severity = "warning" if not config["all_confirmed"] else "critical"
        alerts.append({
            "severity": severity,
            "code": "builder_config_unverified",
            "title": "개선 설정 확인 필요",
            "detail": "두 프로세스의 variants=72, Cas9=60% 런타임 증거가 일치하지 않습니다.",
        })

    bridge_state = bridge_lane["state"]
    streams = bridge_lane["streams"]
    live = [item for item in streams if item["state"] in {"opening", "streaming", "complete"}]
    if bridge_state in ACTIVE_BRIDGE_STATES and streams and not live:
        alerts.append({
            "severity": "critical",
            "code": "all_streams_stopped",
            "title": "브리지 경로 전부 종료",
            "detail": "시드 기반 결과를 업로드할 생존 스트림이 없습니다.",
        })
    elif bridge_state in ACTIVE_BRIDGE_STATES and len(live) == 1:
        alerts.append({
            "severity": "warning",
            "code": "single_stream_remaining",
            "title": "브리지 스트림 1개만 생존",
            "detail": f"{live[0]['label']} 경로만 남았습니다.",
        })

    observed_at = parse_time(bridge_lane.get("last_observed_at"))
    if bridge_state in ACTIVE_BRIDGE_STATES and observed_at:
        age = (now - observed_at).total_seconds()
        if age > 20:
            alerts.append({
                "severity": "warning",
                "code": "bridge_status_stale",
                "title": "브리지 상태 갱신 지연",
                "detail": f"마지막 상태 갱신이 {age:.0f}초 전입니다.",
            })

    if bridge_state == "failed" or bridge_lane.get("failure"):
        failure = bridge_lane.get("failure") or {}
        alerts.append({
            "severity": "critical",
            "code": "bridge_failed",
            "title": "개선 업로드 실패",
            "detail": str(failure.get("category") or failure.get("error") or "원인 기록을 확인하십시오."),
        })

    if safe_lane.get("upload_start_target_missed"):
        alerts.append({
            "severity": "warning",
            "code": "safe_upload_late",
            "title": "안전 제출 시작 목표 초과",
            "detail": "안전 제출은 완료됐지만 권장 시작 시각을 넘겼습니다.",
        })

    if local["score"] is not None and official["score"] is not None:
        delta = abs(float(local["score"]) - float(official["score"]))
        if delta > 1e-9:
            alerts.append({
                "severity": "critical",
                "code": "score_mismatch",
                "title": "로컬·공식 점수 불일치",
                "detail": f"차이 {delta:.12g}점입니다.",
            })

    validation_block = bridge_lane.get("validation_block")
    if (
        isinstance(validation_block, int)
        and current_block is not None
        and current_block > validation_block + 240
        and not official["published"]
    ):
        alerts.append({
            "severity": "warning",
            "code": "official_score_delayed",
            "title": "공식 점수 게시 지연",
            "detail": "통상 게시 구간을 넘겼지만 공식 API에 점수가 없습니다.",
        })

    return alerts


def build_round_state(
    task_dir: Path,
    *,
    processes: dict[str, dict[str, Any]],
    current_block: int | None,
    scoreboard: list[dict[str, Any]] | None,
    miner_hotkey: str,
    expected_config: dict[str, Any],
    loaded_config: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    status = safe_json(task_dir / "status.json") or {}
    task = safe_json(task_dir / "task.json") or {}
    bridge = safe_json(task_dir / "seed_bridge_status.json") or {}
    failure = safe_json(task_dir / "seed_bridge_failure.json") or {}
    received_at = _received_at(task_dir, status, task)
    safe_lane = _safe_lane(status)
    bridge_lane = _bridge_lane(bridge, failure, now, current_block)
    local = _local_summary(task_dir)
    official = _official_summary(scoreboard, miner_hotkey)
    config = _config_summary(expected_config, loaded_config)
    alerts = _alerts(
        received_at,
        safe_lane,
        bridge_lane,
        local,
        official,
        processes,
        config,
        current_block,
        now,
    )

    if official["published"]:
        stage = "official_published"
    elif bridge_lane["state"] == "complete":
        stage = "waiting_official"
    elif bridge_lane["state"] == "failed":
        stage = "bridge_failed"
    elif bridge_lane["state"] in ACTIVE_BRIDGE_STATES:
        stage = bridge_lane["state"]
    elif safe_lane["uploaded"]:
        stage = "safe_uploaded"
    else:
        stage = "query_received"

    severity_order = {"critical": 3, "warning": 2, "info": 1}
    worst = max((severity_order.get(item["severity"], 0) for item in alerts), default=0)
    overall = "critical" if worst >= 3 else "warning" if worst == 2 else "complete" if official["published"] else "healthy"

    validation_block = bridge_lane.get("validation_block")
    return {
        "task_id": task_dir.name,
        "received_at": iso(received_at),
        "stage": stage,
        "overall": overall,
        "current_block": current_block,
        "round_phase": ((current_block - BASE_BLOCK_NUMBER) % INTERVAL_BLOCKS) if current_block is not None else None,
        "blocks_to_validation": max(0, validation_block - current_block)
        if isinstance(validation_block, int) and current_block is not None
        else None,
        "safe": safe_lane,
        "bridge": bridge_lane,
        "local": local,
        "official": official,
        "config": config,
        "alerts": alerts,
    }


def _history_summary(task_dir: Path, scoreboard: list[dict[str, Any]] | None, miner_hotkey: str) -> dict[str, Any]:
    status = safe_json(task_dir / "status.json") or {}
    task = safe_json(task_dir / "task.json") or {}
    bridge = safe_json(task_dir / "seed_bridge_status.json") or {}
    failure = safe_json(task_dir / "seed_bridge_failure.json") or {}
    local = _local_summary(task_dir)
    official = _official_summary(scoreboard, miner_hotkey)
    return {
        "task_id": task_dir.name,
        "received_at": iso(_received_at(task_dir, status, task)),
        "safe_uploaded": bool(status.get("uploaded_at")),
        "bridge_state": bridge.get("state") or ("failed" if failure else "not_started"),
        "seeds": list(bridge.get("round_seeds") or []),
        "local_score": local.get("score"),
        "local_source": local.get("source"),
        "official_score": official.get("score"),
        "official_rank": official.get("rank"),
        "failure_category": failure.get("failure_category") or failure.get("category"),
    }


def build_dashboard_snapshot(
    artifact_root: Path,
    *,
    processes: dict[str, dict[str, Any]] | None = None,
    current_block: int | None = None,
    chain_source: str = "unavailable",
    scoreboards: dict[str, list[dict[str, Any]]] | None = None,
    miner_hotkey: str = "",
    expected_config: dict[str, Any] | None = None,
    loaded_config: dict[str, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    processes = processes or {}
    scoreboards = scoreboards or {}
    expected_config = expected_config or {"guide_variants": 72, "primary_cas_share": 0.60}
    loaded_config = loaded_config or {}

    task_dirs = []
    if artifact_root.exists():
        for path in artifact_root.iterdir():
            if not path.is_dir():
                continue
            status = safe_json(path / "status.json") or {}
            task = safe_json(path / "task.json") or {}
            task_dirs.append((_received_at(path, status, task), path))
    task_dirs.sort(key=lambda item: item[0], reverse=True)

    current = None
    if task_dirs:
        current = build_round_state(
            task_dirs[0][1],
            processes=processes,
            current_block=current_block,
            scoreboard=scoreboards.get(task_dirs[0][1].name),
            miner_hotkey=miner_hotkey,
            expected_config=expected_config,
            loaded_config=loaded_config,
            now=now,
        )
        prediction = {
            "available": False,
            "rank": None,
            "top_score": None,
            "gap_to_first": None,
            "participants": 0,
            "comparison_task_id": None,
        }
        local_score = current["local"].get("score")
        if local_score is not None and not current["official"].get("published"):
            for _, candidate_path in task_dirs[1:]:
                comparison = scoreboards.get(candidate_path.name)
                if not comparison:
                    continue
                scores = sorted(
                    (float(item.get("final_score") or 0.0) for item in comparison),
                    reverse=True,
                )
                if scores:
                    prediction = {
                        "available": True,
                        "rank": 1 + sum(score > float(local_score) for score in scores),
                        "top_score": scores[0],
                        "gap_to_first": float(local_score) - scores[0],
                        "participants": len(scores),
                        "comparison_task_id": candidate_path.name,
                    }
                break
        current["prediction"] = prediction

    history = [
        _history_summary(path, scoreboards.get(path.name), miner_hotkey)
        for _, path in task_dirs[:12]
    ]
    return {
        "generated_at": iso(now),
        "chain": {
            "block": current_block,
            "source": chain_source,
            "round_phase": ((current_block - BASE_BLOCK_NUMBER) % INTERVAL_BLOCKS)
            if current_block is not None
            else None,
        },
        "processes": processes,
        "current": current,
        "history": history,
    }
