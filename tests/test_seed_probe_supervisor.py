import json

from tools.seed_probe_supervisor import discover_tasks, load_state, record_child_result


def _task(root, task_id, *, state="complete"):
    directory = root / task_id
    directory.mkdir()
    for name in (
        "contract.json",
        "hbb_reference.json",
        "cell_types.json",
        "submission.json",
    ):
        (directory / name).write_text("{}", encoding="utf-8")
    (directory / "status.json").write_text(
        json.dumps({"task_id": task_id, "state": state}), encoding="utf-8"
    )
    return directory


def test_discover_tasks_requires_complete_artifacts(tmp_path):
    complete = _task(tmp_path, "complete")
    _task(tmp_path, "building", state="building")
    missing = _task(tmp_path, "missing")
    (missing / "submission.json").unlink()

    found = discover_tasks(tmp_path, 0.0, set())

    assert [(task_id, path) for _mtime, task_id, path in found] == [
        ("complete", complete)
    ]


def test_discover_tasks_honors_completed_and_start_boundary(tmp_path):
    task = _task(tmp_path, "task")
    modified = (task / "status.json").stat().st_mtime

    assert discover_tasks(tmp_path, modified + 1, set()) == []
    assert discover_tasks(tmp_path, 0, {"task"}) == []


def test_load_state_sets_fresh_boundary(tmp_path):
    state = load_state(tmp_path / "absent.json")
    assert state["not_before_epoch"] > 0
    assert state["completed_task_ids"] == []


def test_interrupted_child_remains_retryable():
    state = {"active_task_id": "task", "completed_task_ids": []}
    completed = set()

    record_child_result(state, completed, "task", -15)

    assert completed == set()
    assert state["completed_task_ids"] == []
    assert state["last_failed_task_id"] == "task"


def test_successful_child_is_completed():
    state = {"active_task_id": "task", "completed_task_ids": []}
    completed = set()

    record_child_result(state, completed, "task", 0)

    assert completed == {"task"}
    assert state["last_completed_task_id"] == "task"
