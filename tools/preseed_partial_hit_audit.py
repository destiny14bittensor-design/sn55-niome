#!/usr/bin/env python3
"""Leakage-safe walk-forward audit of partial and Top-K seed predictability."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import random
from typing import Any, Mapping, Sequence

try:
    from tools.preseed_generator_lab import load_tasks
    from tools.preseed_partial_predictor import (
        STRATEGIES,
        SeedDomain,
        configured_domain,
        configured_registry,
        predict_partial,
    )
except ModuleNotFoundError:
    from preseed_generator_lab import load_tasks
    from preseed_partial_predictor import (
        STRATEGIES,
        SeedDomain,
        configured_domain,
        configured_registry,
        predict_partial,
    )


DEFAULT_KS = (3, 5, 10, 20, 50, 100)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def binomial_survival(successes: int, trials: int, probability: float) -> float:
    if not 0 <= successes <= trials:
        raise ValueError("invalid binomial counts")
    if trials == 0:
        return 1.0
    if probability <= 0:
        return 0.0 if successes > 0 else 1.0
    if probability >= 1:
        return 1.0
    return min(
        1.0,
        sum(
            math.comb(trials, value)
            * probability**value
            * (1.0 - probability) ** (trials - value)
            for value in range(successes, trials + 1)
        ),
    )


def bootstrap_rate_interval(
    values: Sequence[int], *, samples: int = 5000, seed: int = 55
) -> list[float | None]:
    if not values:
        return [None, None]
    rng = random.Random(seed)
    rates = []
    for _ in range(samples):
        rates.append(sum(rng.choice(values) for _ in values) / len(values))
    rates.sort()
    return [rates[int(0.025 * (samples - 1))], rates[int(0.975 * (samples - 1))]]


def _rank_of(ranking: Sequence[int], value: int) -> int:
    return ranking.index(value) + 1


def _round_result(
    prediction: Mapping[str, Any], actual: Sequence[int], ks: Sequence[int]
) -> dict[str, Any]:
    domain = prediction["domain"]
    low = int(domain["low"])
    slot_rankings = prediction["slot_rankings"]
    slot_probabilities = prediction["slot_probabilities"]
    set_ranking = prediction["set_ranking"]
    ordered_top1 = [ranking[0] for ranking in slot_rankings]
    unordered_top3 = set_ranking[: len(actual)]
    position_matches = sum(left == right for left, right in zip(ordered_top1, actual))
    set_intersection = len(set(unordered_top3) & set(actual))
    slot_ranks = [_rank_of(ranking, value) for ranking, value in zip(slot_rankings, actual)]
    set_ranks = [_rank_of(set_ranking, value) for value in actual]
    log_losses = []
    briers = []
    for probabilities, value in zip(slot_probabilities, actual):
        probability = max(float(probabilities[value - low]), 1e-300)
        log_losses.append(-math.log(probability))
        briers.append(sum(item * item for item in probabilities) - 2.0 * probability + 1.0)
    return {
        "task_id": prediction["task_id"],
        "created_at": prediction["task_created_at"],
        "history_count": prediction["history_count"],
        "actual_seeds": list(actual),
        "ordered_top1": ordered_top1,
        "unordered_top3": unordered_top3,
        "position_matches": position_matches,
        "set_intersection": set_intersection,
        "slot_ranks": slot_ranks,
        "set_ranks": set_ranks,
        "slot_top_k_hits": {
            str(k): sum(value in ranking[:k] for ranking, value in zip(slot_rankings, actual))
            for k in ks
        },
        "set_top_k_hits": {
            str(k): sum(value in set_ranking[:k] for value in actual) for k in ks
        },
        "mean_log_loss": sum(log_losses) / len(log_losses),
        "mean_brier_score": sum(briers) / len(briers),
        "mean_slot_reciprocal_rank": sum(1.0 / rank for rank in slot_ranks) / len(slot_ranks),
        "mean_set_reciprocal_rank": sum(1.0 / rank for rank in set_ranks) / len(set_ranks),
    }


def _summarise(
    rows: Sequence[Mapping[str, Any]],
    domain: SeedDomain,
    ks: Sequence[int],
    *,
    correction_tests: int,
) -> dict[str, Any]:
    rounds = len(rows)
    slot_trials = rounds * domain.count
    bucket = Counter(int(row["set_intersection"]) for row in rows)
    position_hits = sum(int(row["position_matches"]) for row in rows)
    any_hits = sum(int(row["set_intersection"]) >= 1 for row in rows)
    at_least_two = sum(int(row["set_intersection"]) >= 2 for row in rows)
    all_three = sum(int(row["set_intersection"]) == domain.count for row in rows)
    random_any = 1.0
    if domain.size >= 2 * domain.count:
        random_any = 1.0 - math.comb(domain.size - domain.count, domain.count) / math.comb(
            domain.size, domain.count
        )
    any_p = binomial_survival(any_hits, rounds, random_any)
    slot_p = binomial_survival(position_hits, slot_trials, 1.0 / domain.size)
    top_k: dict[str, Any] = {}
    for k in ks:
        hits = sum(int(row["slot_top_k_hits"][str(k)]) for row in rows)
        raw_p = binomial_survival(hits, slot_trials, min(k, domain.size) / domain.size)
        set_hits = sum(int(row["set_top_k_hits"][str(k)]) for row in rows)
        top_k[str(k)] = {
            "slot_hits": hits,
            "slot_trials": slot_trials,
            "slot_recall": hits / slot_trials if slot_trials else None,
            "random_slot_recall": min(k, domain.size) / domain.size,
            "raw_p_value": raw_p,
            "bonferroni_p_value": min(1.0, raw_p * correction_tests),
            "set_value_hits": set_hits,
            "set_value_trials": slot_trials,
            "set_value_recall": set_hits / slot_trials if slot_trials else None,
        }
    indicators = [int(row["set_intersection"]) >= 1 for row in rows]
    return {
        "rounds": rounds,
        "slot_trials": slot_trials,
        "position_exact_hits": position_hits,
        "position_exact_rate": position_hits / slot_trials if slot_trials else None,
        "position_random_rate": 1.0 / domain.size,
        "position_raw_p_value": slot_p,
        "position_bonferroni_p_value": min(1.0, slot_p * correction_tests),
        "unordered_intersection_histogram": {str(key): bucket.get(key, 0) for key in range(domain.count + 1)},
        "rounds_with_at_least_one": any_hits,
        "rounds_with_at_least_two": at_least_two,
        "rounds_with_all": all_three,
        "round_any_rate": any_hits / rounds if rounds else None,
        "round_any_random_rate": random_any,
        "round_any_bootstrap_95pct": bootstrap_rate_interval(indicators),
        "round_any_raw_p_value": any_p,
        "round_any_bonferroni_p_value": min(1.0, any_p * correction_tests),
        "mean_slot_mrr": (
            sum(float(row["mean_slot_reciprocal_rank"]) for row in rows) / rounds
            if rounds else None
        ),
        "mean_set_mrr": (
            sum(float(row["mean_set_reciprocal_rank"]) for row in rows) / rounds
            if rounds else None
        ),
        "mean_log_loss": (
            sum(float(row["mean_log_loss"]) for row in rows) / rounds if rounds else None
        ),
        "mean_brier_score": (
            sum(float(row["mean_brier_score"]) for row in rows) / rounds if rounds else None
        ),
        "top_k": top_k,
    }


def run_walk_forward_audit(
    records: Sequence[Mapping[str, Any]],
    *,
    min_history: int = 5,
    final_holdout_rounds: int = 4,
    strategies: Sequence[str] = STRATEGIES,
    ks: Sequence[int] = DEFAULT_KS,
    domain: SeedDomain | None = None,
    registry: Sequence[Any] | None = None,
) -> dict[str, Any]:
    domain = domain or configured_domain()
    domain.validate()
    ordered = sorted(records, key=lambda row: str(row.get("created_at") or ""))
    if min_history < 1 or len(ordered) <= min_history:
        raise ValueError("not enough records for walk-forward audit")
    if final_holdout_rounds < 1 or final_holdout_rounds >= len(ordered) - min_history + 1:
        raise ValueError("invalid final holdout size")
    if any(strategy not in STRATEGIES for strategy in strategies):
        raise ValueError("unknown strategy")
    if any(len(row.get("seeds") or []) != domain.count for row in ordered):
        raise ValueError("every audit row needs a complete seed label")
    registry = list(registry or configured_registry(domain))
    holdout_start = len(ordered) - final_holdout_rounds
    rows_by_strategy: dict[str, list[dict[str, Any]]] = {name: [] for name in strategies}
    for index in range(min_history, len(ordered)):
        history = ordered[:index]
        target = ordered[index]
        for strategy in strategies:
            prediction = predict_partial(
                history,
                target,
                registry=registry,
                domain=domain,
                strategy=strategy,
            )
            row = _round_result(prediction, [int(value) for value in target["seeds"]], ks)
            row["split"] = "final-holdout" if index >= holdout_start else "development"
            rows_by_strategy[strategy].append(row)

    correction_tests = len(strategies) * (len(ks) + 2) * 2
    evaluations: dict[str, Any] = {}
    for strategy, rows in rows_by_strategy.items():
        development = [row for row in rows if row["split"] == "development"]
        holdout = [row for row in rows if row["split"] == "final-holdout"]
        evaluations[strategy] = {
            "all_walk_forward": _summarise(
                rows, domain, ks, correction_tests=correction_tests
            ),
            "development": _summarise(
                development, domain, ks, correction_tests=correction_tests
            ),
            "final_holdout": _summarise(
                holdout, domain, ks, correction_tests=correction_tests
            ),
            "rounds": rows,
        }

    primary = evaluations["registry-weighted"]["final_holdout"]
    primary_significant = (
        (
            primary["round_any_bonferroni_p_value"] < 0.01
            or primary["position_bonferroni_p_value"] < 0.01
        )
        and primary["rounds_with_at_least_one"] >= 2
    )
    top_k_significant = any(
        row["bonferroni_p_value"] < 0.01
        and row["slot_recall"] > row["random_slot_recall"]
        for row in primary["top_k"].values()
    )
    if primary_significant:
        verdict = "A"
        conclusion = "practical-partial-exact-signal-detected"
    elif primary["rounds_with_at_least_one"] > 0 or top_k_significant:
        verdict = "B"
        conclusion = "weak-signal-requires-prospective-confirmation"
    else:
        verdict = "C"
        conclusion = "partial-prediction-not-above-random-baseline"

    return {
        "version": 1,
        "generated_at": utc_now(),
        "mode": "offline-public-discovery-expanding-window-partial-hit-audit",
        "safety": {
            "network_calls": False,
            "holdout_labels_opened": False,
            "submission_writes": False,
            "target_seed_removed_before_prediction": True,
        },
        "domain": {"low": domain.low, "high": domain.high, "size": domain.size, "count": domain.count, "distinct": True},
        "split_policy": {
            "ordered_by": "created_at",
            "total_records": len(ordered),
            "minimum_history": min_history,
            "walk_forward_forecasts": len(ordered) - min_history,
            "development_forecasts": max(0, holdout_start - min_history),
            "final_holdout_forecasts": final_holdout_rounds,
            "final_holdout_task_ids": [str(row.get("task_id") or "") for row in ordered[holdout_start:]],
            "model_and_hyperparameter_selection_uses_final_holdout": False,
        },
        "registry": {"models": len(registry), "fixed_before_walk_forward": True},
        "multiple_testing": {"method": "Bonferroni", "tests": correction_tests},
        "score_semantics": {
            "formula": "final_score = mean(per_seed_final_score)",
            "numeric_seed_distance_has_credit": False,
            "one_exact_seed_uplift_share": "one-third of that seed-specific improvement, holding other seed scores fixed",
            "local_oracle_replay": "not-identifiable-from-prediction-audit-alone",
        },
        "evaluations": evaluations,
        "verdict": {"code": verdict, "conclusion": conclusion, "primary_strategy": "registry-weighted"},
    }


def build_markdown(report: Mapping[str, Any]) -> str:
    split = report["split_policy"]
    domain = report["domain"]
    lines = [
        "# Partial seed prediction audit",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "## Method",
        "",
        f"- Discovery records: {split['total_records']}",
        f"- Expanding-window forecasts: {split['walk_forward_forecasts']}",
        f"- Final untouched suffix: {split['final_holdout_forecasts']} rounds",
        f"- Seed domain: {domain['low']}..{domain['high']} ({domain['size']} values), {domain['count']} distinct seeds",
        "- Every target label was removed before prediction; sealed Holdout was not opened.",
        "",
        "## Results",
        "",
        "| Strategy | Split | Exact slots | Rounds >=1 | Top-20 recall | Adjusted p (>=1) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for strategy, evaluation in report["evaluations"].items():
        for label in ("development", "final_holdout", "all_walk_forward"):
            row = evaluation[label]
            lines.append(
                "| {strategy} | {label} | {hits}/{trials} | {any_hits}/{rounds} | {top20:.4f} | {p:.6g} |".format(
                    strategy=strategy,
                    label=label,
                    hits=row["position_exact_hits"],
                    trials=row["slot_trials"],
                    any_hits=row["rounds_with_at_least_one"],
                    rounds=row["rounds"],
                    top20=row["top_k"]["20"]["slot_recall"] or 0.0,
                    p=row["round_any_bonferroni_p_value"],
                )
            )
    verdict = report["verdict"]
    lines.extend([
        "",
        "## Decision",
        "",
        f"**{verdict['code']} — {verdict['conclusion']}**",
        "",
        "Numeric closeness was never counted. Only exact seed equality and predeclared Top-K coverage were scored. "
        "The score benefit of one or two known seeds follows from the validator's arithmetic mean, but an oracle "
        "submission uplift cannot be inferred unless seed-aware candidate payloads are rebuilt and replayed separately.",
        "",
    ])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--min-history", type=int, default=5)
    parser.add_argument("--final-holdout-rounds", type=int, default=4)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_walk_forward_audit(
        load_tasks(args.discovery_json),
        min_history=args.min_history,
        final_holdout_rounds=args.final_holdout_rounds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(build_markdown(report), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "verdict": report["verdict"],
        "final_holdout": report["evaluations"]["registry-weighted"]["final_holdout"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
