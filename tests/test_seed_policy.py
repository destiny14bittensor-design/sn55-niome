import pytest

from niome_subnet.genomics.seed_policy import (
    UINT32_MAX,
    parse_seed_values,
    resolve_seed_plan,
)


def test_parses_legacy_and_extended_seed_shapes_without_old_range_cap():
    assert parse_seed_values("1, 50000,4294967295") == [1, 50000, UINT32_MAX]
    assert parse_seed_values([7, "8", 7]) == [7, 8]
    with pytest.raises(ValueError, match="uint32"):
        parse_seed_values(UINT32_MAX + 1)


def test_nonzero_contract_seeds_are_authoritative_and_exactly_comparable():
    plan = resolve_seed_plan({"seed": "50001,900000"}, "task-a")

    assert plan.mode == "contract-authoritative"
    assert plan.optimization_seeds == (50001, 900000)
    assert plan.evaluation_seeds == (50001, 900000)
    assert plan.comparable_to_official is True


def test_zero_placeholder_quarantines_provisional_block_seeds():
    plan = resolve_seed_plan(
        {"seed": 0},
        "task-b",
        supplied_seeds=[801, 354, 128],
    )

    assert plan.mode == "robust-unknown"
    assert plan.selection_profile == "ranked"
    assert plan.optimization_seeds == ()
    assert len(plan.evaluation_seeds) == 3
    assert not set(plan.evaluation_seeds) & {801, 354, 128}
    assert plan.comparable_to_official is False


def test_provisional_seeds_require_an_explicit_trust_switch():
    plan = resolve_seed_plan(
        {"seed": 0},
        "task-c",
        supplied_seeds=[801, 354, 128],
        trust_supplied_seeds=True,
    )

    assert plan.mode == "supplied-authoritative"
    assert plan.selection_profile == "seed-aware"
    assert plan.optimization_seeds == (801, 354, 128)
    assert plan.comparable_to_official is False


def test_finalized_chain_seeds_override_a_nonzero_contract_seed():
    plan = resolve_seed_plan(
        {"seed": "654,347,964"},
        "task-chain",
        supplied_seeds=[343, 783, 871],
        trust_supplied_seeds=True,
        prefer_supplied_seeds=True,
    )

    assert plan.mode == "chain-authoritative"
    assert plan.source == "finalized-block-hashes"
    assert plan.optimization_seeds == (343, 783, 871)
    assert plan.evaluation_seeds == (343, 783, 871)
    assert plan.selection_profile == "seed-aware"
    assert plan.contract_seed_raw == "654,347,964"
    assert plan.comparable_to_official is True


def test_invalid_contract_seed_fails_closed_into_robust_mode():
    plan = resolve_seed_plan({"seed": UINT32_MAX + 1}, "task-d")

    assert plan.mode == "robust-unknown"
    assert plan.selection_profile == "ranked"
    assert "unusable" in plan.reason
