import sqlite3

import pytest

from tools.seed_probe_inverter import (
    completed_seeds,
    find_three_seed_candidates,
    open_database,
    store_fingerprint,
)


def test_find_three_seed_candidates_recovers_unordered_triple():
    scores = {seed: seed * seed / 17.0 + seed / 13.0 for seed in range(10, 31)}
    expected = (12, 19, 27)
    observed = sum(scores[seed] for seed in expected) / 3.0

    candidates = find_three_seed_candidates(scores, observed, tolerance=1e-11)

    assert expected in {tuple(item["seeds"]) for item in candidates}
    assert candidates[0]["absolute_residual"] <= 1e-11


def test_three_seed_solver_respects_tolerance():
    scores = {1: 0.125, 2: 0.5, 3: 1.125, 4: 2.0}
    observed = (scores[1] + scores[2] + scores[4]) / 3

    assert find_three_seed_candidates(scores, observed + 1e-5, tolerance=1e-9) == []


def test_sqlite_checkpoint_requires_every_lane(tmp_path):
    connection = open_database(tmp_path / "fingerprints.sqlite3")
    connection.executemany(
        "INSERT INTO lanes(lane_id,task_id,hotkey,artifact_dir) VALUES(?,?,?,?)",
        [("a", "task", "hotkey-a", "/a"), ("b", "task", "hotkey-b", "/b")],
    )
    store_fingerprint(
        connection,
        100,
        [
            {
                "lane_id": "a",
                "final_score": 1.0,
                "consistency_factor": 0.1,
                "distribution_fidelity_factor": 0.9,
                "total_weighted_score": 10.0,
            },
            {
                "lane_id": "b",
                "final_score": 2.0,
                "consistency_factor": 0.2,
                "distribution_fidelity_factor": 0.9,
                "total_weighted_score": 10.0,
            },
        ],
    )
    store_fingerprint(
        connection,
        101,
        [
            {
                "lane_id": "a",
                "final_score": 3.0,
                "consistency_factor": 0.3,
                "distribution_fidelity_factor": 0.9,
                "total_weighted_score": 10.0,
            }
        ],
    )
    connection.commit()

    assert completed_seeds(connection, ["a", "b"]) == {100}


def test_open_database_rejects_newer_schema(tmp_path):
    path = tmp_path / "fingerprints.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    connection.execute("INSERT INTO metadata VALUES('schema_version','99')")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="unsupported database schema"):
        open_database(path)
