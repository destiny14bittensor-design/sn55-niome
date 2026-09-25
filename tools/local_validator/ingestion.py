"""Validator-compatible JSON ingestion and pre-Stage-1 truncation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .artifacts import sha256_bytes


@dataclass(frozen=True)
class IngestionResult:
    retained: list[dict[str, Any]]
    dropped: list[dict[str, Any]]
    input_sha256: str
    retained_sha256: str
    input_count: int
    retained_count: int
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_sha256": self.input_sha256,
            "retained_sha256": self.retained_sha256,
            "input_count": self.input_count,
            "retained_count": self.retained_count,
            "truncated": self.truncated,
            "dropped": self.dropped,
        }


def load_submission(path: str | Path) -> tuple[Any, bytes]:
    raw = Path(path).read_bytes()
    return json.loads(raw), raw


def ingest_submission(
    submission: Any,
    contract: dict[str, Any],
    *,
    raw_bytes: bytes | None = None,
) -> IngestionResult:
    """Mirror ``truncate_submission`` while retaining an audit-only drop trace.

    Deliberately no top-level or row schema check is added. A dict top level or a
    non-object row raises at ``exp.get`` just like the production validator.
    """
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    max_experiments = contract["rules"].get("max_experiments")

    for index, exp in enumerate(submission):
        if max_experiments is not None and len(kept) >= max_experiments:
            dropped.extend(
                {"index": remaining, "reason": "max_experiments"}
                for remaining in range(index, len(submission))
            )
            break
        experiment_id = exp.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id.strip():
            dropped.append({"index": index, "reason": "invalid_experiment_id"})
            continue
        if experiment_id in seen_ids:
            dropped.append({"index": index, "reason": "duplicate_experiment_id"})
            continue
        seen_ids.add(experiment_id)
        kept.append(exp)

    canonical_input = raw_bytes
    if canonical_input is None:
        canonical_input = json.dumps(submission, separators=(",", ":"), ensure_ascii=False).encode()
    retained_raw = json.dumps(kept, separators=(",", ":"), ensure_ascii=False).encode()
    return IngestionResult(
        retained=kept,
        dropped=dropped,
        input_sha256=sha256_bytes(canonical_input),
        retained_sha256=sha256_bytes(retained_raw),
        input_count=len(submission),
        retained_count=len(kept),
        truncated=len(kept) != len(submission),
    )
