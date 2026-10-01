import pytest

from tools.preseed_mt_prefix_cpsat import prefix_by_task, stream_rows, tuples_by_task


def test_prefix_report_is_partitioned_and_indexed_by_task():
    payload = {
        "rounds": [
            {
                "task_id": "train",
                "seed_label_partition": "discovery",
                "prefix": [
                    {"shuffle_index": 7, "choice_domain": [1, 4]},
                    {"shuffle_index": 6, "choice_domain": [2]},
                ],
            },
            {
                "task_id": "sealed",
                "seed_label_partition": "sealed",
                "prefix": [{"shuffle_index": 7, "choice_domain": [0]}],
            },
        ]
    }
    assert prefix_by_task(payload) == {
        "train": {7: [1, 4], 6: [2]},
        "sealed": {7: [0]},
    }


def test_allowed_bounded_draw_domain_is_enforced():
    cp_model = pytest.importorskip("ortools.sat.python.cp_model")
    from tools.preseed_mt_cpsat_joint import add_bounded_draw

    model = cp_model.CpModel()
    raw = [model.new_int_var(0, 7, f"raw_{index}") for index in range(3)]
    pointer = model.new_int_var(0, 0, "pointer")
    following, _ = add_bounded_draw(
        model,
        {3: raw},
        pointer,
        maximum=7,
        raw_cap=3,
        max_rejections=0,
        name="choice",
        allowed_values=[2, 5],
    )
    model.add(raw[0] == 5)
    model.add(following == 1)
    solver = cp_model.CpSolver()
    assert solver.solve(model) in {cp_model.OPTIMAL, cp_model.FEASIBLE}


def test_tuple_report_keeps_public_shuffle_constraints_for_sealed_rows():
    payload = {
        "rounds": [
            {
                "task_id": "train",
                "seed_label_partition": "discovery",
                "choice_indices": [7, 6],
                "choice_tuples": [[1, 2], [3, 4]],
            },
            {
                "task_id": "holdout",
                "seed_label_partition": "sealed",
                "choice_indices": [7],
                "choice_tuples": [[1]],
            },
        ]
    }
    assert tuples_by_task(payload) == {
        "train": ([7, 6], [[1, 2], [3, 4]]),
        "holdout": ([7], [[1]]),
    }


def test_stream_window_can_start_at_sealed_public_shuffle():
    payload = {
        "rounds": [
            {"task_id": "d0", "shuffle_size": 8},
            {"task_id": "d1", "shuffle_size": 8},
            {"task_id": "sealed", "shuffle_size": 8},
        ]
    }
    assert [row["task_id"] for row in stream_rows(payload, 1, 2)] == ["sealed"]
