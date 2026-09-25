"""Pure deterministic optimizer for seed-aware NIOME submissions."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
import math
import time
from typing import Any, Iterable, Mapping, Sequence


JointKey = tuple[str, str, str]
OutcomeQuality = tuple[int, int, int, int]

DEFAULT_MINORITY_SHARES = tuple(value / 100 for value in range(14, 37)) + (0.40,)
DEFAULT_PRIMARY_CAS_SHARES = tuple(value / 1000 for value in range(500, 701, 25))
DEFAULT_POOL_PER_BUCKET = 256
DEFAULT_SCORE_LEADERS_PER_BUCKET = 192
DEFAULT_GREEDY_PLAN_LIMIT = 8
DEFAULT_SWAP_LIMIT = 24
DEFAULT_INCOMING_PER_BUCKET = 16
DEFAULT_INCOMING_DIVERSITY_PER_BUCKET = 4
DEFAULT_BALANCE_ANCHOR_SHARES = (0.20, 0.22, 0.24, 0.26)
DEFAULT_TWO_STEP_BEAM_WIDTH = 4


@dataclass(frozen=True)
class SeedCandidate:
    weighted_score: float
    experiment: dict[str, Any]
    outcome_quality: OutcomeQuality

    @property
    def joint_key(self) -> JointKey:
        return (
            str(self.experiment["mutation"]),
            str(self.experiment["cas_system"]),
            str(self.experiment["strand"]),
        )

    @property
    def design_key(self) -> tuple[Any, ...]:
        return (
            self.experiment["cas_system"],
            self.experiment["target_alignment_start"],
            self.experiment["strand"],
            self.experiment["guideRNA"],
        )

    @property
    def stable_key(self) -> tuple[Any, ...]:
        return (
            -self.weighted_score,
            self.experiment["mutation"],
            self.experiment["cas_system"],
            self.experiment["strand"],
            self.experiment["target_alignment_start"],
            self.experiment["guideRNA"],
            self.experiment["experiment_id"],
        )


def build_optimizer_pool(
    candidates: Sequence[SeedCandidate],
    *,
    pool_limit: int = DEFAULT_POOL_PER_BUCKET,
    score_leader_count: int = DEFAULT_SCORE_LEADERS_PER_BUCKET,
) -> tuple[list[SeedCandidate], dict[str, int]]:
    """Return a bounded stable union of score and structural-diversity leaders."""
    if pool_limit <= 0:
        return [], {"score_leaders": 0, "diversity_leaders": 0, "unique_starts": 0}

    ordered = sorted(candidates, key=lambda item: item.stable_key)
    score_limit = min(pool_limit, max(0, score_leader_count))
    selected = list(ordered[:score_limit])
    selected_designs = {candidate.design_key for candidate in selected}
    selected_starts = {
        candidate.experiment["target_alignment_start"] for candidate in selected
    }
    kmer_counts: Counter[str] = Counter()
    for candidate in selected:
        kmer_counts.update(_guide_kmers(str(candidate.experiment["guideRNA"])))

    ranked_diversity: list[tuple[tuple[Any, ...], SeedCandidate]] = []
    for candidate in ordered[score_limit:]:
        if candidate.design_key in selected_designs:
            continue
        guide = str(candidate.experiment["guideRNA"])
        kmers = set(_guide_kmers(guide))
        unseen = sum(kmer_counts[kmer] == 0 for kmer in kmers)
        rare_mass = sum(1.0 / (1.0 + kmer_counts[kmer]) for kmer in kmers)
        new_start = int(
            candidate.experiment["target_alignment_start"] not in selected_starts
        )
        rank = (
            -new_start,
            -unseen,
            -rare_mass,
            -candidate.weighted_score,
            candidate.stable_key,
        )
        ranked_diversity.append((rank, candidate))

    diversity_added = 0
    for _, candidate in sorted(ranked_diversity, key=lambda item: item[0]):
        if len(selected) >= pool_limit:
            break
        if candidate.design_key in selected_designs:
            continue
        selected.append(candidate)
        selected_designs.add(candidate.design_key)
        selected_starts.add(candidate.experiment["target_alignment_start"])
        kmer_counts.update(_guide_kmers(str(candidate.experiment["guideRNA"])))
        diversity_added += 1

    return selected, {
        "score_leaders": min(score_limit, len(selected)),
        "diversity_leaders": diversity_added,
        "unique_starts": len(selected_starts),
    }


def _mixed_incoming_frontier(
    pools: Mapping[JointKey, Sequence[SeedCandidate]],
    selection: Sequence[SeedCandidate],
    *,
    limit_per_bucket: int,
    diversity_per_bucket: int,
) -> list[SeedCandidate]:
    """Keep score leaders and structurally useful rows visible to local search.

    Optimizer pools are stored with score leaders first and diversity leaders
    after them. Taking only a prefix therefore discards the diversity partition
    during swap search. This bounded frontier reserves a few slots per bucket
    for candidates with a new alignment start and unseen or rare guide k-mers,
    measured against the current selection.
    """
    if limit_per_bucket <= 0:
        return []

    selected_designs = {candidate.design_key for candidate in selection}
    selected_starts = Counter(
        candidate.experiment["target_alignment_start"] for candidate in selection
    )
    selected_kmers: Counter[str] = Counter()
    for candidate in selection:
        selected_kmers.update(_guide_kmer_counter(str(candidate.experiment["guideRNA"])))

    diversity_limit = min(max(0, diversity_per_bucket), limit_per_bucket)
    score_limit = limit_per_bucket - diversity_limit
    frontier: list[SeedCandidate] = []
    for key in sorted(pools):
        available: list[SeedCandidate] = []
        seen_designs: set[tuple[Any, ...]] = set()
        for candidate in pools[key]:
            if (
                candidate.design_key in selected_designs
                or candidate.design_key in seen_designs
            ):
                continue
            available.append(candidate)
            seen_designs.add(candidate.design_key)

        bucket_frontier = list(available[:score_limit])
        frontier_designs = {candidate.design_key for candidate in bucket_frontier}
        if diversity_limit:
            ranked_diversity: list[tuple[tuple[Any, ...], SeedCandidate]] = []
            for candidate in available[score_limit:]:
                guide = str(candidate.experiment["guideRNA"])
                kmers = set(_guide_kmer_counter(guide))
                unseen = sum(selected_kmers[kmer] == 0 for kmer in kmers)
                rare_mass = sum(1.0 / (1.0 + selected_kmers[kmer]) for kmer in kmers)
                new_start = int(
                    selected_starts[candidate.experiment["target_alignment_start"]] == 0
                )
                rank = (
                    -new_start,
                    -unseen,
                    -rare_mass,
                    -candidate.weighted_score,
                    candidate.stable_key,
                )
                ranked_diversity.append((rank, candidate))

            for _, candidate in sorted(ranked_diversity, key=lambda item: item[0]):
                if len(bucket_frontier) >= limit_per_bucket:
                    break
                if candidate.design_key in frontier_designs:
                    continue
                bucket_frontier.append(candidate)
                frontier_designs.add(candidate.design_key)

        # Small or heavily de-duplicated buckets may not fill the diversity
        # reservation. Backfill by stable pool order without increasing bounds.
        if len(bucket_frontier) < limit_per_bucket:
            for candidate in available:
                if candidate.design_key in frontier_designs:
                    continue
                bucket_frontier.append(candidate)
                frontier_designs.add(candidate.design_key)
                if len(bucket_frontier) >= limit_per_bucket:
                    break
        frontier.extend(bucket_frontier)
    return frontier

def _entropy_ratio(counter: Counter[Any], full_support: Iterable[Any]) -> float:
    support = list(full_support)
    if len(support) <= 1:
        return 1.0
    total = sum(counter.get(value, 0) for value in support)
    if total <= 0:
        return 0.0
    entropy = 0.0
    for value in support:
        count = counter.get(value, 0)
        if count:
            probability = count / total
            entropy -= probability * math.log2(probability)
    maximum = math.log2(len(support))
    return entropy / maximum if maximum > 0 else 1.0


def _guide_kmers(guide: str, k: int = 12) -> tuple[str, ...]:
    if len(guide) < k:
        return ()
    return tuple(guide[index : index + k] for index in range(len(guide) - k + 1))


@lru_cache(maxsize=8192)
def _guide_kmer_counter(guide: str) -> Counter[str]:
    """Cache immutable-by-convention k-mer counts used in inner loops."""
    return Counter(_guide_kmers(guide))


def _kmer_entropy_ratio(guides: Sequence[str], k: int = 12) -> float:
    counts: Counter[str] = Counter()
    total = 0
    for guide in guides:
        kmers = _guide_kmers(guide, k)
        counts.update(kmers)
        total += len(kmers)
    if total <= 1:
        return 0.0
    entropy = 0.0
    for count in counts.values():
        probability = count / total
        entropy -= probability * math.log2(probability)
    maximum = math.log2(total)
    return entropy / maximum if maximum > 0 else 0.0


def score_selection(
    candidates: Sequence[SeedCandidate],
    *,
    active_mutations: Sequence[str],
    cas_systems: Sequence[str],
    k: int = 12,
) -> dict[str, Any]:
    """Return the exact Stage-2 x Stage-5 proxy for an all-HDR selection."""
    experiments = [candidate.experiment for candidate in candidates]
    mutation_counts = Counter(item["mutation"] for item in experiments)
    cas_counts = Counter(item["cas_system"] for item in experiments)
    strand_counts = Counter(item["strand"] for item in experiments)
    joint_counts = Counter(
        (item["mutation"], item["cas_system"], item["strand"])
        for item in experiments
    )
    guides = [str(item["guideRNA"]) for item in experiments]
    support = [
        (mutation, cas, strand)
        for mutation in active_mutations
        for cas in cas_systems
        for strand in ("+", "-")
    ]
    components = {
        "mutation_coverage_entropy_ratio": _entropy_ratio(
            mutation_counts, active_mutations
        ),
        "cas_system_coverage_entropy_ratio": _entropy_ratio(cas_counts, cas_systems),
        "strand_coverage_entropy_ratio": _entropy_ratio(strand_counts, ("+", "-")),
        "joint_coverage_entropy_ratio": _entropy_ratio(joint_counts, support),
        "kmer_diversity_entropy_ratio": _kmer_entropy_ratio(guides, k=k),
        "distinct_guide_ratio": len(set(guides)) / len(guides) if guides else 0.0,
    }
    if components:
        fidelity = math.exp(
            sum(math.log(max(value, 1e-9)) for value in components.values())
            / len(components)
        )
    else:
        fidelity = 0.0
    weighted_total = sum(candidate.weighted_score for candidate in candidates)
    return {
        "row_count": len(candidates),
        "total_weighted_score": weighted_total,
        "distribution_fidelity": fidelity,
        "proxy_score": weighted_total * fidelity,
        "components": components,
        "counts": {
            "mutations": dict(mutation_counts),
            "cas_systems": dict(cas_counts),
            "strands": dict(strand_counts),
            "joint": {"|".join(key): value for key, value in joint_counts.items()},
        },
    }


def quota_plan(
    *,
    target_count: int,
    active_mutations: Sequence[str],
    cas_systems: Sequence[str],
    mutation_weights: Mapping[str, float],
    minority_share: float,
    primary_cas_share: float,
) -> dict[JointKey, int]:
    """Build the same joint quota shape as the legacy selector, parametrically."""
    if not active_mutations or not cas_systems:
        return {}
    best_mutation = max(
        active_mutations,
        key=lambda mutation: float(mutation_weights.get(mutation, 1.0)),
    )
    if len(active_mutations) == 1:
        mutation_shares = {best_mutation: 1.0}
    else:
        floor = minority_share / (len(active_mutations) - 1)
        mutation_shares = {mutation: floor for mutation in active_mutations}
        mutation_shares[best_mutation] = 1.0 - minority_share

    if len(cas_systems) == 2 and {"Cas9", "Cas12a"}.issubset(cas_systems):
        cas_shares = {
            mutation: (
                {"Cas9": primary_cas_share, "Cas12a": 1.0 - primary_cas_share}
                if mutation == best_mutation
                else {"Cas9": 1.0 - primary_cas_share, "Cas12a": primary_cas_share}
            )
            for mutation in active_mutations
        }
    else:
        cas_shares = {
            mutation: {
                cas: 1.0 / len(cas_systems)
                for cas in cas_systems
            }
            for mutation in active_mutations
        }

    keys = [
        (mutation, cas, strand)
        for mutation in active_mutations
        for cas in cas_systems
        for strand in ("+", "-")
    ]
    exact = {
        key: target_count
        * mutation_shares[key[0]]
        * cas_shares[key[0]][key[1]]
        / 2
        for key in keys
    }
    quotas = {key: int(value) for key, value in exact.items()}
    remainder = target_count - sum(quotas.values())
    for key in sorted(keys, key=lambda item: (-(exact[item] - quotas[item]), item))[
        :remainder
    ]:
        quotas[key] += 1
    return quotas


def _top_score_selection(
    pools: Mapping[JointKey, Sequence[SeedCandidate]],
    quotas: Mapping[JointKey, int],
) -> list[SeedCandidate] | None:
    selected: list[SeedCandidate] = []
    designs: set[tuple[Any, ...]] = set()
    for key in sorted(quotas):
        needed = quotas[key]
        for candidate in pools.get(key, ()):
            if candidate.design_key in designs:
                continue
            designs.add(candidate.design_key)
            selected.append(candidate)
            needed -= 1
            if needed == 0:
                break
        if needed:
            return None
    return selected


def _projected_intrinsic_utility(
    candidate: SeedCandidate,
    *,
    weighted_total: float,
    row_count: int,
    guide_counts: Counter[str],
    kmer_counts: Counter[str],
    kmer_total: int,
    kmer_mass: float,
) -> float:
    guide = str(candidate.experiment["guideRNA"])
    new_guide_count = len(guide_counts) + int(guide_counts[guide] == 0)
    guide_ratio = new_guide_count / (row_count + 1)

    additions = _guide_kmer_counter(guide)
    new_total = kmer_total + sum(additions.values())
    new_mass = kmer_mass
    for kmer, increment in additions.items():
        old = kmer_counts[kmer]
        if old > 0:
            new_mass -= old * math.log2(old)
        updated = old + increment
        new_mass += updated * math.log2(updated)
    if new_total <= 1:
        kmer_ratio = 0.0
    else:
        entropy = math.log2(new_total) - new_mass / new_total
        kmer_ratio = entropy / math.log2(new_total)

    # Joint-count components are fixed by the quota plan. Only Stage 2, guide
    # uniqueness, and k-mer entropy differ between candidates for this slot.
    return math.log(max(weighted_total + candidate.weighted_score, 1e-12)) + (
        math.log(max(guide_ratio, 1e-12))
        + math.log(max(kmer_ratio, 1e-12))
    ) / 6.0


def _diversity_greedy_selection(
    pools: Mapping[JointKey, Sequence[SeedCandidate]],
    quotas: Mapping[JointKey, int],
) -> list[SeedCandidate] | None:
    selected: list[SeedCandidate] = []
    designs: set[tuple[Any, ...]] = set()
    guide_counts: Counter[str] = Counter()
    kmer_counts: Counter[str] = Counter()
    kmer_total = 0
    kmer_mass = 0.0
    weighted_total = 0.0
    remaining = dict(quotas)
    keys = sorted(quotas)

    # Round-robin slots keep the partial objective from being dominated by the
    # largest joint bucket during the first part of the greedy construction.
    while any(value > 0 for value in remaining.values()):
        progressed = False
        for key in keys:
            if remaining[key] <= 0:
                continue
            best: SeedCandidate | None = None
            best_rank: tuple[Any, ...] | None = None
            for candidate in pools.get(key, ()):
                if candidate.design_key in designs:
                    continue
                utility = _projected_intrinsic_utility(
                    candidate,
                    weighted_total=weighted_total,
                    row_count=len(selected),
                    guide_counts=guide_counts,
                    kmer_counts=kmer_counts,
                    kmer_total=kmer_total,
                    kmer_mass=kmer_mass,
                )
                rank = (utility, candidate.weighted_score, tuple(candidate.stable_key))
                if best_rank is None or rank > best_rank:
                    best = candidate
                    best_rank = rank
            if best is None:
                return None

            guide = str(best.experiment["guideRNA"])
            additions = _guide_kmer_counter(guide)
            for kmer, increment in additions.items():
                old = kmer_counts[kmer]
                if old > 0:
                    kmer_mass -= old * math.log2(old)
                updated = old + increment
                kmer_counts[kmer] = updated
                kmer_mass += updated * math.log2(updated)
            kmer_total += sum(additions.values())
            guide_counts[guide] += 1
            weighted_total += best.weighted_score
            designs.add(best.design_key)
            selected.append(best)
            remaining[key] -= 1
            progressed = True
        if not progressed:
            return None
    return selected


def _swap_refine_selection(
    selection: Sequence[SeedCandidate],
    pools: Mapping[JointKey, Sequence[SeedCandidate]],
    *,
    active_mutations: Sequence[str],
    cas_systems: Sequence[str],
    swap_limit: int = DEFAULT_SWAP_LIMIT,
    incoming_per_bucket: int = DEFAULT_INCOMING_PER_BUCKET,
    incoming_diversity_per_bucket: int = 0,
    deadline_monotonic: float | None = None,
) -> tuple[list[SeedCandidate], int, int]:
    """Hill-climb the exact objective, including moves between joint buckets.

    Quota-grid plans are intentionally cheap and structured. This refinement
    removes that structure one row at a time, so strand and joint counts can
    become asymmetric when the Stage-2 gain outweighs the exact Stage-5 cost.
    The incoming frontier is bounded per bucket to keep the live deadline hard.
    """
    selected = list(selection)
    designs = {candidate.design_key for candidate in selected}
    guide_counts = Counter(str(item.experiment["guideRNA"]) for item in selected)
    mutation_counts = Counter(item.experiment["mutation"] for item in selected)
    cas_counts = Counter(item.experiment["cas_system"] for item in selected)
    strand_counts = Counter(item.experiment["strand"] for item in selected)
    joint_counts = Counter(item.joint_key for item in selected)
    joint_support = tuple(
        (mutation, cas, strand)
        for mutation in active_mutations
        for cas in cas_systems
        for strand in ("+", "-")
    )
    kmer_counts: Counter[str] = Counter()
    guide_kmer_cache: dict[str, Counter[str]] = {}

    def guide_kmers(guide: str) -> Counter[str]:
        cached = guide_kmer_cache.get(guide)
        if cached is None:
            cached = _guide_kmer_counter(guide)
            guide_kmer_cache[guide] = cached
        return cached

    for candidate in selected:
        kmer_counts.update(guide_kmers(str(candidate.experiment["guideRNA"])))
    kmer_total = sum(kmer_counts.values())
    kmer_mass = sum(
        count * math.log2(count) for count in kmer_counts.values() if count > 0
    )
    weighted_total = sum(candidate.weighted_score for candidate in selected)

    def projected_entropy(
        counts: Counter[Any],
        support: Sequence[Any],
        outgoing_value: Any,
        incoming_value: Any,
    ) -> float:
        if outgoing_value == incoming_value:
            return _entropy_ratio(counts, support)
        projected = counts.copy()
        projected[outgoing_value] -= 1
        projected[incoming_value] += 1
        return _entropy_ratio(projected, support)

    def swap_utility(
        outgoing: SeedCandidate,
        incoming: SeedCandidate,
        categorical_log_sum: float,
    ) -> float:
        new_weighted = weighted_total - outgoing.weighted_score + incoming.weighted_score
        outgoing_guide = str(outgoing.experiment["guideRNA"])
        incoming_guide = str(incoming.experiment["guideRNA"])
        unique_guides = len(guide_counts)
        if outgoing_guide != incoming_guide:
            if guide_counts[outgoing_guide] == 1:
                unique_guides -= 1
            if guide_counts[incoming_guide] == 0:
                unique_guides += 1
        guide_ratio = unique_guides / len(selected)

        outgoing_kmers = guide_kmers(outgoing_guide)
        incoming_kmers = guide_kmers(incoming_guide)
        new_total = kmer_total - sum(outgoing_kmers.values()) + sum(incoming_kmers.values())
        new_mass = kmer_mass
        for kmer in outgoing_kmers.keys() | incoming_kmers.keys():
            old = kmer_counts[kmer]
            if old > 0:
                new_mass -= old * math.log2(old)
            updated = old - outgoing_kmers[kmer] + incoming_kmers[kmer]
            if updated > 0:
                new_mass += updated * math.log2(updated)
        if new_total <= 1:
            kmer_ratio = 0.0
        else:
            entropy = math.log2(new_total) - new_mass / new_total
            kmer_ratio = entropy / math.log2(new_total)
        return math.log(max(new_weighted, 1e-12)) + (
            categorical_log_sum
            + math.log(max(kmer_ratio, 1e-12))
            + math.log(max(guide_ratio, 1e-12))
        ) / 6.0

    def current_utility() -> float:
        if kmer_total > 1:
            entropy = math.log2(kmer_total) - kmer_mass / kmer_total
            kmer_ratio = entropy / math.log2(kmer_total)
        else:
            kmer_ratio = 0.0
        components = (
            _entropy_ratio(mutation_counts, active_mutations),
            _entropy_ratio(cas_counts, cas_systems),
            _entropy_ratio(strand_counts, ("+", "-")),
            _entropy_ratio(joint_counts, joint_support),
            kmer_ratio,
            len(guide_counts) / len(selected),
        )
        return math.log(max(weighted_total, 1e-12)) + sum(
            math.log(max(value, 1e-12)) for value in components
        ) / 6.0

    swaps = 0
    cross_bucket_swaps = 0
    for _ in range(max(0, swap_limit)):
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            break
        baseline_utility = current_utility()

        best_swap: tuple[int, SeedCandidate] | None = None
        best_utility = baseline_utility
        incoming_frontier = _mixed_incoming_frontier(
            pools,
            selected,
            limit_per_bucket=max(0, incoming_per_bucket),
            diversity_per_bucket=min(
                max(0, incoming_diversity_per_bucket),
                max(0, incoming_per_bucket),
            ),
        )
        categorical_log_by_move: dict[tuple[JointKey, JointKey], float] = {}
        selected_joint_keys = sorted(set(item.joint_key for item in selected))
        incoming_joint_keys = sorted(set(item.joint_key for item in incoming_frontier))
        for outgoing_key in selected_joint_keys:
            for incoming_key in incoming_joint_keys:
                categorical = (
                    projected_entropy(
                        mutation_counts,
                        active_mutations,
                        outgoing_key[0],
                        incoming_key[0],
                    ),
                    projected_entropy(
                        cas_counts,
                        cas_systems,
                        outgoing_key[1],
                        incoming_key[1],
                    ),
                    projected_entropy(
                        strand_counts,
                        ("+", "-"),
                        outgoing_key[2],
                        incoming_key[2],
                    ),
                    projected_entropy(
                        joint_counts,
                        joint_support,
                        outgoing_key,
                        incoming_key,
                    ),
                )
                categorical_log_by_move[(outgoing_key, incoming_key)] = sum(
                    math.log(max(value, 1e-12)) for value in categorical
                )
        for incoming in incoming_frontier:
            for index, outgoing in enumerate(selected):
                # A design cannot appear twice. Replacing the same design is
                # valid, though normally only cross-mutation pools expose it.
                if (
                    incoming.design_key in designs
                    and incoming.design_key != outgoing.design_key
                ):
                    continue
                if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                    break
                utility = swap_utility(
                    outgoing,
                    incoming,
                    categorical_log_by_move[(outgoing.joint_key, incoming.joint_key)],
                )
                if utility > best_utility + 1e-14:
                    best_utility = utility
                    best_swap = (index, incoming)
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                break
        if best_swap is None:
            break

        index, incoming = best_swap
        outgoing = selected[index]
        outgoing_guide = str(outgoing.experiment["guideRNA"])
        incoming_guide = str(incoming.experiment["guideRNA"])
        outgoing_kmers = guide_kmers(outgoing_guide)
        incoming_kmers = guide_kmers(incoming_guide)
        for kmer in outgoing_kmers.keys() | incoming_kmers.keys():
            old = kmer_counts[kmer]
            if old > 0:
                kmer_mass -= old * math.log2(old)
            updated = old - outgoing_kmers[kmer] + incoming_kmers[kmer]
            if updated > 0:
                kmer_counts[kmer] = updated
                kmer_mass += updated * math.log2(updated)
            else:
                kmer_counts.pop(kmer, None)
        kmer_total += sum(incoming_kmers.values()) - sum(outgoing_kmers.values())
        guide_counts[outgoing_guide] -= 1
        if guide_counts[outgoing_guide] == 0:
            del guide_counts[outgoing_guide]
        guide_counts[incoming_guide] += 1
        weighted_total += incoming.weighted_score - outgoing.weighted_score
        mutation_counts[outgoing.experiment["mutation"]] -= 1
        mutation_counts[incoming.experiment["mutation"]] += 1
        cas_counts[outgoing.experiment["cas_system"]] -= 1
        cas_counts[incoming.experiment["cas_system"]] += 1
        strand_counts[outgoing.experiment["strand"]] -= 1
        strand_counts[incoming.experiment["strand"]] += 1
        joint_counts[outgoing.joint_key] -= 1
        joint_counts[incoming.joint_key] += 1
        designs.remove(outgoing.design_key)
        designs.add(incoming.design_key)
        selected[index] = incoming
        swaps += 1
        if outgoing.joint_key != incoming.joint_key:
            cross_bucket_swaps += 1
    return selected, swaps, cross_bucket_swaps


def _two_step_escape_selection(
    selection: Sequence[SeedCandidate],
    pools: Mapping[JointKey, Sequence[SeedCandidate]],
    *,
    active_mutations: Sequence[str],
    cas_systems: Sequence[str],
    beam_width: int = DEFAULT_TWO_STEP_BEAM_WIDTH,
    deadline_monotonic: float | None = None,
) -> tuple[list[SeedCandidate], dict[str, Any]]:
    """Try near-neutral first moves followed by one exact improving swap."""
    original = list(selection)
    original_score = score_selection(
        original,
        active_mutations=active_mutations,
        cas_systems=cas_systems,
    )
    if beam_width <= 0:
        return original, {"evaluated_first_moves": 0, "beam_states": 0, "accepted": False}

    designs = {candidate.design_key for candidate in original}
    kmer_counts: Counter[str] = Counter()
    for candidate in original:
        kmer_counts.update(_guide_kmers(str(candidate.experiment["guideRNA"])))

    by_bucket: dict[JointKey, list[tuple[int, SeedCandidate]]] = {}
    for index, candidate in enumerate(original):
        by_bucket.setdefault(candidate.joint_key, []).append((index, candidate))

    outgoing_frontier: list[tuple[int, SeedCandidate]] = []
    for key in sorted(by_bucket):
        values = by_bucket[key]
        weakest = sorted(values, key=lambda item: (item[1].weighted_score, item[1].stable_key))[:2]
        redundant = sorted(
            values,
            key=lambda item: (
                -sum(
                    max(0, kmer_counts[kmer] - 1)
                    for kmer in _guide_kmers(str(item[1].experiment["guideRNA"]))
                ),
                item[1].weighted_score,
                item[1].stable_key,
            ),
        )[:2]
        seen_indexes: set[int] = set()
        for item in (*weakest, *redundant):
            if item[0] not in seen_indexes:
                outgoing_frontier.append(item)
                seen_indexes.add(item[0])

    incoming_frontier = _mixed_incoming_frontier(
        pools,
        original,
        limit_per_bucket=4,
        diversity_per_bucket=1,
    )

    first_states: list[tuple[float, tuple[Any, ...], list[SeedCandidate]]] = []
    evaluated = 0
    maximum_first_loss = max(0.25, float(original_score["proxy_score"]) * 0.0015)
    for index, outgoing in outgoing_frontier:
        for incoming in incoming_frontier:
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                break
            if incoming.design_key in designs:
                continue
            perturbed = list(original)
            perturbed[index] = incoming
            score = score_selection(
                perturbed,
                active_mutations=active_mutations,
                cas_systems=cas_systems,
            )
            evaluated += 1
            loss = float(original_score["proxy_score"]) - float(score["proxy_score"])
            if loss <= maximum_first_loss:
                stable = (outgoing.stable_key, incoming.stable_key)
                first_states.append((float(score["proxy_score"]), stable, perturbed))
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            break

    shortlisted = sorted(
        first_states,
        key=lambda item: (-item[0], item[1]),
    )[:beam_width]
    best = original
    best_score = original_score
    for _, _, perturbed in shortlisted:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            break
        candidate, _, _ = _swap_refine_selection(
            perturbed,
            pools,
            active_mutations=active_mutations,
            cas_systems=cas_systems,
            swap_limit=1,
            incoming_per_bucket=8,
            incoming_diversity_per_bucket=2,
            deadline_monotonic=deadline_monotonic,
        )
        score = score_selection(
            candidate,
            active_mutations=active_mutations,
            cas_systems=cas_systems,
        )
        if score["proxy_score"] > best_score["proxy_score"] + 1e-12:
            best = candidate
            best_score = score

    return best, {
        "evaluated_first_moves": evaluated,
        "beam_states": len(shortlisted),
        "accepted": best is not original,
        "proxy_improvement": (
            float(best_score["proxy_score"]) - float(original_score["proxy_score"])
        ),
    }


def optimize_all_hdr_selection(
    *,
    pools: Mapping[JointKey, Sequence[SeedCandidate]],
    legacy_selection: Sequence[SeedCandidate],
    target_count: int,
    active_mutations: Sequence[str],
    cas_systems: Sequence[str],
    mutation_weights: Mapping[str, float],
    minority_shares: Sequence[float] = DEFAULT_MINORITY_SHARES,
    primary_cas_shares: Sequence[float] = DEFAULT_PRIMARY_CAS_SHARES,
    greedy_plan_limit: int = DEFAULT_GREEDY_PLAN_LIMIT,
    deadline_monotonic: float | None = None,
) -> tuple[list[SeedCandidate], dict[str, Any]]:
    """Search deterministic quota plans and keep only a non-regressing result."""
    search_started = time.monotonic()
    legacy = list(legacy_selection)
    legacy_score = score_selection(
        legacy, active_mutations=active_mutations, cas_systems=cas_systems
    )
    best = legacy
    best_score = legacy_score
    winning_plan: dict[str, Any] | None = None
    evaluated = 0
    plan_records: list[
        tuple[float, float, float, dict[JointKey, int], list[SeedCandidate], dict[str, Any]]
    ] = []

    for minority_share in minority_shares:
        for primary_cas_share in primary_cas_shares:
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                break
            quotas = quota_plan(
                target_count=target_count,
                active_mutations=active_mutations,
                cas_systems=cas_systems,
                mutation_weights=mutation_weights,
                minority_share=float(minority_share),
                primary_cas_share=float(primary_cas_share),
            )
            candidate_selection = _top_score_selection(pools, quotas)
            if candidate_selection is None or len(candidate_selection) != target_count:
                continue
            evaluated += 1
            score = score_selection(
                candidate_selection,
                active_mutations=active_mutations,
                cas_systems=cas_systems,
            )
            plan_records.append(
                (
                    float(score["proxy_score"]),
                    float(minority_share),
                    float(primary_cas_share),
                    quotas,
                    candidate_selection,
                    score,
                )
            )
            if score["proxy_score"] > best_score["proxy_score"] + 1e-12:
                best = candidate_selection
                best_score = score
                winning_plan = {
                    "minority_share": float(minority_share),
                    "primary_cas_share": float(primary_cas_share),
                    "mode": "top-score",
                    "quotas": {"|".join(key): value for key, value in quotas.items()},
                }
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            break

    # Exact quota scoring is cheap. Diversity-aware greedy construction is the
    # expensive part, so run it only for the strongest quota plans instead of
    # repeating nearly identical work across the entire grid.
    shortlisted = sorted(
        plan_records,
        key=lambda item: (item[0], -item[1], -item[2]),
        reverse=True,
    )[: max(0, greedy_plan_limit)]

    # A top-score construction can have artificially poor k-mer diversity
    # because it repeatedly uses variants from the same anchors. Its observed
    # proxy is therefore not a safe prefilter for the diversity-aware builder.
    # Add plans ranked by an optimistic bound that sets only the recoverable
    # k-mer and distinct-guide components to one. This preserves balanced
    # mutation/Cas/strand plans that otherwise disappear from the top-N list.
    def optimistic_proxy(record: tuple[Any, ...]) -> float:
        score = record[5]
        components = score["components"]
        count_components = (
            components["mutation_coverage_entropy_ratio"],
            components["cas_system_coverage_entropy_ratio"],
            components["strand_coverage_entropy_ratio"],
            components["joint_coverage_entropy_ratio"],
            1.0,
            1.0,
        )
        optimistic_fidelity = math.exp(
            sum(math.log(max(value, 1e-9)) for value in count_components) / 6.0
        )
        return float(score["total_weighted_score"]) * optimistic_fidelity

    optimistic_shortlist = sorted(
        plan_records,
        key=lambda item: (optimistic_proxy(item), item[0], -item[1], -item[2]),
        reverse=True,
    )[: max(0, greedy_plan_limit)]
    shortlisted_by_plan = {
        (record[1], record[2]): record
        for record in (*shortlisted, *optimistic_shortlist)
    }
    ranked_shortlist = sorted(
        shortlisted_by_plan.values(),
        key=lambda item: (item[0], -item[1], -item[2]),
        reverse=True,
    )
    balance_anchors: list[tuple[Any, ...]] = []
    for target_share in DEFAULT_BALANCE_ANCHOR_SHARES:
        if not plan_records:
            break
        anchor = min(
            plan_records,
            key=lambda item: (
                abs(item[1] - target_share),
                abs(item[2] - 0.5),
                -optimistic_proxy(item),
            ),
        )
        if (anchor[1], anchor[2]) not in {
            (record[1], record[2]) for record in balance_anchors
        }:
            balance_anchors.append(anchor)
    anchor_keys = {(record[1], record[2]) for record in balance_anchors}
    # Evaluate coverage anchors first so a tight live deadline cannot recreate
    # the scalar-shortlist blind spot this branch exists to prevent.
    shortlisted = balance_anchors + [
        record
        for record in ranked_shortlist
        if (record[1], record[2]) not in anchor_keys
    ]
    greedy_records: list[dict[str, Any]] = []
    for _, minority_share, primary_cas_share, quotas, _, _ in shortlisted:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            break
        candidate_selection = _diversity_greedy_selection(pools, quotas)
        if candidate_selection is None or len(candidate_selection) != target_count:
            continue
        evaluated += 1
        score = score_selection(
            candidate_selection,
            active_mutations=active_mutations,
            cas_systems=cas_systems,
        )
        greedy_records.append(
            {
                "minority_share": minority_share,
                "primary_cas_share": primary_cas_share,
                "proxy_score": score["proxy_score"],
                "total_weighted_score": score["total_weighted_score"],
                "distribution_fidelity": score["distribution_fidelity"],
            }
        )
        if score["proxy_score"] > best_score["proxy_score"] + 1e-12:
            best = candidate_selection
            best_score = score
            winning_plan = {
                "minority_share": minority_share,
                "primary_cas_share": primary_cas_share,
                "mode": "fidelity-greedy",
                "quotas": {"|".join(key): value for key, value in quotas.items()},
            }

    refined, swap_count, cross_bucket_swap_count = _swap_refine_selection(
        best,
        pools,
        active_mutations=active_mutations,
        cas_systems=cas_systems,
        deadline_monotonic=deadline_monotonic,
    )
    refined_score = score_selection(
        refined,
        active_mutations=active_mutations,
        cas_systems=cas_systems,
    )
    if refined_score["proxy_score"] > best_score["proxy_score"] + 1e-12:
        best = refined
        best_score = refined_score
        if winning_plan is None:
            winning_plan = {"mode": "swap-refined-legacy", "quotas": {}}
        else:
            winning_plan = dict(winning_plan)
            winning_plan["mode"] = f"{winning_plan['mode']}+swap-refined"
    else:
        swap_count = 0
        cross_bucket_swap_count = 0

    diversity_polish = {
        "attempted": False,
        "accepted": False,
        "swap_count": 0,
        "cross_bucket_swap_count": 0,
        "proxy_improvement": 0.0,
    }
    if deadline_monotonic is None or time.monotonic() < deadline_monotonic:
        diversity_polish["attempted"] = True
        diversity_refined, diversity_swaps, diversity_cross_bucket = (
            _swap_refine_selection(
                best,
                pools,
                active_mutations=active_mutations,
                cas_systems=cas_systems,
                swap_limit=4,
                incoming_per_bucket=DEFAULT_INCOMING_DIVERSITY_PER_BUCKET,
                incoming_diversity_per_bucket=DEFAULT_INCOMING_DIVERSITY_PER_BUCKET,
                deadline_monotonic=deadline_monotonic,
            )
        )
        diversity_score = score_selection(
            diversity_refined,
            active_mutations=active_mutations,
            cas_systems=cas_systems,
        )
        diversity_improvement = (
            float(diversity_score["proxy_score"]) - float(best_score["proxy_score"])
        )
        diversity_polish.update(
            {
                "accepted": diversity_improvement > 1e-12,
                "swap_count": diversity_swaps,
                "cross_bucket_swap_count": diversity_cross_bucket,
                "proxy_improvement": diversity_improvement,
            }
        )
        if diversity_improvement > 1e-12:
            best = diversity_refined
            best_score = diversity_score
            if winning_plan is None:
                winning_plan = {"mode": "diversity-polished-legacy", "quotas": {}}
            else:
                winning_plan = dict(winning_plan)
                winning_plan["mode"] = f"{winning_plan['mode']}+diversity-polished"

    two_step_diagnostics = {
        "evaluated_first_moves": 0,
        "beam_states": 0,
        "accepted": False,
        "proxy_improvement": 0.0,
    }
    if (
        swap_count < DEFAULT_SWAP_LIMIT
        and (deadline_monotonic is None or time.monotonic() < deadline_monotonic)
    ):
        escaped, two_step_diagnostics = _two_step_escape_selection(
            best,
            pools,
            active_mutations=active_mutations,
            cas_systems=cas_systems,
            deadline_monotonic=deadline_monotonic,
        )
        escaped_score = score_selection(
            escaped,
            active_mutations=active_mutations,
            cas_systems=cas_systems,
        )
        if escaped_score["proxy_score"] > best_score["proxy_score"] + 1e-12:
            best = escaped
            best_score = escaped_score
            if winning_plan is None:
                winning_plan = {"mode": "two-step-legacy", "quotas": {}}
            else:
                winning_plan = dict(winning_plan)
                winning_plan["mode"] = f"{winning_plan['mode']}+two-step"

    accepted = best is not legacy and best_score["proxy_score"] >= legacy_score["proxy_score"]
    diagnostics = {
        "objective_version": "stage2-x-stage5-v1",
        "evaluated_selection_count": evaluated,
        "deadline_reached": (
            deadline_monotonic is not None
            and time.monotonic() >= deadline_monotonic
        ),
        "accepted": accepted,
        "winning_plan": winning_plan,
        "swap_count": swap_count,
        "cross_bucket_swap_count": cross_bucket_swap_count,
        "diversity_polish": diversity_polish,
        "two_step": two_step_diagnostics,
        "top_quota_plans": [
            {
                "minority_share": minority_share,
                "primary_cas_share": primary_cas_share,
                "proxy_score": score["proxy_score"],
                "total_weighted_score": score["total_weighted_score"],
                "distribution_fidelity": score["distribution_fidelity"],
            }
            for _, minority_share, primary_cas_share, _, _, score in sorted(
                plan_records,
                key=lambda item: (item[0], -item[1], -item[2]),
                reverse=True,
            )[:10]
        ],
        "top_greedy_plans": sorted(
            greedy_records,
            key=lambda item: (
                item["proxy_score"],
                -item["minority_share"],
                -item["primary_cas_share"],
            ),
            reverse=True,
        )[:10],
        "balance_anchor_plans": [
            item
            for item in greedy_records
            if any(
                abs(item["minority_share"] - share) < 1e-12
                for share in DEFAULT_BALANCE_ANCHOR_SHARES
            )
            and abs(item["primary_cas_share"] - 0.5) < 1e-12
        ],
        "legacy": legacy_score,
        "optimized": best_score,
        "proxy_improvement": best_score["proxy_score"] - legacy_score["proxy_score"],
        "search_elapsed_seconds": time.monotonic() - search_started,
    }
    return list(best), diagnostics
