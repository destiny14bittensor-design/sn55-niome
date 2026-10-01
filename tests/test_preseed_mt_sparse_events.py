from tools.preseed_mt_sparse_events import (
    build_sparse_events,
    corridor_for_rank,
    event_rejection_bounds,
    exactly_one_sequential,
)
from tools.preseed_mt_xorsat_joint import Cnf


def test_corridor_lookup_and_conservative_interpolation():
    plan = {"corridors": [{"rank": 1, "checkpoints": {"10": [2, 5], "20": [7, 11]}}]}
    checkpoints = corridor_for_rank(plan, 1)
    assert event_rejection_bounds(5, checkpoints, 99) == (0, 5)
    assert event_rejection_bounds(15, checkpoints, 99) == (2, 11)
    assert event_rejection_bounds(25, checkpoints, 99) == (7, 99)


def test_exactly_one_sequential_accepts_one_and_rejects_two():
    pycryptosat = __import__("pycryptosat")
    solver = pycryptosat.Solver()
    cnf = Cnf(solver)
    values = [cnf.new() for _ in range(3)]
    exactly_one_sequential(cnf, values)
    assert solver.solve(assumptions=[values[1]])[0] is True
    assert solver.solve(assumptions=[values[0], values[1]])[0] is False


def test_sparse_events_include_sealed_prefix_but_not_seed_label():
    rows = [
        {"task_id": "a", "discovery_seed_label": [101, 202, 303]},
        {"task_id": "b", "seed_label_partition": "sealed"},
    ]
    tuples = {"a": [[1, 2]], "b": [[3, 4]]}
    events, groups = build_sparse_events(rows, tuples, [654, 347, 964])
    assert sum(event["kind"] == "discovery-seed" for event in events) == 3
    assert sum(event["kind"] == "shuffle-prefix" for event in events) == 4
    assert len(groups) == 2
    without_prefix, groups = build_sparse_events(
        rows, tuples, [654, 347, 964], include_prefix_tuples=False
    )
    assert all(event["kind"] != "shuffle-prefix" for event in without_prefix)
    assert groups == []
