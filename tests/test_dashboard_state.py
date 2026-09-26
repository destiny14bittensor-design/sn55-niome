from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from niome_subnet.dashboard.state import build_dashboard_snapshot


NOW = datetime(2026, 9, 24, 18, 40, tzinfo=timezone.utc)
HOTKEY = "5CvhkJazfXbFEyUH9x27UE5hVn5yCyFDfk2GqmYbPPmsf9TG"


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def runtime():
    return {
        "niome-dollar1": {
            "status": "online",
            "pid": 101,
            "restarts": 1,
            "started_at": "2026-09-24T17:00:00+00:00",
        },
        "niome-seed-bridge": {
            "status": "online",
            "pid": 102,
            "restarts": 1,
            "started_at": "2026-09-24T17:00:00+00:00",
        },
    }


def loaded_config():
    return {
        "niome-dollar1": {"guide_variants": 72, "primary_cas_share": 0.60},
        "niome-seed-bridge": {"guide_variants": 72, "primary_cas_share": 0.60},
    }


def make_active_task(root: Path, task_id: str = "task-live") -> Path:
    task = root / task_id
    task.mkdir()
    write_json(task / "task.json", {
        "id": task_id,
        "received_at": "2026-09-24T18:33:20+00:00",
        "contract_url": "https://example.invalid/?X-Amz-Signature=task-secret",
    })
    write_json(task / "status.json", {
        "task_id": task_id,
        "received_at": "2026-09-24T18:33:20+00:00",
        "state": "local_validation",
        "uploaded_at": "2026-09-24T18:34:32+00:00",
        "submission_rows": 250,
        "upload_elapsed_seconds": 72.4,
    })
    write_json(task / "request_envelope.json", {
        "presigned_url": "https://s3.invalid/?X-Amz-Signature=never-expose-this",
    })
    write_json(task / "seed_bridge_status.json", {
        "task_id": task_id,
        "state": "waiting_for_seeds",
        "started_at": "2026-09-24T18:33:21+00:00",
        "last_observed_at": "2026-09-24T18:39:55+00:00",
        "seed_target_block": 9139652,
        "live_stream_profiles": ["64kib-primary", "64kib-standby"],
        "stream_results": [
            {
                "label": label,
                "state": "streaming",
                "bytes_sent": 4096,
                "total_bytes": 100000,
                "send_count": 2,
                "last_successful_send_at": "2026-09-24T18:39:59+00:00",
            }
            for label in ("64kib-primary", "64kib-standby")
        ],
    })
    return task


def build(root: Path, **kwargs):
    return build_dashboard_snapshot(
        root,
        processes=kwargs.pop("processes", runtime()),
        current_block=9139400,
        miner_hotkey=HOTKEY,
        loaded_config=loaded_config(),
        now=NOW,
        **kwargs,
    )


def test_parallel_safe_and_bridge_lanes_are_distinct(tmp_path: Path):
    make_active_task(tmp_path)
    snapshot = build(tmp_path)
    current = snapshot["current"]

    assert current["stage"] == "waiting_for_seeds"
    assert current["safe"]["uploaded"] is True
    assert current["bridge"]["state"] == "waiting_for_seeds"
    assert current["bridge"]["blocks_to_seeds"] == 252
    assert len(current["bridge"]["streams"]) == 2
    assert current["config"]["all_match"] is True


def test_sensitive_presigned_urls_never_reach_snapshot(tmp_path: Path):
    make_active_task(tmp_path)
    serialized = json.dumps(build(tmp_path), sort_keys=True)

    assert "X-Amz" not in serialized
    assert "never-expose-this" not in serialized
    assert "task-secret" not in serialized
    assert "request_envelope" not in serialized


def test_all_stopped_streams_raise_critical_alert(tmp_path: Path):
    task = make_active_task(tmp_path)
    bridge = json.loads((task / "seed_bridge_status.json").read_text())
    bridge["live_stream_profiles"] = []
    for stream in bridge["stream_results"]:
        stream["state"] = "failed"
        stream["failure_category"] = "remote_connection_closed"
    write_json(task / "seed_bridge_status.json", bridge)

    current = build(tmp_path)["current"]
    assert current["overall"] == "critical"
    assert "all_streams_stopped" in {alert["code"] for alert in current["alerts"]}


def test_official_score_is_ranked_and_compared_to_local(tmp_path: Path):
    task = make_active_task(tmp_path)
    write_json(task / "seed_bridge_local_validation.json", {
        "final_score": 319.25,
        "breakdown": {"consistency_factor": 1.0, "total_weighted_score": 340.0},
        "valid_experiments": 250,
    })
    scores = [
        {"miner_hotkey": "competitor", "final_score": 316.0, "breakdown": {}},
        {"miner_hotkey": HOTKEY, "final_score": 319.25, "created_at": "2026-09-24T20:00:00", "breakdown": {}},
        {"miner_hotkey": "another", "final_score": 290.0, "breakdown": {}},
    ]

    current = build(tmp_path, scoreboards={task.name: scores})["current"]
    assert current["stage"] == "official_published"
    assert current["official"]["rank"] == 1
    assert current["official"]["gap_to_first"] == 0
    assert current["overall"] == "complete"
    assert "score_mismatch" not in {alert["code"] for alert in current["alerts"]}


def test_unknown_seed_holdout_is_labeled_and_not_compared_as_exact(tmp_path: Path):
    task = make_active_task(tmp_path)
    write_json(task / "local_validation.json", {
        "final_score": 80.0,
        "score_semantics": "unknown-seed-holdout-estimate",
        "comparable_to_official": False,
        "seed_policy": {"mode": "robust-unknown"},
        "breakdown": {"consistency_factor": 0.4},
    })
    scores = [{
        "miner_hotkey": HOTKEY,
        "final_score": 120.0,
        "created_at": "2026-09-24T20:00:00",
        "breakdown": {},
    }]

    current = build(tmp_path, scoreboards={task.name: scores})["current"]

    assert current["local"]["score_semantics"] == "unknown-seed-holdout-estimate"
    assert current["local"]["comparable_to_official"] is False
    assert "score_mismatch" not in {alert["code"] for alert in current["alerts"]}


def test_completed_task_does_not_flag_safe_restarts(tmp_path: Path):
    task = make_active_task(tmp_path)
    bridge = json.loads((task / "seed_bridge_status.json").read_text())
    bridge.update({
        "state": "complete",
        "completed_at": "2026-09-24T19:30:00+00:00",
        "put_result": {"state": "complete", "status": 200},
    })
    write_json(task / "seed_bridge_status.json", bridge)
    write_json(task / "seed_bridge_local_validation.json", {
        "final_score": 319.25,
        "breakdown": {},
        "valid_experiments": 250,
    })
    restarted = runtime()
    for process in restarted.values():
        process["started_at"] = "2026-09-24T20:05:00+00:00"
    scores = [{
        "miner_hotkey": HOTKEY,
        "final_score": 319.25,
        "created_at": "2026-09-24T20:00:00",
        "breakdown": {},
    }]

    current = build(
        tmp_path,
        processes=restarted,
        scoreboards={task.name: scores},
    )["current"]

    assert current["stage"] == "official_published"
    assert current["overall"] == "complete"
    assert "restart_during_task" not in {
        alert["code"] for alert in current["alerts"]
    }


def test_previous_scoreboard_produces_explicit_prediction(tmp_path: Path):
    previous = make_active_task(tmp_path, "task-old")
    write_json(previous / "status.json", {
        "task_id": previous.name,
        "received_at": "2026-09-24T16:00:00+00:00",
        "uploaded_at": "2026-09-24T16:01:00+00:00",
    })
    current_task = make_active_task(tmp_path, "task-new")
    write_json(current_task / "seed_bridge_local_validation.json", {
        "final_score": 319.25,
        "breakdown": {},
    })
    previous_scores = [
        {"miner_hotkey": "first", "final_score": 316.0},
        {"miner_hotkey": "second", "final_score": 300.0},
    ]

    current = build(tmp_path, scoreboards={previous.name: previous_scores})["current"]
    assert current["prediction"]["available"] is True
    assert current["prediction"]["rank"] == 1
    assert current["prediction"]["gap_to_first"] == 3.25
