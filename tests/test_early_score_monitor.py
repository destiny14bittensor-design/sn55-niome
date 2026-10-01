import json
from argparse import Namespace
from pathlib import Path

import pytest

from tools import early_score_monitor as monitor


TASK = "task-1"
WANDB_TASK = "11111111-1111-1111-1111-111111111111"


def row(hotkey: str, at: str) -> dict:
    return {
        "id": f"secret-row-{hotkey}",
        "task_id": TASK,
        "miner_hotkey": hotkey,
        "created_at": at,
        "final_score": 19.123456789,
        "breakdown": {"should": "never be persisted"},
    }


def test_public_individual_filter_rejects_wrong_task_and_hotkey(monkeypatch):
    monkeypatch.setattr(
        monitor,
        "read_json_url",
        lambda _url: {
            "items": [
                row("wanted", "2026-09-29T01:00:00Z"),
                {**row("other", "2026-09-29T01:00:00Z")},
                {**row("wanted", "2026-09-29T01:00:00Z"), "task_id": "old"},
            ]
        },
    )
    rows = monitor.public_individual_rows(TASK, "wanted")
    assert len(rows) == 1
    assert rows[0]["task_id"] == TASK
    assert rows[0]["miner_hotkey"] == "wanted"


def test_batch_and_stagger_classification_and_default_false():
    state = monitor.MonitorState(TASK)
    state.set_cluster(
        monitor.summarize_cluster(
            [row("a", "2026-09-29T01:00:00Z"), row("b", "2026-09-29T01:00:00.2Z")]
        ),
        "2026-09-29T01:00:01Z",
    )
    summary = state.summary(monitor.load_submission_channel(None), "2026-09-29T01:00:02Z")
    assert summary["score_publication"]["classification"] == "batch"
    assert summary["decision"]["actionable"] is False

    state.set_cluster(
        monitor.summarize_cluster(
            [row("a", "2026-09-29T01:00:00Z"), row("b", "2026-09-29T01:00:04Z")]
        ),
        "2026-09-29T01:00:05Z",
    )
    assert state.summary(monitor.load_submission_channel(None), "2026-09-29T01:00:06Z")[
        "score_publication"
    ]["classification"] == "staggered"


def test_actionable_requires_all_four_gates():
    state = monitor.MonitorState(TASK, minimum_early_lead_seconds=1)
    state.add_observation(
        {
            "kind": "individual_score",
            "source": "authorized-jsonl:owned.jsonl",
            "target": "probe",
            "first_seen": "2026-09-29T01:00:00Z",
            "source_at": "2026-09-29T00:59:59Z",
        }
    )
    state.set_cluster(
        monitor.summarize_cluster(
            [
                row("a", "2026-09-29T01:00:10Z"),
                row("b", "2026-09-29T01:00:10Z"),
            ]
        ),
        "2026-09-29T01:00:10Z",
    )
    channel = {
        "provided": True,
        "status": "open",
        "writable": True,
        "independent": True,
        "deadline_at": "2026-09-29T01:02:00.000000+00:00",
        "build_seconds": 30.0,
        "upload_seconds": 10.0,
        "safety_seconds": 5.0,
    }
    summary = state.summary(channel, "2026-09-29T01:00:11Z")
    assert summary["lead_time"]["status"] == "confirmed_before_batch_completion"
    assert summary["submission_channel"]["net_budget_seconds"] == 75
    assert summary["decision"]["actionable"] is True

    channel["independent"] = False
    summary = state.summary(channel, "2026-09-29T01:00:11Z")
    assert summary["decision"]["actionable"] is False
    assert "channel_independent_of_score_source" in summary["decision"]["failed_gates"]


def test_signed_contract_requires_matching_owned_envelope(tmp_path: Path):
    envelope = tmp_path / "request_envelope.json"
    envelope.write_text(
        json.dumps({"task": {"id": TASK, "contract_url": "https://signed.invalid/token"}})
    )
    assert monitor.load_request_envelope(envelope, TASK).startswith("https://")
    with pytest.raises(ValueError):
        monitor.load_request_envelope(envelope, "different-task")


def test_authorized_jsonl_is_allowlisted_and_task_scoped(tmp_path: Path):
    events = tmp_path / "owned.jsonl"
    events.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "task_id": TASK,
                        "kind": "individual_score",
                        "target": "probe",
                        "source_at": "2026-09-29T01:00:00Z",
                        "score": 99,
                        "url": "https://must-not-leak.invalid",
                    }
                ),
                json.dumps({"task_id": "other", "kind": "individual_score"}),
                "not-json",
            ]
        )
    )
    result = monitor.read_authorized_jsonl(events, TASK, "2026-09-29T01:00:01Z")
    assert len(result) == 1
    assert set(result[0]) == {"kind", "source", "target", "first_seen", "source_at"}


def test_offline_fixture_writes_sanitized_timeline_and_atomic_summary(tmp_path: Path):
    fixture = tmp_path / "fixture.json"
    timeline = tmp_path / "timeline.jsonl"
    summary_path = tmp_path / "summary.json"
    fixture.write_text(
        json.dumps(
            {
                "frames": [
                    {
                        "observed_at": "2026-09-29T01:00:00Z",
                        "individual_rows": {"probe": [row("hot-secret", "2026-09-29T00:59:59Z")]},
                        "cluster_rows": [],
                        "submission_channel": {
                            "status": "open",
                            "writable": True,
                            "independent_of_score_source": True,
                            "deadline_at": "2026-09-29T01:03:00Z",
                            "build_seconds": 20,
                            "upload_seconds": 10,
                            "safety_seconds": 5,
                            "url": "https://must-not-leak.invalid",
                        },
                    },
                    {
                        "observed_at": "2026-09-29T01:00:10Z",
                        "individual_rows": {"probe": [row("hot-secret", "2026-09-29T00:59:59Z")]},
                        "cluster_rows": [
                            row("hot-secret", "2026-09-29T01:00:09Z"),
                            row("other-hot", "2026-09-29T01:00:09Z"),
                        ],
                        "submission_channel": {
                            "status": "open",
                            "writable": True,
                            "independent_of_score_source": True,
                            "deadline_at": "2026-09-29T01:03:00Z",
                            "build_seconds": 20,
                            "upload_seconds": 10,
                            "safety_seconds": 5,
                        },
                    },
                ]
            }
        )
    )
    args = Namespace(
        task_id=TASK,
        target=["probe=hot-secret"],
        request_envelope=None,
        owned_artifact=[],
        authorized_events=[],
        submission_status=None,
        timeline=timeline,
        summary=summary_path,
        fixture=fixture,
        interval=0,
        timeout=60,
        batch_spread_seconds=1,
        minimum_early_lead_seconds=1,
        once=False,
    )
    monitor.run(args)
    result = json.loads(summary_path.read_text())
    assert result["decision"]["actionable"] is True
    assert result["score_publication"]["cohort_size"] == 2
    assert result["score_publication"]["classification"] == "batch"
    persisted = timeline.read_text() + summary_path.read_text()
    assert "must-not-leak" not in persisted
    assert "secret-row" not in persisted
    assert "19.123" not in persisted
    assert "breakdown" not in persisted
    assert "hot-secret" not in persisted


def test_owned_artifact_extracts_timestamps_but_not_arbitrary_fields():
    observations = monitor.owned_timestamp_observations(
        {
            "task_id": TASK,
            "validation_started_at": "2026-09-29T01:00:00Z",
            "presigned_url": "https://must-not-leak.invalid",
            "headers": {"authorization": "secret"},
        },
        source="owned-artifact:status.json",
        task_id=TASK,
        observed_at="2026-09-29T01:00:01Z",
    )
    assert len(observations) == 1
    assert observations[0]["event"] == "validation_started_at"
    assert "url" not in json.dumps(observations)


def test_each_owned_validation_timestamp_has_a_stable_source_entry():
    state = monitor.MonitorState(TASK)
    for observation in monitor.owned_timestamp_observations(
        {
            "task_id": TASK,
            "validation_started_at": "2026-09-29T01:00:00Z",
            "validation_completed_at": "2026-09-29T01:00:10Z",
        },
        source="owned-artifact:status.json",
        task_id=TASK,
        observed_at="2026-09-29T01:00:11Z",
    ):
        state.add_observation(observation)
    summary = state.summary(monitor.load_submission_channel(None), "2026-09-29T01:00:12Z")
    owned_sources = [key for key in summary["sources"] if key.startswith("owned-artifact")]
    assert len(owned_sources) == 2


def test_target_found_only_in_full_batch_is_not_mislabeled_early():
    state = monitor.MonitorState(TASK, batch_settle_seconds=0)
    state.set_cluster(
        monitor.summarize_cluster(
            [row("owned", "2026-09-29T01:00:00Z"), row("other", "2026-09-29T01:00:00Z")]
        ),
        "2026-09-29T01:00:01Z",
    )
    state.add_observation(
        {
            "kind": "individual_score",
            "source": "public-task-cluster",
            "target": "owned",
            "first_seen": "2026-09-29T01:00:01Z",
            "source_at": "2026-09-29T01:00:00Z",
        }
    )
    summary = state.summary(
        monitor.load_submission_channel(None), "2026-09-29T01:00:02Z"
    )
    assert summary["lead_time"]["status"] == "same_cohort_snapshot"
    assert summary["decision"]["gates"]["early_individual_score"] is False


def test_public_wandb_scores_require_exact_task_and_do_not_return_values():
    lines = [
        {
            "timestamp": "2026-09-29T01:00:00Z",
            "line": f"Fetched task {WANDB_TASK}",
        },
        {
            "timestamp": "2026-09-29T01:00:10Z",
            "line": (
                "Scores: [MinerScore(uid=230, breakdown={}, "
                "final_score=123.456789, log=''), "
                "MinerScore(uid=4, breakdown={}, final_score=22.5, log='')]"
            ),
        },
    ]
    observations, armed_at = monitor.public_wandb_score_observations(
        lines,
        task_id=WANDB_TASK,
        observed_at="2026-09-29T01:00:10.5Z",
    )
    assert armed_at == "2026-09-29T01:00:00.000000+00:00"
    assert [item["target"] for item in observations] == ["uid:230", "uid:4"]
    persisted = json.dumps(observations)
    assert "123.456789" not in persisted
    assert "22.5" not in persisted


def test_public_wandb_later_task_fetch_disarms_association():
    lines = [
        {
            "timestamp": "2026-09-29T01:00:00Z",
            "line": f"Fetched task {WANDB_TASK}",
        },
        {
            "timestamp": "2026-09-29T01:00:05Z",
            "line": "Fetched task 00000000-0000-0000-0000-000000000002",
        },
        {
            "timestamp": "2026-09-29T01:00:10Z",
            "line": "Scores: [MinerScore(uid=230, breakdown={}, final_score=9.0)]",
        },
    ]
    observations, armed_at = monitor.public_wandb_score_observations(
        lines,
        task_id=WANDB_TASK,
        observed_at="2026-09-29T01:00:11Z",
    )
    assert observations == []
    assert armed_at is None


def test_submission_channel_rejects_a_different_task(tmp_path: Path):
    channel = tmp_path / "channel.json"
    channel.write_text(json.dumps({"task_id": "old-task", "writable": True}))
    with pytest.raises(ValueError):
        monitor.load_submission_channel(channel, TASK)
