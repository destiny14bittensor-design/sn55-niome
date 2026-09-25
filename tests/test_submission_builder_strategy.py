from niome_subnet.genomics.submission_builder import (
    EXPLORATION_GRID_STEP_THOUSANDTHS,
    EXPLORATION_VARIANTS_PER_ANCHOR,
    GUIDE_VARIANTS_PER_TARGET,
    PRIMARY_CAS_SHARE,
    SEED_FOCUSED_VARIANTS_PER_ANCHOR,
    _focused_guide_variants,
    _guide_variants,
    _profiled_share_grid,
)


def test_seed_aware_search_keeps_a_wide_valid_variant_frontier():
    guide = "A" * 20
    variants = _guide_variants(
        guide,
        "Cas9",
        {},
        max_mismatches=3,
    )

    assert GUIDE_VARIANTS_PER_TARGET == 72
    assert len(variants) == GUIDE_VARIANTS_PER_TARGET
    assert len(set(variants)) == len(variants)
    assert all(
        sum(left != right for left, right in zip(guide, variant)) <= 3
        for variant in variants
    )


def test_primary_cas_share_preserves_diversity_while_favoring_score():
    assert PRIMARY_CAS_SHARE == 0.60


def test_profiled_share_grid_is_deterministic_distinct_and_keeps_baseline():
    baseline = (0.50, 0.60, 0.70)
    first = _profiled_share_grid(
        "dollar3-hotkey",
        lower_thousandths=500,
        upper_thousandths=700,
        baseline=baseline,
    )
    second = _profiled_share_grid(
        "dollar3-hotkey",
        lower_thousandths=500,
        upper_thousandths=700,
        baseline=baseline,
    )
    identity_grids = {
        _profiled_share_grid(
            f"hotkey-{index}",
            lower_thousandths=500,
            upper_thousandths=700,
            baseline=baseline,
        )
        for index in range(10)
    }

    assert first == second
    assert len(identity_grids) > 1
    assert set(baseline).issubset(first)
    assert EXPLORATION_GRID_STEP_THOUSANDTHS == 5
    assert EXPLORATION_VARIANTS_PER_ANCHOR < SEED_FOCUSED_VARIANTS_PER_ANCHOR


def test_focused_variant_reservoir_is_wide_bounded_and_deterministic():
    guide = "A" * 20
    first = _focused_guide_variants(
        guide,
        "Cas9",
        {},
        max_mismatches=3,
        salt="task|mutation|Cas9|+|100",
    )
    second = _focused_guide_variants(
        guide,
        "Cas9",
        {},
        max_mismatches=3,
        salt="task|mutation|Cas9|+|100",
    )

    assert first == second
    assert len(first) == SEED_FOCUSED_VARIANTS_PER_ANCHOR
    assert len(set(first)) == len(first)
    assert all(
        1 <= sum(left != right for left, right in zip(guide, variant)) <= 3
        for variant in first
        if variant != guide
    )


def test_live_exploration_profiles_open_distinct_candidate_reservoirs():
    profiles = (
        "5Cd42XsDyg9QGovQKCVffbd2nk6cpfQbsCLZo4FF2fhLoveS",
        "5EFDuGe2nXZfb3cRG1K8KTs6ihePcMMUCsSJdcn9fLaaW6mT",
        "5ES1fyTQdvQjtmGokDSsb3fw2MyvzygAQ1oiRKiy9MDJwqKU",
    )
    reservoirs = {
        tuple(
            _focused_guide_variants(
                "A" * 20,
                "Cas9",
                {},
                max_mismatches=3,
                salt=(
                    "task|mutation|Cas9|+|100|158,569,938|"
                    f"{profile}"
                ),
                limit=EXPLORATION_VARIANTS_PER_ANCHOR,
            )
        )
        for profile in profiles
    }

    assert len(reservoirs) == len(profiles)
