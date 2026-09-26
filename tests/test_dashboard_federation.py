from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from niome_subnet.dashboard.federation import (
    FederationSourceConfig,
    SourceObservation,
    build_federated_state,
    parse_remote_sources,
)


NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
FULL_HOTKEY = "5CvhkJazfXbFEyUH9x27UE5hVn5yCyFDfk2GqmYbPPmsf9TG"


def source(
    source_id: str,
    lane: str,
    score: float,
    *,
    task_id: str = "task-shared",
    generated_at: datetime = NOW,
    block: int = 9_151_700,
) -> SourceObservation:
    return SourceObservation(
        config=FederationSourceConfig(source_id, source_id.title()),
        last_success_at=generated_at,
        last_checked_at=NOW,
        latency_ms=12.0,
        snapshot={
            "generated_at": generated_at.isoformat(),
            "version": 10,
            "chain": {"block": block, "round_phase": 240, "source": "finney"},
            "fleet": {
                "overall": "healthy",
                "online": 1,
                "total": 1,
                "active_task_id": task_id,
                "alerts": [],
            },
            "miners": [
                {
                    "id": lane,
                    "label": lane,
                    "uid": 1,
                    "hotkey": FULL_HOTKEY,
                    "hotkey_short": "short…key",
                    "profile": "baseline",
                    "online": True,
                    "overall": "healthy",
                    "resources": {},
                    "processes": {},
                    "alerts": [],
                    "history": [],
                    "current": {
                        "task_id": task_id,
                        "received_at": "2026-09-26T11:30:00+00:00",
                        "local": {
                            "score": score,
                            "score_semantics": "official-seed-replay",
                            "comparable_to_official": True,
                        },
                        "bridge": {
                            "failure": {
                                "error": (
                                    "PUT https://secret.invalid/key?X-Amz-Signature=nope "
                                    f"for {FULL_HOTKEY}"
                                )
                            }
                        },
                    },
                    "comparison": {"comparable": True, "delta": 0.0},
                }
            ],
        },
    )


def test_federation_namespaces_miners_and_ranks_same_task() -> None:
    state = build_federated_state(
        [source("tao-fleet", "tao1", 330.0), source("dollar-fleet", "dollar1", 332.0)],
        now=NOW,
    )

    assert state["schema_version"] == 1
    assert state["fleet"]["online"] == 2
    assert state["fleet"]["total"] == 2
    assert state["fleet"]["task_top_score"] == 332.0
    assert {miner["id"] for miner in state["miners"]} == {
        "tao-fleet:tao1",
        "dollar-fleet:dollar1",
    }
    comparisons = {
        miner["lane_id"]: miner["federation_comparison"]
        for miner in state["miners"]
    }
    assert comparisons["dollar1"]["rank"] == 1
    assert comparisons["tao1"]["delta_to_top"] == -2.0


def test_federation_does_not_compare_different_tasks() -> None:
    state = build_federated_state(
        [
            source("tao-fleet", "tao1", 400.0, task_id="old-task"),
            source("dollar-fleet", "dollar1", 200.0, task_id="new-task"),
        ],
        now=NOW,
    )

    assert len(state["tasks"]) == 2
    for miner in state["miners"]:
        assert miner["federation_comparison"]["participants"] == 1
        assert miner["federation_comparison"]["delta_to_top"] == 0.0


def test_stale_and_block_skew_are_explicit_alerts() -> None:
    stale = source(
        "tao-fleet",
        "tao1",
        330.0,
        generated_at=NOW - timedelta(seconds=20),
        block=9_151_690,
    )
    current = source("dollar-fleet", "dollar1", 332.0, block=9_151_700)
    state = build_federated_state([stale, current], now=NOW)

    source_rows = {item["id"]: item for item in state["sources"]}
    assert source_rows["tao-fleet"]["transport"] == "stale"
    codes = {alert["code"] for alert in state["fleet"]["alerts"]}
    assert {"source_stale", "chain_block_skew"}.issubset(codes)


def test_unreachable_source_keeps_cached_data_but_marks_it_offline() -> None:
    observation = source(
        "tao-fleet",
        "tao1",
        330.0,
        generated_at=NOW - timedelta(seconds=90),
    )
    observation.error = "ConnectError: refused"
    state = build_federated_state([observation], now=NOW)

    assert state["sources"][0]["transport"] == "unreachable"
    assert state["miners"][0]["online"] is False
    assert state["fleet"]["online"] == 0
    assert state["fleet"]["overall"] == "critical"


def test_federation_redacts_full_hotkeys_and_urls() -> None:
    state = build_federated_state([source("tao-fleet", "tao1", 330.0)], now=NOW)
    serialized = json.dumps(state)

    assert FULL_HOTKEY not in serialized
    assert "secret.invalid" not in serialized
    assert "X-Amz-Signature=nope" not in serialized
    assert "short" in serialized


def test_parse_remote_sources_validates_startup_allow_list() -> None:
    sources = parse_remote_sources(
        '[{"id":"tao-fleet","label":"Tao / Won","url":"http://108.181.196.26:8111/api/fleet/state"}]'
    )
    assert sources[0].source_id == "tao-fleet"
    assert sources[0].state_url.endswith("/api/fleet/state")

    with pytest.raises(ValueError, match="credentials"):
        parse_remote_sources(
            '[{"id":"bad","url":"http://user:pass@example.test/state"}]'
        )


def test_ranking_uses_latest_official_task_and_previous_rank() -> None:
    tao = source("tao-fleet", "tao1", 330.0)
    dollar = source("dollar-fleet", "dollar1", 332.0)
    tao.snapshot["miners"][0]["history"] = [
        {
            "task_id": "rank-latest",
            "received_at": "2026-09-26T10:00:00+00:00",
            "official_rank": 1,
            "official_score": 256.5,
        },
        {
            "task_id": "rank-previous",
            "received_at": "2026-09-26T08:00:00+00:00",
            "official_rank": 6,
            "official_score": 329.5,
        },
    ]
    dollar.snapshot["miners"][0]["history"] = [
        {
            "task_id": "rank-latest",
            "received_at": "2026-09-26T09:55:00+00:00",
            "official_rank": 2,
            "official_score": 256.2,
        },
        {
            "task_id": "rank-previous",
            "received_at": "2026-09-26T07:55:00+00:00",
            "official_rank": 7,
            "official_score": 329.2,
        },
    ]

    state = build_federated_state([dollar, tao], now=NOW)
    ranking = state["ranking"]

    assert ranking["task_id"] == "rank-latest"
    assert ranking["previous_task_id"] == "rank-previous"
    assert [row["miner_id"] for row in ranking["rows"]] == [
        "tao-fleet:tao1",
        "dollar-fleet:dollar1",
    ]
    assert ranking["rows"][0]["movement"] == 5
    assert all("hotkey_short" not in row for row in ranking["rows"])
