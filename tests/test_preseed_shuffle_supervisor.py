from tools.preseed_shuffle_supervisor import (
    completed_round_count,
    should_refresh_constraints,
)


def test_only_new_completed_round_triggers_expensive_refresh() -> None:
    leak = {"summary": {"round_count": 29, "incomplete_round_count": 1}}
    assert completed_round_count(leak) == 29
    assert should_refresh_constraints(leak, {"constraint_rounds": 28}) is True
    assert should_refresh_constraints(leak, {"constraint_rounds": 29}) is False


def test_incomplete_round_does_not_advance_completed_count() -> None:
    leak = {"summary": {"round_count": 28, "incomplete_round_count": 1}}
    assert should_refresh_constraints(leak, {"constraint_rounds": 28}) is False
