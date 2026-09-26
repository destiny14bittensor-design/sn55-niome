"""Keep a live S3 PUT open until NIOME's chain-derived round seeds exist.

The validator's ordinary miner process remains the reliability path: it writes a
baseline object promptly.  This sidecar opens several PUT profiles while the
presigned URL is valid and streams JSON whitespace to keep them alive.  The
authoritative seeds are derived from finalized round block hashes exactly like
the validator; the task contract seed is retained only as forensic telemetry.
S3 object replacement is atomic, so failed or cancelled bridges leave the
already completed baseline object in place.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import http.client
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

# Direct execution (including PM2) puts ``tools/`` rather than the repository
# root on sys.path.  Keep imports identical to ``python -m tools...``.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import bittensor as bt

from niome_subnet.genomics.consistency_control import (
    ConsistencyDecision,
    ConsistencySample,
    decide_consistency_target,
)
from niome_subnet.genomics.submission_builder import (
    GUIDE_VARIANTS_PER_TARGET,
    PRIMARY_CAS_SHARE,
    build_submission,
)
from niome_subnet.genomics.seed_policy import resolve_seed_plan
from niome_subnet.miner.task_processor import (
    DEFAULT_URL_TTL_SECONDS,
    _presigned_url_deadline,
)
from niome_subnet.utils.misc import FINALITY_LAG
from niome_subnet.utils.seeds import seed_blocks, seeds_from_block_hashes
from niome_subnet.utils.settings import (
    CHR11_PATH,
    MINER_SCORE_URL,
    SEED_BLOCK,
    VALIDATION_BLOCK,
)
from tools.local_validator.artifacts import ArtifactBundle, sha256_bytes
from tools.local_validator.evaluator import evaluate_submission


logger = logging.getLogger("niome_seed_bridge")

ARTIFACT_ROOT = Path(os.getenv("NIOME_ARTIFACT_ROOT", "artifacts/live"))
EXPLORATION_PROFILE = os.getenv("NIOME_EXPLORATION_PROFILE", "").strip() or None
OBSERVE_CHAIN_SEEDS_ONLY = os.getenv(
    "NIOME_BRIDGE_OBSERVE_ONLY", ""
).strip().lower() in {"1", "true", "yes"}
CONSISTENCY_CONTROL_ENABLED = os.getenv(
    "NIOME_CONSISTENCY_CONTROL", "true"
).strip().lower() not in {"0", "false", "no"}
CONSISTENCY_HISTORY_LIMIT = 5
CONSISTENCY_SCORE_TIMEOUT_SECONDS = 10.0
CONSISTENCY_REPLAY_BUDGET_SECONDS = 45.0
CONSISTENCY_UPLOAD_RESERVE_SECONDS = 75.0
CONSISTENCY_MIN_REPLAY_SECONDS = 30.0
POLL_SECONDS = 2.0
CONTRACT_POLL_SECONDS = 6.0
CONTRACT_HTTP_TIMEOUT_SECONDS = 15.0
FAST_SEED_FOCUSED_VARIANTS_PER_ANCHOR = 1
FAST_SEED_OPTIMIZER_TIME_BUDGET_SECONDS = 5.0
STREAM_INTERVAL_SECONDS = 1.0
# Keep two independent 64 KiB/s connections rather than the ineffective 4/16
# KiB/s probes.  Completion remains sequential: the standby stays slow while
# the primary flushes and is promoted only if the primary fails.  The fixed
# body size provides a little over two hours, longer than a 720-block round.
STREAM_PROFILES = (
    ("64kib-primary", 64 * 1024, 576 * 1024 * 1024),
    ("64kib-standby", 64 * 1024, 576 * 1024 * 1024),
)
OPEN_MINIMUM_REMAINING_SECONDS = 15.0
STANDBY_OPEN_TARGET_REMAINING_SECONDS = 30.0
STANDBY_MAX_START_DELAY_SECONDS = 60.0
ARTIFACT_WAIT_SECONDS = 90.0
PADDING_CHUNK_BYTES = 1024 * 1024
ACTIVE_BRIDGE_STATES = {
    "created",
    "opening",
    "streaming",
    "waiting_for_contract_seed",
    "waiting_for_seeds",
    "waiting_for_seed_blocks",
    "waiting_for_seed_finality",
    "building",
    "observation_build_complete",
    "finishing_upload",
}


def _round_coordinates(block: int) -> tuple[int, list[int], int, int]:
    """Return the immutable round boundaries used by the validator."""
    blocks = seed_blocks(block)
    round_start = blocks[0] - SEED_BLOCK
    return (
        round_start,
        blocks,
        blocks[-1] + FINALITY_LAG,
        round_start + VALIDATION_BLOCK,
    )


def _timing_probe(
    *,
    current_block: int,
    validation_block: int,
    observed_seconds_per_block: float | None,
    bridges: list["SlowPut"],
) -> dict[str, Any]:
    blocks_remaining = validation_block - current_block
    seconds_remaining = (
        max(0.0, blocks_remaining * observed_seconds_per_block)
        if observed_seconds_per_block and observed_seconds_per_block > 0
        else None
    )
    streams = []
    for bridge in bridges:
        snapshot = bridge.snapshot()
        remaining_bytes = max(
            0, int(snapshot.get("total_bytes", 0)) - int(snapshot.get("bytes_sent", 0))
        )
        required_rate = (
            remaining_bytes / seconds_remaining
            if seconds_remaining and seconds_remaining > 0
            else None
        )
        streams.append(
            {
                "label": bridge.label,
                "state": snapshot.get("state"),
                "bytes_sent": snapshot.get("bytes_sent"),
                "remaining_bytes": remaining_bytes,
                "required_body_bytes_per_second": required_rate,
            }
        )
    return {
        "current_block": current_block,
        "validation_block": validation_block,
        "blocks_remaining_to_validation": blocks_remaining,
        "observed_seconds_per_block": observed_seconds_per_block,
        "estimated_seconds_to_validation": seconds_remaining,
        "streams": streams,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_event(path: Path, event: str, **details: Any) -> None:
    record = {"at": _utc_now(), "event": event, **details}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _safe_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _fetch_historical_top_score(
    task_dir: Path,
    *,
    expected_local_score: float,
) -> float | None:
    """Fetch and cache only the non-sensitive official round summary."""
    cache_path = task_dir / "seed_bridge_official_summary.json"
    cached = _safe_json(cache_path)
    if isinstance(cached, dict):
        top_score = cached.get("top_score")
        if (
            cached.get("local_score_verified") is True
            and isinstance(top_score, (int, float))
            and float(top_score) > 0.0
        ):
            return float(top_score)

    query = urlencode({"task_id": task_dir.name})
    request = Request(
        f"{MINER_SCORE_URL}?{query}",
        headers={"Accept": "application/json"},
    )
    with urlopen(request, timeout=CONSISTENCY_SCORE_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read())
    items = payload.get("items") if isinstance(payload, dict) else None
    scores = [
        float(item["final_score"])
        for item in (items or [])
        if isinstance(item, dict)
        and isinstance(item.get("final_score"), (int, float))
        and float(item["final_score"]) > 0.0
    ]
    if not scores:
        return None
    local_score_verified = any(
        abs(score - expected_local_score)
        <= 1e-7 * max(1.0, abs(score), abs(expected_local_score))
        for score in scores
    )
    if not local_score_verified:
        return None
    top_score = max(scores)
    _write_json(
        cache_path,
        {
            "task_id": task_dir.name,
            "top_score": top_score,
            "participants": len(scores),
            "local_score_verified": True,
            "verified_local_score": expected_local_score,
            "fetched_at": _utc_now(),
        },
    )
    return top_score


def _consistency_sample_from_payload(
    task_id: str,
    local_payload: dict[str, Any],
    top_score: float,
) -> ConsistencySample | None:
    """Accept only exact, chain-authoritative completed-round replays."""
    seed_policy = local_payload.get("seed_policy")
    breakdown = local_payload.get("breakdown")
    if not isinstance(seed_policy, dict) or not isinstance(breakdown, dict):
        return None
    if seed_policy.get("mode") != "chain-authoritative":
        return None
    if not bool(local_payload.get("comparable_to_official")):
        return None
    weighted = breakdown.get("total_weighted_score")
    fidelity = breakdown.get("distribution_fidelity_factor")
    consistency = breakdown.get("consistency_factor")
    if not all(
        isinstance(value, (int, float))
        for value in (weighted, fidelity, consistency, top_score)
    ):
        return None
    baseline = float(weighted) * float(fidelity)
    if baseline <= 0.0 or float(top_score) <= 0.0:
        return None
    return ConsistencySample(
        task_id=task_id,
        top_score=float(top_score),
        baseline_score=baseline,
        realized_consistency=float(consistency),
    )


def _resolve_live_consistency_decision(
    *,
    artifact_root: Path,
    current_task_id: str,
) -> tuple[ConsistencyDecision, list[ConsistencySample]]:
    """Build a decision from recent rounds without trusting stale regimes."""
    candidates = sorted(
        (
            path
            for path in artifact_root.iterdir()
            if path.is_dir() and path.name != current_task_id
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    samples: list[ConsistencySample] = []
    for candidate in candidates:
        if len(samples) >= CONSISTENCY_HISTORY_LIMIT:
            break
        bridge_status = _safe_json(candidate / "seed_bridge_status.json")
        local_payload = _safe_json(candidate / "seed_bridge_local_validation.json")
        if not isinstance(bridge_status, dict) or bridge_status.get("state") != "complete":
            continue
        if not isinstance(local_payload, dict):
            continue
        seed_policy = local_payload.get("seed_policy")
        if not isinstance(seed_policy, dict) or seed_policy.get("mode") != "chain-authoritative":
            continue
        local_score = local_payload.get("final_score")
        if not isinstance(local_score, (int, float)):
            continue
        try:
            top_score = _fetch_historical_top_score(
                candidate,
                expected_local_score=float(local_score),
            )
        except Exception as error:
            logger.warning(
                "Could not fetch official score summary for task %s: %s",
                candidate.name,
                error,
            )
            continue
        if top_score is None:
            continue
        sample = _consistency_sample_from_payload(
            candidate.name,
            local_payload,
            top_score,
        )
        if sample is not None:
            samples.append(sample)
    return (
        decide_consistency_target(
            samples,
            enabled=CONSISTENCY_CONTROL_ENABLED,
            history_limit=CONSISTENCY_HISTORY_LIMIT,
        ),
        samples,
    )


def _choose_exact_consistency_candidate(
    *,
    candidates: list[tuple[str, list[dict[str, Any]]]],
    artifacts: ArtifactBundle,
    seeds: list[int],
    target_consistency: float,
    budget_seconds: float = CONSISTENCY_REPLAY_BUDGET_SECONDS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Replay a bounded candidate frontier and never select below target."""
    if not candidates:
        raise ValueError("exact consistency search requires candidates")
    scoring_contract = dict(artifacts.contract)
    scoring_contract["seed"] = ",".join(str(seed) for seed in seeds)
    scoring_artifacts = replace(artifacts, contract=scoring_contract)
    # Establish the maximum-score safety candidate first.  If the time budget
    # expires later, this exact result remains available as the fail-safe.
    def replay_priority(item: tuple[str, list[dict[str, Any]]]) -> tuple[int, int, str]:
        label = item[0]
        if label == "max-score-fallback":
            return (0, 0, label)
        try:
            percent = int(label.rsplit("-", 1)[-1].removesuffix("pct"))
        except ValueError:
            percent = 0
        # After establishing the safety fallback, probe the high-consistency
        # end first.  The 45-second live budget normally fits two more
        # exact RF replays.
        return (1, -percent, label)

    ordered = sorted(candidates, key=replay_priority)
    started = time.monotonic()
    evaluated: list[dict[str, Any]] = []
    submissions_by_label = {label: submission for label, submission in ordered}
    for label, candidate in ordered:
        if evaluated and time.monotonic() - started >= budget_seconds:
            break
        raw = json.dumps(
            candidate,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        result = evaluate_submission(
            candidate,
            scoring_artifacts,
            raw_submission_bytes=raw,
        )
        evaluated.append(
            {
                "label": label,
                "rows": len(candidate),
                "submission_sha256": sha256_bytes(raw),
                "final_score": result.final_score,
                "consistency_factor": result.breakdown.get(
                    "consistency_factor"
                ),
                "total_weighted_score": result.breakdown.get(
                    "total_weighted_score"
                ),
                "distribution_fidelity_factor": result.breakdown.get(
                    "distribution_fidelity_factor"
                ),
            }
        )

    eligible = [
        item
        for item in evaluated
        if isinstance(item.get("consistency_factor"), (int, float))
        and float(item["consistency_factor"]) + 1e-12 >= target_consistency
    ]
    if eligible:
        chosen = min(
            eligible,
            key=lambda item: (
                float(item["consistency_factor"]) - target_consistency,
                -float(item["final_score"]),
                item["label"],
            ),
        )
        fallback_used = chosen["label"] == "max-score-fallback"
    else:
        chosen = next(
            (
                item
                for item in evaluated
                if item["label"] == "max-score-fallback"
            ),
            None,
        )
        if chosen is None:
            raise RuntimeError(
                "exact consistency frontier had no target-safe or maximum-score result"
            )
        fallback_used = True
    return submissions_by_label[chosen["label"]], {
        "enabled": True,
        "target_consistency": target_consistency,
        "budget_seconds": budget_seconds,
        "elapsed_seconds": time.monotonic() - started,
        "candidate_count": len(candidates),
        "evaluated_count": len(evaluated),
        "deadline_reached": len(evaluated) < len(candidates),
        "selected_label": chosen["label"],
        "fallback_used": fallback_used,
        "selected": chosen,
        "results": evaluated,
    }


def _failure_category(error: Exception, *, cancelled: bool, status: int | None) -> str:
    """Reduce low-level failures to stable, actionable forensic categories."""
    if cancelled:
        return "cancelled_by_bridge"
    if status is not None:
        return "s3_http_rejection"
    if isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
        return "remote_connection_closed"
    if isinstance(error, TimeoutError):
        return "socket_timeout"
    message = str(error).lower()
    if "exhausted reserved body" in message:
        return "reserved_body_exhausted"
    if "payload exceeds reserved body" in message:
        return "submission_exceeded_reservation"
    if isinstance(error, OSError):
        return "socket_os_error"
    return "bridge_internal_error"


def _submission_tail(submission_raw: bytes) -> bytes:
    """Return bytes that complete a JSON list whose opening ``[`` was sent."""
    if not submission_raw.startswith(b"["):
        raise ValueError("submission payload must be a JSON list")
    return submission_raw[1:]


def _envelope_seconds_remaining(envelope_path: Path) -> float:
    """Return usable URL lifetime, including a receipt-time fallback."""
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    deadline, source = _presigned_url_deadline(str(envelope["presigned_url"]))
    if source == "fallback-receipt":
        age = max(0.0, time.time() - envelope_path.stat().st_mtime)
        return DEFAULT_URL_TTL_SECONDS - age
    return deadline - time.monotonic()


def _fetch_refreshed_contract(task_dir: Path) -> dict[str, Any]:
    """Re-read the task's signed contract object without logging its URL.

    The legacy validator calls ``fetch_task`` again immediately before scoring.
    The task URL outlives the miner upload URL, so polling that same object
    reproduces the validator's view when the backend replaces the placeholder
    seed with the real random seed set.
    """
    task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    contract_url = str(task["contract_url"])
    request = Request(contract_url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=CONTRACT_HTTP_TIMEOUT_SECONDS) as response:
        payload = response.read()
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("refreshed contract is not a JSON object")
    return value


def _assert_contract_seed_only_changed(
    original: dict[str, Any], refreshed: dict[str, Any]
) -> None:
    """Fail closed if the late object is not the same task contract."""
    original_body = {key: value for key, value in original.items() if key != "seed"}
    refreshed_body = {key: value for key, value in refreshed.items() if key != "seed"}
    if original_body != refreshed_body:
        raise ValueError("refreshed task contract changed fields other than seed")


def _standby_start_delay(url_seconds_remaining: float) -> float:
    """Stagger the standby without risking the signed URL opening window."""
    return max(
        0.0,
        min(
            STANDBY_MAX_START_DELAY_SECONDS,
            url_seconds_remaining - STANDBY_OPEN_TARGET_REMAINING_SECONDS,
        ),
    )


def _wait_for_authoritative_contract_seed(
    *,
    task_dir: Path,
    artifacts: ArtifactBundle,
    bridges: list["SlowPut"],
    state: dict[str, Any],
    status_path: Path,
    events_path: Path,
) -> tuple[ArtifactBundle, Any]:
    """Poll the signed task object until legacy random seeds become visible."""
    state.update(
        {
            "state": "waiting_for_contract_seed",
            "contract_poll_started_at": _utc_now(),
        }
    )
    _write_json(status_path, state)
    _append_event(events_path, "waiting_for_contract_seed")
    attempt = 0
    last_seed_raw: Any = artifacts.contract.get("seed")
    last_error: str | None = None
    last_live_labels: tuple[str, ...] | None = None
    while True:
        live_labels = tuple(
            bridge.label for bridge in bridges if not bridge.done.is_set()
        )
        if not live_labels:
            raise RuntimeError(
                "all slow PUT profiles stopped before authoritative contract seeds "
                f"were published: {[bridge.snapshot() for bridge in bridges]}"
            )
        attempt += 1
        try:
            refreshed = _fetch_refreshed_contract(task_dir)
        except Exception as error:
            # A transient task-object read must not abandon a surviving PUT.
            # The terminal condition is loss of all streams, recorded above.
            last_error = f"{type(error).__name__}: {error}"
        else:
            # This check intentionally sits outside the transient network/JSON
            # handler. A different task body is an integrity failure, not a
            # reason to keep polling and eventually overwrite the safe object.
            _assert_contract_seed_only_changed(artifacts.contract, refreshed)
            last_seed_raw = refreshed.get("seed")
            seed_plan = resolve_seed_plan(refreshed, task_dir.name)
            last_error = None
            if seed_plan.mode == "contract-authoritative":
                refreshed_artifacts = replace(artifacts, contract=refreshed)
                _write_json(task_dir / "refreshed_contract.json", refreshed)
                _append_event(
                    events_path,
                    "authoritative_contract_seed_observed",
                    contract_poll_attempts=attempt,
                    round_seeds=list(seed_plan.optimization_seeds),
                    seed_policy=seed_plan.as_dict(),
                )
                return refreshed_artifacts, seed_plan

        snapshots = [bridge.snapshot() for bridge in bridges]
        state.update(
            {
                "last_observed_at": _utc_now(),
                "contract_poll_attempts": attempt,
                "last_contract_seed_raw": last_seed_raw,
                "last_contract_poll_error": last_error,
                "stream_results": snapshots,
                "live_stream_profiles": list(live_labels),
            }
        )
        if live_labels != last_live_labels:
            _append_event(
                events_path,
                "stream_set_changed",
                live_stream_profiles=list(live_labels),
                streams=snapshots,
            )
            last_live_labels = live_labels
        _write_json(status_path, state)
        time.sleep(CONTRACT_POLL_SECONDS)


class SlowPut:
    """A fixed-length PUT whose body can be completed asynchronously."""

    def __init__(
        self,
        url: str,
        *,
        total_bytes: int,
        stream_bytes: int,
        stream_interval_seconds: float = STREAM_INTERVAL_SECONDS,
        label: str = "stream",
    ):
        self.url = url
        self.total_bytes = total_bytes
        self.stream_bytes = stream_bytes
        self.stream_interval_seconds = stream_interval_seconds
        self.label = label
        self.payload_ready = threading.Event()
        self.done = threading.Event()
        self.payload: bytes | None = None
        self._result_lock = threading.Lock()
        self.result: dict[str, Any] = {
            "label": label,
            "state": "created",
            "total_bytes": total_bytes,
            "stream_bytes": stream_bytes,
            "stream_interval_seconds": stream_interval_seconds,
            "bytes_sent": 0,
            "send_count": 0,
        }
        self._cancelled = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="niome-seed-bridge-put",
        )

    def start(self) -> None:
        self._thread.start()

    def finish_with(self, submission_raw: bytes) -> None:
        self.payload = submission_raw
        self.payload_ready.set()

    def cancel(self) -> None:
        self._cancelled.set()
        self.payload_ready.set()

    def snapshot(self) -> dict[str, Any]:
        with self._result_lock:
            return dict(self.result)

    def _record(self, **values: Any) -> None:
        with self._result_lock:
            self.result.update(values)

    def _run(self) -> None:
        parsed = urlparse(self.url)
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        connection: http.client.HTTPSConnection | None = None
        sent = 0
        send_count = 0
        started_monotonic = time.monotonic()
        self._record(state="opening", started_at=_utc_now())
        try:
            connection = http.client.HTTPSConnection(
                parsed.hostname,
                parsed.port,
                timeout=30,
            )
            connection.putrequest(
                "PUT",
                target,
                skip_host=True,
                skip_accept_encoding=True,
            )
            connection.putheader("Host", parsed.netloc)
            connection.putheader("Content-Length", str(self.total_bytes))
            connection.putheader("Connection", "close")
            connection.endheaders()
            socket = getattr(connection, "sock", None)
            self._record(
                state="streaming",
                headers_sent_at=_utc_now(),
                local_address=str(socket.getsockname()) if socket else None,
                peer_address=str(socket.getpeername()) if socket else None,
                tls_version=socket.version() if socket and hasattr(socket, "version") else None,
                tls_cipher=(
                    socket.cipher()[0]
                    if socket and hasattr(socket, "cipher") and socket.cipher()
                    else None
                ),
            )
            connection.send(b"[")
            sent = 1
            send_count = 1
            self._record(
                bytes_sent=sent,
                send_count=send_count,
                last_successful_send_at=_utc_now(),
            )

            while not self.payload_ready.wait(self.stream_interval_seconds):
                if self._cancelled.is_set():
                    raise RuntimeError("slow PUT cancelled")
                chunk_size = min(self.stream_bytes, self.total_bytes - sent)
                if chunk_size <= 0:
                    raise RuntimeError("slow PUT exhausted reserved body bytes")
                connection.send(b" " * chunk_size)
                sent += chunk_size
                send_count += 1
                self._record(
                    bytes_sent=sent,
                    send_count=send_count,
                    last_successful_send_at=_utc_now(),
                    elapsed_seconds=time.monotonic() - started_monotonic,
                )

            if self._cancelled.is_set():
                raise RuntimeError("slow PUT cancelled")
            if self.payload is None:
                raise RuntimeError("slow PUT completed without a payload")

            tail = _submission_tail(self.payload)
            if sent + len(tail) > self.total_bytes:
                raise RuntimeError(
                    f"payload exceeds reserved body: {sent + len(tail)} > {self.total_bytes}"
                )
            self._record(state="completing", completion_started_at=_utc_now())
            connection.send(tail)
            sent += len(tail)
            send_count += 1
            self._record(bytes_sent=sent, send_count=send_count)
            while sent < self.total_bytes:
                chunk_size = min(PADDING_CHUNK_BYTES, self.total_bytes - sent)
                connection.send(b" " * chunk_size)
                sent += chunk_size
                send_count += 1
                self._record(bytes_sent=sent, send_count=send_count)
            response = connection.getresponse()
            response_body = response.read(4096)
            response_headers = {
                key.lower(): value
                for key, value in getattr(response, "getheaders", lambda: [])()
                if key.lower() in {"date", "server", "x-amz-request-id", "x-amz-id-2"}
            }
            self._record(
                state="complete",
                status=response.status,
                reason=response.reason,
                bytes_sent=sent,
                send_count=send_count,
                response_headers=response_headers,
                response_excerpt=response_body.decode("utf-8", errors="replace"),
            )
            if not 200 <= response.status < 300:
                raise RuntimeError(
                    f"slow PUT returned {response.status} {response.reason}: "
                    f"{self.result['response_excerpt']}"
                )
        except Exception as error:
            previous = self.snapshot()
            self._record(
                state="cancelled" if self._cancelled.is_set() else "failed",
                failure_stage=previous.get("state"),
                failure_category=_failure_category(
                    error,
                    cancelled=self._cancelled.is_set(),
                    status=previous.get("status"),
                ),
                failed_at=_utc_now(),
                error_type=type(error).__name__,
                error=str(error),
                error_errno=getattr(error, "errno", None),
                bytes_sent=sent,
                send_count=send_count,
            )
            logger.warning("Slow PUT %s stopped: %s", self.label, self.snapshot())
        finally:
            if connection is not None:
                connection.close()
            self._record(
                finished_at=_utc_now(),
                elapsed_seconds=time.monotonic() - started_monotonic,
                average_body_bytes_per_second=(
                    sent / max(0.001, time.monotonic() - started_monotonic)
                ),
            )
            self.done.set()


def _wait_for_artifacts(task_dir: Path) -> ArtifactBundle:
    required = [
        task_dir / "contract.json",
        task_dir / "hbb_reference.json",
        task_dir / "cell_types.json",
        Path(CHR11_PATH),
    ]
    deadline = time.monotonic() + ARTIFACT_WAIT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if all(path.exists() for path in required):
            try:
                return ArtifactBundle.from_paths(
                    contract_path=required[0],
                    hbb_reference_path=required[1],
                    cell_types_path=required[2],
                    chromosome_11_path=required[3],
                )
            except (json.JSONDecodeError, OSError, ValueError) as error:
                # The miner writes these files independently.  Seeing all paths
                # does not guarantee the final JSON rename/write has completed.
                last_error = error
        time.sleep(0.5)
    missing = [str(path) for path in required if not path.exists()]
    detail = f"; last read error: {last_error}" if last_error else ""
    raise TimeoutError(f"task artifacts did not arrive: {missing}{detail}")


def _quarantine_inherited_unknown_seed_tasks() -> int:
    """Close the forensic state left by pre-transition sidecar processes.

    A restarted process cannot resume an existing HTTP request body. Marking an
    inherited unknown-seed bridge as quarantined accurately records that its
    incomplete PUT cannot replace the already-completed ordinary submission.
    """
    quarantined = 0
    for status_path in ARTIFACT_ROOT.glob("*/seed_bridge_status.json"):
        task_dir = status_path.parent
        contract_path = task_dir / "contract.json"
        events_path = task_dir / "seed_bridge_events.jsonl"
        try:
            state = json.loads(status_path.read_text(encoding="utf-8"))
            if state.get("state") not in ACTIVE_BRIDGE_STATES:
                continue
            if state.get("process_pid") == os.getpid():
                continue
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            seed_plan = resolve_seed_plan(contract, task_dir.name)
            state.update(
                {
                    "state": "quarantined",
                    "completed_at": _utc_now(),
                    "seed_policy": seed_plan.as_dict(),
                    "reason": (
                        "inherited pre-transition block-seed bridge was closed "
                        "during robust-mode deployment"
                    ),
                }
            )
            _write_json(status_path, state)
            _append_event(
                events_path,
                "bridge_quarantined_on_startup",
                seed_policy=seed_plan.as_dict(),
                reason=state["reason"],
            )
            quarantined += 1
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logger.exception("Could not quarantine inherited bridge %s", task_dir)
    return quarantined


def _handle_envelope(envelope_path: Path) -> None:
    task_dir = envelope_path.parent
    bridge_status_path = task_dir / "seed_bridge_status.json"
    bridge_events_path = task_dir / "seed_bridge_events.jsonl"
    bridge_failure_path = task_dir / "seed_bridge_failure.json"
    bridges: list[SlowPut] = []
    state: dict[str, Any] = {
        "task_id": task_dir.name,
        "process_pid": os.getpid(),
        "started_at": _utc_now(),
        "state": "opening",
    }
    _write_json(bridge_status_path, state)
    _append_event(bridge_events_path, "handler_started", **state)
    try:
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        presigned_url = str(envelope["presigned_url"])
        url_deadline, _ = _presigned_url_deadline(presigned_url)
        remaining = url_deadline - time.monotonic()
        if remaining < OPEN_MINIMUM_REMAINING_SECONDS:
            raise RuntimeError(
                f"only {remaining:.3f}s remained before the presigned URL expired"
            )

        standby_delay = _standby_start_delay(remaining)
        put_opened_at = _utc_now()
        for index, (label, stream_bytes, total_bytes) in enumerate(STREAM_PROFILES):
            if index:
                time.sleep(standby_delay)
            bridge = SlowPut(
                presigned_url,
                total_bytes=total_bytes,
                stream_bytes=stream_bytes,
                label=label,
            )
            bridge.start()
            bridges.append(bridge)
            _append_event(
                bridge_events_path,
                "stream_started",
                profile=label,
                start_delay_seconds=standby_delay if index else 0.0,
                url_seconds_remaining=max(0.0, url_deadline - time.monotonic()),
                stream=bridge.snapshot(),
            )
        state.update(
            {
                "state": "streaming",
                "put_opened_at": put_opened_at,
                "url_seconds_remaining_at_open": remaining,
                "standby_start_delay_seconds": standby_delay,
                "stream_profiles": [
                    {
                        "label": bridge.label,
                        "bytes_per_second": bridge.stream_bytes
                        / bridge.stream_interval_seconds,
                        "fixed_upload_bytes": bridge.total_bytes,
                        "role": (
                            "primary" if bridge.label.endswith("primary") else "standby"
                        ),
                    }
                    for bridge in bridges
                ],
            }
        )
        _write_json(bridge_status_path, state)
        _append_event(
            bridge_events_path,
            "streams_started",
            url_seconds_remaining_at_open=remaining,
            streams=[bridge.snapshot() for bridge in bridges],
        )

        artifacts = _wait_for_artifacts(task_dir)
        consistency_decision, consistency_samples = (
            _resolve_live_consistency_decision(
                artifact_root=ARTIFACT_ROOT,
                current_task_id=task_dir.name,
            )
        )
        consistency_control = consistency_decision.as_dict()
        consistency_control["samples"] = [
            sample.as_dict() for sample in consistency_samples
        ]
        state["consistency_control"] = consistency_control
        _write_json(bridge_status_path, state)
        _write_json(
            task_dir / "seed_bridge_consistency_decision.json",
            consistency_control,
        )
        _append_event(
            bridge_events_path,
            "consistency_policy_resolved",
            consistency_control=consistency_control,
        )
        subtensor = bt.Subtensor(network="finney")
        current_block = int(subtensor.block)
        round_start, round_seed_blocks, seed_finality_block, validation_block = (
            _round_coordinates(current_block)
        )
        wait_started_at = time.monotonic()
        wait_started_block = current_block
        observed_seconds_per_block: float | None = None
        seed_read_target_block = (
            seed_finality_block
            if OBSERVE_CHAIN_SEEDS_ONLY
            else round_seed_blocks[-1]
        )
        waiting_state = (
            "waiting_for_seed_finality"
            if OBSERVE_CHAIN_SEEDS_ONLY
            else "waiting_for_seed_blocks"
        )
        state.update(
            {
                "state": waiting_state,
                "bridge_mode": (
                    "observe-chain-seeds" if OBSERVE_CHAIN_SEEDS_ONLY else "submit-chain-seeds"
                ),
                "contract_seed_telemetry": artifacts.contract.get("seed"),
                "current_block_at_open": current_block,
                "round_start_block": round_start,
                "seed_blocks": round_seed_blocks,
                "seed_finality_block": seed_finality_block,
                "seed_read_target_block": seed_read_target_block,
                "validation_block": validation_block,
            }
        )
        _write_json(bridge_status_path, state)
        _append_event(
            bridge_events_path,
            waiting_state,
            current_block=current_block,
            round_start_block=round_start,
            seed_blocks=round_seed_blocks,
            seed_finality_block=seed_finality_block,
            seed_read_target_block=seed_read_target_block,
            validation_block=validation_block,
            contract_seed_telemetry=artifacts.contract.get("seed"),
        )
        last_live_labels: tuple[str, ...] | None = None
        while current_block < seed_read_target_block:
            live_labels = tuple(
                bridge.label for bridge in bridges if not bridge.done.is_set()
            )
            if not live_labels:
                raise RuntimeError(
                    "all slow PUT profiles stopped before the seed read target: "
                    f"{[bridge.snapshot() for bridge in bridges]}"
                )
            elapsed = time.monotonic() - wait_started_at
            advanced = current_block - wait_started_block
            if advanced > 0:
                observed_seconds_per_block = elapsed / advanced
            snapshots = [bridge.snapshot() for bridge in bridges]
            state.update(
                {
                    "last_observed_at": _utc_now(),
                    "current_block": current_block,
                    "observed_seconds_per_block": observed_seconds_per_block,
                    "stream_results": snapshots,
                }
            )
            if live_labels != last_live_labels:
                state.update(
                    {
                        "live_stream_profiles": list(live_labels),
                        "stopped_streams": [
                            bridge.snapshot()
                            for bridge in bridges
                            if bridge.done.is_set()
                        ],
                    }
                )
                _append_event(
                    bridge_events_path,
                    "stream_set_changed",
                    current_block=current_block,
                    live_stream_profiles=list(live_labels),
                    streams=snapshots,
                )
                last_live_labels = live_labels
            _write_json(bridge_status_path, state)
            time.sleep(6)
            current_block = int(subtensor.block)

        elapsed = time.monotonic() - wait_started_at
        advanced = current_block - wait_started_block
        if advanced > 0:
            observed_seconds_per_block = elapsed / advanced
        block_hashes = [
            str(subtensor.block_info(block).hash) for block in round_seed_blocks
        ]
        chain_seeds = seeds_from_block_hashes(block_hashes)
        seed_plan = resolve_seed_plan(
            artifacts.contract,
            task_dir.name,
            supplied_seeds=chain_seeds,
            trust_supplied_seeds=True,
            prefer_supplied_seeds=True,
        )
        seed_block = current_block
        state["chain_seed_proof"] = {
            "round_start_block": round_start,
            "seed_blocks": round_seed_blocks,
            "block_hashes": block_hashes,
            "seeds": chain_seeds,
            "observed_at_block": current_block,
            "finality_depth": current_block - round_seed_blocks[-1],
            "finality_confirmed": current_block >= seed_finality_block,
        }

        seeds = list(seed_plan.optimization_seeds)
        state.update(
            {
                "state": "building",
                "seed_read_block": seed_block,
                "round_seeds": seeds,
                "seed_policy": seed_plan.as_dict(),
                "build_started_at": _utc_now(),
            }
        )
        _write_json(bridge_status_path, state)
        _append_event(
            bridge_events_path,
            "seeds_available",
            seed_read_block=seed_block,
            round_seeds=seeds,
            seed_policy=seed_plan.as_dict(),
            streams=[bridge.snapshot() for bridge in bridges],
        )

        build_started = time.monotonic()
        consistency_candidates: list[
            tuple[str, list[dict[str, Any]]]
        ] = []
        submission, diagnostics = build_submission(
            contract=artifacts.contract,
            reference=artifacts.hbb_reference,
            chromosome_11=artifacts.chromosome_11,
            cell_types=artifacts.cell_types,
            selection_profile=seed_plan.selection_profile,
            round_seeds=seeds,
            seed_focused_variants_per_anchor=(
                FAST_SEED_FOCUSED_VARIANTS_PER_ANCHOR
                if seed_plan.selection_profile == "seed-aware"
                else None
            ),
            seed_optimizer_time_budget_seconds=(
                FAST_SEED_OPTIMIZER_TIME_BUDGET_SECONDS
                if seed_plan.selection_profile == "seed-aware"
                else None
            ),
            exploration_profile=(
                EXPLORATION_PROFILE
                if seed_plan.selection_profile == "seed-aware"
                else None
            ),
            consistency_target=consistency_decision.target_consistency,
            consistency_candidate_submissions=(
                consistency_candidates
                if consistency_decision.targeting_enabled
                else None
            ),
        )
        if consistency_decision.target_consistency is not None:
            block_after_build = int(subtensor.block)
            seconds_per_block = observed_seconds_per_block or 12.0
            estimated_seconds_to_validation = max(
                0.0,
                (validation_block - block_after_build) * seconds_per_block,
            )
            replay_budget = min(
                CONSISTENCY_REPLAY_BUDGET_SECONDS,
                max(
                    0.0,
                    estimated_seconds_to_validation
                    - CONSISTENCY_UPLOAD_RESERVE_SECONDS,
                ),
            )
            if replay_budget < CONSISTENCY_MIN_REPLAY_SECONDS:
                fallback = next(
                    candidate
                    for label, candidate in consistency_candidates
                    if label == "max-score-fallback"
                )
                submission = fallback
                exact_search = {
                    "enabled": False,
                    "skip_reason": "insufficient_pre_validation_time",
                    "target_consistency": (
                        consistency_decision.target_consistency
                    ),
                    "estimated_seconds_to_validation": (
                        estimated_seconds_to_validation
                    ),
                    "required_upload_reserve_seconds": (
                        CONSISTENCY_UPLOAD_RESERVE_SECONDS
                    ),
                    "selected_label": "max-score-fallback",
                    "fallback_used": True,
                }
            else:
                submission, exact_search = _choose_exact_consistency_candidate(
                    candidates=consistency_candidates,
                    artifacts=artifacts,
                    seeds=seeds,
                    target_consistency=(
                        consistency_decision.target_consistency
                    ),
                    budget_seconds=replay_budget,
                )
            diagnostics["exact_consistency_search"] = exact_search
            consistency_control["exact_search"] = exact_search
            diagnostics["selected_count"] = len(submission)
            diagnostics["bucket_selected_counts"] = {
                key: sum(
                    row["mutation"] == key.split("|")[0]
                    and row["cas_system"] == key.split("|")[1]
                    and row["strand"] == key.split("|")[2]
                    for row in submission
                )
                for key in diagnostics.get("joint_bucket_quotas", {})
            }
        diagnostics["seed_policy"] = seed_plan.as_dict()
        diagnostics["consistency_decision"] = consistency_control
        submission_raw = json.dumps(
            submission,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        submission_artifact = (
            "seed_bridge_observation_submission.json"
            if OBSERVE_CHAIN_SEEDS_ONLY
            else "seed_bridge_submission.json"
        )
        (task_dir / submission_artifact).write_bytes(submission_raw)
        _write_json(task_dir / "seed_bridge_builder_diagnostics.json", diagnostics)
        state.update(
            {
                "state": (
                    "observation_build_complete"
                    if OBSERVE_CHAIN_SEEDS_ONLY
                    else "finishing_upload"
                ),
                "build_elapsed_seconds": time.monotonic() - build_started,
                "submission_rows": len(submission),
                "submission_sha256": sha256_bytes(submission_raw),
                "submission_artifact": submission_artifact,
            }
        )
        _write_json(bridge_status_path, state)
        _append_event(
            bridge_events_path,
            "submission_built",
            build_elapsed_seconds=state["build_elapsed_seconds"],
            submission_rows=len(submission),
            submission_sha256=state["submission_sha256"],
        )
        if not OBSERVE_CHAIN_SEEDS_ONLY:
            current_block = int(subtensor.block)
            while current_block < seed_finality_block:
                if not any(not bridge.done.is_set() for bridge in bridges):
                    raise RuntimeError(
                        "all slow PUT profiles stopped before seed finality confirmation"
                    )
                time.sleep(6)
                current_block = int(subtensor.block)
            confirmed_hashes = [
                str(subtensor.block_info(block).hash) for block in round_seed_blocks
            ]
            if confirmed_hashes != block_hashes:
                raise RuntimeError(
                    "seed block hashes changed before finality; refusing optimized PUT"
                )
            state["chain_seed_proof"].update(
                {
                    "confirmed_block_hashes": confirmed_hashes,
                    "confirmed_at_block": current_block,
                    "finality_depth": current_block - round_seed_blocks[-1],
                    "finality_confirmed": True,
                }
            )
            state["seed_finality_confirmed_at"] = _utc_now()
            _write_json(bridge_status_path, state)
            _append_event(
                bridge_events_path,
                "seed_finality_confirmed",
                chain_seed_proof=state["chain_seed_proof"],
                streams=[bridge.snapshot() for bridge in bridges],
            )
        if OBSERVE_CHAIN_SEEDS_ONLY:
            block_after_build = int(subtensor.block)
            timing = _timing_probe(
                current_block=block_after_build,
                validation_block=validation_block,
                observed_seconds_per_block=observed_seconds_per_block,
                bridges=bridges,
            )
            for candidate in bridges:
                if not candidate.done.is_set():
                    candidate.cancel()
            for candidate in bridges:
                candidate.done.wait(5)
            state.update(
                {
                    "state": "observed_no_submission",
                    "completed_at": _utc_now(),
                    "submission_attempted": False,
                    "put_completion_skipped": True,
                    "timing_probe": timing,
                    "stream_results": [bridge.snapshot() for bridge in bridges],
                }
            )
            _write_json(bridge_status_path, state)
            _append_event(
                bridge_events_path,
                "observation_complete_no_submission",
                round_seeds=seeds,
                chain_seed_proof=state["chain_seed_proof"],
                build_elapsed_seconds=state["build_elapsed_seconds"],
                timing_probe=timing,
                streams=state["stream_results"],
            )
            logger.info(
                "Observed chain seeds for task %s without completing PUT: seeds=%s timing=%s",
                task_dir.name,
                seeds,
                timing,
            )
            return

        completed_bridge: SlowPut | None = None
        for candidate in bridges:
            if candidate.done.is_set():
                continue
            _append_event(
                bridge_events_path,
                "upload_completion_started",
                stream=candidate.snapshot(),
            )
            candidate.finish_with(submission_raw)
            if not candidate.done.wait(180):
                candidate.cancel()
                _append_event(
                    bridge_events_path,
                    "upload_completion_timed_out",
                    stream=candidate.snapshot(),
                )
                continue
            _append_event(
                bridge_events_path,
                "upload_completion_finished",
                stream=candidate.snapshot(),
            )
            if "error" not in candidate.snapshot():
                completed_bridge = candidate
                break
        if completed_bridge is None:
            raise RuntimeError(
                "no slow PUT profile completed: "
                f"{[bridge.snapshot() for bridge in bridges]}"
            )
        for candidate in bridges:
            if candidate is not completed_bridge and not candidate.done.is_set():
                candidate.cancel()
        for candidate in bridges:
            if candidate is not completed_bridge:
                candidate.done.wait(5)

        scoring_contract = dict(artifacts.contract)
        scoring_contract["seed"] = ",".join(
            str(seed) for seed in seed_plan.evaluation_seeds
        )
        result = evaluate_submission(
            submission,
            replace(artifacts, contract=scoring_contract),
            raw_submission_bytes=submission_raw,
        )
        local_payload = result.as_dict()
        local_payload.update(
            {
                "score_semantics": (
                    "official-seed-replay"
                    if seed_plan.comparable_to_official
                    else "provisional-seed-estimate"
                ),
                "comparable_to_official": seed_plan.comparable_to_official,
                "seed_policy": seed_plan.as_dict(),
                "consistency_control": {
                    **consistency_control,
                    "baseline_score": (
                        result.breakdown.get("total_weighted_score", 0.0)
                        * result.breakdown.get(
                            "distribution_fidelity_factor", 0.0
                        )
                    ),
                    "target_final_score": (
                        result.breakdown.get("total_weighted_score", 0.0)
                        * result.breakdown.get(
                            "distribution_fidelity_factor", 0.0
                        )
                        * consistency_decision.target_consistency
                        if consistency_decision.target_consistency is not None
                        else None
                    ),
                    "achieved_consistency": result.breakdown.get(
                        "consistency_factor"
                    ),
                    "achieved_final_score": result.final_score,
                    "target_error": (
                        result.breakdown.get("consistency_factor")
                        - consistency_decision.target_consistency
                        if consistency_decision.target_consistency is not None
                        else None
                    ),
                },
            }
        )
        _write_json(task_dir / "seed_bridge_local_validation.json", local_payload)
        diagnostics["consistency_control"]["achieved_consistency"] = (
            result.breakdown.get("consistency_factor")
        )
        diagnostics["consistency_control"]["achieved_final_score"] = (
            result.final_score
        )
        _write_json(task_dir / "seed_bridge_builder_diagnostics.json", diagnostics)
        state.update(
            {
                "state": "complete",
                "completed_at": _utc_now(),
                "put_result": completed_bridge.snapshot(),
                "stream_results": [bridge.snapshot() for bridge in bridges],
                "local_final_score": result.final_score,
                "local_breakdown": result.breakdown,
                "consistency_control": local_payload["consistency_control"],
            }
        )
        _write_json(bridge_status_path, state)
        _append_event(
            bridge_events_path,
            "bridge_complete",
            local_final_score=result.final_score,
            streams=[bridge.snapshot() for bridge in bridges],
        )
        logger.info(
            "Seed bridge completed task %s with seeds=%s score=%s",
            task_dir.name,
            seeds,
            result.final_score,
        )
    except Exception as error:
        for bridge in bridges:
            if not bridge.done.is_set():
                bridge.cancel()
        # Give worker threads a bounded chance to persist their terminal error.
        # Without this, the handler failure file can capture a stale "streaming"
        # state even though cancellation/failure is already in progress.
        for bridge in bridges:
            bridge.done.wait(2)
        stream_results = [bridge.snapshot() for bridge in bridges]
        state.update(
            {
                "state": "failed",
                "failed_at": _utc_now(),
                "error_type": type(error).__name__,
                "error": str(error),
                "failure_summary": {
                    result["label"]: result.get("failure_category", result.get("state"))
                    for result in stream_results
                },
                "stream_results": stream_results,
            }
        )
        _write_json(bridge_status_path, state)
        _write_json(bridge_failure_path, state)
        _append_event(
            bridge_events_path,
            "bridge_failed",
            error_type=type(error).__name__,
            error=str(error),
            failure_summary=state["failure_summary"],
            streams=stream_results,
        )
        logger.exception("Seed bridge failed for task %s", task_dir.name)


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    active: dict[str, threading.Thread] = {}
    logger.info(
        "Seed bridge watching %s builder_guide_variants=%d primary_cas_share=%.2f "
        "mode=%s fast_focus=%d fast_optimizer=%.1fs exploration=%s streams=%s "
        "standby_max_delay=%.1fs",
        ARTIFACT_ROOT.resolve(),
        GUIDE_VARIANTS_PER_TARGET,
        PRIMARY_CAS_SHARE,
        "observe-chain-seeds" if OBSERVE_CHAIN_SEEDS_ONLY else "submit-chain-seeds",
        FAST_SEED_FOCUSED_VARIANTS_PER_ANCHOR,
        FAST_SEED_OPTIMIZER_TIME_BUDGET_SECONDS,
        "common+residual" if EXPLORATION_PROFILE else "common-champion",
        ",".join(profile[0] for profile in STREAM_PROFILES),
        STANDBY_MAX_START_DELAY_SECONDS,
    )
    quarantined = _quarantine_inherited_unknown_seed_tasks()
    if quarantined:
        logger.warning(
            "Quarantined %d inherited unknown-seed bridge task(s)", quarantined
        )
    while True:
        for envelope_path in sorted(ARTIFACT_ROOT.glob("*/request_envelope.json")):
            task_id = envelope_path.parent.name
            if task_id in active:
                continue
            try:
                remaining = _envelope_seconds_remaining(envelope_path)
            except (KeyError, OSError, ValueError, json.JSONDecodeError) as error:
                logger.warning("Could not inspect envelope %s: %s", envelope_path, error)
                continue
            if remaining < OPEN_MINIMUM_REMAINING_SECONDS:
                continue
            thread = threading.Thread(
                target=_handle_envelope,
                args=(envelope_path,),
                daemon=True,
                name=f"niome-seed-bridge-{task_id[:8]}",
            )
            active[task_id] = thread
            thread.start()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
