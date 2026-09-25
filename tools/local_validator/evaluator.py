"""End-to-end side-effect-free SN55 local validator runner."""

from __future__ import annotations

from dataclasses import dataclass
import platform
from typing import Any

import numpy as np
import pandas as pd
import sklearn

from .artifacts import ArtifactBundle
from .ingestion import IngestionResult, ingest_submission
from .stage12 import run_stage12
from .stage3 import experiment_seed, run_stage3
from .stage4 import run_stage4
from .stage5 import run_stage5


def parse_seeds(raw: Any) -> list[int]:
    return [int(value) for value in str(raw).split(",") if value.strip() != ""]


@dataclass(frozen=True)
class EvaluationResult:
    uid: int
    final_score: float
    breakdown: dict[str, Any]
    ingestion: dict[str, Any]
    valid_experiments: list[dict[str, Any]]
    invalid_experiments: list[dict[str, Any]]
    per_seed: list[dict[str, Any]]
    provenance: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "final_score": self.final_score,
            "breakdown": self.breakdown,
            "ingestion": self.ingestion,
            "valid_experiments": self.valid_experiments,
            "invalid_experiments": self.invalid_experiments,
            "per_seed": self.per_seed,
            "provenance": self.provenance,
        }


def evaluate_submission(
    submission: Any,
    artifacts: ArtifactBundle,
    *,
    uid: int = 0,
    raw_submission_bytes: bytes | None = None,
) -> EvaluationResult:
    """Run the public validator pipeline without writing to S3/backend/chain."""
    ingestion: IngestionResult = ingest_submission(
        submission,
        artifacts.contract,
        raw_bytes=raw_submission_bytes,
    )
    valid, invalid, stage12_provenance = run_stage12(
        ingestion.retained,
        contract=artifacts.contract,
        reference=artifacts.hbb_reference,
        chromosome_11=artifacts.chromosome_11,
        cell_types=artifacts.cell_types,
    )
    seeds = parse_seeds(artifacts.contract["seed"])
    per_seed = []
    for seed in seeds:
        stage3_results, stage3_summary = run_stage3(valid, seed)
        stage4 = run_stage4(valid, stage3_results, seed=seed)
        stage5 = run_stage5(
            valid,
            stage3_results,
            stage4,
            artifacts.contract,
        )
        per_seed.append(
            {
                "seed": seed,
                "stage3_results": stage3_results,
                "stage3_summary": stage3_summary,
                "experiment_seeds": {
                    item["experiment"]["experiment_id"]: experiment_seed(
                        seed, item["experiment"]
                    )
                    for item in valid
                },
                "stage4": stage4,
                "stage5": stage5,
            }
        )

    def average(key: str) -> float:
        # Deliberately retains the production empty-seed ZeroDivisionError.
        return sum(item["stage5"][key] for item in per_seed) / len(per_seed)

    breakdown = {
        "n_valid_experiments": int(round(average("n_valid_experiments"))),
        "total_weighted_score": average("total_weighted_score"),
        "consistency_score": average("consistency_score"),
        "consistency_factor": average("consistency_factor"),
        "distribution_fidelity_score": average("distribution_fidelity_score"),
        "distribution_fidelity_factor": average("distribution_fidelity_factor"),
    }
    provenance = {
        "artifact_manifest": artifacts.manifest,
        "runtime": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "scikit_learn": sklearn.__version__,
        },
        "stage12": stage12_provenance,
        "seeds": seeds,
        "clone_mode": "public-code-compatible-no-network-side-effects",
    }
    return EvaluationResult(
        uid=uid,
        final_score=average("final_score"),
        breakdown=breakdown,
        ingestion=ingestion.as_dict(),
        valid_experiments=valid,
        invalid_experiments=invalid,
        per_seed=per_seed,
        provenance=provenance,
    )
