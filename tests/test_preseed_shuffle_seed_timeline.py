from tools.preseed_shuffle_seed_timeline import build_timeline


def test_timeline_selects_same_task_not_next_task() -> None:
    first = "11111111-1111-1111-1111-111111111111"
    second = "22222222-2222-2222-2222-222222222222"
    nodes = [
        {"timestamp": "1", "line": f"Fetched task {first}"},
        {"timestamp": "2", "line": "Validating miners' submissions ..."},
        {"timestamp": "3", "line": "Generated seeds: 123,456,789"},
        {"timestamp": "4", "line": f"Fetched task {second}"},
    ]
    result = build_timeline(
        nodes, {first: [123, 456, 789], second: [222, 333, 444]}
    )
    assert result["coupling_offset"] == "same-fetched-task"
    assert result["current_task_matches"] == 1
    assert result["next_task_matches"] == 0
