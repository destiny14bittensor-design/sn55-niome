#!/usr/bin/env python3
"""Offline score replay for robust versus 1/2/3-seed oracle submissions."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Callable, Sequence

from niome_subnet.genomics.submission_builder import build_submission
from tools.local_validator.artifacts import ArtifactBundle
from tools.local_validator.evaluator import evaluate_submission


def replay_oracles(
    artifacts: ArtifactBundle,
    robust_submission: list[dict[str, Any]],
    actual_seeds: Sequence[int],
    *,
    focused_variants_per_anchor: int = 128,
    optimizer_seconds: float = 8.0,
    builder: Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]] = build_submission,
    evaluator: Callable[..., Any] = evaluate_submission,
) -> dict[str, Any]:
    seeds = [int(value) for value in actual_seeds]
    if len(seeds) != 3 or len(set(seeds)) != 3:
        raise ValueError("exactly three distinct actual seeds are required")
    scoring_contract = dict(artifacts.contract)
    scoring_contract["seed"] = ",".join(str(value) for value in seeds)
    scoring_artifacts = replace(artifacts, contract=scoring_contract)

    def evaluate(label: str, submission: list[dict[str, Any]], started: float, **extra: Any) -> dict[str, Any]:
        result = evaluator(submission, scoring_artifacts)
        return {
            "scenario": label,
            "final_score": float(result.final_score),
            "per_seed_final_scores": [
                float(row["stage5"]["final_score"]) for row in result.per_seed
            ],
            "rows": len(submission),
            "elapsed_seconds": time.monotonic() - started,
            **extra,
        }

    started = time.monotonic()
    scenarios = [evaluate("robust-existing", robust_submission, started, optimization_seeds=[])]
    for count in (1, 2, 3):
        started = time.monotonic()
        submission, diagnostics = builder(
            contract=artifacts.contract,
            reference=artifacts.hbb_reference,
            chromosome_11=artifacts.chromosome_11,
            cell_types=artifacts.cell_types,
            selection_profile="seed-aware",
            round_seeds=seeds[:count],
            seed_focused_variants_per_anchor=focused_variants_per_anchor,
            seed_optimizer_time_budget_seconds=optimizer_seconds,
        )
        build_seconds = time.monotonic() - started
        scenarios.append(evaluate(
            f"oracle-{count}",
            submission,
            started,
            optimization_seeds=seeds[:count],
            build_seconds=build_seconds,
            focused_candidates_added=int(diagnostics.get("focused_candidates_added") or 0),
        ))
    baseline = scenarios[0]["final_score"]
    for row in scenarios:
        row["uplift_over_robust"] = row["final_score"] - baseline
        row["multiple_over_robust"] = (
            row["final_score"] / baseline if baseline else None
        )
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "mode": "offline-local-validator-partial-seed-oracle-replay",
        "safety": {"network_calls": False, "submission_writes": False},
        "actual_seeds": seeds,
        "settings": {
            "focused_variants_per_anchor": focused_variants_per_anchor,
            "optimizer_seconds": optimizer_seconds,
        },
        "scenarios": scenarios,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--actual-seeds", required=True)
    parser.add_argument("--chromosome", type=Path, default=Path("data/chr11.fa"))
    parser.add_argument("--focused-variants-per-anchor", type=int, default=128)
    parser.add_argument("--optimizer-seconds", type=float, default=8.0)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    task_dir = args.task_dir
    artifacts = ArtifactBundle.from_paths(
        contract_path=task_dir / "contract.json",
        hbb_reference_path=task_dir / "hbb_reference.json",
        chromosome_11_path=args.chromosome,
        cell_types_path=task_dir / "cell_types.json",
    )
    robust_submission = json.loads((task_dir / "submission.json").read_text(encoding="utf-8"))
    report = replay_oracles(
        artifacts,
        robust_submission,
        [int(value.strip()) for value in args.actual_seeds.split(",") if value.strip()],
        focused_variants_per_anchor=max(1, args.focused_variants_per_anchor),
        optimizer_seconds=max(0.0, args.optimizer_seconds),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "scenarios": report["scenarios"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
