from tools.preseed_shuffle_forced_bits import project_row


def test_complete_unique_shuffle_forces_every_choice_bit():
    row = {
        "task_id": "synthetic",
        "seed_label_partition": "discovery",
        "initial_uids": [0, 1, 2, 3, 4],
        "initial_domain_sequence": ["u0", "u1", "u2", "u3", "u4"],
        "ordered_uid_domains": ["u3", "u4", "u0", "u2", "u1"],
        "uid_domains": {f"u{uid}": [uid] for uid in range(5)},
    }
    result = project_row(row, per_query_time_limit=1, threads=1)
    assert result["status"] == "complete"
    assert result["bits_unknown"] == 0
    assert result["bits_forced"] == result["bits_tested"]


def test_partial_shuffle_never_marks_unknown_as_forced():
    row = {
        "task_id": "synthetic-partial",
        "seed_label_partition": "discovery",
        "initial_uids": [0, 1, 2, 3, 4],
        "initial_domain_sequence": ["u0", "u1", "u2", "u3", "u4"],
        "ordered_uid_domains": ["u3", "u4", "u0", "u1"],
        "uid_domains": {f"u{uid}": [uid] for uid in range(5)},
    }
    result = project_row(row, per_query_time_limit=1, threads=1)
    assert result["status"] == "complete"
    assert result["bits_unknown"] == 0
    assert 0 < result["bits_forced"] < result["bits_tested"]
