import math

from tools.preseed_shuffle_leak_audit import (
    extract_order_events,
    permutation_information_bits,
    summarize_nodes,
)


def test_multiset_permutation_information_handles_duplicate_endpoints():
    # 4! / 2! possible observable orders.
    assert math.isclose(permutation_information_bits(["a", "a", "b", "c"]), math.log2(12))


def test_summary_never_retains_endpoint_values_or_seed_values():
    task_id = "00000000-0000-4000-8000-000000000001"
    summary = summarize_nodes(
        [
            {"timestamp": "1", "line": f"Fetched task {task_id}"},
            {"timestamp": "2", "line": 'HTTP Request: POST http://secret.example:8091/forward "200"'},
            {"timestamp": "3", "line": 'HTTP Request: POST http://secret.example:8091/forward "200"'},
            {"timestamp": "4", "line": 'HTTP Request: POST http://other.example:8092/forward "200"'},
            {"timestamp": "5", "line": "Generated seeds: 123,456,789"},
        ]
    )
    encoded = str(summary)
    assert "secret.example" not in encoded
    assert "123" not in encoded
    assert summary["rounds"][0]["forward_observations"] == 3
    assert summary["rounds"][0]["published_seed_triplet_observed"] is True


def test_error_uid_replaces_duplicate_http_status_event_and_appends_connection_error():
    task_id = "00000000-0000-4000-8000-000000000001"
    events = extract_order_events(
        [
            {"line": f"Fetched task {task_id}"},
            {"line": 'HTTP Request: POST http://host:1/forward "HTTP/1.1 500"'},
            {"line": "Error querying miner 7 at host:1: bad status"},
            {"line": "Error querying miner 9 at other:2: All connection attempts failed"},
        ]
    )
    assert events[task_id] == [
        {
            "kind": "uid",
            "value": 7,
            "endpoint": "host:1",
            "timestamp": "",
            "source": "validator-error",
        },
        {
            "kind": "uid",
            "value": 9,
            "endpoint": "other:2",
            "timestamp": "",
            "source": "validator-error",
        },
    ]
