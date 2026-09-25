"""Deterministic baseline submission builder for live NIOME tasks."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import math
import random
import time
from typing import Any

from niome_subnet.genomics.validation.stage12 import (
    build_kmer_index,
    check_pam,
    gc_content,
    offtarget_uniqueness,
    reverse_complement,
    stage1,
    stage2,
)
from niome_subnet.genomics.validation.stage3 import simulate
from niome_subnet.genomics.seed_optimizer import (
    DEFAULT_POOL_PER_BUCKET,
    SeedCandidate,
    build_optimizer_pool,
    optimize_all_hdr_selection,
)


GUIDE_VARIANTS_PER_TARGET = 72
MIN_MUTATION_SHARE = 0.16
SEED_AWARE_MIN_MUTATION_SHARE = 0.24
PRIMARY_CAS_SHARE = 0.60
SEED_FOCUSED_ANCHORS_PER_BUCKET = 4
SEED_FOCUSED_VARIANTS_PER_ANCHOR = 1024
SEED_FOCUSED_BATCH_SIZE = 256
SEED_FOCUSED_MIN_BATCHES = 2
SEED_FOCUSED_RESERVOIR_RESERVE_FACTOR = 1.10
SEED_FOCUSED_NEAR_BEST_TOLERANCE = 0.01
SEED_OPTIMIZER_TIME_BUDGET_SECONDS = 20.0
EXPLORATION_POOL_PER_BUCKET = 320
EXPLORATION_SCORE_LEADERS_PER_BUCKET = 240
EXPLORATION_OPTIMIZER_TIME_BUDGET_SECONDS = 12.0
EXPLORATION_GRID_STEP_THOUSANDTHS = 5
EXPLORATION_VARIANTS_PER_ANCHOR = 384


def _guide_for(seq: str, start: int, length: int, strand: str) -> str:
    target = seq[start : start + length]
    return target if strand == "+" else reverse_complement(target)


def _guide_variants(
    guide: str,
    cas: str,
    kmer_index: dict[str, list[int]],
    *,
    max_mismatches: int,
    limit: int = GUIDE_VARIANTS_PER_TARGET,
) -> list[str]:
    """Return high-quality deterministic mismatch variants accepted by Stage 1.

    Stage 1 permits up to three guide/target mismatches. A seed-region edit usually
    removes genomic off-target hits, while one or two additional edits can bring GC
    content close to the Stage-2 optimum of 0.5. Searching only the best shallow
    branches keeps live-task generation comfortably inside the upload deadline.
    """
    if max_mismatches <= 0:
        return [guide]

    alphabet = "ACGT"
    seed_positions = (
        range(max(0, len(guide) - 12), len(guide))
        if cas == "Cas9"
        else range(0, min(12, len(guide)))
    )

    def quality(candidate: str) -> tuple[float, float, str]:
        gc_score = 1.0 - abs(gc_content(candidate) - 0.5) * 2
        uniqueness = offtarget_uniqueness(candidate, cas, kmer_index)
        return uniqueness, gc_score, candidate

    candidates = {guide}
    frontier: list[str] = []
    for position in seed_positions:
        for base in alphabet:
            if base == guide[position]:
                continue
            variant = guide[:position] + base + guide[position + 1 :]
            candidates.add(variant)
            frontier.append(variant)
    frontier = sorted(set(frontier), key=quality, reverse=True)[:8]

    for _ in range(1, max(1, min(max_mismatches, 3))):
        expanded: set[str] = set()
        for current in frontier:
            for position, original in enumerate(guide):
                if current[position] != original:
                    continue
                for base in alphabet:
                    if base == original:
                        continue
                    expanded.add(current[:position] + base + current[position + 1 :])
        if not expanded:
            break
        ranked = sorted(expanded, key=quality, reverse=True)
        candidates.update(ranked[:24])
        frontier = ranked[:8]

    return sorted(candidates, key=quality, reverse=True)[:limit]


def _focused_guide_variants(
    guide: str,
    cas: str,
    kmer_index: dict[str, list[int]],
    *,
    max_mismatches: int,
    salt: str,
    limit: int = SEED_FOCUSED_VARIANTS_PER_ANCHOR,
) -> list[str]:
    """Expand a bounded, deterministic variant reservoir for top PAM anchors.

    The ordinary frontier is deliberately shallow because it is applied to
    every PAM site. After round seeds are known we can afford a wider reservoir
    only at the structurally strongest anchors. Sampling mismatch combinations
    avoids the lexicographic bias of a fully sorted combinatorial expansion.
    """
    if max_mismatches <= 0 or limit <= 1:
        return [guide]

    candidates = set(
        _guide_variants(
            guide,
            cas,
            kmer_index,
            max_mismatches=max_mismatches,
            limit=limit,
        )
    )
    seed_positions = list(
        range(max(0, len(guide) - 12), len(guide))
        if cas == "Cas9"
        else range(0, min(12, len(guide)))
    )
    all_positions = list(range(len(guide)))
    alphabet = "ACGT"
    rng_seed = int.from_bytes(hashlib.sha256(salt.encode()).digest()[:8], "big")
    rng = random.Random(rng_seed)
    attempts = 0
    max_attempts = limit * 30
    mismatch_limit = max(1, min(int(max_mismatches), 3))

    while len(candidates) < limit and attempts < max_attempts:
        attempts += 1
        mismatch_count = rng.randint(1, mismatch_limit)
        positions = rng.sample(all_positions, mismatch_count)
        # A seed-region change normally produces the best off-target factor.
        if seed_positions and not any(position in seed_positions for position in positions):
            positions[-1] = rng.choice(seed_positions)
            positions = list(dict.fromkeys(positions))
            while len(positions) < mismatch_count:
                candidate_position = rng.choice(all_positions)
                if candidate_position not in positions:
                    positions.append(candidate_position)

        variant = list(guide)
        for position in positions:
            choices = [base for base in alphabet if base != guide[position]]
            variant[position] = rng.choice(choices)
        candidates.add("".join(variant))

    def quality(candidate: str) -> tuple[float, float, str]:
        gc_score = 1.0 - abs(gc_content(candidate) - 0.5) * 2
        return offtarget_uniqueness(candidate, cas, kmer_index), gc_score, candidate

    return sorted(candidates, key=quality, reverse=True)[:limit]


def _candidate_window(
    mutation_coordinate: int,
    contract: dict[str, Any],
    sequence_length: int,
) -> range:
    padding = int(contract["rules"]["base_padding"])
    if contract["rules"].get("proximity_gate", False):
        radius = max(0, padding)
    else:
        radius = max(5_000, padding * 3)
    lower = max(0, mutation_coordinate - radius)
    upper = min(sequence_length - 24, mutation_coordinate + radius + 1)
    return range(lower, max(lower, upper))


def _profiled_share_grid(
    profile: str,
    *,
    lower_thousandths: int,
    upper_thousandths: int,
    baseline: tuple[float, ...],
) -> tuple[float, ...]:
    """Return a stable, identity-specific fine grid that includes the baseline.

    The profile changes only the additional search lattice.  Keeping every
    baseline point in the grid means an exploratory lane can always retain the
    ordinary optimizer result, while the hash-derived offset makes separate
    profiles inspect different quota boundaries without random behaviour.
    """
    normalized = profile.strip()
    if not normalized:
        return baseline
    offset = int.from_bytes(
        hashlib.sha256(normalized.encode()).digest()[:2], "big"
    ) % EXPLORATION_GRID_STEP_THOUSANDTHS
    start = lower_thousandths + offset
    profiled = {
        value / 1000
        for value in range(
            start,
            upper_thousandths + 1,
            EXPLORATION_GRID_STEP_THOUSANDTHS,
        )
    }
    profiled.update(baseline)
    return tuple(sorted(profiled))


def build_submission(
    *,
    contract: dict[str, Any],
    reference: dict[str, Any],
    chromosome_11: str,
    cell_types: dict[str, Any],
    fallback_max_experiments: int = 250,
    deadline_monotonic: float | None = None,
    selection_profile: str = "ranked",
    round_seeds: list[int] | None = None,
    exploration_profile: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Generate valid, diverse rows and rank within each coverage bucket.

    The builder uses only public task artifacts and the same Stage 1/2 functions
    as the validator. It balances mutation/Cas/strand buckets before filling any
    remaining capacity by Stage 2 weighted score.
    """
    exploration_profile = (exploration_profile or "").strip()
    exploration_enabled = bool(
        exploration_profile
        and selection_profile == "seed-aware"
        and round_seeds
    )
    exploration_profile_id = (
        hashlib.sha256(exploration_profile.encode()).hexdigest()[:12]
        if exploration_enabled
        else None
    )

    mutation_map = reference["mutation_map"]
    active_mutations = contract["active_mutations"]
    cas_systems = [
        cas
        for cas in contract["rules"].get("cas_systems", ["Cas9", "Cas12a"])
        if cas in ("Cas9", "Cas12a")
    ]
    max_experiments = contract["rules"].get("max_experiments")
    target_count = (
        fallback_max_experiments if max_experiments is None else max(0, int(max_experiments))
    )

    def deadline_expired() -> bool:
        return (
            deadline_monotonic is not None
            and time.monotonic() >= deadline_monotonic
        )

    gene_start = reference["gene_region"]["start"]
    gene_end = reference["gene_region"]["end"]
    flank_start = max(0, gene_start - 50_000)
    flank_end = min(len(chromosome_11), gene_end + 50_000)
    kmer_index = build_kmer_index(chromosome_11[flank_start:flank_end], 12)

    buckets: dict[tuple[str, str, str], list[tuple[float, dict[str, Any]]]] = defaultdict(list)
    exploration_buckets: dict[
        tuple[str, str, str], list[tuple[float, dict[str, Any]]]
    ] = defaultdict(list)
    anchors: dict[tuple[str, str, str], list[tuple[int, int, str]]] = defaultdict(list)
    scanned = 0
    deadline_reached = deadline_expired()
    for mutation in active_mutations:
        if deadline_reached:
            break
        coordinate = mutation_map[mutation]
        for cas in cas_systems:
            if deadline_reached:
                break
            for strand in ("+", "-"):
                if deadline_reached:
                    break
                for start in _candidate_window(coordinate, contract, len(chromosome_11)):
                    if deadline_expired():
                        deadline_reached = True
                        break
                    for length in (20, 23):
                        scanned += 1
                        pam_ok, _ = check_pam(
                            chromosome_11, start, length, cas, strand
                        )
                        if not pam_ok:
                            continue
                        exact_guide = _guide_for(
                            chromosome_11, start, length, strand
                        )
                        anchors[(mutation, cas, strand)].append(
                            (start, length, exact_guide)
                        )
                        for guide in _guide_variants(
                            exact_guide,
                            cas,
                            kmer_index,
                            max_mismatches=int(
                                contract["rules"].get("max_mismatches", 0)
                            ),
                        ):
                            identity = "|".join(
                                [mutation, cas, strand, str(start), str(length), guide]
                            )
                            experiment = {
                                "experiment_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                                "guideRNA": guide,
                                "target_alignment_start": start,
                                "target_alignment_end": start + length,
                                "strand": strand,
                                "mutation": mutation,
                                "cas_system": cas,
                            }
                            if contract.get("cell_type") is not None:
                                experiment["cell_type"] = contract["cell_type"]
                            stage1_score, _ = stage1(
                                experiment, chromosome_11, mutation_map, contract
                            )
                            if stage1_score != 1.0:
                                continue
                            _, stage2_info = stage2(
                                cell_types,
                                experiment,
                                chromosome_11,
                                mutation_map,
                                contract,
                                kmer_index,
                            )
                            buckets[(mutation, cas, strand)].append(
                                (stage2_info["weighted_score"], experiment)
                            )

    feature_cache: dict[str, tuple[float, float, float]] = {}
    outcome_cache: dict[str, tuple[int, int, int, int]] = {}

    def cache_seed_outcome_quality(
        experiment: dict[str, Any],
        stage2_info: dict[str, Any] | None = None,
    ) -> tuple[int, int, int, int]:
        experiment_id = experiment["experiment_id"]
        if experiment_id in outcome_cache:
            return outcome_cache[experiment_id]
        if stage2_info is None:
            _, stage2_info = stage2(
                cell_types,
                experiment,
                chromosome_11,
                mutation_map,
                contract,
                kmer_index,
            )
        wrapped = {
            "experiment": experiment,
            "features": {
                "gc": stage2_info["gc"],
                "distance_to_mutation": stage2_info["distance"],
                "gc_score": stage2_info["gc_score"],
                "dist_score": stage2_info["dist_score"],
                "consistency": stage2_info["consistency"],
                "offtarget_factor": stage2_info["offtarget_factor"],
                "mutation_weight": stage2_info["mutation_weight"],
                "cell_type": stage2_info["cell_type"],
                "cell_type_accessibility": stage2_info["cell_type_accessibility"],
                "mutation_region": stage2_info["mutation_region"],
                "region_energy_offset": stage2_info["region_energy_offset"],
            },
        }
        outcomes = [simulate(wrapped, seed) for seed in (round_seeds or [])]
        hdr_count = sum(item["outcome"] == "HDR" for item in outcomes)
        cut_count = sum(item["outcome"] != "no_cut" for item in outcomes)
        total_indel = sum(int(item["indel_length"]) for item in outcomes)
        quality = (
            int(bool(outcomes) and hdr_count == len(outcomes)),
            hdr_count,
            cut_count,
            -total_indel,
        )
        outcome_cache[experiment_id] = quality
        return quality

    focused_candidates_added = 0
    exploration_candidates_added = 0
    exploration_all_hdr_candidates_added = 0
    focused_anchor_counts: dict[str, int] = {}
    focused_adaptive: dict[str, dict[str, Any]] = {}
    if selection_profile == "seed-aware" and round_seeds and not deadline_reached:
        max_mismatches = int(contract["rules"].get("max_mismatches", 0))
        focus_best_mutation = max(
            active_mutations,
            key=lambda mutation: float(
                contract.get("mutation_weights", {}).get(mutation, 1.0)
            ),
        )
        for key, key_anchors in anchors.items():
            if deadline_expired():
                deadline_reached = True
                break
            mutation, cas, strand = key
            coordinate = mutation_map[mutation]
            strongest = sorted(
                key_anchors,
                key=lambda item: (
                    abs(item[0] - coordinate),
                    item[0],
                    item[1],
                    item[2],
                ),
            )[:SEED_FOCUSED_ANCHORS_PER_BUCKET]
            focused_anchor_counts["|".join(key)] = len(strongest)
            known_ids = {
                experiment["experiment_id"] for _, experiment in buckets[key]
            }
            variants_by_anchor: list[tuple[int, int, list[str]]] = []
            for start, length, exact_guide in strongest:
                salt = "|".join(
                    (
                        mutation,
                        cas,
                        strand,
                        str(start),
                        str(length),
                        ",".join(str(seed) for seed in round_seeds),
                    )
                )
                variants_by_anchor.append(
                    (
                        start,
                        length,
                        _focused_guide_variants(
                            exact_guide,
                            cas,
                            kmer_index,
                            max_mismatches=max_mismatches,
                            salt=salt,
                        ),
                    )
                )

            focused_all_hdr: list[tuple[float, int]] = []
            focus_mutation_share = (
                1.0 - SEED_AWARE_MIN_MUTATION_SHARE
                if mutation == focus_best_mutation
                else SEED_AWARE_MIN_MUTATION_SHARE
                / max(1, len(active_mutations) - 1)
            )
            near_best_target = max(
                1,
                math.ceil(
                    target_count
                    * focus_mutation_share
                    * PRIMARY_CAS_SHARE
                    / 2
                    * SEED_FOCUSED_RESERVOIR_RESERVE_FACTOR
                ),
            )
            bucket_added = 0
            batches_completed = 0
            stop_reason = "max_reservoir"
            near_best_count = 0
            near_best_starts = 0
            for offset in range(
                0,
                SEED_FOCUSED_VARIANTS_PER_ANCHOR,
                SEED_FOCUSED_BATCH_SIZE,
            ):
                if deadline_expired():
                    deadline_reached = True
                    stop_reason = "deadline"
                    break
                for start, length, variants in variants_by_anchor:
                    for guide in variants[offset : offset + SEED_FOCUSED_BATCH_SIZE]:
                        if deadline_expired():
                            deadline_reached = True
                            stop_reason = "deadline"
                            break
                        identity = "|".join(
                            [mutation, cas, strand, str(start), str(length), guide]
                        )
                        experiment_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
                        if experiment_id in known_ids:
                            continue
                        experiment = {
                            "experiment_id": experiment_id,
                            "guideRNA": guide,
                            "target_alignment_start": start,
                            "target_alignment_end": start + length,
                            "strand": strand,
                            "mutation": mutation,
                            "cas_system": cas,
                        }
                        if contract.get("cell_type") is not None:
                            experiment["cell_type"] = contract["cell_type"]
                        stage1_score, _ = stage1(
                            experiment, chromosome_11, mutation_map, contract
                        )
                        if stage1_score != 1.0:
                            continue
                        _, stage2_info = stage2(
                            cell_types,
                            experiment,
                            chromosome_11,
                            mutation_map,
                            contract,
                            kmer_index,
                        )
                        weighted_score = float(stage2_info["weighted_score"])
                        buckets[key].append((weighted_score, experiment))
                        known_ids.add(experiment_id)
                        focused_candidates_added += 1
                        bucket_added += 1
                        if cache_seed_outcome_quality(experiment, stage2_info)[0] == 1:
                            focused_all_hdr.append((weighted_score, start))
                    if deadline_reached:
                        break
                if deadline_reached:
                    break
                batches_completed += 1
                if focused_all_hdr:
                    best_score = max(score for score, _ in focused_all_hdr)
                    near_best = [
                        (score, start)
                        for score, start in focused_all_hdr
                        if score >= best_score - SEED_FOCUSED_NEAR_BEST_TOLERANCE
                    ]
                    near_best_count = len(near_best)
                    near_best_starts = len({start for _, start in near_best})
                    if (
                        batches_completed >= SEED_FOCUSED_MIN_BATCHES
                        and near_best_count >= near_best_target
                    ):
                        stop_reason = "sufficient_near_best_all_hdr"
                        break
            focused_adaptive["|".join(key)] = {
                "candidates_added": bucket_added,
                "batches_completed": batches_completed,
                "all_hdr_count": len(focused_all_hdr),
                "near_best_all_hdr_count": near_best_count,
                "near_best_target": near_best_target,
                "near_best_start_count": near_best_starts,
                "stop_reason": stop_reason,
            }
            if exploration_enabled and not deadline_reached:
                exploration_known_ids = set(known_ids)
                for start, length, exact_guide in strongest:
                    profile_salt = "|".join(
                        (
                            mutation,
                            cas,
                            strand,
                            str(start),
                            str(length),
                            ",".join(str(seed) for seed in round_seeds),
                            exploration_profile,
                        )
                    )
                    profile_variants = _focused_guide_variants(
                        exact_guide,
                        cas,
                        kmer_index,
                        max_mismatches=max_mismatches,
                        salt=profile_salt,
                        limit=EXPLORATION_VARIANTS_PER_ANCHOR,
                    )
                    for guide in profile_variants:
                        if deadline_expired():
                            deadline_reached = True
                            break
                        identity = "|".join(
                            [mutation, cas, strand, str(start), str(length), guide]
                        )
                        experiment_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
                        if experiment_id in exploration_known_ids:
                            continue
                        experiment = {
                            "experiment_id": experiment_id,
                            "guideRNA": guide,
                            "target_alignment_start": start,
                            "target_alignment_end": start + length,
                            "strand": strand,
                            "mutation": mutation,
                            "cas_system": cas,
                        }
                        if contract.get("cell_type") is not None:
                            experiment["cell_type"] = contract["cell_type"]
                        stage1_score, _ = stage1(
                            experiment, chromosome_11, mutation_map, contract
                        )
                        if stage1_score != 1.0:
                            continue
                        _, stage2_info = stage2(
                            cell_types,
                            experiment,
                            chromosome_11,
                            mutation_map,
                            contract,
                            kmer_index,
                        )
                        exploration_known_ids.add(experiment_id)
                        exploration_candidates_added += 1
                        if cache_seed_outcome_quality(experiment, stage2_info)[0] != 1:
                            continue
                        exploration_all_hdr_candidates_added += 1
                        exploration_buckets[key].append(
                            (float(stage2_info["weighted_score"]), experiment)
                        )
                    if deadline_reached:
                        break

    def rank_key(item: tuple[float, dict[str, Any]]) -> tuple[Any, ...]:
        score, experiment = item
        return (
            -score,
            experiment["mutation"],
            experiment["cas_system"],
            experiment["strand"],
            experiment["target_alignment_start"],
            experiment["guideRNA"],
        )

    # Preserve enough representation from every mutation/Cas/strand joint bucket
    # for Stage 5, but assign most capacity to the highest mutation weight. With
    # two mutations this is an 84/16 split, matching the high-scoring frontier's
    # Stage-2/fidelity trade-off while keeping all eight joint buckets populated.
    mutation_weights = contract.get("mutation_weights", {})
    mutation_shares: dict[str, float] = {}
    if len(active_mutations) <= 1:
        mutation_shares = {mutation: 1.0 for mutation in active_mutations}
    else:
        minority_share = (
            SEED_AWARE_MIN_MUTATION_SHARE
            if selection_profile == "seed-aware" and round_seeds
            else MIN_MUTATION_SHARE
        )
        floor = minority_share / (len(active_mutations) - 1)
        best_mutation = max(
            active_mutations,
            key=lambda mutation: float(mutation_weights.get(mutation, 1.0)),
        )
        mutation_shares = {mutation: floor for mutation in active_mutations}
        mutation_shares[best_mutation] = 1.0 - minority_share

    joint_keys = [
        (mutation, cas, strand)
        for mutation in active_mutations
        for cas in cas_systems
        for strand in ("+", "-")
    ]
    if len(cas_systems) == 2 and "Cas9" in cas_systems and "Cas12a" in cas_systems:
        best_mutation = max(
            active_mutations,
            key=lambda mutation: float(mutation_weights.get(mutation, 1.0)),
        )
        cas_shares = {
            mutation: (
                {"Cas9": PRIMARY_CAS_SHARE, "Cas12a": 1.0 - PRIMARY_CAS_SHARE}
                if mutation == best_mutation
                else {"Cas9": 1.0 - PRIMARY_CAS_SHARE, "Cas12a": PRIMARY_CAS_SHARE}
            )
            for mutation in active_mutations
        }
    else:
        cas_shares = {
            mutation: {cas: 1.0 / max(1, len(cas_systems)) for cas in cas_systems}
            for mutation in active_mutations
        }
    exact_quotas: dict[tuple[str, str, str], float] = {
        key: target_count
        * mutation_shares.get(key[0], 0.0)
        * cas_shares[key[0]][key[1]]
        / 2
        for key in joint_keys
    }
    quotas = {key: int(value) for key, value in exact_quotas.items()}
    remainder = target_count - sum(quotas.values())
    for key in sorted(
        joint_keys,
        key=lambda item: (-(exact_quotas[item] - quotas[item]), item),
    )[:remainder]:
        quotas[key] += 1

    selected: list[dict[str, Any]] = []
    selected_designs: set[tuple[Any, ...]] = set()

    def add_ordered(values: list[tuple[float, dict[str, Any]]], limit: int) -> None:
        added = 0
        for _, experiment in values:
            design = (
                experiment["cas_system"],
                experiment["target_alignment_start"],
                experiment["strand"],
                experiment["guideRNA"],
            )
            if design in selected_designs:
                continue
            selected_designs.add(design)
            selected.append(experiment)
            added += 1
            if added >= limit or len(selected) >= target_count:
                return

    def add_ranked(values: list[tuple[float, dict[str, Any]]], limit: int) -> None:
        add_ordered(sorted(values, key=rank_key), limit)

    def selection_features(experiment: dict[str, Any]) -> tuple[float, float, float]:
        experiment_id = experiment["experiment_id"]
        if experiment_id not in feature_cache:
            _, info = stage2(
                cell_types,
                experiment,
                chromosome_11,
                mutation_map,
                contract,
                kmer_index,
            )
            energy = max(
                0.0,
                min(
                    1.0,
                    info["cell_type_accessibility"]
                    * (
                        1.8 * info["gc"]
                        + 0.6 * math.exp(-info["distance"] / 1500)
                        + info["region_energy_offset"]
                    ),
                ),
            )
            feature_cache[experiment_id] = (energy, info["gc"], info["distance"])
        return feature_cache[experiment_id]

    def seed_outcome_quality(experiment: dict[str, Any]) -> tuple[int, int, int, int]:
        """Rank a design by deterministic outcomes for already-public seeds.

        Once the seed blocks exist, guide sequence is a legitimate selectable
        design variable.  Prefer designs that are HDR in every benchmark run;
        they make all three Stage-4 targets stable instead of pseudorandom.
        """
        return cache_seed_outcome_quality(experiment)

    def add_seed_aware(values: list[tuple[float, dict[str, Any]]], limit: int) -> None:
        ranked = sorted(
            values,
            key=lambda item: (
                *(-value for value in seed_outcome_quality(item[1])),
                *rank_key(item),
            ),
        )
        add_ordered(ranked, limit)

    def add_energy_anchored(
        values: list[tuple[float, dict[str, Any]]],
        limit: int,
        anchors: tuple[float, ...],
    ) -> None:
        """Fill a quota around several energy anchors without wasting score.

        Energy drives the simulated cut and repair probabilities and is visible
        to Stage 4.  A spread of energy values is therefore substantially more
        learnable than a pile-up at energy=1.  Ranking by structural score is the
        tie-breaker, preserving as much Stage-2 value as each anchor permits.
        """
        pools = [
            sorted(
                values,
                key=lambda item: (
                    abs(selection_features(item[1])[0] - anchor),
                    *rank_key(item),
                ),
            )
            for anchor in anchors
        ]
        cursors = [0] * len(pools)
        added = 0
        while added < limit:
            progressed = False
            for pool_index, pool in enumerate(pools):
                while cursors[pool_index] < len(pool):
                    experiment = pool[cursors[pool_index]][1]
                    cursors[pool_index] += 1
                    design = (
                        experiment["cas_system"],
                        experiment["target_alignment_start"],
                        experiment["strand"],
                        experiment["guideRNA"],
                    )
                    if design in selected_designs:
                        continue
                    selected_designs.add(design)
                    selected.append(experiment)
                    added += 1
                    progressed = True
                    break
                if added >= limit:
                    return
            if not progressed:
                return

    for key in joint_keys:
        values = buckets.get(key, [])
        if selection_profile == "seed-aware" and round_seeds:
            add_seed_aware(values, quotas[key])
        elif selection_profile == "energy-spread":
            add_energy_anchored(values, quotas[key], (0.45, 0.60, 0.75, 0.90, 1.0))
        elif selection_profile == "cas-separated":
            anchors = (
                (0.72, 0.84, 0.94, 1.0)
                if key[1] == "Cas9"
                else (0.25, 0.40, 0.55, 0.68)
            )
            add_energy_anchored(values, quotas[key], anchors)
        else:
            add_ranked(values, quotas[key])

    if len(selected) < target_count:
        add_ranked(
            [item for values in buckets.values() for item in values],
            target_count - len(selected),
        )

    optimizer_diagnostics: dict[str, Any] | None = None
    all_hdr_candidate_counts: dict[str, int] = {}
    if (
        selection_profile == "seed-aware"
        and round_seeds
        and len(selected) == target_count
    ):
        optimizer_total_started = time.monotonic()
        pool_prepare_elapsed = 0.0
        search_elapsed = 0.0
        optimizer_pool_diagnostics: dict[str, dict[str, int]] = {}
        exploration_pool_diagnostics: dict[str, dict[str, int]] = {}
        exploration_pools: dict[
            tuple[str, str, str], list[SeedCandidate]
        ] = {}
        score_by_id = {
            experiment["experiment_id"]: score
            for values in buckets.values()
            for score, experiment in values
        }
        legacy_candidates = [
            SeedCandidate(
                weighted_score=score_by_id[experiment["experiment_id"]],
                experiment=experiment,
                outcome_quality=seed_outcome_quality(experiment),
            )
            for experiment in selected
        ]
        legacy_is_all_hdr = all(
            candidate.outcome_quality[0] == 1 for candidate in legacy_candidates
        )
        fallback_row_count = sum(
            candidate.outcome_quality[0] != 1 for candidate in legacy_candidates
        )
        if legacy_is_all_hdr:
            pool_prepare_started = time.monotonic()
            optimizer_pools: dict[
                tuple[str, str, str], list[SeedCandidate]
            ] = {}
            for key, values in buckets.items():
                all_hdr_values = [
                    (score, experiment)
                    for score, experiment in values
                    if seed_outcome_quality(experiment)[0] == 1
                ]
                all_hdr_values.sort(key=rank_key)
                all_hdr_candidate_counts["|".join(key)] = len(all_hdr_values)
                all_hdr_candidates = [
                    SeedCandidate(
                        weighted_score=score,
                        experiment=experiment,
                        outcome_quality=outcome_cache[experiment["experiment_id"]],
                    )
                    for score, experiment in all_hdr_values
                ]
                optimizer_pools[key], pool_diagnostics = build_optimizer_pool(
                    all_hdr_candidates,
                    pool_limit=DEFAULT_POOL_PER_BUCKET,
                )
                optimizer_pool_diagnostics["|".join(key)] = pool_diagnostics
                if exploration_enabled:
                    exploration_values = list(all_hdr_values)
                    exploration_values.extend(exploration_buckets.get(key, ()))
                    exploration_values.sort(key=rank_key)
                    exploration_candidates = [
                        SeedCandidate(
                            weighted_score=score,
                            experiment=experiment,
                            outcome_quality=outcome_cache[experiment["experiment_id"]],
                        )
                        for score, experiment in exploration_values
                    ]
                    (
                        exploration_pools[key],
                        exploration_pool_diagnostics["|".join(key)],
                    ) = build_optimizer_pool(
                        exploration_candidates,
                        pool_limit=EXPLORATION_POOL_PER_BUCKET,
                        score_leader_count=EXPLORATION_SCORE_LEADERS_PER_BUCKET,
                    )

            pool_prepare_elapsed = time.monotonic() - pool_prepare_started
            search_started = time.monotonic()
            search_deadline = search_started + SEED_OPTIMIZER_TIME_BUDGET_SECONDS
            if deadline_monotonic is not None:
                search_deadline = min(search_deadline, deadline_monotonic)

            optimized, optimizer_diagnostics = optimize_all_hdr_selection(
                pools=optimizer_pools,
                legacy_selection=legacy_candidates,
                target_count=target_count,
                active_mutations=active_mutations,
                cas_systems=cas_systems,
                mutation_weights=mutation_weights,
                deadline_monotonic=search_deadline,
            )
            if exploration_enabled:
                exploration_started = time.monotonic()
                exploration_deadline = (
                    exploration_started
                    + EXPLORATION_OPTIMIZER_TIME_BUDGET_SECONDS
                )
                if deadline_monotonic is not None:
                    exploration_deadline = min(
                        exploration_deadline,
                        deadline_monotonic,
                    )
                baseline_proxy = float(
                    optimizer_diagnostics["optimized"]["proxy_score"]
                )
                explored, exploration_diagnostics = optimize_all_hdr_selection(
                    pools=exploration_pools,
                    legacy_selection=optimized,
                    target_count=target_count,
                    active_mutations=active_mutations,
                    cas_systems=cas_systems,
                    mutation_weights=mutation_weights,
                    minority_shares=_profiled_share_grid(
                        exploration_profile,
                        lower_thousandths=140,
                        upper_thousandths=400,
                        baseline=tuple(
                            value / 100 for value in range(14, 37)
                        )
                        + (0.40,),
                    ),
                    primary_cas_shares=_profiled_share_grid(
                        exploration_profile,
                        lower_thousandths=500,
                        upper_thousandths=700,
                        baseline=tuple(
                            value / 1000 for value in range(500, 701, 25)
                        ),
                    ),
                    greedy_plan_limit=12,
                    deadline_monotonic=exploration_deadline,
                )
                explored_proxy = float(
                    exploration_diagnostics["optimized"]["proxy_score"]
                )
                exploration_accepted = (
                    explored_proxy > baseline_proxy + 1e-12
                )
                if exploration_accepted:
                    optimized = explored
                    optimizer_diagnostics["accepted"] = True
                    optimizer_diagnostics["optimized"] = exploration_diagnostics[
                        "optimized"
                    ]
                    optimizer_diagnostics["proxy_improvement"] = (
                        explored_proxy
                        - float(optimizer_diagnostics["legacy"]["proxy_score"])
                    )
                optimizer_diagnostics["exploration"] = {
                    "enabled": True,
                    "profile_id": exploration_profile_id,
                    "accepted": exploration_accepted,
                    "baseline_proxy_score": baseline_proxy,
                    "explored_proxy_score": explored_proxy,
                    "proxy_improvement_over_baseline": (
                        explored_proxy - baseline_proxy
                    ),
                    "pool_limit_per_bucket": EXPLORATION_POOL_PER_BUCKET,
                    "score_leaders_per_bucket": (
                        EXPLORATION_SCORE_LEADERS_PER_BUCKET
                    ),
                    "pool_composition": exploration_pool_diagnostics,
                    "new_valid_candidates": exploration_candidates_added,
                    "new_all_hdr_candidates": (
                        exploration_all_hdr_candidates_added
                    ),
                    "search": exploration_diagnostics,
                    "elapsed_seconds": time.monotonic() - exploration_started,
                }
            search_elapsed = time.monotonic() - search_started
            selected = [candidate.experiment for candidate in optimized]
        else:
            optimizer_diagnostics = {
                "objective_version": "stage2-x-stage5-v1",
                "accepted": False,
                "skip_reason": "legacy_selection_not_all_hdr",
            }
        optimizer_diagnostics["pool_prepare_elapsed_seconds"] = pool_prepare_elapsed
        optimizer_diagnostics["search_elapsed_seconds"] = search_elapsed
        optimizer_diagnostics["total_elapsed_seconds"] = (
            time.monotonic() - optimizer_total_started
        )
        # Backward-compatible alias for existing dashboard consumers.
        optimizer_diagnostics["elapsed_seconds"] = optimizer_diagnostics[
            "total_elapsed_seconds"
        ]
        optimizer_diagnostics["pool_composition"] = optimizer_pool_diagnostics
        optimizer_diagnostics["all_hdr_candidate_counts"] = (
            all_hdr_candidate_counts
        )
        optimizer_diagnostics["fallback_row_count"] = fallback_row_count

    diagnostics = {
        "target_count": target_count,
        "selected_count": len(selected),
        "scanned_candidates": scanned,
        "deadline_reached": deadline_reached,
        "selection_strategy": selection_profile,
        "exploration": {
            "enabled": exploration_enabled,
            "profile_id": exploration_profile_id,
        },
        "round_seeds": list(round_seeds or []),
        "focused_candidates_added": focused_candidates_added,
        "exploration_candidates_added": exploration_candidates_added,
        "exploration_all_hdr_candidates_added": (
            exploration_all_hdr_candidates_added
        ),
        "focused_anchor_counts": focused_anchor_counts,
        "focused_adaptive": focused_adaptive,
        "joint_bucket_quotas": {
            "|".join(key): value for key, value in quotas.items()
        },
        "bucket_candidate_counts": {
            "|".join(key): len(values) for key, values in buckets.items()
        },
        "bucket_selected_counts": dict(
            sorted(
                {
                    "|".join(key): sum(
                        row["mutation"] == key[0]
                        and row["cas_system"] == key[1]
                        and row["strand"] == key[2]
                        for row in selected
                    )
                    for key in buckets
                }.items()
            )
        ),
        "optimizer": optimizer_diagnostics,
    }
    return selected, diagnostics
