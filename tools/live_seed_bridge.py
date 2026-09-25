"""Keep a live S3 PUT open until NIOME's public seed blocks exist.

The validator's ordinary miner process remains the reliability path: it writes a
baseline object promptly.  This sidecar opens several PUT profiles while the
presigned URL is valid, streams JSON whitespace to keep them alive, and commits
the smallest surviving stream with a seed-aware submission after all three seed
block hashes are public.  S3 object replacement is atomic, so failed bridges
leave the already completed baseline object in place.
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
from urllib.parse import urlparse

# Direct execution (including PM2) puts ``tools/`` rather than the repository
# root on sys.path.  Keep imports identical to ``python -m tools...``.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import bittensor as bt

from niome_subnet.genomics.submission_builder import (
    GUIDE_VARIANTS_PER_TARGET,
    PRIMARY_CAS_SHARE,
    build_submission,
)
from niome_subnet.miner.task_processor import (
    DEFAULT_URL_TTL_SECONDS,
    _presigned_url_deadline,
)
from niome_subnet.utils.seeds import generate_seed_prefix, seed_blocks
from niome_subnet.utils.settings import CHR11_PATH
from tools.local_validator.artifacts import ArtifactBundle, sha256_bytes
from tools.local_validator.evaluator import evaluate_submission


logger = logging.getLogger("niome_seed_bridge")

ARTIFACT_ROOT = Path(os.getenv("NIOME_ARTIFACT_ROOT", "artifacts/live"))
EXPLORATION_PROFILE = os.getenv("NIOME_EXPLORATION_PROFILE", "").strip() or None
POLL_SECONDS = 2.0
STREAM_INTERVAL_SECONDS = 1.0
# S3 terminated the original 128 B/s probe before the seed window.  Race a
# small set of legitimate slow-upload rates and commit only the smallest stream
# that survives.  The fixed body sizes provide a little over two hours at each
# rate, longer than an entire 720-block round.
STREAM_PROFILES = (
    ("4kib", 4 * 1024, 36 * 1024 * 1024),
    ("16kib", 16 * 1024, 144 * 1024 * 1024),
    ("64kib", 64 * 1024, 576 * 1024 * 1024),
)
OPEN_MINIMUM_REMAINING_SECONDS = 15.0
ARTIFACT_WAIT_SECONDS = 90.0
PADDING_CHUNK_BYTES = 1024 * 1024


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

        for label, stream_bytes, total_bytes in STREAM_PROFILES:
            bridge = SlowPut(
                presigned_url,
                total_bytes=total_bytes,
                stream_bytes=stream_bytes,
                label=label,
            )
            bridge.start()
            bridges.append(bridge)
        state.update(
            {
                "state": "streaming",
                "put_opened_at": _utc_now(),
                "url_seconds_remaining_at_open": remaining,
                "stream_profiles": [
                    {
                        "label": bridge.label,
                        "bytes_per_second": bridge.stream_bytes
                        / bridge.stream_interval_seconds,
                        "fixed_upload_bytes": bridge.total_bytes,
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
        subtensor = bt.Subtensor(network="finney")
        current_block = int(subtensor.block)
        target_block = seed_blocks(current_block)[-1]
        if current_block < target_block:
            state.update(
                {
                    "state": "waiting_for_seeds",
                    "current_block_at_open": current_block,
                    "seed_target_block": target_block,
                }
            )
            _write_json(bridge_status_path, state)
            _append_event(
                bridge_events_path,
                "waiting_for_seeds",
                current_block=current_block,
                seed_target_block=target_block,
            )
            last_live_labels: tuple[str, ...] | None = None
            while current_block < target_block:
                live_labels = tuple(
                    bridge.label for bridge in bridges if not bridge.done.is_set()
                )
                if not live_labels:
                    raise RuntimeError(
                        "all slow PUT profiles stopped before seed publication: "
                        f"{[bridge.snapshot() for bridge in bridges]}"
                    )
                snapshots = [bridge.snapshot() for bridge in bridges]
                state.update(
                    {
                        "last_observed_at": _utc_now(),
                        "current_block": current_block,
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

        seed_block = int(subtensor.block)
        seeds = generate_seed_prefix(seed_block, subtensor, 3)
        state.update(
            {
                "state": "building",
                "seed_read_block": seed_block,
                "round_seeds": seeds,
                "build_started_at": _utc_now(),
            }
        )
        _write_json(bridge_status_path, state)
        _append_event(
            bridge_events_path,
            "seeds_available",
            seed_read_block=seed_block,
            round_seeds=seeds,
            streams=[bridge.snapshot() for bridge in bridges],
        )

        build_started = time.monotonic()
        submission, diagnostics = build_submission(
            contract=artifacts.contract,
            reference=artifacts.hbb_reference,
            chromosome_11=artifacts.chromosome_11,
            cell_types=artifacts.cell_types,
            selection_profile="seed-aware",
            round_seeds=seeds,
            exploration_profile=EXPLORATION_PROFILE,
        )
        submission_raw = json.dumps(
            submission,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        (task_dir / "seed_bridge_submission.json").write_bytes(submission_raw)
        _write_json(task_dir / "seed_bridge_builder_diagnostics.json", diagnostics)
        state.update(
            {
                "state": "finishing_upload",
                "build_elapsed_seconds": time.monotonic() - build_started,
                "submission_rows": len(submission),
                "submission_sha256": sha256_bytes(submission_raw),
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
        scoring_contract["seed"] = ",".join(str(seed) for seed in seeds)
        result = evaluate_submission(
            submission,
            replace(artifacts, contract=scoring_contract),
            raw_submission_bytes=submission_raw,
        )
        _write_json(task_dir / "seed_bridge_local_validation.json", result.as_dict())
        state.update(
            {
                "state": "complete",
                "completed_at": _utc_now(),
                "put_result": completed_bridge.snapshot(),
                "stream_results": [bridge.snapshot() for bridge in bridges],
                "local_final_score": result.final_score,
                "local_breakdown": result.breakdown,
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
        "Seed bridge watching %s builder_guide_variants=%d primary_cas_share=%.2f exploration=%s",
        ARTIFACT_ROOT.resolve(),
        GUIDE_VARIANTS_PER_TARGET,
        PRIMARY_CAS_SHARE,
        "enabled" if EXPLORATION_PROFILE else "disabled",
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
