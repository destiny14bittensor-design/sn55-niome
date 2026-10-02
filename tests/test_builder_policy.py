import pytest

from niome_subnet.genomics.builder_policy import (
    BUILDER_POLICIES,
    DEFAULT_BUILDER_POLICY,
    resolve_builder_policy,
)
from niome_subnet.genomics.seed_policy import resolve_seed_plan
from niome_subnet.miner.task_processor import (
    _candidate_variants,
    _select_candidate_index,
)


def _score(index, mean, per_seed, fidelity=0.9, rows=250):
    return {
        "index": index,
        "final_score": mean,
        "per_seed_final_scores": per_seed,
        "rows": rows,
        "breakdown": {"distribution_fidelity_factor": fidelity},
    }


def test_portfolio_policies_have_distinct_roles_and_controls():
    active = {
        "champion-v1",
        "champion-cas55-minor20-v2",
        "champion-cas65-minor20-v2",
        "champion-minor20-v2",
    }
    assert active <= set(BUILDER_POLICIES)
    policies = [BUILDER_POLICIES[policy_id] for policy_id in active]
    assert len({policy.role for policy in policies}) == 4
    assert len(
        {
            (
                policy.unknown_seed_selection_profile,
                policy.primary_cas_share,
                policy.minority_mutation_share,
                policy.consistency_objective,
                policy.stress_seed_ensemble_id,
            )
            for policy in policies
        }
    ) == 4


def test_policy_resolution_fails_closed(monkeypatch):
    monkeypatch.setenv("NIOME_BUILDER_POLICY", "maximin-v1")
    assert resolve_builder_policy().policy_id == "maximin-v1"
    with pytest.raises(ValueError, match="unknown NIOME_BUILDER_POLICY"):
        resolve_builder_policy("typo")


def test_policy_candidate_ratios_are_applied_without_losing_full_fallback():
    baseline = [
        {"experiment_id": str(index), "guideRNA": f"guide-{index}"}
        for index in range(100)
    ]
    variants = _candidate_variants(baseline, BUILDER_POLICIES["maximin-v1"])

    assert variants[0] == ("balanced-full", baseline)
    assert [len(candidate) for _, candidate in variants[:4]] == [100, 90, 80, 70]


def test_candidate_objectives_choose_different_risk_profiles():
    results = [
        _score(0, 50, [40, 50, 60], fidelity=0.88),
        _score(1, 49, [47, 49, 51], fidelity=0.84),
        _score(2, 48, [42, 48, 54], fidelity=0.96),
        _score(3, 52, [35, 50, 71], fidelity=0.86),
    ]

    assert _select_candidate_index(results, BUILDER_POLICIES["champion-v1"]) == 3
    assert _select_candidate_index(results, BUILDER_POLICIES["maximin-v1"]) == 1
    assert _select_candidate_index(results, BUILDER_POLICIES["fidelity-v1"]) == 2
    # Candidate 3 has the largest upside but violates 85% of baseline's
    # minimum (34 < 35 would fail; here 35 is safe), so it is intentionally used.
    assert _select_candidate_index(results, BUILDER_POLICIES["upside-v1"]) == 3


def test_safe_upside_rejects_a_catastrophic_candidate():
    results = [
        _score(0, 50, [40, 50, 60]),
        _score(1, 55, [10, 55, 100]),
        _score(2, 51, [38, 50, 65]),
    ]
    assert _select_candidate_index(results, BUILDER_POLICIES["upside-v1"]) == 2


def test_policy_domains_create_separate_deterministic_stress_ensembles():
    plans = {
        policy.policy_id: resolve_seed_plan(
            {"seed": 0},
            "task-1",
            stress_seed_ensemble_id=policy.stress_seed_ensemble_id,
        )
        for policy in BUILDER_POLICIES.values()
    }
    assert all(plan.mode == "robust-unknown" for plan in plans.values())
    assert len({plan.evaluation_seeds for plan in plans.values()}) == len(
        {policy.stress_seed_ensemble_id for policy in BUILDER_POLICIES.values()}
    )
    assert resolve_builder_policy(None) in BUILDER_POLICIES.values()
    assert DEFAULT_BUILDER_POLICY.policy_id == "champion-v1"
