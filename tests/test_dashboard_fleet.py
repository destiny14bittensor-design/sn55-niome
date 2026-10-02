from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from niome_subnet.dashboard.fleet import (
    FleetDashboardCollector,
    MinerLaneConfig,
    build_fleet_state,
    scores_for_task,
)
from niome_subnet.dashboard.server import FLEET_LANES, select_fleet_lanes


def lane(number: int, profile: str = "baseline") -> MinerLaneConfig:
    return MinerLaneConfig(
        lane_id=f"dollar{number}",
        label=f"dollar{number}",
        uid=number,
        hotkey=f"hotkey-{number}",
        artifact_root=Path(f"/tmp/dollar{number}"),
        miner_process=f"miner-{number}",
        bridge_process=f"bridge-{number}",
        axon_port=8090 + number,
        profile=profile,
    )


def process(name: str, *, status: str = "online", cpu: float = 1.5) -> dict:
    return {
        "name": name,
        "pid": 100,
        "status": status,
        "restarts": 0,
        "cpu_percent": cpu,
        "memory_bytes": 1024,
    }


def snapshot(task_id: str, score: float, received: str) -> dict:
    return {
        "current": {
            "task_id": task_id,
            "received_at": received,
            "stage": "official_published",
            "overall": "complete",
            "local": {"score": score - 0.25},
            "official": {"published": True, "score": score, "rank": 2},
            "alerts": [],
        },
        "history": [],
    }


def all_processes(lanes: list[MinerLaneConfig]) -> dict:
    return {
        name: process(name)
        for item in lanes
        for name in (item.miner_process, item.bridge_process)
    }


def test_four_lanes_and_same_task_baseline_delta() -> None:
    lanes = [lane(1), lane(2), lane(3, "exploration"), lane(4, "exploration")]
    received = "2026-09-25T12:00:00+00:00"
    state = build_fleet_state(
        lanes=lanes,
        lane_snapshots={
            "dollar1": snapshot("task-a", 100.0, received),
            "dollar2": snapshot("task-a", 102.0, received),
            "dollar3": snapshot("task-a", 103.0, received),
            "dollar4": snapshot("task-a", 99.0, received),
        },
        processes=all_processes(lanes),
        current_block=9_000_000,
        chain_source="finney",
        now=datetime(2026, 9, 25, 12, 1, tzinfo=timezone.utc),
    )

    assert [item["id"] for item in state["miners"]] == [
        "dollar1",
        "dollar2",
        "dollar3",
        "dollar4",
    ]
    assert state["fleet"]["online"] == 4
    assert state["fleet"]["coverage"] == 4
    assert state["fleet"]["baseline_score"] == 101.0
    comparisons = {item["id"]: item["comparison"] for item in state["miners"]}
    assert comparisons["dollar1"]["delta"] == -1.0
    assert comparisons["dollar3"]["delta"] == 2.0


def test_different_task_is_not_compared_to_active_task() -> None:
    lanes = [lane(1), lane(2), lane(3, "exploration"), lane(4, "exploration")]
    snapshots = {
        "dollar1": snapshot("older", 400.0, "2026-09-25T11:00:00+00:00"),
        "dollar2": snapshot("older", 400.0, "2026-09-25T11:00:00+00:00"),
        "dollar3": snapshot("newer", 200.0, "2026-09-25T12:00:00+00:00"),
        "dollar4": snapshot("newer", 201.0, "2026-09-25T12:00:01+00:00"),
    }
    state = build_fleet_state(
        lanes=lanes,
        lane_snapshots=snapshots,
        processes=all_processes(lanes),
        current_block=None,
        chain_source="unavailable",
    )

    assert state["fleet"]["active_task_id"] == "newer"
    assert state["fleet"]["coverage"] == 2
    assert state["fleet"]["baseline_score"] is None
    assert all(not item["comparison"]["comparable"] for item in state["miners"])


def test_offline_lane_raises_fleet_critical_alert() -> None:
    lanes = [lane(1), lane(2), lane(3, "exploration"), lane(4, "exploration")]
    processes = all_processes(lanes)
    processes["miner-4"] = process("miner-4", status="stopped")
    state = build_fleet_state(
        lanes=lanes,
        lane_snapshots={},
        processes=processes,
        current_block=None,
        chain_source="unavailable",
    )

    assert state["fleet"]["online"] == 3
    assert state["fleet"]["overall"] == "critical"
    dollar4 = next(item for item in state["miners"] if item["id"] == "dollar4")
    assert dollar4["online"] is False
    assert dollar4["alerts"][0]["code"] == "lane_process_offline"


def test_seed_bridge_is_optional_for_honest_submission_health() -> None:
    lanes = [replace(lane(1), bridge_process=None)]
    processes = {"miner-1": process("miner-1")}
    state = build_fleet_state(
        lanes=lanes,
        lane_snapshots={},
        processes=processes,
        current_block=None,
        chain_source="unavailable",
    )

    assert state["fleet"]["online"] == 1
    assert state["miners"][0]["online"] is True


def test_live_fleet_has_one_control_per_host_pair() -> None:
    profiles = {item.lane_id: item.profile for item in FLEET_LANES}

    assert profiles == {
        "tao1": "baseline",
        "tao2": "exploration",
        "won1": "baseline",
        "won2": "exploration",
    }


def test_live_fleet_does_not_require_seed_bridges() -> None:
    assert all(item.bridge_process is None for item in FLEET_LANES)

    processes = {
        item.miner_process: process(item.miner_process) for item in FLEET_LANES
    }
    state = build_fleet_state(
        lanes=FLEET_LANES,
        lane_snapshots={},
        processes=processes,
        current_block=None,
        chain_source="unavailable",
    )

    assert state["fleet"]["online"] == 4
    assert not {
        alert["code"]
        for alert in state["fleet"]["alerts"]
        if alert["code"] == "lane_process_offline"
    }


def test_host_lane_selection_can_assign_only_won_miners() -> None:
    selected = select_fleet_lanes("won1,won2")

    assert [lane.lane_id for lane in selected] == ["won1", "won2"]


def test_score_poll_uses_mature_current_task_without_validation_block() -> None:
    collector = FleetDashboardCollector(
        lanes=[lane(1)], score_url="https://example.invalid/scores"
    )
    collector._lane_snapshots = {
        "dollar1": {
            "current": {
                "task_id": "mature-current",
                "received_at": "2020-01-01T00:00:00+00:00",
                "bridge": {"validation_block": None},
            },
            "history": [],
        }
    }

    assert collector._next_score_task(1000.0) == "mature-current"


def test_score_poll_does_not_skip_first_history_item() -> None:
    collector = FleetDashboardCollector(
        lanes=[lane(1)], score_url="https://example.invalid/scores"
    )
    collector._lane_snapshots = {
        "dollar1": {
            "current": {},
            "history": [{"task_id": "latest-history"}, {"task_id": "older"}],
        }
    }

    assert collector._next_score_task(1000.0) == "latest-history"


def test_score_api_rows_are_filtered_locally_by_task_id() -> None:
    rows = [
        {"task_id": "current", "miner_hotkey": "a", "final_score": 10.0},
        {"task_id": "older", "miner_hotkey": "a", "final_score": 99.0},
        {"miner_hotkey": "a", "final_score": 88.0},
    ]

    assert scores_for_task(rows, "current") == [rows[0]]
