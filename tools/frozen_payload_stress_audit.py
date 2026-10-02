#!/usr/bin/env python3
"""Compare frozen NIOME payloads on paired, synthetic unknown-seed ensembles."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from niome_subnet.genomics.seed_policy import deterministic_stress_seeds
from niome_subnet.miner.portfolio_audit import jaccard, pearson
from tools.local_validator.artifacts import ArtifactBundle, read_first_fasta, sha256_bytes
from tools.local_validator.evaluator import evaluate_submission


def parse_submission(value: str) -> tuple[str, Path]:
    label, separator, path = value.partition("=")
    if not separator or not label.strip() or not path.strip():
        raise argparse.ArgumentTypeError("--submission must be LABEL=PATH")
    return label.strip(), Path(path.strip())


def parse_order_variant(value: str) -> tuple[str, str, str]:
    label, separator, source_and_salt = value.partition("=")
    source, salt_separator, salt = source_and_salt.partition(":")
    if (
        not separator
        or not salt_separator
        or not label.strip()
        or not source.strip()
        or not salt.strip()
    ):
        raise argparse.ArgumentTypeError(
            "--order-variant must be LABEL=SOURCE_LABEL:SALT"
        )
    return label.strip(), source.strip(), salt.strip()


def salted_order(rows: list[dict[str, Any]], salt: str) -> list[dict[str, Any]]:
    """Return the same rows in a deterministic, domain-separated order."""
    return sorted(
        rows,
        key=lambda item: (
            hashlib.sha256(
                f"{salt}|{item.get('experiment_id', '')}".encode()
            ).digest(),
            str(item.get("experiment_id", "")),
        ),
    )


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def submission_summary(observations: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [float(item["score"]) for item in observations]
    consistencies = [float(item["consistency"]) for item in observations]
    return {
        "ensembles": len(observations),
        "score_minimum": min(scores) if scores else None,
        "score_p25": percentile(scores, 0.25),
        "score_median": statistics.median(scores) if scores else None,
        "score_maximum": max(scores) if scores else None,
        "consistency_minimum": min(consistencies) if consistencies else None,
        "consistency_p25": percentile(consistencies, 0.25),
        "consistency_median": (
            statistics.median(consistencies) if consistencies else None
        ),
        "all_valid": all(item["invalid_experiments"] == 0 for item in observations),
    }


def paired_summary(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> dict[str, Any]:
    if len(left) != len(right):
        raise ValueError("paired observations must have equal length")
    left_scores = [float(item["score"]) for item in left]
    right_scores = [float(item["score"]) for item in right]
    deltas = [right_score - left_score for left_score, right_score in zip(left_scores, right_scores)]
    return {
        "ensembles": len(deltas),
        "left_wins": sum(delta < 0.0 for delta in deltas),
        "right_wins": sum(delta > 0.0 for delta in deltas),
        "ties": sum(delta == 0.0 for delta in deltas),
        "right_win_rate": (
            sum(delta > 0.0 for delta in deltas) / len(deltas)
            if deltas
            else None
        ),
        "right_minus_left_minimum": min(deltas) if deltas else None,
        "right_minus_left_median": statistics.median(deltas) if deltas else None,
        "right_minus_left_maximum": max(deltas) if deltas else None,
        "score_correlation": pearson(left_scores, right_scores),
    }


def best_of_pair_summary(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> dict[str, Any]:
    if len(left) != len(right):
        raise ValueError("paired observations must have equal length")
    best = [
        max((left_item, right_item), key=lambda item: float(item["score"]))
        for left_item, right_item in zip(left, right)
    ]
    return submission_summary(best)


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_default(value: Any) -> Any:
    item = getattr(value, "item", None)
    if callable(item):
        return item()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument(
        "--submission",
        action="append",
        type=parse_submission,
        required=True,
        help="repeat LABEL=PATH for each frozen payload",
    )
    parser.add_argument(
        "--order-variant",
        action="append",
        type=parse_order_variant,
        default=[],
        help="repeat LABEL=SOURCE_LABEL:SALT for deterministic row-order variants",
    )
    parser.add_argument("--ensembles", type=int, default=8)
    parser.add_argument("--ensemble-offset", type=int, default=0)
    parser.add_argument("--seeds-per-ensemble", type=int, default=3)
    parser.add_argument("--domain", default="top30-frozen-payload-v1")
    parser.add_argument("--chromosome", type=Path, default=ROOT / "data" / "chr11.fa")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if (
        args.ensembles < 1
        or args.seeds_per_ensemble < 1
        or args.ensemble_offset < 0
    ):
        parser.error(
            "--ensembles and --seeds-per-ensemble must be positive and "
            "--ensemble-offset must be non-negative"
        )
    submissions = dict(args.submission)
    if len(submissions) != len(args.submission):
        parser.error("submission labels must be unique")
    variant_labels = [label for label, _, _ in args.order_variant]
    if len(set(variant_labels)) != len(variant_labels):
        parser.error("order-variant labels must be unique")
    if set(variant_labels) & set(submissions):
        parser.error("submission and order-variant labels must be distinct")

    contract = _json(args.task_root / "contract.json")
    reference = _json(args.task_root / "hbb_reference.json")
    cell_types = _json(args.task_root / "cell_types.json")
    chromosome, _ = read_first_fasta(args.chromosome)
    task_id = args.task_root.name

    rows: dict[str, list[dict[str, Any]]] = {}
    identities: dict[str, set[str]] = {}
    digests: dict[str, str] = {}
    for label, path in submissions.items():
        raw = path.read_bytes()
        value = json.loads(raw)
        if not isinstance(value, list):
            parser.error(f"submission {label} must be a JSON list")
        identifiers = {
            str(item["experiment_id"])
            for item in value
            if isinstance(item, dict) and item.get("experiment_id") is not None
        }
        if len(identifiers) != len(value):
            parser.error(f"submission {label} requires unique experiment_id values")
        rows[label] = value
        identities[label] = identifiers
        digests[label] = sha256_bytes(raw)

    order_variants = {}
    for label, source, salt in args.order_variant:
        if source not in rows:
            parser.error(f"order-variant source {source!r} is not a submission label")
        value = salted_order(rows[source], salt)
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        rows[label] = value
        identities[label] = set(identities[source])
        digests[label] = sha256_bytes(raw)
        order_variants[label] = {"source": source, "salt": salt}
    if len(rows) < 2:
        parser.error("at least two submissions or order variants are required")

    observations: dict[str, list[dict[str, Any]]] = {
        label: [] for label in rows
    }
    ensembles = []
    for local_index in range(args.ensembles):
        index = args.ensemble_offset + local_index
        seeds = deterministic_stress_seeds(
            task_id,
            domain=f"{args.domain}|ensemble-{index}",
            count=args.seeds_per_ensemble,
        )
        stress_contract = dict(contract)
        stress_contract["seed"] = ",".join(str(seed) for seed in seeds)
        artifacts = ArtifactBundle(
            contract=stress_contract,
            hbb_reference=reference,
            chromosome_11=chromosome,
            cell_types=cell_types,
            manifest={"source": "frozen-payload-stress-audit"},
        )
        ensemble_results = {}
        for label in rows:
            result = evaluate_submission(rows[label], artifacts)
            breakdown = result.breakdown
            observation = {
                "score": result.final_score,
                "weighted_score": breakdown["total_weighted_score"],
                "consistency": breakdown["consistency_score"],
                "fidelity": breakdown["distribution_fidelity_factor"],
                "invalid_experiments": len(result.invalid_experiments),
            }
            observations[label].append(observation)
            ensemble_results[label] = observation
        ensembles.append({"index": index, "seeds": seeds, "results": ensemble_results})
        print(
            f"evaluated ensemble {local_index + 1}/{args.ensembles} "
            f"(global {index})",
            flush=True,
        )

    labels = list(rows)
    pairs = []
    for left_index, left in enumerate(labels):
        for right in labels[left_index + 1 :]:
            pairs.append(
                {
                    "left": left,
                    "right": right,
                    "payload_jaccard": jaccard(identities[left], identities[right]),
                    **paired_summary(observations[left], observations[right]),
                    "best_of_pair": best_of_pair_summary(
                        observations[left], observations[right]
                    ),
                }
            )
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "frozen payloads evaluated on paired deterministic synthetic seed triples",
        "task_id": task_id,
        "domain": args.domain,
        "ensemble_offset": args.ensemble_offset,
        "seeds_per_ensemble": args.seeds_per_ensemble,
        "submission_sha256": digests,
        "order_variants": order_variants,
        "summaries": {
            label: submission_summary(values)
            for label, values in observations.items()
        },
        "pairs": pairs,
        "ensembles": ensembles,
        "safety": {
            "uses_published_task_score": False,
            "uses_published_task_seed": False,
            "writes_submission": False,
        },
    }
    encoded = json.dumps(report, indent=2, default=_json_default) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"summaries": report["summaries"], "pairs": pairs}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
