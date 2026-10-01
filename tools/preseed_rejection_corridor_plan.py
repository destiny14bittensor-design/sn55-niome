#!/usr/bin/env python3
"""Plan high-mass rejection corridors for the exact MT alignment solver.

The broad cumulative 3.5-sigma lattice grows with the square root of the full
stream and dominates memory.  This tool simulates the known NumPy masked-draw
rejection law, then greedily selects a small set of checkpoint corridors whose
union covers most simulated paths.  Coverage is empirical and never presented
as a proof; shards that do not cover a path cannot reject the generator family.

Only public shuffle sizes and fixed Discovery labels are read.  Sealed seed
labels are neither required nor opened.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

try:
    from tools.preseed_mt_cpsat_joint import atomic_json
    from tools.preseed_mt_xorsat_joint import build_draw_layout, interval_bits
except ModuleNotFoundError:
    from preseed_mt_cpsat_joint import atomic_json
    from preseed_mt_xorsat_joint import build_draw_layout, interval_bits


def checkpoint_draws(draws: int, interval: int) -> list[int]:
    values = list(range(max(1, int(interval)), int(draws) + 1, max(1, int(interval))))
    if not values or values[-1] != int(draws):
        values.append(int(draws))
    return values


def simulate_rejection_profiles(
    maxima: list[int],
    checkpoints: list[int],
    *,
    samples: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Sample cumulative masked-value rejection counts at checkpoints."""
    result = np.empty((int(samples), len(checkpoints)), dtype=np.int32)
    cumulative = np.zeros(int(samples), dtype=np.int32)
    checkpoint_index = 0
    for draw, maximum in enumerate(maxima, 1):
        probability = (int(maximum) + 1) / (1 << interval_bits(int(maximum)))
        cumulative += rng.geometric(probability, size=int(samples)).astype(np.int32) - 1
        if draw == checkpoints[checkpoint_index]:
            result[:, checkpoint_index] = cumulative
            checkpoint_index += 1
            if checkpoint_index == len(checkpoints):
                break
    return result


def simulate_rejection_profiles_with_gaps(
    maxima: list[int],
    checkpoints: list[int],
    *,
    samples: int,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    """Sample checkpoints and retain each exact per-draw rejection path.

    A selected corridor centre can consequently be tested as a fixed path,
    without replacing the simulated path by an arbitrary interpolation of its
    checkpoints.  uint16 is ample for these bounded draws, but the explicit
    guard keeps the representation exact rather than silently clipping an
    exceptionally long geometric tail.
    """
    sample_count = int(samples)
    profiles = np.empty((sample_count, len(checkpoints)), dtype=np.int32)
    gaps = np.empty((sample_count, len(maxima)), dtype=np.uint16)
    cumulative = np.zeros(sample_count, dtype=np.int32)
    checkpoint_index = 0
    for draw, maximum in enumerate(maxima, 1):
        probability = (int(maximum) + 1) / (1 << interval_bits(int(maximum)))
        current = rng.geometric(probability, size=sample_count).astype(np.int64) - 1
        if int(current.max(initial=0)) > np.iinfo(np.uint16).max:
            raise OverflowError("simulated rejection gap does not fit uint16")
        gaps[:, draw - 1] = current.astype(np.uint16)
        cumulative += current.astype(np.int32)
        if draw == checkpoints[checkpoint_index]:
            profiles[:, checkpoint_index] = cumulative
            checkpoint_index += 1
            if checkpoint_index == len(checkpoints):
                break
    return profiles, gaps


def rejection_path_sha256(gaps: list[int] | np.ndarray) -> str:
    encoded = ",".join(str(int(value)) for value in gaps).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def greedy_corridor_cover(
    profiles: np.ndarray,
    *,
    half_width: int,
    corridor_count: int,
    candidate_count: int,
    rng: np.random.Generator,
) -> tuple[list[int], list[float]]:
    """Greedily cover uncovered samples using candidate sample trajectories."""
    if profiles.ndim != 2 or len(profiles) == 0:
        raise ValueError("profiles must be a non-empty 2D array")
    uncovered = np.ones(len(profiles), dtype=bool)
    selected: list[int] = []
    coverage: list[float] = []
    median = np.median(profiles, axis=0)
    median_index = int(np.argmin(np.sum(np.square(profiles - median), axis=1)))
    for step in range(max(1, int(corridor_count))):
        uncovered_indices = np.flatnonzero(uncovered)
        if len(uncovered_indices) == 0:
            break
        if step == 0:
            candidates = np.array([median_index], dtype=np.int64)
        else:
            candidates = rng.choice(
                uncovered_indices,
                size=min(max(1, int(candidate_count)), len(uncovered_indices)),
                replace=False,
            )
        values = profiles[uncovered_indices]
        best_index = None
        best_hits = None
        best_count = -1
        for candidate in candidates:
            hits = (
                np.max(np.abs(values - profiles[int(candidate)]), axis=1)
                <= int(half_width)
            )
            count = int(hits.sum())
            if count > best_count:
                best_index = int(candidate)
                best_hits = hits
                best_count = count
        assert best_index is not None and best_hits is not None
        selected.append(best_index)
        uncovered[uncovered_indices[best_hits]] = False
        coverage.append(float(1.0 - uncovered.mean()))
    return selected, coverage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--include-sealed-shuffles", action="store_true")
    parser.add_argument("--checkpoint-interval", type=int, default=128)
    parser.add_argument("--half-width", type=int, default=80)
    parser.add_argument("--corridors", type=int, default=4)
    parser.add_argument("--samples", type=int, default=10_000)
    parser.add_argument("--candidate-count", type=int, default=128)
    parser.add_argument("--random-seed", type=int, default=20260929)
    parser.add_argument("--prelude", default="654,347,964")
    parser.add_argument(
        "--seed-method",
        choices=("integers-unique", "choice-without-replacement"),
        default="integers-unique",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_rejection_corridors.json"),
    )
    args = parser.parse_args()

    payload = json.loads(args.constraints.read_text(encoding="utf-8"))
    all_rows = [
        row
        for row in payload.get("rounds") or []
        if int(row.get("shuffle_size") or 0) > 1
    ]
    discovery_rows = [
        row
        for row in all_rows
        if row.get("seed_label_partition") == "discovery"
        and len(row.get("discovery_seed_label") or []) == 3
    ]
    rows = (
        all_rows[: max(1, args.rounds)]
        if args.include_sealed_shuffles
        else discovery_rows[: max(1, args.rounds)]
    )
    prelude = [int(value) for value in args.prelude.split(",") if value.strip()]
    maxima, _expected, _round_slices, _seed_slices = build_draw_layout(
        rows, prelude, args.seed_method
    )
    checkpoints = checkpoint_draws(len(maxima), args.checkpoint_interval)
    rng = np.random.default_rng(int(args.random_seed))
    profiles, rejection_gaps = simulate_rejection_profiles_with_gaps(
        maxima,
        checkpoints,
        samples=max(100, int(args.samples)),
        rng=rng,
    )
    indices, coverage = greedy_corridor_cover(
        profiles,
        half_width=max(0, int(args.half_width)),
        corridor_count=max(1, int(args.corridors)),
        candidate_count=max(1, int(args.candidate_count)),
        rng=rng,
    )
    corridors: list[dict[str, Any]] = []
    for rank, (index, covered) in enumerate(zip(indices, coverage), 1):
        center = profiles[index]
        bounds = {
            str(draw): [
                max(0, int(value) - max(0, int(args.half_width))),
                int(value) + max(0, int(args.half_width)),
            ]
            for draw, value in zip(checkpoints, center)
        }
        corridors.append(
            {
                "rank": rank,
                "sample_index": int(index),
                "cumulative_empirical_coverage": round(covered, 6),
                "center_final_rejections": int(center[-1]),
                "rejection_gaps": [int(value) for value in rejection_gaps[index]],
                "rejection_gaps_sha256": rejection_path_sha256(
                    rejection_gaps[index]
                ),
                "checkpoints": bounds,
                "solver_arguments": [
                    f"--rejection-checkpoint={draw}:{low}:{high}"
                    for draw, (low, high) in bounds.items()
                ],
            }
        )
    report = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "numpy-masked-rejection-corridor-plan",
        "summary": {
            "rounds_modelled": len(rows),
            "discovery_rounds_modelled": sum(
                row.get("seed_label_partition") == "discovery" for row in rows
            ),
            "sealed_shuffle_rounds_modelled": sum(
                row.get("seed_label_partition") != "discovery" for row in rows
            ),
            "sealed_seed_labels_opened": 0,
            "bounded_draws": len(maxima),
            "seed_method": args.seed_method,
            "checkpoint_interval": max(1, int(args.checkpoint_interval)),
            "checkpoint_count": len(checkpoints),
            "half_width": max(0, int(args.half_width)),
            "simulation_samples": len(profiles),
            "corridors": len(corridors),
            "empirical_union_coverage": (
                corridors[-1]["cumulative_empirical_coverage"] if corridors else 0.0
            ),
            "mean_final_rejections": round(float(profiles[:, -1].mean()), 6),
            "std_final_rejections": round(float(profiles[:, -1].std()), 6),
        },
        "corridors": corridors,
        "interpretation": (
            "Coverage is a Monte Carlo estimate for masked range rejections, not a proof. "
            "A SAT shard is only a compatibility witness; UNSAT rejects only that corridor."
        ),
        "safety": {
            "discovery_only_labels": True,
            "sealed_seed_labels_opened": False,
            "network_reads": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
