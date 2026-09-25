"""Pure local clone of NIOME Stage 5 coverage and final multiplication."""

from __future__ import annotations

from collections import Counter, defaultdict
import math
from typing import Any, Iterable


def shannon_entropy(counts: Iterable[int]) -> float:
    counts = list(counts)
    total = sum(counts)
    if total <= 0:
        return 0.0
    value = 0.0
    for count in counts:
        if count > 0:
            probability = count / total
            value -= probability * math.log2(probability)
    return value


def coverage_entropy_ratio(observed: Counter, full_support: Iterable[Any]) -> float:
    support = list(full_support)
    if len(support) <= 1:
        return 1.0
    entropy = shannon_entropy(observed.get(category, 0) for category in support)
    maximum = math.log2(len(support))
    return entropy / maximum if maximum > 0 else 1.0


def extract_kmers(seq: str, k: int) -> list[str]:
    if len(seq) < k:
        return []
    return [seq[index : index + k] for index in range(len(seq) - k + 1)]


def kmer_diversity_entropy_ratio(guides: list[str], k: int = 12) -> float:
    pool: Counter = Counter()
    total = 0
    for guide in guides:
        kmers = extract_kmers(guide, k)
        pool.update(kmers)
        total += len(kmers)
    if total <= 1:
        return 0.0
    entropy = shannon_entropy(pool.values())
    maximum = math.log2(total)
    return entropy / maximum if maximum > 0 else 0.0


def distinct_guide_ratio(guides: list[str]) -> float:
    return len(set(guides)) / len(guides) if guides else 0.0


def geometric_mean(values: list[float], epsilon: float = 1e-9) -> float:
    if not values:
        return 0.0
    clipped = [max(value, epsilon) for value in values]
    return math.exp(sum(math.log(value) for value in clipped) / len(clipped))


def jensen_shannon_divergence(counts_p: Counter, counts_q: Counter):
    total_p, total_q = sum(counts_p.values()), sum(counts_q.values())
    if total_p == 0 or total_q == 0:
        return None
    categories = set(counts_p) | set(counts_q)
    p = {category: counts_p.get(category, 0) / total_p for category in categories}
    q = {category: counts_q.get(category, 0) / total_q for category in categories}
    midpoint = {category: 0.5 * (p[category] + q[category]) for category in categories}

    def kl(left, right):
        return sum(
            left[category] * math.log2(left[category] / right[category])
            for category in categories
            if left[category] > 0
        )

    return 0.5 * kl(p, midpoint) + 0.5 * kl(q, midpoint)


def wasserstein_1d(sample_p: list[float], sample_q: list[float], n_quantiles: int = 200):
    if not sample_p or not sample_q:
        return None
    sorted_p, sorted_q = sorted(sample_p), sorted(sample_q)

    def quantile(sample, q):
        index = min(len(sample) - 1, max(0, int(round(q * (len(sample) - 1)))))
        return sample[index]

    differences = []
    for index in range(n_quantiles):
        q = index / (n_quantiles - 1) if n_quantiles > 1 else 0.5
        differences.append(abs(quantile(sorted_p, q) - quantile(sorted_q, q)))
    return sum(differences) / len(differences)


def compute_distribution_fidelity(
    valid_experiments: list[dict[str, Any]],
    stage3_results: list[dict[str, Any]],
    contract: dict[str, Any],
    *,
    k: int = 12,
) -> dict[str, Any]:
    active_mutations = contract["active_mutations"]
    cas_systems = contract["rules"].get("cas_systems", ["Cas9", "Cas12a"])
    strands = ["+", "-"]
    count = len(valid_experiments)
    if count == 0:
        return {
            "n_valid_experiments": 0,
            "distribution_fidelity_score": 0.0,
            "note": "no valid experiments",
        }

    mutation_counts = Counter(item["experiment"]["mutation"] for item in valid_experiments)
    cas_counts = Counter(item["experiment"]["cas_system"] for item in valid_experiments)
    strand_counts = Counter(item["experiment"].get("strand") for item in valid_experiments)
    joint_counts = Counter(
        (
            item["experiment"]["mutation"],
            item["experiment"]["cas_system"],
            item["experiment"].get("strand"),
        )
        for item in valid_experiments
    )
    joint_support = [
        (mutation, cas, strand)
        for mutation in active_mutations
        for cas in cas_systems
        for strand in strands
    ]
    mutation_ratio = coverage_entropy_ratio(mutation_counts, active_mutations)
    cas_ratio = coverage_entropy_ratio(cas_counts, cas_systems)
    strand_ratio = coverage_entropy_ratio(strand_counts, strands)
    joint_ratio = coverage_entropy_ratio(joint_counts, joint_support)
    guides = [item["experiment"]["guideRNA"] for item in valid_experiments]
    kmer_ratio = kmer_diversity_entropy_ratio(guides, k=k)
    guide_ratio = distinct_guide_ratio(guides)
    fidelity = geometric_mean(
        [mutation_ratio, cas_ratio, strand_ratio, joint_ratio, kmer_ratio, guide_ratio]
    )

    by_cas_repair: dict[str, Counter] = defaultdict(Counter)
    by_cas_indel: dict[str, list[int]] = defaultdict(list)
    for result in stage3_results:
        by_cas_repair[result["cas"]][result["outcome"]] += 1
        by_cas_indel[result["cas"]].append(result["indel_length"])
    present = [cas for cas in cas_systems if sum(by_cas_repair[cas].values()) >= 5]
    cas_shift: dict[str, Any] = {"insufficient_data": True}
    if len(present) >= 2:
        first, second = present[:2]
        cas_shift = {
            "insufficient_data": False,
            "compared": [first, second],
            "repair_mode_jensen_shannon_divergence": jensen_shannon_divergence(
                by_cas_repair[first], by_cas_repair[second]
            ),
            "indel_length_wasserstein_distance": wasserstein_1d(
                by_cas_indel[first], by_cas_indel[second]
            ),
        }
    return {
        "n_valid_experiments": count,
        "mutation_coverage_entropy_ratio": mutation_ratio,
        "cas_system_coverage_entropy_ratio": cas_ratio,
        "strand_coverage_entropy_ratio": strand_ratio,
        "joint_coverage_entropy_ratio": joint_ratio,
        "kmer_diversity_entropy_ratio": kmer_ratio,
        "distinct_guide_ratio": guide_ratio,
        "distribution_fidelity_score": fidelity,
        "cas_specific_shift_diagnostic": cas_shift,
        "coverage_detail": {
            "mutation_counts": dict(mutation_counts),
            "cas_system_counts": dict(cas_counts),
            "strand_counts": dict(strand_counts),
        },
    }


def run_stage5(
    valid_experiments: list[dict[str, Any]],
    stage3_results: list[dict[str, Any]],
    stage4: dict[str, Any],
    contract: dict[str, Any],
    *,
    k: int = 12,
) -> dict[str, Any]:
    distribution = compute_distribution_fidelity(
        valid_experiments, stage3_results, contract, k=k
    )
    factor = max(0.0, min(1.0, distribution.get("distribution_fidelity_score", 0.0)))
    return {
        "n_valid_experiments": stage4.get("n_valid_experiments"),
        "total_weighted_score": stage4["total_weighted_score"],
        "consistency_score": stage4.get("consistency_score"),
        "consistency_factor": stage4["consistency_factor"],
        "distribution_fidelity_score": distribution.get("distribution_fidelity_score"),
        "distribution_fidelity_factor": factor,
        "final_score": stage4["total_weighted_score"]
        * stage4["consistency_factor"]
        * factor,
        "distribution_trace": distribution,
    }
