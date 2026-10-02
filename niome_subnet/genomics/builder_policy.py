"""Explicit, auditable submission policies for the four-miner portfolio.

The live miner used to hide most strategy choices in module constants.  That
made every hotkey produce the same payload even when the process manager gave
the corresponding bridge a different exploration profile.  A
``BuilderPolicy`` is immutable and is passed through the complete live path so
the policy recorded beside a submission is the policy that actually built it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from typing import Any


@dataclass(frozen=True)
class BuilderPolicy:
    """Bounded strategy controls that are safe to persist with an artifact."""

    policy_id: str
    role: str
    unknown_seed_selection_profile: str
    tie_break_salt: str
    guide_variants_per_target: int
    primary_cas_share: float
    minority_mutation_share: float
    diversity_weight: float
    consistency_objective: str
    stress_seed_ensemble_id: str
    candidate_ratios: tuple[float, ...]
    near_tie_score_tolerance: float = 0.0
    include_distinct_guides_candidate: bool = False
    upside_safety_ratio: float = 0.85
    lifecycle: str = "canary"

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise ValueError("policy_id must not be empty")
        if self.unknown_seed_selection_profile not in {
            "ranked",
            "energy-spread",
            "fidelity-spread",
            "cas-separated",
            "ranked-energy-10",
            "ranked-energy-20",
            "ranked-energy-30",
            "ranked-reservoir",
        }:
            raise ValueError("unsupported unknown-seed selection profile")
        if not 1 <= self.guide_variants_per_target <= 512:
            raise ValueError("guide_variants_per_target must be in 1..512")
        for name, value in (
            ("primary_cas_share", self.primary_cas_share),
            ("minority_mutation_share", self.minority_mutation_share),
            ("diversity_weight", self.diversity_weight),
            ("upside_safety_ratio", self.upside_safety_ratio),
            ("near_tie_score_tolerance", self.near_tie_score_tolerance),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in 0..1")
        if self.consistency_objective not in {
            "mean-then-min",
            "maximin",
            "fidelity-first",
            "safe-upside",
        }:
            raise ValueError("unsupported consistency objective")
        if not self.candidate_ratios or self.candidate_ratios[0] != 1.0:
            raise ValueError("candidate_ratios must start with the full submission")
        if any(not 0.0 < value <= 1.0 for value in self.candidate_ratios):
            raise ValueError("candidate ratios must be in (0, 1]")

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["candidate_ratios"] = list(self.candidate_ratios)
        return value


# These policies are intentionally conservative.  Their purpose is to create
# genuinely different, valid first submissions while historical replay decides
# which lane deserves promotion.  No policy is labelled "verified" until that
# gate passes.
BUILDER_POLICIES: dict[str, BuilderPolicy] = {
    "champion-v1": BuilderPolicy(
        policy_id="champion-v1",
        role="structural-score-reference",
        unknown_seed_selection_profile="ranked",
        tie_break_salt="",
        guide_variants_per_target=72,
        primary_cas_share=0.60,
        minority_mutation_share=0.16,
        diversity_weight=0.0,
        consistency_objective="mean-then-min",
        stress_seed_ensemble_id="broad-a-v1",
        candidate_ratios=(1.0,),
    ),
    "maximin-v1": BuilderPolicy(
        policy_id="maximin-v1",
        role="unknown-seed-downside-protection",
        unknown_seed_selection_profile="energy-spread",
        tie_break_salt="maximin-v1",
        guide_variants_per_target=72,
        primary_cas_share=0.60,
        minority_mutation_share=0.20,
        diversity_weight=0.35,
        consistency_objective="maximin",
        stress_seed_ensemble_id="broad-b-v1",
        candidate_ratios=(1.0, 0.90, 0.80, 0.70),
        include_distinct_guides_candidate=True,
    ),
    "fidelity-v1": BuilderPolicy(
        policy_id="fidelity-v1",
        role="distribution-fidelity-protection",
        unknown_seed_selection_profile="fidelity-spread",
        tie_break_salt="fidelity-v1",
        guide_variants_per_target=72,
        primary_cas_share=0.50,
        minority_mutation_share=0.25,
        diversity_weight=0.20,
        consistency_objective="fidelity-first",
        stress_seed_ensemble_id="broad-c-v1",
        candidate_ratios=(1.0, 0.95, 0.90, 0.85),
        include_distinct_guides_candidate=True,
    ),
    "upside-v1": BuilderPolicy(
        policy_id="upside-v1",
        role="anti-correlated-safe-upside",
        unknown_seed_selection_profile="cas-separated",
        tie_break_salt="upside-v1",
        guide_variants_per_target=72,
        primary_cas_share=0.70,
        minority_mutation_share=0.32,
        diversity_weight=0.65,
        consistency_objective="safe-upside",
        stress_seed_ensemble_id="broad-d-v1",
        candidate_ratios=(1.0, 0.90, 0.80, 0.70),
        include_distinct_guides_candidate=True,
        upside_safety_ratio=0.85,
    ),
    "champion-minor20-v2": BuilderPolicy(
        policy_id="champion-minor20-v2",
        role="champion-balanced-fidelity",
        unknown_seed_selection_profile="ranked",
        tie_break_salt="champion-minor20-v2",
        guide_variants_per_target=72,
        primary_cas_share=0.60,
        minority_mutation_share=0.20,
        diversity_weight=0.0,
        consistency_objective="mean-then-min",
        stress_seed_ensemble_id="broad-b-v1",
        candidate_ratios=(1.0,),
    ),
    "champion-cas55-minor20-v2": BuilderPolicy(
        policy_id="champion-cas55-minor20-v2",
        role="champion-cas55-diversifier",
        unknown_seed_selection_profile="ranked",
        tie_break_salt="champion-cas55-minor20-v2",
        guide_variants_per_target=72,
        primary_cas_share=0.55,
        minority_mutation_share=0.20,
        diversity_weight=0.0,
        consistency_objective="mean-then-min",
        stress_seed_ensemble_id="broad-c-v1",
        candidate_ratios=(1.0,),
    ),
    "champion-cas65-minor20-v2": BuilderPolicy(
        policy_id="champion-cas65-minor20-v2",
        role="champion-cas65-diversifier",
        unknown_seed_selection_profile="ranked",
        tie_break_salt="champion-cas65-minor20-v2",
        guide_variants_per_target=72,
        primary_cas_share=0.65,
        minority_mutation_share=0.20,
        diversity_weight=0.0,
        consistency_objective="mean-then-min",
        stress_seed_ensemble_id="broad-d-v1",
        candidate_ratios=(1.0,),
    ),
    "champion-hybrid10-cas55-v3": BuilderPolicy(
        policy_id="champion-hybrid10-cas55-v3",
        role="top80-low-risk-energy-signal",
        unknown_seed_selection_profile="ranked-energy-10",
        tie_break_salt="champion-hybrid10-cas55-v3",
        guide_variants_per_target=72,
        primary_cas_share=0.55,
        minority_mutation_share=0.20,
        diversity_weight=0.10,
        consistency_objective="mean-then-min",
        stress_seed_ensemble_id="broad-c-v1",
        candidate_ratios=(1.0,),
    ),
    "champion-hybrid20-cas65-v3": BuilderPolicy(
        policy_id="champion-hybrid20-cas65-v3",
        role="top80-balanced-energy-signal",
        unknown_seed_selection_profile="ranked-energy-20",
        tie_break_salt="champion-hybrid20-cas65-v3",
        guide_variants_per_target=72,
        primary_cas_share=0.65,
        minority_mutation_share=0.20,
        diversity_weight=0.20,
        consistency_objective="mean-then-min",
        stress_seed_ensemble_id="broad-d-v1",
        candidate_ratios=(1.0,),
    ),
    "champion-hybrid30-v3": BuilderPolicy(
        policy_id="champion-hybrid30-v3",
        role="top80-high-energy-signal",
        unknown_seed_selection_profile="ranked-energy-30",
        tie_break_salt="champion-hybrid30-v3",
        guide_variants_per_target=72,
        primary_cas_share=0.60,
        minority_mutation_share=0.20,
        diversity_weight=0.30,
        consistency_objective="mean-then-min",
        stress_seed_ensemble_id="broad-b-v1",
        candidate_ratios=(1.0,),
    ),
    "champion-reservoir001-cas55-v3": BuilderPolicy(
        policy_id="champion-reservoir001-cas55-v3",
        role="top80-near-tie-low-spread",
        unknown_seed_selection_profile="ranked-reservoir",
        tie_break_salt="champion-reservoir001-cas55-v3",
        guide_variants_per_target=72,
        primary_cas_share=0.55,
        minority_mutation_share=0.20,
        diversity_weight=0.0,
        consistency_objective="mean-then-min",
        stress_seed_ensemble_id="broad-c-v1",
        candidate_ratios=(1.0,),
        near_tie_score_tolerance=0.001,
    ),
    "champion-reservoir003-cas65-v3": BuilderPolicy(
        policy_id="champion-reservoir003-cas65-v3",
        role="top80-near-tie-medium-spread",
        unknown_seed_selection_profile="ranked-reservoir",
        tie_break_salt="champion-reservoir003-cas65-v3",
        guide_variants_per_target=72,
        primary_cas_share=0.65,
        minority_mutation_share=0.20,
        diversity_weight=0.0,
        consistency_objective="mean-then-min",
        stress_seed_ensemble_id="broad-d-v1",
        candidate_ratios=(1.0,),
        near_tie_score_tolerance=0.003,
    ),
    "champion-reservoir005-v3": BuilderPolicy(
        policy_id="champion-reservoir005-v3",
        role="top80-near-tie-high-spread",
        unknown_seed_selection_profile="ranked-reservoir",
        tie_break_salt="champion-reservoir005-v3",
        guide_variants_per_target=72,
        primary_cas_share=0.60,
        minority_mutation_share=0.20,
        diversity_weight=0.0,
        consistency_objective="mean-then-min",
        stress_seed_ensemble_id="broad-b-v1",
        candidate_ratios=(1.0,),
        near_tie_score_tolerance=0.005,
    ),
}

DEFAULT_BUILDER_POLICY_ID = "champion-v1"
DEFAULT_BUILDER_POLICY = BUILDER_POLICIES[DEFAULT_BUILDER_POLICY_ID]


def resolve_builder_policy(value: str | None = None) -> BuilderPolicy:
    """Resolve an exact built-in policy; unknown names fail closed."""

    policy_id = (value or os.getenv("NIOME_BUILDER_POLICY") or "").strip()
    policy_id = policy_id or DEFAULT_BUILDER_POLICY_ID
    try:
        return BUILDER_POLICIES[policy_id]
    except KeyError as error:
        choices = ", ".join(sorted(BUILDER_POLICIES))
        raise ValueError(
            f"unknown NIOME_BUILDER_POLICY {policy_id!r}; choose one of {choices}"
        ) from error
