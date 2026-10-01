import json
from pathlib import Path

from tools.early_score_supervisor import (
    build_submission_channel_evidence,
    discover_tasks,
    load_state,
    monitor_command,
    record_child_result,
)


def _envelope(root: Path, task_id: str) -> Path:
    task_dir = root / task_id
    task_dir.mkdir(parents=True)
    path = task_dir / "request_envelope.json"
    path.write_text(json.dumps({"task": {"id": task_id}}), encoding="utf-8")
    return path


def test_discovery_deduplicates_same_owned_task(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    a = _envelope(first, "task-a")
    b = _envelope(second, "task-a")
    found = discover_tasks([first, second], 0, set())
    assert len(found) == 1
    assert found[0][1] == "task-a"
    assert found[0][2] in {a, b}


def test_new_state_is_observation_only(tmp_path: Path):
    state = load_state(tmp_path / "missing.json", 3600)
    assert state["mode"].endswith("observation-only")
    assert state["safety"]["submission_writes"] is False


def test_failed_child_remains_retryable():
    state = {"active_task_id": "task", "completed_task_ids": []}
    completed: set[str] = set()
    record_child_result(state, completed, "task", -15)
    assert completed == set()
    assert state["last_failed_task_id"] == "task"


def test_monitor_command_never_contains_submission_action(tmp_path: Path):
    envelope = _envelope(tmp_path, "task-a")
    command = monitor_command(
        script=Path("monitor.py"),
        task_id="task-a",
        envelope_path=envelope,
        output_root=tmp_path / "out",
        targets=["miner=hotkey"],
        interval=0.5,
        timeout=100,
    )
    assert "--stop-after-batch" in command
    assert not any("submit" in token.lower() or token.upper() == "PUT" for token in command)


def test_monitor_command_can_add_public_wandb_without_credentials(tmp_path: Path):
    envelope = _envelope(tmp_path, "task-a")
    command = monitor_command(
        script=Path("monitor.py"),
        task_id="task-a",
        envelope_path=envelope,
        output_root=tmp_path / "out",
        targets=["miner=hotkey"],
        interval=0.5,
        timeout=100,
        wandb_public_run="genomes/niome/non2mca3",
    )
    assert command[-2:] == ["--wandb-public-run", "genomes/niome/non2mca3"]
    assert not any("api_key" in token.lower() for token in command)


def test_channel_evidence_strips_url_and_respects_disabled_policy(tmp_path: Path):
    envelope = _envelope(tmp_path, "task-a")
    envelope.write_text(
        json.dumps(
            {
                "task": {"id": "task-a"},
                "presigned_url": "https://owned.invalid/object?Expires=1999999999&Signature=secret",
            }
        ),
        encoding="utf-8",
    )
    (envelope.parent / "seed_bridge_status.json").write_text(
        json.dumps({"same_round_overwrite_enabled": False}), encoding="utf-8"
    )
    output = tmp_path / "out" / "channel.json"
    evidence = build_submission_channel_evidence(envelope, output)
    assert evidence["writable"] is False
    assert evidence["status"] == "policy_disabled"
    persisted = output.read_text(encoding="utf-8")
    assert "Signature" not in persisted
    assert "secret" not in persisted
