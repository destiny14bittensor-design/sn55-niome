from niome_subnet.genomics.seed_optimizer import (
    SeedCandidate,
    _mixed_incoming_frontier,
    build_optimizer_pool,
    optimize_all_hdr_selection,
    quota_plan,
    score_selection,
)
from niome_subnet.genomics.validation.stage5 import compute_distribution_fidelity


ACTIVE_MUTATIONS = ("high", "low")
CAS_SYSTEMS = ("Cas9", "Cas12a")


def _guide(index: int, length: int = 20) -> str:
    alphabet = "ACGT"
    value = index + 1
    bases = []
    for _ in range(length):
        bases.append(alphabet[value % 4])
        value = value // 4 + 1
    return "".join(bases)


def _candidate(
    mutation: str,
    cas: str,
    strand: str,
    index: int,
    score: float,
) -> SeedCandidate:
    experiment = {
        "experiment_id": f"{mutation}-{cas}-{strand}-{index}",
        "guideRNA": _guide(index),
        "target_alignment_start": 1000 + index,
        "target_alignment_end": 1020 + index,
        "strand": strand,
        "mutation": mutation,
        "cas_system": cas,
    }
    return SeedCandidate(score, experiment, (1, 3, 3, 0))


def test_score_proxy_reproduces_validator_distribution_fidelity():
    candidates = []
    for index, (mutation, cas, strand) in enumerate(
        (mutation, cas, strand)
        for mutation in ACTIVE_MUTATIONS
        for cas in CAS_SYSTEMS
        for strand in ("+", "-")
    ):
        candidates.append(_candidate(mutation, cas, strand, index, 1.0 + index / 10))

    proxy = score_selection(
        candidates,
        active_mutations=ACTIVE_MUTATIONS,
        cas_systems=CAS_SYSTEMS,
    )
    validator = compute_distribution_fidelity(
        [
            {
                "experiment": candidate.experiment,
                "stage2": {"weighted_score": candidate.weighted_score},
            }
            for candidate in candidates
        ],
        [],
        {
            "active_mutations": list(ACTIVE_MUTATIONS),
            "rules": {"cas_systems": list(CAS_SYSTEMS)},
        },
    )

    assert proxy["distribution_fidelity"] == validator["distribution_fidelity_score"]
    assert proxy["total_weighted_score"] == sum(
        candidate.weighted_score for candidate in candidates
    )


def test_optimizer_pool_unions_score_and_diversity_leaders():
    candidates = []
    for index in range(12):
        candidate = _candidate(
            "high",
            "Cas9",
            "+",
            index,
            2.0 - index / 100,
        )
        candidate.experiment["target_alignment_start"] = 1000 if index < 8 else 2000 + index
        candidates.append(candidate)

    pool, diagnostics = build_optimizer_pool(
        candidates,
        pool_limit=6,
        score_leader_count=4,
    )

    assert [item.weighted_score for item in pool[:4]] == [2.0, 1.99, 1.98, 1.97]
    assert len(pool) == 6
    assert diagnostics["score_leaders"] == 4
    assert diagnostics["diversity_leaders"] == 2
    assert diagnostics["unique_starts"] >= 3
    assert len({item.design_key for item in pool}) == len(pool)


def test_swap_frontier_reserves_slots_for_pool_diversity_tail():
    selected = [
        _candidate("high", "Cas9", "+", index, 3.0 - index / 100)
        for index in range(4)
    ]
    for candidate in selected:
        candidate.experiment["target_alignment_start"] = 1000

    pool = []
    for index in range(20, 28):
        candidate = _candidate("high", "Cas9", "+", index, 2.0 - index / 1000)
        candidate.experiment["target_alignment_start"] = 1000
        pool.append(candidate)
    diversity_tail = _candidate("high", "Cas9", "+", 99, 1.0)
    diversity_tail.experiment["target_alignment_start"] = 9000
    pool.append(diversity_tail)

    frontier = _mixed_incoming_frontier(
        {("high", "Cas9", "+"): pool},
        selected,
        limit_per_bucket=4,
        diversity_per_bucket=1,
    )

    assert len(frontier) == 4
    assert [item.experiment["experiment_id"] for item in frontier[:3]] == [
        item.experiment["experiment_id"] for item in pool[:3]
    ]
    assert frontier[-1].experiment["experiment_id"] == (
        diversity_tail.experiment["experiment_id"]
    )


def test_optimizer_is_deterministic_and_never_regresses_legacy_proxy():
    pools = {}
    next_index = 0
    for mutation in ACTIVE_MUTATIONS:
        for cas in CAS_SYSTEMS:
            for strand in ("+", "-"):
                key = (mutation, cas, strand)
                base = 2.0 if mutation == "high" else 1.0
                pools[key] = []
                for offset in range(18):
                    pools[key].append(
                        _candidate(
                            mutation,
                            cas,
                            strand,
                            next_index,
                            base - offset / 1000,
                        )
                    )
                    next_index += 1

    legacy_quotas = quota_plan(
        target_count=40,
        active_mutations=ACTIVE_MUTATIONS,
        cas_systems=CAS_SYSTEMS,
        mutation_weights={"high": 2.0, "low": 1.0},
        minority_share=0.24,
        primary_cas_share=0.60,
    )
    legacy = []
    for key in sorted(legacy_quotas):
        legacy.extend(pools[key][: legacy_quotas[key]])

    first, first_diagnostics = optimize_all_hdr_selection(
        pools=pools,
        legacy_selection=legacy,
        target_count=40,
        active_mutations=ACTIVE_MUTATIONS,
        cas_systems=CAS_SYSTEMS,
        mutation_weights={"high": 2.0, "low": 1.0},
    )
    second, second_diagnostics = optimize_all_hdr_selection(
        pools=pools,
        legacy_selection=legacy,
        target_count=40,
        active_mutations=ACTIVE_MUTATIONS,
        cas_systems=CAS_SYSTEMS,
        mutation_weights={"high": 2.0, "low": 1.0},
    )

    assert [item.experiment["experiment_id"] for item in first] == [
        item.experiment["experiment_id"] for item in second
    ]
    assert first_diagnostics["optimized"]["proxy_score"] >= (
        first_diagnostics["legacy"]["proxy_score"]
    )
    assert second_diagnostics["optimized"] == first_diagnostics["optimized"]
    assert first_diagnostics["search_elapsed_seconds"] >= 0.0
    assert "two_step" in first_diagnostics
    assert [
        item["minority_share"]
        for item in first_diagnostics["balance_anchor_plans"]
    ] == [0.20, 0.22, 0.24, 0.26]
    assert len(first) == 40
    assert all(candidate.outcome_quality[0] == 1 for candidate in first)

    expired, expired_diagnostics = optimize_all_hdr_selection(
        pools=pools,
        legacy_selection=legacy,
        target_count=40,
        active_mutations=ACTIVE_MUTATIONS,
        cas_systems=CAS_SYSTEMS,
        mutation_weights={"high": 2.0, "low": 1.0},
        deadline_monotonic=0.0,
    )
    assert [item.experiment["experiment_id"] for item in expired] == [
        item.experiment["experiment_id"] for item in legacy
    ]
    assert expired_diagnostics["deadline_reached"] is True


def test_optimizer_can_escape_symmetric_joint_quotas():
    pools = {}
    next_index = 10_000
    favored = ("high", "Cas9", "-")
    for mutation in ACTIVE_MUTATIONS:
        for cas in CAS_SYSTEMS:
            for strand in ("+", "-"):
                key = (mutation, cas, strand)
                base = 5.0 if key == favored else (2.0 if mutation == "high" else 1.0)
                pools[key] = []
                for offset in range(50):
                    pools[key].append(
                        _candidate(
                            mutation,
                            cas,
                            strand,
                            next_index,
                            base - offset / 10_000,
                        )
                    )
                    next_index += 1

    symmetric_quotas = quota_plan(
        target_count=40,
        active_mutations=ACTIVE_MUTATIONS,
        cas_systems=CAS_SYSTEMS,
        mutation_weights={"high": 2.0, "low": 1.0},
        minority_share=0.20,
        primary_cas_share=0.50,
    )
    legacy = []
    for key in sorted(symmetric_quotas):
        legacy.extend(pools[key][: symmetric_quotas[key]])

    optimized, diagnostics = optimize_all_hdr_selection(
        pools=pools,
        legacy_selection=legacy,
        target_count=40,
        active_mutations=ACTIVE_MUTATIONS,
        cas_systems=CAS_SYSTEMS,
        mutation_weights={"high": 2.0, "low": 1.0},
        minority_shares=(0.20,),
        primary_cas_shares=(0.50,),
        greedy_plan_limit=0,
    )

    optimized_counts = diagnostics["optimized"]["counts"]["joint"]
    assert diagnostics["cross_bucket_swap_count"] > 0
    assert optimized_counts["high|Cas9|-"] > symmetric_quotas[favored]
    assert diagnostics["optimized"]["proxy_score"] > diagnostics["legacy"]["proxy_score"]
    assert len(optimized) == 40
