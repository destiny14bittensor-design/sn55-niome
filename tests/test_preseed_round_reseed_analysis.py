from tools.preseed_round_reseed_analysis import (
    exact_affine_relations,
    modulo_window_coverage,
    parse_epoch_microseconds,
    rank_constant_relations,
)


def test_parse_epoch_microseconds_is_exact() -> None:
    assert parse_epoch_microseconds("1970-01-01T00:00:01.234567+00:00") == 1_234_567


def test_constant_relation_requires_same_value_across_tasks() -> None:
    sources = {"clock": [100, 200, 300, 400]}
    candidates = [[1, 107], [207, 9], [307], [407, 99]]
    result = rank_constant_relations(candidates, sources)["clock"]
    assert result["additive_max_support"] == 4
    assert result["additive_top"][0] == {"constant": 7, "task_support": 4}
    assert result["exact_constant_relation"] is True


def test_missing_source_rows_are_not_counted() -> None:
    result = rank_constant_relations(
        [[15], [25], [999]], {"clock": [10, 20, None]}
    )["clock"]
    assert result["evaluable_tasks"] == 2
    assert result["additive_max_support"] == 2


def test_affine_relation_must_select_candidate_in_every_task() -> None:
    result = exact_affine_relations(
        [[123, 37], [40], [43, 999], [46]], {"block": [10, 11, 12, 13]}
    )["block"]
    assert result["exact_affine_count"] == 1
    assert result["exact_affine"] == [{"multiplier": 3, "increment": 7}]


def test_affine_relation_rejects_partial_fit() -> None:
    result = exact_affine_relations(
        [[7], [12], [18]], {"round": [0, 1, 2]}
    )["round"]
    assert result["exact_affine_count"] == 0


def test_affine_relation_fails_if_any_task_has_no_preimage() -> None:
    result = exact_affine_relations(
        [[7], [12], []], {"round": [0, 1, 2]}
    )["round"]
    assert result["tasks_without_uint32_preimage"] == 1
    assert result["exact_affine_count"] == 0


def test_modulo_nanosecond_window_handles_uint32_wrap() -> None:
    result = modulo_window_coverage(
        [[3], [90]],
        {"validation:nanoseconds": [(1 << 32) - 2, 100]},
        radii=(5, 20),
    )["validation:nanoseconds"]
    assert result["coverage"] == {"5": 1, "20": 2}
