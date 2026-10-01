from datetime import datetime, timezone
import random

from tools.preseed_validation_time_search import search_unit, validation_times


def test_validation_times_associate_events_with_current_task():
    task = "00000000-0000-4000-8000-000000000001"
    timestamp = "2026-09-29T01:02:03.500000Z"
    result = validation_times(
        [
            {"timestamp": "2026-09-29T00:00:00Z", "line": f"Fetched task {task}"},
            {"timestamp": timestamp, "line": "Validating miners' submissions ..."},
        ]
    )
    assert result[task] == datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


def test_fixed_second_offset_search_recovers_synthetic_python_clock_model():
    timestamps = [1_700_000_000.25, 1_700_001_000.75, 1_700_002_000.1]
    rows = [
        (timestamp, random.Random(int(timestamp) + 37).sample(range(100, 1000), 3))
        for timestamp in timestamps
    ]
    result = search_unit(rows, "seconds", 1, 50, workers=1)
    assert any(
        hit["delta"] == 37 and hit["method"] == "python-sample"
        for hit in result["full_discovery_hits"]
    )
