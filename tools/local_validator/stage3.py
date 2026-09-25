"""Pure local clone of NIOME Stage 3 synthetic outcomes."""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import math
import random
from typing import Any


def experiment_seed(round_seed: int, submitted_exp: dict[str, Any]) -> int:
    key = "|".join(
        str(value)
        for value in (
            round_seed,
            submitted_exp["mutation"],
            submitted_exp["cas_system"],
            submitted_exp["guideRNA"],
            submitted_exp["target_alignment_start"],
            submitted_exp.get("strand"),
        )
    )
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % (2**32)


def extract_features(exp: dict[str, Any]) -> dict[str, Any]:
    features = exp["features"]
    return {
        "gc": features["gc"],
        "distance": features["distance_to_mutation"],
        "gc_score": features["gc_score"],
        "dist_score": features["dist_score"],
        "consistency": features["consistency"],
        "cell_type_accessibility": features.get("cell_type_accessibility", 1.0),
        "mutation_weight": features.get("mutation_weight", 1.0),
        "region_energy_offset": features.get("region_energy_offset", 0.0),
    }


def sequence_energy(features: dict[str, Any]) -> float:
    return max(
        0.0,
        min(
            1.0,
            features["cell_type_accessibility"]
            * (
                1.8 * features["gc"]
                + 0.6 * math.exp(-features["distance"] / 1500)
                + features.get("region_energy_offset", 0.0)
            ),
        ),
    )


def microhomology_trigger(features: dict[str, Any], rng: random.Random) -> bool:
    gc = features["gc"]
    probability = min(0.6, gc * (1 - gc) * 2.2)
    return rng.random() < probability


def cut_probability(cas: str, energy: float) -> float:
    base = 0.86 if cas == "Cas9" else 0.78
    return min(0.99, max(0.4, base + 0.18 * energy))


def repair_mode(cas: str, energy: float, microhomology: bool, rng: random.Random) -> str:
    hdr = (0.32 if cas == "Cas9" else 0.24) + 0.35 * energy
    mh_nhej = 0.30 if microhomology else 0.12
    blunt = 0.35
    draw = rng.random() * (hdr + mh_nhej + blunt)
    if draw < hdr:
        return "HDR"
    if draw < hdr + mh_nhej:
        return "MH_NHEJ"
    return "BLUNT_NHEJ"


def sample_indel_length(mode: str, rng: random.Random) -> int:
    if mode == "HDR":
        return 0
    if mode == "MH_NHEJ":
        return max(1, int(rng.gammavariate(2.2, 2.8)))
    if mode == "BLUNT_NHEJ":
        return max(1, int(rng.expovariate(0.6)))
    return 1


def simulate(exp: dict[str, Any], round_seed: int) -> dict[str, Any]:
    features = extract_features(exp)
    submitted = exp["experiment"]
    experiment_id = submitted["experiment_id"]
    cas = submitted["cas_system"]
    mutation = submitted["mutation"]
    derived_seed = experiment_seed(round_seed, submitted)
    rng = random.Random(derived_seed)
    energy = sequence_energy(features)
    microhomology = microhomology_trigger(features, rng)
    cut_p = cut_probability(cas, energy)
    if rng.random() > cut_p:
        return {
            "experiment_id": experiment_id,
            "mutation": mutation,
            "mutation_weight": features["mutation_weight"],
            "cas": cas,
            "outcome": "no_cut",
            "indel_length": 0,
            "features": features,
            "energy": energy,
            "mh": microhomology,
        }
    mode = repair_mode(cas, energy, microhomology, rng)
    return {
        "experiment_id": experiment_id,
        "mutation": mutation,
        "mutation_weight": features["mutation_weight"],
        "cas": cas,
        "outcome": mode,
        "indel_length": sample_indel_length(mode, rng),
        "features": features,
        "energy": energy,
        "mh": microhomology,
    }


def _pearson_corr(xs: list[float], ys: list[float]) -> float:
    count = len(xs)
    if count < 2:
        return 0.0
    mean_x, mean_y = sum(xs) / count, sum(ys) / count
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    denominator = math.sqrt(var_x * var_y)
    return covariance / denominator if denominator > 0 else 0.0


def _group_by_mutation(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[result["mutation"]].append(result)
    breakdown = {}
    for mutation, group in grouped.items():
        count = len(group)
        breakdown[mutation] = {
            "mutation_weight": group[0]["mutation_weight"],
            "n": count,
            "cut_rate": sum(item["outcome"] != "no_cut" for item in group) / count,
            "mean_energy": sum(item["energy"] for item in group) / count,
            "mean_indel_length": sum(item["indel_length"] for item in group) / count,
        }
    return dict(
        sorted(breakdown.items(), key=lambda item: item[1]["mutation_weight"], reverse=True)
    )


def run_stage3(valid_experiments: list[dict[str, Any]], seed: int):
    results = [simulate(exp, seed) for exp in valid_experiments]
    count = len(results)
    outcome_counts = Counter(item["outcome"] for item in results)
    indels = [item["indel_length"] for item in results]
    energies = [item["energy"] for item in results]
    weights = [item["mutation_weight"] for item in results]
    cuts = [0.0 if item["outcome"] == "no_cut" else 1.0 for item in results]
    summary = {
        "n": count,
        "cut_rate": 1 - outcome_counts["no_cut"] / count if count else 0.0,
        "mean_indel_length": sum(indels) / max(1, len(indels)),
        "mean_energy": sum(energies) / max(1, len(energies)),
        "outcomes": dict(outcome_counts),
        "mutation_weight_breakdown": _group_by_mutation(results),
        "weight_correlations": {
            "weight_vs_cut": _pearson_corr(weights, cuts),
            "weight_vs_energy": _pearson_corr(weights, energies),
            "weight_vs_indel_length": _pearson_corr(weights, indels),
        },
    }
    return results, summary
