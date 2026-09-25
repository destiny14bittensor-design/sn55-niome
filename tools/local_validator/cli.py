"""Command-line interface for local evaluation and weight simulation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .artifacts import ArtifactBundle
from .evaluator import evaluate_submission
from .ingestion import load_submission
from .weights import simulate_weights


def _write_json(value: Any, output: str | None) -> None:
    rendered = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=True)
    if output:
        Path(output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


def _evaluate(args: argparse.Namespace) -> int:
    artifacts = ArtifactBundle.from_paths(
        contract_path=args.contract,
        hbb_reference_path=args.hbb_reference,
        chromosome_11_path=args.chromosome_11,
        cell_types_path=args.cell_types,
    )
    submission, raw = load_submission(args.submission)
    result = evaluate_submission(
        submission,
        artifacts,
        uid=args.uid,
        raw_submission_bytes=raw,
    )
    _write_json(result.as_dict(), args.output)
    return 0


def _weights(args: argparse.Namespace) -> int:
    scores = json.loads(Path(args.scores).read_text(encoding="utf-8"))
    if isinstance(scores, dict):
        max_uid = max(int(uid) for uid in scores)
        dense = [0.0] * (max_uid + 1)
        for uid, score in scores.items():
            dense[int(uid)] = score
        scores = dense
    result = simulate_weights(
        scores,
        owner_uid=args.owner_uid,
        burning_rate=args.burning_rate,
        min_allowed_weights=args.min_allowed_weights,
        max_weight_limit=args.max_weight_limit,
    )
    _write_json(result, args.output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SN55 local validator clone")
    subcommands = parser.add_subparsers(dest="command", required=True)
    evaluate = subcommands.add_parser("evaluate", help="Run Stage 1-5 locally")
    evaluate.add_argument("--submission", required=True)
    evaluate.add_argument("--contract", required=True)
    evaluate.add_argument("--hbb-reference", required=True)
    evaluate.add_argument("--chromosome-11", required=True)
    evaluate.add_argument("--cell-types", required=True)
    evaluate.add_argument("--uid", type=int, default=0)
    evaluate.add_argument("--output")
    evaluate.set_defaults(handler=_evaluate)

    weights = subcommands.add_parser("weights", help="Simulate score-to-weight conversion")
    weights.add_argument("--scores", required=True, help="JSON list or UID-to-score object")
    weights.add_argument("--owner-uid", required=True, type=int)
    weights.add_argument("--burning-rate", type=float, default=0.02)
    weights.add_argument("--min-allowed-weights", type=int, default=1)
    weights.add_argument("--max-weight-limit", type=float, default=65_535)
    weights.add_argument("--output")
    weights.set_defaults(handler=_weights)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)
