"""Spawn-safe local candidate evaluator with a deliberately small import graph."""

from __future__ import annotations

from dataclasses import replace
import json
from typing import Any

from tools.local_validator.artifacts import ArtifactBundle, sha256_bytes
from tools.local_validator.evaluator import evaluate_submission


def evaluation_worker(
    candidates: list[tuple[str, list[dict[str, Any]]]],
    artifact_paths: dict[str, str],
    output_queue,
    evaluation_seeds: list[int] | None = None,
) -> None:
    """Score candidates in an isolated process so the parent can hard-stop it."""
    try:
        artifacts = ArtifactBundle.from_paths(
            contract_path=artifact_paths["contract_path"],
            hbb_reference_path=artifact_paths["hbb_reference_path"],
            chromosome_11_path=artifact_paths["chromosome_11_path"],
            cell_types_path=artifact_paths["cell_types_path"],
        )
        if evaluation_seeds:
            scoring_contract = dict(artifacts.contract)
            scoring_contract["seed"] = ",".join(
                str(seed) for seed in evaluation_seeds
            )
            artifacts = replace(artifacts, contract=scoring_contract)
        for index, (label, submission) in enumerate(candidates):
            try:
                raw = json.dumps(
                    submission, separators=(",", ":"), ensure_ascii=False
                ).encode()
                result = evaluate_submission(
                    submission,
                    artifacts,
                    raw_submission_bytes=raw,
                )
                output_queue.put(
                    {
                        "index": index,
                        "label": label,
                        "rows": len(submission),
                        "final_score": result.final_score,
                        "per_seed_final_scores": [
                            float(item["stage5"]["final_score"])
                            for item in result.per_seed
                        ],
                        "breakdown": result.breakdown,
                        "submission_sha256": sha256_bytes(raw),
                    }
                )
            except Exception as error:
                output_queue.put(
                    {
                        "index": index,
                        "label": label,
                        "rows": len(submission),
                        "error_type": type(error).__name__,
                        "error": str(error),
                    }
                )
        output_queue.put({"done": True})
    except Exception as error:
        output_queue.put(
            {
                "worker_error_type": type(error).__name__,
                "worker_error": str(error),
                "done": True,
            }
        )
