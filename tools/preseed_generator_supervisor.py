#!/usr/bin/env python3
"""Continuously join public tasks to finalized chain blocks and run Track A.

This supervisor is the network-facing collection layer.  It reads only the
public task history and public finalized Finney headers, writes auditable split
files, then invokes the offline pre-seed laboratory in-process.  It never reads
wallets, scores for prediction, signed URLs, or submission channels.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sys
import time
from typing import Any, Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from niome_subnet.utils.misc import FINALITY_LAG

try:
    from tools.preseed_generator_lab import (
        attach_chain_context,
        atomic_json,
        load_ledger,
        load_registry,
        normalize_task,
        run_lab,
    )
    from tools.seed_prng_fingerprint import fetch_tasks, safe_record
    from tools.seed_epoch_policy import (
        CURRENT_EPOCH_STARTED_AT,
        configured_epoch_view,
        is_current_epoch_time,
        public_policy_metadata,
        valid_current_seed_label,
    )
    from tools.public_block_header import public_header
except ModuleNotFoundError:
    from preseed_generator_lab import (
        attach_chain_context,
        atomic_json,
        load_ledger,
        load_registry,
        normalize_task,
        run_lab,
    )
    from seed_prng_fingerprint import fetch_tasks, safe_record
    from seed_epoch_policy import (
        CURRENT_EPOCH_STARTED_AT,
        configured_epoch_view,
        is_current_epoch_time,
        public_policy_metadata,
        valid_current_seed_label,
    )
    from public_block_header import public_header


DEFAULT_SPLIT_MANIFEST = (
    Path(__file__).resolve().parents[1] / "data" / "preseed_discovery_manifest.json"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def info_time(info: Any) -> datetime | None:
    value = getattr(info, "timestamp", None)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return parse_time(str(value or ""))


def public_block(info: Any) -> dict[str, Any]:
    block_hash = str(info.hash)
    if not block_hash.startswith("0x") or len(block_hash) != 66:
        raise ValueError("unexpected public block hash")
    timestamp = info_time(info)
    result = {
        "number": int(info.number),
        "hash": block_hash,
        "timestamp": timestamp.isoformat() if timestamp else None,
        "observed_at": utc_now(),
    }
    header = public_header(info)
    if header is not None:
        result["header"] = header
    return result


def refresh_context_headers(
    context: Mapping[str, Any],
    *,
    finalized_head: int,
    block_info: Callable[[int], Any],
) -> dict[str, Any]:
    """Backfill public headers into a cached task context by exact height."""
    refreshed = dict(context)
    roles = {
        str(role): dict(material)
        for role, material in (context.get("block_context") or {}).items()
        if isinstance(material, Mapping)
    }
    created = roles.get("created") or {}
    created_number = created.get("number")
    if isinstance(created_number, int):
        expected = {
            "created_minus_1": max(0, created_number - 1),
            "created": created_number,
            "created_plus_1": created_number + 1,
            "round_start": created_number,
            "validation": created_number + 450,
        }
        for role, number in expected.items():
            current = roles.get(role) or {}
            if number > finalized_head or current.get("header"):
                continue
            info = block_info(number)
            if info is not None:
                roles[role] = public_block(info)
    refreshed["block_context"] = roles
    refreshed["header_material_version"] = 1
    refreshed["refreshed_at"] = utc_now()
    return refreshed


def locate_floor_block(
    target: datetime,
    *,
    lower: int,
    upper: int,
    block_info: Callable[[int], Any],
) -> int | None:
    """Return the greatest block whose public timestamp is <= target."""
    lower = max(0, int(lower))
    upper = max(lower, int(upper))
    lower_info = block_info(lower)
    upper_info = block_info(upper)
    lower_time = info_time(lower_info) if lower_info is not None else None
    upper_time = info_time(upper_info) if upper_info is not None else None
    if lower_time is None or upper_time is None or target < lower_time:
        return None
    if target >= upper_time:
        return upper
    result = lower
    while lower <= upper:
        middle = (lower + upper) // 2
        info = block_info(middle)
        timestamp = info_time(info) if info is not None else None
        if timestamp is None:
            upper = middle - 1
        elif timestamp <= target:
            result = middle
            lower = middle + 1
        else:
            upper = middle - 1
    return result


def context_for_task(
    task_id: str,
    created_at: str,
    *,
    finalized_head: int,
    max_age_blocks: int,
    block_info: Callable[[int], Any],
) -> dict[str, Any] | None:
    created_time = parse_time(created_at)
    if created_time is None:
        return None
    created_block = locate_floor_block(
        created_time,
        lower=max(0, finalized_head - max(1, max_age_blocks)),
        upper=finalized_head,
        block_info=block_info,
    )
    if created_block is None:
        return None
    roles: dict[str, dict[str, Any]] = {}
    block_numbers = {
        "created_minus_1": max(0, created_block - 1),
        "created": created_block,
        "created_plus_1": created_block + 1,
        "round_start": created_block,
        "validation": created_block + 450,
    }
    for role, number in block_numbers.items():
        if number > finalized_head:
            continue
        info = block_info(number)
        if info is not None:
            roles[role] = public_block(info)
    return {
        "task_id": task_id,
        "task_created_at": created_at,
        "located_at": utc_now(),
        "method": "public-finalized-timestamp-floor-v1",
        "block_context": roles,
    }


def load_cache(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("tasks"), dict):
            return value
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return {"schema_version": 1, "tasks": {}}


def valid_seed_label(record: Mapping[str, Any]) -> bool:
    return valid_current_seed_label(record)


def task_record(task: dict[str, Any]) -> dict[str, Any]:
    record = safe_record(task)
    record["score_published_at"] = (
        str(task.get("updated_at") or task.get("created_at") or "")
        if record["seeds"]
        else None
    )
    return record


def freeze_split_manifest(
    tasks: list[dict[str, Any]], existing: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """Freeze the first valid 20+5 labels in this epoch exactly once.

    A rolling ``last N`` window is useful for bounded RPC work but must never
    redefine discovery or holdout after a new task appears.  The manifest is
    persisted in the collector cache and IDs, rather than positions, own the
    split from that point forward.
    """
    if isinstance(existing, Mapping):
        discovery = [str(value) for value in existing.get("discovery") or []]
        holdout = [str(value) for value in existing.get("holdout") or []]
        if len(discovery) == 20 and len(holdout) == 5 and not (set(discovery) & set(holdout)):
            return dict(existing)
    labeled = sorted(
        (
            task
            for task in tasks
            if is_current_epoch_time(str(task.get("created_at") or ""))
            and valid_seed_label(safe_record(task))
        ),
        key=lambda task: str(task.get("created_at") or ""),
    )
    if len(labeled) < 25:
        return None
    return {
        "version": 1,
        "epoch_started_at": CURRENT_EPOCH_STARTED_AT,
        "frozen_at": utc_now(),
        "discovery": [str(task["id"]) for task in labeled[:20]],
        "holdout": [str(task["id"]) for task in labeled[20:25]],
    }


def load_split_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("split manifest must be an object")
    discovery = [str(item) for item in value.get("discovery") or []]
    holdout = [str(item) for item in value.get("holdout") or []]
    if len(discovery) != 20 or len(holdout) != 5:
        raise ValueError("split manifest must contain exactly 20 discovery and 5 holdout IDs")
    if len(set(discovery)) != 20 or len(set(holdout)) != 5 or set(discovery) & set(holdout):
        raise ValueError("split manifest IDs must be unique and disjoint")
    return {**dict(value), "discovery": discovery, "holdout": holdout}


def research_task_window(
    tasks: list[dict[str, Any]],
    labeled_limit: int = 30,
    pinned_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Bound RPC work to the configured current epoch only."""
    ordered = sorted(
        (
            task
            for task in tasks
            if is_current_epoch_time(str(task.get("created_at") or ""))
        ),
        key=lambda task: str(task.get("created_at") or ""),
    )
    labeled = [task for task in ordered if valid_seed_label(safe_record(task))]
    seedless = [task for task in ordered if not safe_record(task).get("seeds")]
    pinned_ids = pinned_ids or set()
    pinned = [task for task in ordered if str(task.get("id") or "") in pinned_ids]
    selected = [*pinned, *labeled[-max(25, labeled_limit) :], *seedless[-3:]]
    by_id = {str(task.get("id") or ""): task for task in selected if task.get("id")}
    return sorted(by_id.values(), key=lambda task: str(task.get("created_at") or ""))


def split_records(
    tasks: list[dict[str, Any]],
    context_by_task: Mapping[str, Any],
    manifest: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    records = sorted(
        (
            task_record(task)
            for task in tasks
            if is_current_epoch_time(str(task.get("created_at") or ""))
        ),
        key=lambda row: row["created_at"],
    )
    joined = []
    for record in records:
        context = context_by_task.get(record["task_id"])
        if not isinstance(context, Mapping) or not context.get("block_context"):
            continue
        raw = {**record, "block_context": context["block_context"]}
        try:
            joined.append(normalize_task(raw))
        except ValueError:
            continue
    labeled = [record for record in joined if valid_seed_label(record)]
    if isinstance(manifest, Mapping):
        by_id = {record["task_id"]: record for record in labeled}
        discovery = [
            by_id[task_id]
            for task_id in manifest.get("discovery") or []
            if task_id in by_id
        ]
        holdout = [
            by_id[task_id]
            for task_id in manifest.get("holdout") or []
            if task_id in by_id
        ]
    else:
        discovery = labeled[:20]
        holdout = labeled[20:25]
    reserved_ids = {record["task_id"] for record in (*discovery, *holdout)}
    prospective = [record for record in joined if record["task_id"] not in reserved_ids]
    return discovery, holdout, prospective


def write_dataset(path: Path, rows: list[dict[str, Any]]) -> None:
    atomic_json(path, {"generated_at": utc_now(), "records": rows})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--registry-json", type=Path)
    parser.add_argument("--split-manifest", type=Path, default=DEFAULT_SPLIT_MANIFEST)
    parser.add_argument("--network", default="finney")
    parser.add_argument("--poll-interval", type=float, default=30.0)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--max-age-blocks", type=int, default=50_000)
    parser.add_argument("--finality-lag", type=int, default=FINALITY_LAG)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    import bittensor as bt

    report_path = args.report.resolve()
    ledger_path = args.ledger.resolve()
    cache_path = args.cache.resolve()
    dataset_root = args.dataset_root.resolve()
    registry = load_registry(args.registry_json.resolve() if args.registry_json else None)
    configured_manifest = load_split_manifest(args.split_manifest.resolve())
    cache = load_cache(cache_path)
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    subtensor = bt.Subtensor(network=args.network)
    try:
        while not stopping:
            try:
                fetched_tasks = fetch_tasks(max_pages=max(1, args.max_pages))
                policy_view = configured_epoch_view(
                    [safe_record(task) for task in fetched_tasks]
                )
                policy_metadata = public_policy_metadata(policy_view)
                previous_policy = cache.get("epoch_policy")
                if (
                    isinstance(previous_policy, Mapping)
                    and previous_policy.get("id") == policy_metadata.get("id")
                    and previous_policy.get("gate_open") is False
                ):
                    policy_metadata["status"] = "change-detected"
                    policy_metadata["gate_open"] = False
                    policy_metadata["violation"] = previous_policy.get("violation")
                    policy_metadata["latched"] = True
                if cache.get("epoch_policy") != policy_metadata:
                    cache["epoch_policy"] = policy_metadata
                    cache["updated_at"] = utc_now()
                    atomic_json(cache_path, cache)
                manifest = configured_manifest or freeze_split_manifest(
                    fetched_tasks, cache.get("split_manifest")
                )
                if manifest is not None and cache.get("split_manifest") != manifest:
                    cache["split_manifest"] = manifest
                    cache["updated_at"] = utc_now()
                    atomic_json(cache_path, cache)
                pinned_ids = set()
                if manifest is not None:
                    pinned_ids.update(manifest.get("discovery") or [])
                    pinned_ids.update(manifest.get("holdout") or [])
                tasks = research_task_window(fetched_tasks, pinned_ids=pinned_ids)
                finalized = max(0, int(subtensor.block) - max(0, args.finality_lag))
                for task in tasks:
                    record = safe_record(task)
                    task_id = record["task_id"]
                    if not task_id:
                        continue
                    existing = cache["tasks"].get(task_id)
                    if isinstance(existing, Mapping):
                        refreshed = refresh_context_headers(
                            existing,
                            finalized_head=finalized,
                            block_info=subtensor.block_info,
                        )
                        if refreshed.get("block_context") != existing.get("block_context"):
                            cache["tasks"][task_id] = refreshed
                            cache["updated_at"] = utc_now()
                            atomic_json(cache_path, cache)
                        continue
                    context = context_for_task(
                        task_id,
                        record["created_at"],
                        finalized_head=finalized,
                        max_age_blocks=args.max_age_blocks,
                        block_info=subtensor.block_info,
                    )
                    if context is not None:
                        cache["tasks"][task_id] = context
                        cache["updated_at"] = utc_now()
                        atomic_json(cache_path, cache)
                discovery, holdout, prospective = split_records(
                    tasks, cache["tasks"], manifest=manifest
                )
                dataset_root.mkdir(parents=True, exist_ok=True)
                write_dataset(dataset_root / "discovery.json", discovery)
                write_dataset(dataset_root / "holdout.json", holdout)
                write_dataset(dataset_root / "prospective.json", prospective)
                report, ledger = run_lab(
                    discovery=discovery,
                    holdout=holdout,
                    prospective=prospective,
                    registry=registry,
                    ledger=load_ledger(ledger_path),
                    epoch_policy=policy_metadata,
                )
                report["collector"] = {
                    "status": "healthy",
                    "finalized_head": finalized,
                    "cached_task_contexts": len(cache["tasks"]),
                    "updated_at": utc_now(),
                    "public_chain_only": True,
                    "configured_epoch_started_at": CURRENT_EPOCH_STARTED_AT,
                }
                atomic_json(ledger_path, ledger)
                atomic_json(report_path, report)
                print(
                    json.dumps(
                        {
                            "event": "preseed_generator_updated",
                            "discovery": len(discovery),
                            "holdout": len(holdout),
                            "prospective": len(prospective),
                            "models_registered": report["evaluation"]["models_registered"],
                            "models_evaluated": report["evaluation"]["models_fully_evaluable"],
                            "models_input_blocked": report["evaluation"]["models_input_blocked"],
                        }
                    ),
                    flush=True,
                )
            except Exception as error:
                previous = {}
                try:
                    previous = json.loads(report_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    pass
                previous["collector"] = {
                    "status": "error",
                    "error_type": type(error).__name__,
                    "updated_at": utc_now(),
                    "public_chain_only": True,
                }
                atomic_json(report_path, previous)
                print(json.dumps({"event": "preseed_generator_error", "type": type(error).__name__}), flush=True)
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(max(5.0, args.poll_interval))
    finally:
        close = getattr(subtensor, "close", None)
        if callable(close):
            close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
