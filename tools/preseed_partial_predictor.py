#!/usr/bin/env python3
"""Causal partial-seed rankings from public, already-labelled Discovery rows.

The predictor never consumes the target row's seed label.  It combines the
explicit public-input model registry with only older labels and emits a full
probability ranking so Top-K claims can be audited without moving the goalpost.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from tools.preseed_generator_lab import (
        ModelSpec,
        default_model_registry,
        load_tasks,
        predict_model,
    )
    from tools.seed_epoch_policy import (
        CURRENT_SEED_COUNT,
        CURRENT_SEED_HIGH,
        CURRENT_SEED_LOW,
    )
except ModuleNotFoundError:
    from preseed_generator_lab import (
        ModelSpec,
        default_model_registry,
        load_tasks,
        predict_model,
    )
    from seed_epoch_policy import (
        CURRENT_SEED_COUNT,
        CURRENT_SEED_HIGH,
        CURRENT_SEED_LOW,
    )


STRATEGIES = (
    "registry-weighted",
    "registry-uniform",
    "history-frequency",
    "previous-round",
)


@dataclass(frozen=True)
class SeedDomain:
    low: int
    high: int
    count: int = CURRENT_SEED_COUNT

    @property
    def size(self) -> int:
        return self.high - self.low + 1

    @property
    def values(self) -> range:
        return range(self.low, self.high + 1)

    def validate(self) -> None:
        if self.low < 0 or self.high < self.low:
            raise ValueError("invalid seed domain")
        if self.count < 1 or self.count > self.size:
            raise ValueError("invalid distinct seed count")


def configured_domain() -> SeedDomain:
    domain = SeedDomain(CURRENT_SEED_LOW, CURRENT_SEED_HIGH, CURRENT_SEED_COUNT)
    domain.validate()
    return domain


def configured_registry(domain: SeedDomain) -> list[ModelSpec]:
    """Bind the explicit registry to the active epoch instead of stale literals."""
    return [
        replace(model, seed_low=domain.low, seed_high=domain.high)
        for model in default_model_registry()
    ]


def _labelled_history(
    records: Sequence[Mapping[str, Any]], domain: SeedDomain
) -> list[Mapping[str, Any]]:
    ordered = sorted(records, key=lambda row: str(row.get("created_at") or ""))
    for row in ordered:
        seeds = [int(value) for value in row.get("seeds") or []]
        if len(seeds) != domain.count or len(set(seeds)) != domain.count:
            raise ValueError("history rows require distinct complete seed labels")
        if any(value < domain.low or value > domain.high for value in seeds):
            raise ValueError("history seed is outside configured domain")
    return ordered


def _target_without_label(target: Mapping[str, Any]) -> dict[str, Any]:
    clone = dict(target)
    clone["seeds"] = []
    return clone


def _model_weights(
    history: Sequence[Mapping[str, Any]],
    registry: Sequence[ModelSpec],
    domain: SeedDomain,
    *,
    weighted: bool,
) -> list[float]:
    if not weighted:
        return [1.0] * len(registry)
    weights: list[float] = []
    for model in registry:
        hits = 0
        trials = 0
        for row in history:
            predicted = predict_model(model, row)
            if predicted is None:
                continue
            actual = [int(value) for value in row["seeds"]]
            hits += sum(left == right for left, right in zip(predicted, actual))
            trials += len(actual)
        # Beta(1, N-1) has the random-domain hit rate 1/N as its prior mean.
        # Dividing by that baseline makes the value an interpretable relative
        # likelihood multiplier while keeping zero-hit models alive.
        weights.append(domain.size * (hits + 1.0) / (trials + domain.size))
    return weights


def _normalise(votes: Sequence[float]) -> list[float]:
    total = float(sum(votes))
    if not math.isfinite(total) or total <= 0:
        return [1.0 / len(votes)] * len(votes)
    return [float(value) / total for value in votes]


def _rank(probabilities: Sequence[float], domain: SeedDomain) -> list[int]:
    return sorted(domain.values, key=lambda value: (-probabilities[value - domain.low], value))


def _registry_probabilities(
    history: Sequence[Mapping[str, Any]],
    target: Mapping[str, Any],
    registry: Sequence[ModelSpec],
    domain: SeedDomain,
    *,
    weighted: bool,
) -> tuple[list[list[float]], list[float], dict[str, Any]]:
    # One total pseudo-vote spread over the domain prevents false certainty and
    # keeps log loss finite when the registry never proposes the true value.
    slot_votes = [[1.0 / domain.size] * domain.size for _ in range(domain.count)]
    set_votes = [1.0 / domain.size] * domain.size
    weights = _model_weights(history, registry, domain, weighted=weighted)
    evaluable = 0
    for model, weight in zip(registry, weights):
        predicted = predict_model(model, target)
        if predicted is None or len(predicted) != domain.count:
            continue
        evaluable += 1
        for slot, value in enumerate(predicted):
            if domain.low <= value <= domain.high:
                slot_votes[slot][value - domain.low] += weight
                set_votes[value - domain.low] += weight
    return (
        [_normalise(row) for row in slot_votes],
        _normalise(set_votes),
        {
            "registered_models": len(registry),
            "evaluable_models": evaluable,
            "historically_weighted": weighted,
        },
    )


def _history_frequency_probabilities(
    history: Sequence[Mapping[str, Any]], domain: SeedDomain
) -> tuple[list[list[float]], list[float], dict[str, Any]]:
    slot_votes = [[1.0] * domain.size for _ in range(domain.count)]
    set_votes = [1.0] * domain.size
    for row in history:
        for slot, value in enumerate(row["seeds"]):
            slot_votes[slot][int(value) - domain.low] += 1.0
            set_votes[int(value) - domain.low] += 1.0
    return (
        [_normalise(row) for row in slot_votes],
        _normalise(set_votes),
        {"history_rows": len(history), "laplace_smoothing": 1.0},
    )


def _previous_round_probabilities(
    history: Sequence[Mapping[str, Any]], domain: SeedDomain
) -> tuple[list[list[float]], list[float], dict[str, Any]]:
    if not history:
        raise ValueError("previous-round strategy requires history")
    previous = [int(value) for value in history[-1]["seeds"]]
    # Fixed 50% mass on the previous value and 50% over the remainder.  The
    # exact ranking is what is tested; this conservative distribution exists
    # only for proper scoring rules.
    remainder = 0.5 / max(1, domain.size - 1)
    slot_probabilities: list[list[float]] = []
    for value in previous:
        row = [remainder] * domain.size
        row[value - domain.low] = 0.5
        slot_probabilities.append(row)
    set_probabilities = [remainder] * domain.size
    for value in previous:
        set_probabilities[value - domain.low] = 0.5 / len(previous)
    set_probabilities = _normalise(set_probabilities)
    return slot_probabilities, set_probabilities, {"previous_seeds": previous}


def predict_partial(
    history: Sequence[Mapping[str, Any]],
    target: Mapping[str, Any],
    *,
    registry: Sequence[ModelSpec] | None = None,
    domain: SeedDomain | None = None,
    strategy: str = "registry-weighted",
) -> dict[str, Any]:
    """Return causal probability/ranking arrays for one unlabelled target."""
    domain = domain or configured_domain()
    domain.validate()
    history = _labelled_history(history, domain)
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}")
    clean_target = _target_without_label(target)
    target_time = str(clean_target.get("created_at") or "")
    if target_time and any(str(row.get("created_at") or "") >= target_time for row in history):
        raise ValueError("history must be strictly older than target")

    if strategy.startswith("registry-"):
        registry = list(registry or configured_registry(domain))
        slot_probabilities, set_probabilities, details = _registry_probabilities(
            history,
            clean_target,
            registry,
            domain,
            weighted=strategy == "registry-weighted",
        )
    elif strategy == "history-frequency":
        slot_probabilities, set_probabilities, details = _history_frequency_probabilities(
            history, domain
        )
    else:
        slot_probabilities, set_probabilities, details = _previous_round_probabilities(
            history, domain
        )

    slot_rankings = [_rank(row, domain) for row in slot_probabilities]
    set_ranking = _rank(set_probabilities, domain)
    return {
        "strategy": strategy,
        "task_id": str(clean_target.get("task_id") or clean_target.get("id") or ""),
        "task_created_at": clean_target.get("created_at"),
        "history_count": len(history),
        "history_through": history[-1].get("created_at") if history else None,
        "target_label_consumed": False,
        "domain": {"low": domain.low, "high": domain.high, "count": domain.count},
        "slot_rankings": slot_rankings,
        "set_ranking": set_ranking,
        "slot_probabilities": slot_probabilities,
        "set_probabilities": set_probabilities,
        "details": details,
    }


def serialise_prediction(prediction: Mapping[str, Any], *, top_k: int) -> dict[str, Any]:
    domain = prediction["domain"]
    low = int(domain["low"])
    result = {key: value for key, value in prediction.items() if key not in {
        "slot_probabilities", "set_probabilities", "slot_rankings", "set_ranking"
    }}
    result["top_k"] = top_k
    result["slot_top_k"] = []
    for ranking, probabilities in zip(
        prediction["slot_rankings"], prediction["slot_probabilities"]
    ):
        result["slot_top_k"].append([
            {"seed": seed, "probability": probabilities[seed - low]}
            for seed in ranking[:top_k]
        ])
    result["set_top_k"] = [
        {"seed": seed, "probability": prediction["set_probabilities"][seed - low]}
        for seed in prediction["set_ranking"][:top_k]
    ]
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-json", type=Path, required=True)
    parser.add_argument("--target-json", type=Path, required=True)
    parser.add_argument("--strategy", choices=STRATEGIES, default="registry-weighted")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    history = load_tasks(args.history_json)
    targets = load_tasks(args.target_json)
    if len(targets) != 1:
        raise ValueError("target JSON must contain exactly one task")
    prediction = serialise_prediction(
        predict_partial(history, targets[0], strategy=args.strategy),
        top_k=max(1, args.top_k),
    )
    raw = json.dumps(prediction, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(raw, encoding="utf-8")
    else:
        print(raw, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
