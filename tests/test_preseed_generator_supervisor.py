from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.preseed_generator_supervisor import (
    context_for_task,
    freeze_split_manifest,
    locate_floor_block,
    refresh_context_headers,
    research_task_window,
    split_records,
)


class Info:
    def __init__(self, number: int, start: datetime):
        self.number = number
        self.timestamp = start + timedelta(seconds=12 * number)
        self.hash = "0x" + f"{number:064x}"
        self.header = {
            "number": number,
            "parentHash": "0x" + f"{max(0, number - 1):064x}",
            "digest": {"logs": []},
        }


START = datetime(2026, 9, 26, tzinfo=timezone.utc)


def _info(number: int) -> Info:
    return Info(number, START)


def test_binary_search_finds_public_timestamp_floor():
    target = START + timedelta(seconds=12 * 50 + 5)
    assert locate_floor_block(target, lower=0, upper=100, block_info=_info) == 50


def test_context_roles_never_exceed_finalized_head():
    context = context_for_task(
        "task",
        (START + timedelta(seconds=12 * 100 + 2)).isoformat(),
        finalized_head=200,
        max_age_blocks=200,
        block_info=_info,
    )
    assert context is not None
    assert context["block_context"]["created"]["number"] == 100
    assert context["block_context"]["created"]["header"]["number"] == 100
    assert "validation" not in context["block_context"]


def test_cached_context_is_backfilled_by_exact_height():
    context = {
        "task_id": "task",
        "block_context": {
            "created": {"number": 100, "hash": "0x" + f"{100:064x}"},
        },
    }
    refreshed = refresh_context_headers(
        context,
        finalized_head=600,
        block_info=_info,
    )
    assert refreshed["block_context"]["created"]["header"]["number"] == 100
    assert refreshed["block_context"]["validation"]["number"] == 550


def _task(index: int, seed: str | int) -> dict:
    return {
        "id": f"00000000-0000-4000-8000-{index:012d}",
        "created_at": (START + timedelta(minutes=index)).isoformat(),
        "content": {"contract": {"seed": seed, "active_mutations": ["m1"], "cell_type": "K562"}},
    }


def test_split_is_disjoint_and_keeps_seedless_prospective():
    tasks = [_task(index, f"{100 + index},{200 + index},{300 + index}") for index in range(26)]
    tasks.append(_task(26, 0))
    contexts = {
        task["id"]: {
            "block_context": {"created": {"number": index, "hash": "0x" + f"{index:064x}"}}
        }
        for index, task in enumerate(tasks)
    }
    discovery, holdout, prospective = split_records(tasks, contexts)
    assert (len(discovery), len(holdout), len(prospective)) == (20, 5, 2)
    assert not ({row["task_id"] for row in discovery} & {row["task_id"] for row in prospective})


def test_research_window_bounds_historical_rpc_work():
    tasks = [_task(index, f"{100 + index},{200 + index},{300 + index}") for index in range(40)]
    tasks.append(_task(40, 0))
    window = research_task_window(tasks, labeled_limit=30)
    assert len(window) == 31
    assert tasks[0]["id"] not in {task["id"] for task in window}
    assert tasks[-1]["id"] in {task["id"] for task in window}


def test_frozen_manifest_keeps_discovery_stable_as_new_tasks_arrive():
    tasks = [_task(index, f"{100 + index},{200 + index},{300 + index}") for index in range(30)]
    manifest = freeze_split_manifest(tasks)
    assert manifest is not None
    assert manifest["discovery"] == [task["id"] for task in tasks[:20]]
    assert manifest["holdout"] == [task["id"] for task in tasks[20:25]]

    later = [*tasks, *[_task(index, f"{100 + index},{200 + index},{300 + index}") for index in range(30, 40)]]
    assert freeze_split_manifest(later, manifest) == manifest
    window = research_task_window(
        later,
        labeled_limit=30,
        pinned_ids=set(manifest["discovery"] + manifest["holdout"]),
    )
    assert set(manifest["discovery"] + manifest["holdout"]) <= {
        task["id"] for task in window
    }


def test_research_window_excludes_pre_epoch_history():
    historical = _task(1, "101,202,303")
    historical["created_at"] = "2026-09-25T20:30:35Z"
    current = _task(2, "104,205,306")
    window = research_task_window([historical, current])
    assert [task["id"] for task in window] == [current["id"]]
