#!/usr/bin/env python3
"""Build a credential-free fingerprint of NIOME's public task RNG outputs.

Only the public task-history endpoint is read.  Signed URLs, headers, W&B data,
and score rows are neither requested nor persisted.  The resulting snapshot is
intended for generator-family testing and future state-recovery research.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

import numpy as np
from scipy.optimize import brentq
from scipy.stats import chisquare

try:
    from tools.seed_epoch_policy import configured_epoch_view, public_policy_metadata
except ModuleNotFoundError:
    from seed_epoch_policy import configured_epoch_view, public_policy_metadata


TASKS_URL = "https://niome-api.genomes.io/api/v3/tasks"
USER_AGENT = "niome-public-prng-fingerprint/1"


def fetch_tasks(per_page: int = 100, max_pages: int = 100) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        query = urlencode({"page": page, "per_page": per_page})
        request = Request(
            f"{TASKS_URL}?{query}",
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
        batch = payload.get("items") or []
        tasks.extend(batch)
        if len(batch) < per_page:
            break
    return tasks


def parse_seed(raw: Any) -> list[int]:
    if raw in (None, "", 0, "0"):
        return []
    try:
        values = [int(part.strip()) for part in str(raw).split(",") if part.strip()]
    except (TypeError, ValueError):
        return []
    return values if values and all(value >= 0 for value in values) else []


def safe_record(task: dict[str, Any]) -> dict[str, Any]:
    content = task.get("content") or {}
    contract = content.get("contract") or {}
    reference = content.get("hbb_reference") or {}
    contract_material = {
        key: contract.get(key)
        for key in (
            "version",
            "active_mutations",
            "mutation_weights",
            "mutation_regions",
            "cell_type",
            "rules",
        )
        if key in contract
    }
    reference_material = {
        key: reference.get(key)
        for key in ("window_id", "chromosome", "gene_region", "mutation_map")
        if key in reference
    }
    return {
        "task_id": str(task.get("id") or ""),
        "created_at": str(task.get("created_at") or ""),
        "seeds": parse_seed(contract.get("seed")),
        "mutations": [str(value) for value in contract.get("active_mutations") or []],
        "cell_type": str(contract.get("cell_type") or ""),
        "contract_material": json.dumps(
            contract_material, sort_keys=True, separators=(",", ":")
        ),
        "reference_material": json.dumps(
            reference_material, sort_keys=True, separators=(",", ":")
        ),
    }


def _sample_python(value: Any, low: int, high: int) -> list[int]:
    return random.Random(value).sample(range(low, high + 1), 3)


def _digest_triplet(
    value: str, algorithm: str, byteorder: str, low: int, high: int
) -> list[int]:
    digest = hashlib.new(algorithm, value.encode()).digest()
    seeds: list[int] = []
    offset = 0
    span = high - low + 1
    while len(seeds) < 3:
        if offset + 8 > len(digest):
            digest = hashlib.new(algorithm, digest).digest()
            offset = 0
        candidate = low + int.from_bytes(digest[offset : offset + 8], byteorder) % span
        offset += 8
        if candidate not in seeds:
            seeds.append(candidate)
    return seeds


def candidate_generators(record: dict[str, Any]) -> dict[str, Callable[[], list[int]]]:
    task_id = record["task_id"]
    created_at = record["created_at"]
    try:
        uuid_int = UUID(task_id).int
    except (ValueError, AttributeError):
        uuid_int = 0
    try:
        timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        timestamp = 0.0

    candidates: dict[str, Callable[[], list[int]]] = {}
    seed_inputs = {
        "uuid-string": task_id,
        "uuid-int": uuid_int,
        "created-second": int(timestamp),
        "created-millisecond": int(timestamp * 1_000),
        "created-microsecond": int(timestamp * 1_000_000),
        "uuid+created": f"{task_id}:{created_at}",
        "mutations": "|".join(record["mutations"]),
        "cell-type": record["cell_type"],
        "contract-json": record["contract_material"],
        "reference-json": record["reference_material"],
        "uuid+contract": f"{task_id}:{record['contract_material']}",
    }
    for low, high in ((0, 1000), (1, 1000), (100, 999)):
        domain = f"{low}-{high}"
        for input_name, seed_input in seed_inputs.items():
            candidates[f"python-random:{domain}:{input_name}"] = (
                lambda v=seed_input, lo=low, hi=high: _sample_python(v, lo, hi)
            )
        for input_name, seed_input in seed_inputs.items():
            text_input = str(seed_input)
            for algorithm in ("md5", "sha1", "sha256", "sha512", "blake2b"):
                for byteorder in ("big", "little"):
                    name = f"hash:{domain}:{algorithm}:{byteorder}:{input_name}"
                    candidates[name] = (
                        lambda v=text_input, a=algorithm, b=byteorder, lo=low, hi=high: _digest_triplet(
                            v, a, b, lo, hi
                        )
                    )
    return candidates


def candidate_results(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    triples = [
        record
        for record in records
        if len(record["seeds"]) == 3
        and all(0 <= seed <= 1000 for seed in record["seeds"])
    ]
    for record in triples:
        expected = record["seeds"]
        for name, generate in candidate_generators(record).items():
            predicted = generate()
            stats = result.setdefault(
                name, {"exact_triplets": 0, "position_matches": 0, "tested": 0}
            )
            stats["tested"] += 1
            stats["exact_triplets"] += int(predicted == expected)
            stats["position_matches"] += sum(
                left == right for left, right in zip(predicted, expected)
            )
    return dict(sorted(result.items()))


def estimate_catalog_size(
    observed_unique: int, task_count: int, choices_per_task: int = 2
) -> float | None:
    """Estimate catalog size assuming uniform sampling without replacement.

    This is diagnostic only.  Weighted or filtered mutation selection invalidates
    the model, which is why the report retains the observed count separately.
    """
    if observed_unique <= choices_per_task or task_count <= 0:
        return None

    def expected_unique(catalog_size: float) -> float:
        unseen_probability = (1.0 - choices_per_task / catalog_size) ** task_count
        return catalog_size * (1.0 - unseen_probability)

    lower = float(observed_unique) + 1e-6
    upper = max(lower * 2, task_count * choices_per_task * 100.0)
    return float(
        brentq(
            lambda catalog_size: expected_unique(catalog_size) - observed_unique,
            lower,
            upper,
        )
    )


def seed_regime(seeds: list[int]) -> str:
    if not seeds:
        return "unknown"
    if len(seeds) == 1:
        return "single"
    if len(seeds) == 3 and all(0 <= seed <= 1000 for seed in seeds):
        return "triple-0-1000"
    if len(seeds) == 3:
        return "triple-other"
    return f"other-{len(seeds)}"


def build_seed_epochs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group consecutive seeded tasks, ignoring unpublished gaps between them."""
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for record in records:
        regime = seed_regime(record["seeds"])
        if regime == "unknown":
            continue
        if not groups or groups[-1][0] != regime:
            groups.append((regime, [record]))
        else:
            groups[-1][1].append(record)

    epochs = []
    for index, (regime, members) in enumerate(groups):
        values = [seed for member in members for seed in member["seeds"]]
        epochs.append(
            {
                "index": index,
                "regime": regime,
                "started_at": members[0]["created_at"],
                "ended_at": members[-1]["created_at"],
                "task_count": len(members),
                "seed_value_count": len(values),
                "seed_minimum": min(values),
                "seed_maximum": max(values),
                "unique_mutations_observed": len(
                    {mutation for member in members for mutation in member["mutations"]}
                ),
                "cell_types_observed": sorted(
                    {member["cell_type"] for member in members if member["cell_type"]}
                ),
                "seed_information_bits": round(
                    len(values) * math.log2(1001), 2
                )
                if regime == "triple-0-1000"
                else None,
            }
        )
    return epochs


def best_lcg_fit(values: list[int], modulus: int, offset: int = 0) -> dict[str, int]:
    normalized = [value - offset for value in values]
    best = {"transition_matches": 0, "transitions": max(0, len(values) - 1), "a": 0, "c": 0}
    if len(values) < 2:
        return best
    for multiplier in range(modulus):
        constants = Counter(
            (right - multiplier * left) % modulus
            for left, right in zip(normalized, normalized[1:])
        )
        constant, matches = constants.most_common(1)[0]
        if matches > best["transition_matches"]:
            best = {
                "transition_matches": matches,
                "transitions": len(values) - 1,
                "a": multiplier,
                "c": constant,
            }
    return best


def chain_candidate_exact_hits(records: list[dict[str, Any]]) -> dict[str, Any]:
    hits: Counter[str] = Counter()
    pairs = list(zip(records, records[1:]))
    candidates_per_pair = 0
    for previous, current in pairs:
        inputs = {
            "previous-seeds": ",".join(map(str, previous["seeds"])),
            "previous-task-id": previous["task_id"],
            "previous-contract": previous["contract_material"],
            "previous+current-id": f"{previous['task_id']}:{current['task_id']}",
        }
        pair_candidate_count = 0
        for low, high in ((0, 1000), (1, 1000), (100, 999)):
            domain = f"{low}-{high}"
            for input_name, value in inputs.items():
                pair_candidate_count += 1
                if _sample_python(value, low, high) == current["seeds"]:
                    hits[f"python:{domain}:{input_name}"] += 1
                for algorithm in ("md5", "sha1", "sha256", "sha512", "blake2b"):
                    pair_candidate_count += 1
                    if (
                        _digest_triplet(value, algorithm, "big", low, high)
                        == current["seeds"]
                    ):
                        hits[f"hash:{domain}:{algorithm}:{input_name}"] += 1
        candidates_per_pair = pair_candidate_count
    return {
        "task_pairs": len(pairs),
        "candidates_per_pair": candidates_per_pair,
        "exact_hits": dict(sorted(hits.items())),
    }


def build_report(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    records = sorted((safe_record(task) for task in tasks), key=lambda row: row["created_at"])
    seeded = [record for record in records if record["seeds"]]
    triples = [record for record in records if len(record["seeds"]) == 3]
    normal_triples = [
        record
        for record in triples
        if all(0 <= seed <= 1000 for seed in record["seeds"])
    ]
    anomalous_triples = [record for record in triples if record not in normal_triples]
    seed_values = [seed for record in normal_triples for seed in record["seeds"]]
    mutations = sorted({value for record in records for value in record["mutations"]})
    cells = sorted({record["cell_type"] for record in records if record["cell_type"]})
    estimated_catalog_size = estimate_catalog_size(len(mutations), len(records))
    mutation_occurrences = Counter(
        value for record in records for value in record["mutations"]
    )
    occurrence_profile = Counter(mutation_occurrences.values())

    bins = [0] * 10
    for seed in seed_values:
        bins[min(seed // 100, 9)] += 1
    expected = [len(seed_values) * 100 / 1001] * 9
    expected.append(len(seed_values) * 101 / 1001)
    chi = chisquare(bins, expected) if seed_values and all(bins) else None
    matrix = np.asarray([record["seeds"] for record in normal_triples], dtype=float)
    correlations = (
        np.corrcoef(matrix, rowvar=False).round(6).tolist()
        if len(matrix) >= 2
        else []
    )

    mutation_choice_bits = (
        math.log2(len(mutations) * (len(mutations) - 1)) if len(mutations) > 1 else 0
    )
    cell_choice_bits = math.log2(len(cells)) if cells else 0
    seed_bits = len(seed_values) * math.log2(1001)
    metadata_bits_observed_catalog = len(records) * (
        mutation_choice_bits + cell_choice_bits
    )
    estimated_metadata_bits = None
    if estimated_catalog_size is not None:
        estimated_mutation_bits = math.log2(
            estimated_catalog_size * (estimated_catalog_size - 1)
        )
        estimated_metadata_bits = len(records) * (
            estimated_mutation_bits + cell_choice_bits
        )
    seed_epochs = build_seed_epochs(records)
    configured_view = configured_epoch_view(records)
    latest_epoch_records = list(configured_view["labeled_records"])
    latest_normal_epoch = next(
        (
            epoch
            for epoch in reversed(seed_epochs)
            if latest_epoch_records
            and epoch["started_at"] == latest_epoch_records[0]["created_at"]
        ),
        None,
    )
    latest_values = [
        seed for record in latest_epoch_records for seed in record["seeds"]
    ]

    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": TASKS_URL,
        "safety": {
            "public_only": True,
            "contains_signed_urls": False,
            "contains_credentials": False,
            "contains_score_rows": False,
        },
        "counts": {
            "tasks": len(records),
            "seeded_tasks": len(seeded),
            "three_seed_tasks": len(triples),
            "normal_three_seed_tasks": len(normal_triples),
            "anomalous_three_seed_tasks": len(anomalous_triples),
            "three_seed_values": len(seed_values),
            "unique_mutations": len(mutations),
            "unique_cell_types": len(cells),
        },
        "seed_range": {
            "minimum": min(seed_values) if seed_values else None,
            "maximum": max(seed_values) if seed_values else None,
            "normal_domain": [0, 1000],
            "anomalous_triplets": [
                {
                    "task_id": record["task_id"],
                    "created_at": record["created_at"],
                    "seeds": record["seeds"],
                }
                for record in anomalous_triples
            ],
            "within_task_duplicates": sum(
                len(set(record["seeds"])) != len(record["seeds"])
                for record in normal_triples
            ),
            "hundred_bins": bins,
            "uniform_bins_chi_square": float(chi.statistic) if chi else None,
            "uniform_bins_p_value": float(chi.pvalue) if chi else None,
            "position_correlation": correlations,
        },
        "information_upper_bound": {
            "observed_seed_bits": round(seed_bits, 2),
            "mutation_and_cell_bits_using_observed_union": round(
                metadata_bits_observed_catalog, 2
            ),
            "combined_bits_using_observed_union": round(
                seed_bits + metadata_bits_observed_catalog, 2
            ),
            "estimated_mutation_and_cell_bits_if_uniform_sampling": (
                round(estimated_metadata_bits, 2)
                if estimated_metadata_bits is not None
                else None
            ),
            "estimated_combined_bits_if_uniform_sampling": (
                round(seed_bits + estimated_metadata_bits, 2)
                if estimated_metadata_bits is not None
                else None
            ),
            "mt19937_state_bits": 19_937,
            "warning": (
                "These are information estimates, not a recovery proof. Full catalog "
                "membership/order, RNG call order, rejection draws, selection uniformity, "
                "and generator continuity remain unknown."
            ),
        },
        "candidate_scope": "configured-current-epoch-only",
        "candidate_results": candidate_results(latest_epoch_records),
        "research_epoch": public_policy_metadata(configured_view),
        "seed_epochs": seed_epochs,
        "latest_epoch_weak_generator_checks": {
            "epoch_index": latest_normal_epoch["index"] if latest_normal_epoch else None,
            "lcg": {
                "mod-900-offset-100": best_lcg_fit(latest_values, 900, 100),
                "mod-1000": best_lcg_fit(latest_values, 1000),
                "mod-1001": best_lcg_fit(latest_values, 1001),
            },
            "previous-task-chain_candidates": chain_candidate_exact_hits(
                latest_epoch_records
            ),
        },
        "catalog_fingerprint": {
            "observed_unique": len(mutations),
            "estimated_total_if_uniform_sampling": (
                round(estimated_catalog_size, 2)
                if estimated_catalog_size is not None
                else None
            ),
            "estimated_unseen_if_uniform_sampling": (
                round(estimated_catalog_size - len(mutations), 2)
                if estimated_catalog_size is not None
                else None
            ),
            "uniform_sampling_model_is_diagnostic_only": True,
            "occurrence_frequency_of_frequencies": {
                str(frequency): count
                for frequency, count in sorted(occurrence_profile.items())
            },
            "mutations_seen_at_least_six_times": sum(
                count
                for frequency, count in occurrence_profile.items()
                if frequency >= 6
            ),
            "maximum_observed_frequency": max(mutation_occurrences.values(), default=0),
            "lexicographic_sha256": hashlib.sha256(
                "\n".join(mutations).encode()
            ).hexdigest(),
            "cell_types": cells,
        },
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-pages", type=int, default=100)
    args = parser.parse_args()

    report = build_report(fetch_tasks(max_pages=max(1, args.max_pages)))
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(
            json.dumps(
                {
                    "output": str(args.output.resolve()),
                    "counts": report["counts"],
                    "information_upper_bound": report["information_upper_bound"],
                },
                ensure_ascii=False,
            )
        )
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
