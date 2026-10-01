import json
from pathlib import Path

from tools.seed_signal_supervisor import discover_tasks, load_state, record_child_result


def _envelope(root: Path, task_id: str) -> Path:
    task_dir = root / task_id
    task_dir.mkdir()
    path = task_dir / "request_envelope.json"
    path.write_text(json.dumps({"task": {"id": task_id}}), encoding="utf-8")
    return path


def test_discovery_ignores_old_and_completed_tasks(tmp_path: Path):
    old = _envelope(tmp_path, "old")
    new = _envelope(tmp_path, "new")
    old.touch()
    cutoff = old.stat().st_mtime + 0.001
    new.touch()
    # Filesystems with coarse timestamps still exercise completed filtering.
    found = discover_tasks(tmp_path, min(cutoff, new.stat().st_mtime), {"old"})
    assert [(task_id, path.name) for _, task_id, path in found] == [("new", "new")]


def test_new_state_starts_at_current_time(tmp_path: Path):
    state = load_state(tmp_path / "missing.json")
    assert state["completed_task_ids"] == []
    assert state["active_task_id"] is None
    assert state["not_before_epoch"] > 0


def test_interrupted_race_is_not_completed():
    state = {"active_task_id": "task", "completed_task_ids": []}
    completed = set()

    record_child_result(state, completed, "task", -15)

    assert completed == set()
    assert state["last_failed_task_id"] == "task"


def test_successful_race_is_completed():
    state = {"active_task_id": "task", "completed_task_ids": []}
    completed = set()

    record_child_result(state, completed, "task", 0)

    assert completed == {"task"}
    assert state["last_completed_task_id"] == "task"
