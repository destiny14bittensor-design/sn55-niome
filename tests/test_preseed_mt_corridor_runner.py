import pytest

from tools.preseed_mt_corridor_runner import corridor_arguments


def test_corridor_arguments_select_exact_rank():
    plan = {
        "corridors": [
            {"rank": 1, "solver_arguments": ["--rejection-checkpoint=10:1:2"]},
            {"rank": 2, "solver_arguments": ["--rejection-checkpoint=10:3:4"]},
        ]
    }
    assert corridor_arguments(plan, 2) == ["--rejection-checkpoint=10:3:4"]
    with pytest.raises(ValueError):
        corridor_arguments(plan, 3)
