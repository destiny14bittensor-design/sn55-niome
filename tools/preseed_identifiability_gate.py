#!/usr/bin/env python3
"""Quantify which seed-generator classes can be identified from Discovery.

This report prevents a successful fit to a short bounded-output sequence from
being mistaken for a predictive state recovery.  It never reads Holdout labels.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

try:
    from tools.preseed_mt_cpsat_joint import atomic_json
except ModuleNotFoundError:
    from preseed_mt_cpsat_joint import atomic_json


MT19937_EFFECTIVE_BITS = 19_937


def ordered_distinct_bits(low: int, high: int, count: int) -> float:
    population = int(high) - int(low) + 1
    if count < 0 or population < count:
        raise ValueError("invalid ordered-distinct output space")
    return sum(math.log2(population - offset) for offset in range(count))


def discovery_count(state: dict[str, Any]) -> int:
    return int(state["split_policy"]["counts"]["discovery"])


def exact_uid_information_upper_bound(
    constraints: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return an intentionally optimistic information bound for shuffle leaks.

    The number of distinct orders of the observed sanitized group-token
    multiset is an upper bound on leaked order information.  Partial event
    streams, missing insertions, and domain ambiguity only reduce useful
    information.  Exact singleton domains are counted separately for audit,
    but are already included in the multiset-order bound.
    """
    if not constraints:
        return {
            "shuffle_rounds": 0,
            "exact_uid_observations": 0,
            "maximum_shuffle_information_bits": 0.0,
        }
    rounds = 0
    observations = 0
    bits = 0.0
    for row in constraints.get("rounds") or []:
        size = int(row.get("shuffle_size") or 0)
        if size <= 0:
            continue
        domains = {
            str(key): [int(value) for value in values]
            for key, values in (row.get("uid_domains") or {}).items()
        }
        exact_tokens = {
            str(token)
            for token in (row.get("ordered_uid_domains") or [])
            if len(domains.get(str(token)) or []) == 1
        }
        count = min(size, len(exact_tokens))
        group_tokens = [
            str(token)
            for token in (
                row.get("ordered_group_domains")
                or row.get("ordered_uid_domains")
                or []
            )
        ]
        group_counts: dict[str, int] = {}
        for token in group_tokens:
            group_counts[token] = group_counts.get(token, 0) + 1
        rounds += 1
        observations += count
        bits += (
            math.lgamma(len(group_tokens) + 1)
            - sum(math.lgamma(value + 1) for value in group_counts.values())
        ) / math.log(2)
    return {
        "shuffle_rounds": rounds,
        "exact_uid_observations": observations,
        "maximum_shuffle_information_bits": round(bits, 6),
    }


def build_report(
    state: dict[str, Any], constraints: dict[str, Any] | None = None
) -> dict[str, Any]:
    count = discovery_count(state)
    expected = state["epoch_policy"]["expected"]
    seed_count = int(expected["seed_count"])
    low, high = (int(value) for value in expected["seed_range"])
    distinct = bool(expected.get("distinct"))
    if distinct:
        bits_per_task = ordered_distinct_bits(low, high, seed_count)
    else:
        bits_per_task = seed_count * math.log2(high - low + 1)
    label_bits = count * bits_per_task
    shuffle_bound = exact_uid_information_upper_bound(constraints)
    shuffle_bits = float(shuffle_bound["maximum_shuffle_information_bits"])
    combined_bits = label_bits + shuffle_bits
    label_only_residual = max(0.0, MT19937_EFFECTIVE_BITS - label_bits)
    residual = max(0.0, MT19937_EFFECTIVE_BITS - combined_bits)
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "discovery-generator-identifiability-gate",
        "summary": {
            "discovery_tasks": count,
            "seed_values": count * seed_count,
            "seed_low": low,
            "seed_high": high,
            "ordered_distinct": distinct,
            "maximum_label_information_bits": round(label_bits, 6),
            **shuffle_bound,
            "maximum_combined_information_bits": round(combined_bits, 6),
            "mt19937_effective_state_bits": MT19937_EFFECTIVE_BITS,
            "minimum_unresolved_mt19937_bits_from_labels_only": round(
                label_only_residual, 6
            ),
            "minimum_unresolved_mt19937_bits_under_optimistic_shuffle_bound": round(
                residual, 6
            ),
            "labels_only_mt19937_identifiable": label_bits >= MT19937_EFFECTIVE_BITS,
            "full_state_identifiable_under_optimistic_shuffle_bound": (
                combined_bits >= MT19937_EFFECTIVE_BITS
            ),
        },
        "generator_classes": {
            "public_deterministic_mapping": {
                "identifiable_from_20_tasks": True,
                "method": "ordered exact replay on Discovery, then sealed Holdout",
            },
            "small_hidden_state": {
                "identifiable_from_20_tasks": "model-dependent",
                "method": "recover state and predict an excluded Discovery suffix",
            },
            "python_or_numpy_mt19937_from_seed_labels_only": {
                "identifiable_from_20_tasks": False,
                "reason": "bounded-output information is far below effective state size",
            },
            "validator_numpy_mt19937_plus_public_shuffle": {
                "identifiable_from_20_tasks": "feasible only if RNG coupling and exact alignment hold",
                "method": "joint Fisher--Yates/rejection/state recovery",
            },
            "os_csprng_or_keyed_prf": {
                "identifiable_from_20_tasks": False,
                "reason": "samples do not reveal the secret entropy or key",
            },
        },
        "acceptance_gate": (
            "A generator is accepted only after it predicts an excluded Discovery suffix "
            "exactly and then passes sealed Holdout or a prospective task."
        ),
        "safety": {
            "holdout_opened": False,
            "network_reads": False,
            "submission_writes": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument(
        "--shuffle-constraints",
        type=Path,
        help="Optional event-index shuffle corpus used only for an information upper bound.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_identifiability_gate.json"),
    )
    args = parser.parse_args()
    state = json.loads(args.state.read_text(encoding="utf-8"))
    constraints = (
        json.loads(args.shuffle_constraints.read_text(encoding="utf-8"))
        if args.shuffle_constraints
        else None
    )
    report = build_report(state, constraints)
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
