from datetime import datetime

from tools.preseed_event_clock_search import earliest_score_times, generated_seed_times


def test_generated_seed_time_is_bound_to_current_fetched_task():
    task = "00000000-0000-4000-8000-000000000001"
    timestamp = "2026-09-29T01:02:03.500000Z"
    result = generated_seed_times(
        [
            {"timestamp": "2026-09-29T00:00:00Z", "line": f"Fetched task {task}"},
            {"timestamp": timestamp, "line": "Generated seeds: 123,456,789"},
        ]
    )
    assert result[task] == datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


def test_earliest_score_time_uses_source_time_not_fetch_order():
    task = "00000000-0000-4000-8000-000000000001"

    def fetcher(_task_id):
        return [
            {"created_at": "2026-09-29T01:02:04Z"},
            {"created_at": "2026-09-29T01:02:03Z"},
        ]

    result = earliest_score_times([task], fetcher=fetcher)
    assert result[task] == datetime.fromisoformat("2026-09-29T01:02:03+00:00").timestamp()
