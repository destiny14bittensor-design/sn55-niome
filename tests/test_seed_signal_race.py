from tools import seed_signal_race as race


def test_parse_seeds_rejects_unknown_and_partial_zero_values():
    assert race.parse_seeds(0) == []
    assert race.parse_seeds("123,0,456") == []
    assert race.parse_seeds("123, 456, 789") == [123, 456, 789]


def test_first_score_time_rejects_backend_filter_mismatch(monkeypatch):
    monkeypatch.setattr(
        race,
        "read_json",
        lambda _url: {
            "items": [
                {
                    "task_id": "previous-task",
                    "created_at": "2026-09-28T00:00:00Z",
                }
            ]
        },
    )

    assert race.first_score_time("current-task") is None


def test_first_score_time_accepts_matching_task(monkeypatch):
    monkeypatch.setattr(
        race,
        "read_json",
        lambda _url: {
            "items": [
                {
                    "task_id": "current-task",
                    "created_at": "2026-09-28T01:02:03Z",
                }
            ]
        },
    )

    assert race.first_score_time("current-task") == "2026-09-28T01:02:03Z"
