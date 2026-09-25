"""Capture, solve, upload, and locally evaluate a live NIOME task."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import math
import multiprocessing as mp
from pathlib import Path
import queue
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from niome_subnet.genomics.model import Task
from niome_subnet.genomics.submission_builder import build_submission
from niome_subnet.utils.settings import CELL_TYPES_URL, CHR11_PATH
from tools.local_validator.artifacts import ArtifactBundle, sha256_bytes
from tools.local_validator.evaluator import evaluate_submission


logger = logging.getLogger(__name__)

DEFAULT_URL_TTL_SECONDS = 300.0
UPLOAD_START_TARGET_SECONDS = 240.0
UPLOAD_SAFETY_MARGIN_SECONDS = 60.0
DOWNLOAD_STAGE_BUDGET_SECONDS = 90.0
DOWNLOAD_STAGE_FRACTION = 0.35
CANDIDATE_BUILD_BUDGET_SECONDS = 150.0
CANDIDATE_BUILD_FRACTION = 0.60
BUILDER_UPLOAD_RESERVE_SECONDS = 15.0
MAX_DOWNLOAD_TIMEOUT_SECONDS = 60.0
MAX_UPLOAD_ATTEMPTS = 3
MAX_UPLOAD_ATTEMPT_SECONDS = 60.0
MAX_SCORING_CANDIDATES = 5


class DeadlineExceeded(RuntimeError):
    """Raised when the live-task upload URL can no longer be used safely."""


def _presigned_url_deadline(
    url: str,
    *,
    now_wall: float | None = None,
    now_monotonic: float | None = None,
) -> tuple[float, str]:
    """Return the URL expiry as a monotonic deadline.

    AWS presigned URLs carry their exact issue time and lifetime. Fall back to
    the subnet's documented 300-second lifetime for non-AWS-compatible URLs.
    """
    wall = time.time() if now_wall is None else now_wall
    monotonic = time.monotonic() if now_monotonic is None else now_monotonic
    query = {
        key.lower(): values
        for key, values in parse_qs(urlparse(url).query).items()
    }
    issued_values = query.get("x-amz-date")
    expires_values = query.get("x-amz-expires")
    if issued_values and expires_values:
        try:
            issued = datetime.strptime(
                issued_values[0], "%Y%m%dT%H%M%SZ"
            ).replace(tzinfo=timezone.utc)
            expiry_wall = issued.timestamp() + float(expires_values[0])
            return monotonic + (expiry_wall - wall), "aws-query"
        except (TypeError, ValueError, OverflowError):
            pass
    legacy_expires_values = query.get("expires")
    if legacy_expires_values:
        try:
            expiry_wall = float(legacy_expires_values[0])
            return monotonic + (expiry_wall - wall), "aws-query-v2"
        except (TypeError, ValueError, OverflowError):
            pass
    return monotonic + DEFAULT_URL_TTL_SECONDS, "fallback-receipt"


def _remaining_seconds(deadline_monotonic: float) -> float:
    return deadline_monotonic - time.monotonic()


def _bounded_timeout(deadline_monotonic: float, cap: float) -> float:
    remaining = _remaining_seconds(deadline_monotonic)
    if remaining <= 0:
        raise DeadlineExceeded("live-task deadline expired")
    return max(0.1, min(cap, remaining))


def _download(url: str, *, deadline_monotonic: float) -> bytes:
    response = requests.get(
        url,
        timeout=_bounded_timeout(
            deadline_monotonic, MAX_DOWNLOAD_TIMEOUT_SECONDS
        ),
    )
    response.raise_for_status()
    return response.content


def _upload_with_deadline(
    url: str,
    submission_raw: bytes,
    *,
    deadline_monotonic: float,
) -> tuple[requests.Response, int]:
    """Upload with bounded retries that can never run past URL expiry."""
    last_error: Exception | None = None
    for attempt in range(1, MAX_UPLOAD_ATTEMPTS + 1):
        try:
            response = requests.put(
                url,
                data=submission_raw,
                timeout=_bounded_timeout(
                    deadline_monotonic, MAX_UPLOAD_ATTEMPT_SECONDS
                ),
            )
            response.raise_for_status()
            return response, attempt
        except (requests.RequestException, DeadlineExceeded) as error:
            last_error = error
            remaining = _remaining_seconds(deadline_monotonic)
            if attempt >= MAX_UPLOAD_ATTEMPTS or remaining <= 2.0:
                raise
            time.sleep(min(2 ** (attempt - 1), max(0.0, remaining - 1.0)))
    raise RuntimeError("upload attempts exhausted") from last_error


def _candidate_variants(
    baseline: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Create deterministic dataset-level alternatives from the safe baseline."""
    variants: list[tuple[str, list[dict[str, Any]]]] = [("balanced-full", baseline)]
    if not baseline:
        return variants

    for label, ratio in (
        ("balanced-85pct", 0.85),
        ("balanced-70pct", 0.70),
        ("balanced-50pct", 0.50),
    ):
        count = max(1, int(round(len(baseline) * ratio)))
        if count < len(baseline):
            variants.append((label, baseline[:count]))

    unique_guides: list[dict[str, Any]] = []
    seen_guides: set[str] = set()
    for experiment in baseline:
        guide = str(experiment.get("guideRNA", ""))
        if guide in seen_guides:
            continue
        seen_guides.add(guide)
        unique_guides.append(experiment)
    if len(unique_guides) < len(baseline):
        variants.append(("distinct-guides", unique_guides))

    deduplicated: list[tuple[str, list[dict[str, Any]]]] = []
    seen_payloads: set[str] = set()
    for label, candidate in variants:
        payload = json.dumps(
            candidate, separators=(",", ":"), ensure_ascii=False
        )
        digest = sha256_bytes(payload.encode())
        if digest in seen_payloads:
            continue
        seen_payloads.add(digest)
        deduplicated.append((label, candidate))
    return deduplicated[:MAX_SCORING_CANDIDATES]


def _evaluation_worker(
    candidates: list[tuple[str, list[dict[str, Any]]]],
    artifact_paths: dict[str, str],
    output_queue,
) -> None:
    """Score candidates in an isolated process so the parent can hard-stop it."""
    try:
        artifacts = ArtifactBundle.from_paths(
            contract_path=artifact_paths["contract_path"],
            hbb_reference_path=artifact_paths["hbb_reference_path"],
            chromosome_11_path=artifact_paths["chromosome_11_path"],
            cell_types_path=artifact_paths["cell_types_path"],
        )
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


def _score_candidates_until_deadline(
    candidates: list[tuple[str, list[dict[str, Any]]]],
    artifact_paths: dict[str, str],
    *,
    deadline_monotonic: float,
) -> tuple[list[dict[str, Any]], bool]:
    """Return completed scores and terminate scoring at the hard deadline."""
    if _remaining_seconds(deadline_monotonic) <= 0:
        return [], True

    context = mp.get_context("spawn")
    output_queue = context.Queue()
    process = context.Process(
        target=_evaluation_worker,
        args=(candidates, artifact_paths, output_queue),
        daemon=True,
    )
    results: list[dict[str, Any]] = []
    completed = False
    process.start()
    try:
        while _remaining_seconds(deadline_monotonic) > 0:
            try:
                message = output_queue.get(
                    timeout=min(0.5, _remaining_seconds(deadline_monotonic))
                )
            except queue.Empty:
                if not process.is_alive():
                    break
                continue
            if message.get("done"):
                if message.get("worker_error"):
                    results.append(message)
                completed = True
                break
            results.append(message)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
        else:
            process.join(timeout=1)
        output_queue.close()
        output_queue.join_thread()
    return results, not completed


def _safe_task_id(task_id: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", task_id).strip("._")
    return sanitized or sha256_bytes(task_id.encode())[:16]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=True) + "\n",
        encoding="utf-8",
    )


def persist_task_envelope(
    *,
    task: Task,
    presigned_url: str,
    caller_hotkey: str,
    artifact_root: str | Path = "artifacts/live",
) -> Path:
    """Persist the complete live request before acknowledging the validator.

    The upload URL is short lived, but it is the one irreplaceable part of a
    request after a process restart.  Keeping it in a mode-0600 envelope lets a
    restarted miner resume while the URL is still valid.  The regular task.json
    remains credential-free and suitable for long-term diagnostics.
    """
    task_dir = Path(artifact_root) / _safe_task_id(task.id)
    task_dir.mkdir(parents=True, exist_ok=True)
    envelope_path = task_dir / "request_envelope.json"
    _write_json(
        envelope_path,
        {
            "task": task.model_dump(),
            "presigned_url": presigned_url,
            "caller_hotkey": caller_hotkey,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    envelope_path.chmod(0o600)
    return envelope_path


def pending_task_envelopes(
    artifact_root: str | Path = "artifacts/live",
) -> list[tuple[Task, str, str]]:
    """Return resumable requests whose upload URLs have not expired."""
    root = Path(artifact_root)
    if not root.exists():
        return []
    pending: list[tuple[Task, str, str]] = []
    for envelope_path in sorted(root.glob("*/request_envelope.json")):
        try:
            envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
            task = Task.model_validate(envelope["task"])
            presigned_url = str(envelope["presigned_url"])
            caller_hotkey = str(envelope["caller_hotkey"])
            status_path = envelope_path.with_name("status.json")
            status = (
                json.loads(status_path.read_text(encoding="utf-8"))
                if status_path.exists()
                else {}
            )
            if status.get("state") == "complete":
                continue
            url_deadline, _ = _presigned_url_deadline(presigned_url)
            if _remaining_seconds(url_deadline) <= UPLOAD_SAFETY_MARGIN_SECONDS:
                continue
            pending.append((task, presigned_url, caller_hotkey))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            logger.exception("Ignoring invalid live-task envelope %s", envelope_path)
    return pending


def process_live_task(
    *,
    task: Task,
    presigned_url: str,
    caller_hotkey: str,
    artifact_root: str | Path = "artifacts/live",
    round_seeds: list[int] | None = None,
) -> dict[str, Any]:
    persist_task_envelope(
        task=task,
        presigned_url=presigned_url,
        caller_hotkey=caller_hotkey,
        artifact_root=artifact_root,
    )
    received_at = datetime.now(timezone.utc).isoformat()
    received_monotonic = time.monotonic()
    url_deadline, deadline_source = _presigned_url_deadline(
        presigned_url,
        now_monotonic=received_monotonic,
    )
    url_seconds_remaining = _remaining_seconds(url_deadline)
    upload_start_budget = min(
        UPLOAD_START_TARGET_SECONDS,
        url_seconds_remaining - UPLOAD_SAFETY_MARGIN_SECONDS,
    )
    upload_start_deadline = received_monotonic + upload_start_budget
    download_deadline = received_monotonic + min(
        DOWNLOAD_STAGE_BUDGET_SECONDS,
        max(1.0, upload_start_budget * DOWNLOAD_STAGE_FRACTION),
    )
    builder_deadline = max(
        download_deadline,
        received_monotonic
        + min(
            CANDIDATE_BUILD_BUDGET_SECONDS,
            max(1.0, upload_start_budget * CANDIDATE_BUILD_FRACTION),
        ),
    )
    scoring_deadline = max(
        builder_deadline,
        upload_start_deadline - BUILDER_UPLOAD_RESERVE_SECONDS,
    )
    task_dir = Path(artifact_root) / _safe_task_id(task.id)
    task_dir.mkdir(parents=True, exist_ok=True)
    status_path = task_dir / "status.json"
    status = {
        "task_id": task.id,
        "caller_hotkey": caller_hotkey,
        "received_at": received_at,
        "state": "capturing",
        "deadline_source": deadline_source,
        "url_seconds_remaining_at_receipt": url_seconds_remaining,
        "upload_start_budget_seconds": upload_start_budget,
    }
    _write_json(status_path, status)

    try:
        if upload_start_budget <= 0:
            raise DeadlineExceeded(
                f"presigned upload URL has only {url_seconds_remaining:.3f}s remaining"
            )
        contract_raw = _download(
            task.contract_url, deadline_monotonic=download_deadline
        )
        reference_raw = _download(
            task.hbb_ref_url, deadline_monotonic=download_deadline
        )
        cell_types_raw = _download(
            CELL_TYPES_URL, deadline_monotonic=download_deadline
        )
        contract = json.loads(contract_raw)
        reference = json.loads(reference_raw)
        cell_types = json.loads(cell_types_raw)
        contract_path = task_dir / "contract.json"
        reference_path = task_dir / "hbb_reference.json"
        cell_types_path = task_dir / "cell_types.json"
        contract_path.write_bytes(contract_raw)
        reference_path.write_bytes(reference_raw)
        cell_types_path.write_bytes(cell_types_raw)
        _write_json(
            task_dir / "task.json",
            {
                "id": task.id,
                "contract_url": task.contract_url,
                "hbb_ref_url": task.hbb_ref_url,
                "caller_hotkey": caller_hotkey,
                "received_at": received_at,
            },
        )

        chromosome_path = Path(CHR11_PATH)
        if not chromosome_path.exists():
            raise FileNotFoundError(
                f"Missing {chromosome_path}; run scripts/run_validator.sh once or download Ensembl chr11"
            )
        artifacts = ArtifactBundle.from_paths(
            contract_path=contract_path,
            hbb_reference_path=reference_path,
            chromosome_11_path=chromosome_path,
            cell_types_path=cell_types_path,
        )
        submission, builder_diagnostics = build_submission(
            contract=contract,
            reference=reference,
            chromosome_11=artifacts.chromosome_11,
            cell_types=cell_types,
            deadline_monotonic=builder_deadline,
            selection_profile="seed-aware" if round_seeds else "ranked",
            round_seeds=round_seeds,
        )
        candidates = (
            [("seed-aware-full", submission)]
            if round_seeds
            else _candidate_variants(submission)
        )
        artifact_paths = {
            "contract_path": str(contract_path.resolve()),
            "hbb_reference_path": str(reference_path.resolve()),
            "chromosome_11_path": str(chromosome_path.resolve()),
            "cell_types_path": str(cell_types_path.resolve()),
        }
        if round_seeds:
            candidate_scores, scoring_deadline_reached = [], False
        else:
            candidate_scores, scoring_deadline_reached = (
                _score_candidates_until_deadline(
                    candidates,
                    artifact_paths,
                    deadline_monotonic=scoring_deadline,
                )
            )
        successful_scores = [
            item
            for item in candidate_scores
            if "final_score" in item
            and item.get("index") is not None
            and math.isfinite(float(item["final_score"]))
        ]
        selected_index = 0
        if successful_scores:
            selected_index = max(
                successful_scores,
                key=lambda item: (
                    float(item["final_score"]),
                    int(item.get("rows", 0)),
                    -int(item["index"]),
                ),
            )["index"]
        selected_label, submission = candidates[selected_index]
        scoring_diagnostics = {
            "candidate_count": len(candidates),
            "completed_candidate_count": len(successful_scores),
            "deadline_reached": scoring_deadline_reached,
            "selected_index": selected_index,
            "selected_label": selected_label,
            "fallback_to_unscored_baseline": not successful_scores,
            "results": candidate_scores,
        }
        _write_json(task_dir / "candidate_scores.json", scoring_diagnostics)
        builder_diagnostics.update(
            {
                "candidate_count": len(candidates),
                "selected_candidate": selected_label,
                "local_scoring_deadline_reached": scoring_deadline_reached,
            }
        )
        submission_raw = json.dumps(
            submission, separators=(",", ":"), ensure_ascii=False
        ).encode()
        submission_path = task_dir / "submission.json"
        submission_path.write_bytes(submission_raw)
        _write_json(task_dir / "builder_diagnostics.json", builder_diagnostics)

        upload_started_monotonic = time.monotonic()
        upload_start_elapsed = upload_started_monotonic - received_monotonic
        if upload_started_monotonic >= url_deadline:
            raise DeadlineExceeded("presigned upload URL expired before upload")
        status.update(
            {
                "state": "uploading",
                "submission_rows": len(submission),
                "submission_sha256": sha256_bytes(submission_raw),
                "upload_start_elapsed_seconds": upload_start_elapsed,
                "upload_start_target_missed": (
                    upload_started_monotonic > upload_start_deadline
                ),
            }
        )
        _write_json(status_path, status)
        upload, upload_attempts = _upload_with_deadline(
            presigned_url,
            submission_raw,
            deadline_monotonic=url_deadline,
        )
        uploaded_at = datetime.now(timezone.utc).isoformat()

        status.update(
            {
                "state": "local_validation",
                "uploaded_at": uploaded_at,
                "upload_attempts": upload_attempts,
                "upload_elapsed_seconds": time.monotonic() - received_monotonic,
            }
        )
        _write_json(status_path, status)
        evaluation_artifacts = artifacts
        if round_seeds:
            scoring_contract = dict(contract)
            scoring_contract["seed"] = ",".join(str(seed) for seed in round_seeds)
            evaluation_artifacts = replace(artifacts, contract=scoring_contract)
        local_result = evaluate_submission(
            submission,
            evaluation_artifacts,
            raw_submission_bytes=submission_raw,
        )
        _write_json(task_dir / "local_validation.json", local_result.as_dict())
        manifest = {
            "task_id": task.id,
            "received_at": received_at,
            "uploaded_at": uploaded_at,
            "files": {
                "contract.json": sha256_bytes(contract_raw),
                "hbb_reference.json": sha256_bytes(reference_raw),
                "cell_types.json": sha256_bytes(cell_types_raw),
                "submission.json": sha256_bytes(submission_raw),
                "chr11.fa": artifacts.manifest["files"]["chromosome_11"]["sha256"],
            },
        }
        _write_json(task_dir / "manifest.json", manifest)
        status.update(
            {
                "state": "complete",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "local_final_score": local_result.final_score,
                "local_breakdown": local_result.breakdown,
            }
        )
        _write_json(status_path, status)
        logger.info(
            "Completed task %s: rows=%d local_score=%s artifact_dir=%s",
            task.id,
            len(submission),
            local_result.final_score,
            task_dir,
        )
        return status
    except Exception as error:
        status.update(
            {
                "state": "failed",
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
        _write_json(status_path, status)
        logger.exception("Live task processing failed for task %s", task.id)
        raise
