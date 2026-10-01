#!/usr/bin/env python3
"""Offline, public-input-only laboratory for pre-score seed hypotheses.

This tool deliberately has no HTTP client and no submission path.  It evaluates
an explicit registry of deterministic hypotheses against disjoint discovery and
holdout datasets, records limited stateful-sequence diagnostics, and may write a
prospective *shadow* prediction only when exactly one hypothesis passes both
gates.  Chain material must be supplied by the operator as offline JSON.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from tools.seed_epoch_policy import (
        CURRENT_EPOCH_ID,
        CURRENT_EPOCH_STARTED_AT,
        configured_epoch_view,
        is_current_epoch_time,
        public_policy_metadata,
        valid_current_seed_label,
    )
except ModuleNotFoundError:
    from seed_epoch_policy import (
        CURRENT_EPOCH_ID,
        CURRENT_EPOCH_STARTED_AT,
        configured_epoch_view,
        is_current_epoch_time,
        public_policy_metadata,
        valid_current_seed_label,
    )


QUARANTINED_STATELESS_CANDIDATES = 363
SEED_LOW = 100
SEED_HIGH = 999
ALLOWED_BLOCK_ROLES = (
    "created_minus_1",
    "created",
    "created_plus_1",
    "round_start",
    "validation",
)
FORBIDDEN_INPUT_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "password",
    "presigned_url",
    "secret",
    "signed_url",
    "token",
}
ALLOWED_COMPONENTS = {
    "task.id",
    "task.created_at",
    "task.contract",
    *(f"block.{role}.hash" for role in ALLOWED_BLOCK_ROLES),
    *(f"block.{role}.header" for role in ALLOWED_BLOCK_ROLES),
    *(f"block.{role}.number" for role in ALLOWED_BLOCK_ROLES),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def atomic_json(path: Path, value: Any) -> None:
    """Write JSON using same-directory replace so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _key_token(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")


def reject_private_material(value: Any, location: str = "input") -> None:
    """Reject credential-bearing documents before they can enter an artifact."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            token = _key_token(key)
            if token in FORBIDDEN_INPUT_KEYS:
                raise ValueError(f"private input key is forbidden at {location}.{key}")
            reject_private_material(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_private_material(child, f"{location}[{index}]")


def _parse_seeds(raw: Any) -> list[int]:
    if raw in (None, "", 0, "0"):
        return []
    if isinstance(raw, list):
        values = raw
    else:
        values = [part.strip() for part in str(raw).split(",") if part.strip()]
    try:
        parsed = [int(value) for value in values]
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid seed value: {raw!r}") from error
    if parsed and (len(parsed) != 3 or len(set(parsed)) != 3):
        raise ValueError("seed label must be an empty value or three distinct integers")
    if any(value < SEED_LOW or value > SEED_HIGH for value in parsed):
        raise ValueError(f"seed labels must be within {SEED_LOW}..{SEED_HIGH}")
    return parsed


def _records_from_document(document: Any) -> list[dict[str, Any]]:
    reject_private_material(document)
    if isinstance(document, list):
        records = document
    elif isinstance(document, Mapping):
        records = document.get("records")
        if records is None:
            records = document.get("items")
        if records is None and document.get("id"):
            records = [document]
    else:
        records = None
    if not isinstance(records, list):
        raise ValueError("task JSON must be a list or contain an items/records list")
    return records


def normalize_task(raw: Mapping[str, Any]) -> dict[str, Any]:
    content = raw.get("content") if isinstance(raw.get("content"), Mapping) else {}
    contract = content.get("contract") if isinstance(content.get("contract"), Mapping) else {}
    task_id = str(raw.get("task_id") or raw.get("id") or "")
    if not task_id:
        raise ValueError("task_id/id is required")
    created_at = str(raw.get("created_at") or "")
    seeds = _parse_seeds(raw.get("seeds", contract.get("seed")))
    contract_value = raw.get("contract_material")
    if contract_value is None:
        # Preserve only the public contract body; seed is removed to prevent a
        # hypothesis from trivially consuming its own label.
        contract_value = {key: value for key, value in contract.items() if key != "seed"}
    elif isinstance(contract_value, str):
        try:
            decoded = json.loads(contract_value)
            if isinstance(decoded, Mapping):
                contract_value = {key: value for key, value in decoded.items() if key != "seed"}
        except json.JSONDecodeError:
            pass
    block_context = raw.get("block_context") or raw.get("blocks") or {}
    if not isinstance(block_context, Mapping):
        raise ValueError(f"block_context must be an object for {task_id}")
    return {
        "task_id": task_id,
        "created_at": created_at,
        "score_published_at": raw.get("score_published_at"),
        "seeds": seeds,
        "contract_material": _canonical(contract_value),
        "block_context": _normalize_block_context(block_context),
    }


def _normalize_block_context(raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for role, material in raw.items():
        if role not in ALLOWED_BLOCK_ROLES:
            continue
        if isinstance(material, str):
            material = {"hash": material}
        if not isinstance(material, Mapping):
            raise ValueError(f"block role {role} must be a hash string or object")
        selected = {
            key: material.get(key)
            for key in ("number", "hash", "header", "observed_at")
            if material.get(key) is not None
        }
        result[role] = selected
    return result


def load_tasks(path: Path) -> list[dict[str, Any]]:
    return [normalize_task(raw) for raw in _records_from_document(read_json(path))]


def load_chain_context(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    document = read_json(path)
    reject_private_material(document, "chain_input")
    if isinstance(document, Mapping) and isinstance(document.get("tasks"), Mapping):
        iterable = [
            {"task_id": task_id, "block_context": context}
            for task_id, context in document["tasks"].items()
        ]
    elif isinstance(document, Mapping) and isinstance(document.get("records"), list):
        iterable = document["records"]
    elif isinstance(document, list):
        iterable = document
    else:
        raise ValueError("chain JSON must be a list or contain tasks/records")
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for item in iterable:
        if not isinstance(item, Mapping):
            raise ValueError("chain records must be objects")
        task_id = str(item.get("task_id") or item.get("id") or "")
        context = item.get("block_context") or item.get("blocks")
        if not task_id or not isinstance(context, Mapping):
            raise ValueError("each chain record needs task_id and block_context")
        result[task_id] = _normalize_block_context(context)
    return result


def attach_chain_context(
    records: Sequence[dict[str, Any]],
    chain_sources: Iterable[Mapping[str, Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for source in chain_sources:
        for task_id, context in source.items():
            target = merged.setdefault(str(task_id), {})
            target.update(context)
    attached = []
    for record in records:
        clone = dict(record)
        context = dict(record.get("block_context") or {})
        context.update(merged.get(record["task_id"], {}))
        clone["block_context"] = context
        attached.append(clone)
    return attached


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    family: str
    components: tuple[str, ...]
    algorithm: str = "sha256"
    byteorder: str = "big"
    extraction: str = "counter64"
    delimiter: str = "\x1f"
    seed_low: int = SEED_LOW
    seed_high: int = SEED_HIGH

    def validate(self) -> None:
        if not self.model_id or self.family not in {
            "public-digest-triplet-v1",
            "public-component-digest-mix-v1",
        }:
            raise ValueError(f"unsupported model: {self.model_id!r}")
        if not self.components or any(value not in ALLOWED_COMPONENTS for value in self.components):
            raise ValueError(f"model {self.model_id} has a forbidden/unknown component")
        if self.algorithm not in {"sha256", "sha512", "blake2b"}:
            raise ValueError(f"unsupported digest algorithm in {self.model_id}")
        if self.byteorder not in {"big", "little"}:
            raise ValueError(f"unsupported byte order in {self.model_id}")
        if self.extraction not in {"counter64", "digest-chunks64"}:
            raise ValueError(f"unsupported extraction in {self.model_id}")
        if self.seed_low < 0 or self.seed_high <= self.seed_low:
            raise ValueError(f"invalid seed domain in {self.model_id}")


def default_model_registry() -> list[ModelSpec]:
    """Return a stable, explicit family registry for public block material.

    These are new chain-aware hypotheses, not an expansion of the quarantined
    UUID/time-only 363-candidate baseline.
    """
    specs: list[ModelSpec] = []
    for role in ALLOWED_BLOCK_ROLES:
        for block_field in ("hash", "header"):
            block_component = f"block.{role}.{block_field}"
            for task_component in ("task.id", "task.contract"):
                for order_name, components in (
                    ("block-task", (block_component, task_component)),
                    ("task-block", (task_component, block_component)),
                ):
                    for algorithm in ("sha256", "blake2b"):
                        for byteorder in ("big", "little"):
                            for extraction in ("counter64", "digest-chunks64"):
                                for family_name, family in (
                                    ("concat", "public-digest-triplet-v1"),
                                    ("component-mix", "public-component-digest-mix-v1"),
                                ):
                                    model_id = ":".join(
                                        (
                                            "chain-v1",
                                            role,
                                            block_field,
                                            task_component.split(".")[-1],
                                            order_name,
                                            family_name,
                                            algorithm,
                                            byteorder,
                                            extraction,
                                        )
                                    )
                                    specs.append(
                                        ModelSpec(
                                            model_id=model_id,
                                            family=family,
                                            components=components,
                                            algorithm=algorithm,
                                            byteorder=byteorder,
                                            extraction=extraction,
                                        )
                                    )
    return specs


def load_registry(path: Path | None) -> list[ModelSpec]:
    if path is None:
        specs = default_model_registry()
    else:
        document = read_json(path)
        reject_private_material(document, "model_registry")
        rows = document.get("models") if isinstance(document, Mapping) else document
        if not isinstance(rows, list):
            raise ValueError("registry JSON must be a list or contain models")
        specs = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("registry entries must be objects")
            normalized = dict(row)
            normalized["components"] = tuple(normalized.get("components") or ())
            specs.append(ModelSpec(**normalized))
    identifiers: set[str] = set()
    for spec in specs:
        spec.validate()
        if spec.model_id in identifiers:
            raise ValueError(f"duplicate model id: {spec.model_id}")
        identifiers.add(spec.model_id)
    return specs


def _component(record: Mapping[str, Any], name: str) -> str | None:
    if name == "task.id":
        return str(record.get("task_id") or "") or None
    if name == "task.created_at":
        return str(record.get("created_at") or "") or None
    if name == "task.contract":
        return str(record.get("contract_material") or "") or None
    _, role, field = name.split(".", 2)
    value = ((record.get("block_context") or {}).get(role) or {}).get(field)
    if value is None or value == "":
        return None
    return _canonical(value)


def _digest(algorithm: str, data: bytes) -> bytes:
    return hashlib.new(algorithm, data).digest()


def predict_model(spec: ModelSpec, record: Mapping[str, Any]) -> list[int] | None:
    values = [_component(record, name) for name in spec.components]
    if any(value is None for value in values):
        return None
    if spec.family == "public-component-digest-mix-v1":
        # Hash each public component independently before combining it.  This is
        # a distinct, domain-separated family rather than another delimiter
        # variation of the direct concatenation family.
        material = b"".join(
            len(digest).to_bytes(2, "big") + digest
            for digest in (
                _digest(spec.algorithm, f"component:{index}:".encode() + (value or "").encode())
                for index, value in enumerate(values)
            )
        )
    else:
        material = spec.delimiter.join(value or "" for value in values).encode()
    span = spec.seed_high - spec.seed_low + 1
    seeds: list[int] = []
    if spec.extraction == "counter64":
        counter = 0
        while len(seeds) < 3:
            digest = _digest(spec.algorithm, material + counter.to_bytes(4, "big"))
            candidate = spec.seed_low + int.from_bytes(digest[:8], spec.byteorder) % span
            counter += 1
            if candidate not in seeds:
                seeds.append(candidate)
    else:
        digest = _digest(spec.algorithm, material)
        offset = 0
        while len(seeds) < 3:
            if offset + 8 > len(digest):
                digest = _digest(spec.algorithm, digest)
                offset = 0
            candidate = spec.seed_low + int.from_bytes(
                digest[offset : offset + 8], spec.byteorder
            ) % span
            offset += 8
            if candidate not in seeds:
                seeds.append(candidate)
    return seeds


def evaluate_model(spec: ModelSpec, records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    stats = {
        "records": len(records),
        "evaluable": 0,
        "missing_inputs": 0,
        "exact_ordered": 0,
        "exact_unordered": 0,
        "position_matches": 0,
    }
    for record in records:
        expected = list(record.get("seeds") or [])
        if len(expected) != 3:
            raise ValueError("discovery/holdout records must have three seed labels")
        predicted = predict_model(spec, record)
        if predicted is None:
            stats["missing_inputs"] += 1
            continue
        stats["evaluable"] += 1
        stats["exact_ordered"] += int(predicted == expected)
        stats["exact_unordered"] += int(sorted(predicted) == sorted(expected))
        stats["position_matches"] += sum(left == right for left, right in zip(predicted, expected))
    return stats


def _passes(stats: Mapping[str, int], expected_count: int) -> bool:
    return (
        stats["records"] == expected_count
        and stats["evaluable"] == expected_count
        and stats["missing_inputs"] == 0
        and stats["exact_ordered"] == expected_count
    )


def _best_lcg(values: Sequence[int], modulus: int, offset: int) -> dict[str, Any]:
    normalized = [value - offset for value in values]
    transitions = max(0, len(normalized) - 1)
    best = {"a": None, "c": None, "matches": 0, "transitions": transitions}
    if transitions == 0:
        return {**best, "falsified_on_observed_sequence": None}
    for multiplier in range(modulus):
        constants = Counter(
            (right - multiplier * left) % modulus
            for left, right in zip(normalized, normalized[1:])
        )
        constant, matches = constants.most_common(1)[0]
        if matches > best["matches"]:
            best = {"a": multiplier, "c": constant, "matches": matches, "transitions": transitions}
    best["falsified_on_observed_sequence"] = best["matches"] < transitions
    return best


def _xorshift(value: int, width: int, shifts: tuple[int, int, int]) -> int:
    mask = (1 << width) - 1
    left, middle, right = shifts
    value ^= (value << left) & mask
    value ^= value >> middle
    value ^= (value << right) & mask
    return value & mask


def stateful_sequence_diagnostics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [seed for record in records for seed in record.get("seeds") or []]
    xorshift_rows = []
    for width, shifts in ((10, (3, 7, 5)), (10, (5, 3, 7)), (16, (7, 9, 8)), (16, (5, 7, 9))):
        matches = sum(
            SEED_LOW + _xorshift(left - SEED_LOW, width, shifts) % (SEED_HIGH - SEED_LOW + 1)
            == right
            for left, right in zip(values, values[1:])
        )
        transitions = max(0, len(values) - 1)
        xorshift_rows.append(
            {
                "width": width,
                "shifts": list(shifts),
                "matches": matches,
                "transitions": transitions,
                "falsified_on_observed_sequence": matches < transitions if transitions else None,
            }
        )
    return {
        "diagnostic_only": True,
        "claim": "Falsifies only the listed direct-output recurrences on the observed sequence.",
        "limitations": (
            "It does not exclude hidden state, truncated/reduced outputs, skipped draws, "
            "worker interleaving, reseeding, or other LCG/xorshift parameterizations."
        ),
        "seed_values": len(values),
        "lcg": {
            "mod-900-offset-100": _best_lcg(values, 900, 100),
            "mod-1000-offset-0": _best_lcg(values, 1000, 0),
            "mod-1001-offset-0": _best_lcg(values, 1001, 0),
        },
        "xorshift_like": xorshift_rows,
    }


def _registry_digest(registry: Sequence[ModelSpec]) -> str:
    encoded = json.dumps(
        [asdict(spec) for spec in registry], sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _record_commitment(record: Mapping[str, Any]) -> str:
    safe = {
        "task_id": record["task_id"],
        "created_at": record.get("created_at"),
        "score_published_at": record.get("score_published_at"),
        "contract_material": record.get("contract_material"),
        "block_context": record.get("block_context"),
    }
    return hashlib.sha256(_canonical(safe).encode()).hexdigest()


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "predictions": []}
    value = read_json(path)
    if not isinstance(value, Mapping) or not isinstance(value.get("predictions"), list):
        raise ValueError("ledger must be an object containing predictions")
    return {"version": 1, "predictions": [dict(row) for row in value["predictions"]]}


def update_ledger(
    ledger: Mapping[str, Any],
    prospective: Sequence[Mapping[str, Any]],
    accepted_models: Sequence[ModelSpec],
    *,
    registry_sha256: str,
    now: str,
    epoch_id: str = CURRENT_EPOCH_ID,
) -> dict[str, Any]:
    predictions = [dict(row) for row in ledger.get("predictions") or []]
    by_task = {str(row.get("task_id") or ""): row for row in predictions}
    prospective_by_task = {record["task_id"]: record for record in prospective}

    # Only a previously timestamped shadow prediction can be resolved.  A newly
    # supplied labeled record is never backfilled as a prediction.
    for row in predictions:
        record = prospective_by_task.get(str(row.get("task_id") or ""))
        if not row.get("epoch_id") and (
            is_current_epoch_time(row.get("task_created_at"))
            or is_current_epoch_time((record or {}).get("created_at"))
        ):
            row["epoch_id"] = epoch_id
        if row.get("epoch_id") != epoch_id:
            continue
        actual = list((record or {}).get("seeds") or [])
        if len(actual) == 3 and not row.get("resolved_at"):
            predicted = list(row.get("predicted_seeds") or [])
            row.update(
                {
                    "actual_seeds": actual,
                    "exact_ordered": predicted == actual,
                    "exact_unordered": sorted(predicted) == sorted(actual),
                    "score_published_at": record.get("score_published_at"),
                    "resolved_at": now,
                    "status": "resolved",
                }
            )

    if len(accepted_models) == 1:
        model = accepted_models[0]
        for record in sorted(prospective, key=lambda row: str(row.get("created_at") or "")):
            task_id = record["task_id"]
            if task_id in by_task or record.get("seeds") or record.get("score_published_at"):
                continue
            predicted = predict_model(model, record)
            if predicted is None:
                continue
            row = {
                "task_id": task_id,
                "task_created_at": record.get("created_at"),
                "epoch_id": epoch_id,
                "model_id": model.model_id,
                "predicted_seeds": predicted,
                "predicted_at": now,
                "score_published_at_at_prediction": None,
                "input_commitment_sha256": _record_commitment(record),
                "registry_sha256": registry_sha256,
                "actual_seeds": None,
                "exact_ordered": None,
                "exact_unordered": None,
                "resolved_at": None,
                "status": "pending",
            }
            predictions.append(row)
            by_task[task_id] = row

    return {"version": 1, "updated_at": now, "predictions": predictions}


def validate_splits(
    discovery: Sequence[Mapping[str, Any]],
    holdout: Sequence[Mapping[str, Any]],
    prospective: Sequence[Mapping[str, Any]],
) -> None:
    names = {"discovery": discovery, "holdout": holdout, "prospective": prospective}
    ids: dict[str, set[str]] = {}
    for name, rows in names.items():
        values = [str(row.get("task_id") or "") for row in rows]
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate task id inside {name} split")
        ids[name] = set(values)
    for left, right in (("discovery", "holdout"), ("discovery", "prospective"), ("holdout", "prospective")):
        overlap = ids[left] & ids[right]
        if overlap:
            raise ValueError(f"split contamination between {left} and {right}: {sorted(overlap)}")
    for name in ("discovery", "holdout"):
        if any(len(row.get("seeds") or []) != 3 for row in names[name]):
            raise ValueError(f"every {name} record must have a three-seed label")
    for name, rows in names.items():
        if any(not is_current_epoch_time(row.get("created_at")) for row in rows):
            raise ValueError(
                f"{name} contains a task before configured epoch {CURRENT_EPOCH_STARTED_AT}"
            )
    for name in ("discovery", "holdout"):
        if any(not valid_current_seed_label(row) for row in names[name]):
            raise ValueError(f"every {name} label must match the configured epoch shape")


def run_lab(
    *,
    discovery: Sequence[dict[str, Any]],
    holdout: Sequence[dict[str, Any]],
    prospective: Sequence[dict[str, Any]],
    registry: Sequence[ModelSpec],
    ledger: Mapping[str, Any],
    min_discovery: int = 20,
    min_holdout: int = 5,
    now: str | None = None,
    epoch_policy: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_splits(discovery, holdout, prospective)
    if min_discovery < 1 or min_holdout < 1:
        raise ValueError("minimum split sizes must be positive")
    for spec in registry:
        spec.validate()
    now = now or utc_now()
    if epoch_policy is None:
        epoch_policy = public_policy_metadata(
            configured_epoch_view([*discovery, *holdout, *prospective])
        )
    epoch_gate_open = (
        epoch_policy.get("id") == CURRENT_EPOCH_ID
        and epoch_policy.get("gate_open") is True
    )
    sufficient = (
        epoch_gate_open
        and len(discovery) >= min_discovery
        and len(holdout) >= min_holdout
    )
    evaluations: dict[str, dict[str, Any]] = {}
    discovery_pass: list[ModelSpec] = []
    for spec in registry:
        discovery_stats = evaluate_model(spec, discovery)
        row: dict[str, Any] = {"discovery": discovery_stats, "holdout": None}
        if sufficient and _passes(discovery_stats, len(discovery)):
            discovery_pass.append(spec)
        evaluations[spec.model_id] = row

    holdout_pass: list[ModelSpec] = []
    for spec in discovery_pass:
        stats = evaluate_model(spec, holdout)
        evaluations[spec.model_id]["holdout"] = stats
        if _passes(stats, len(holdout)):
            holdout_pass.append(spec)

    fully_evaluable = sum(
        row["discovery"]["evaluable"] == len(discovery)
        and row["discovery"]["missing_inputs"] == 0
        for row in evaluations.values()
    )
    input_blocked = sum(
        row["discovery"]["evaluable"] == 0
        and row["discovery"]["missing_inputs"] > 0
        for row in evaluations.values()
    )
    partially_evaluable = len(evaluations) - fully_evaluable - input_blocked

    registry_sha256 = _registry_digest(registry)
    updated_ledger = update_ledger(
        ledger,
        prospective,
        holdout_pass,
        registry_sha256=registry_sha256,
        now=now,
        epoch_id=CURRENT_EPOCH_ID,
    )
    epoch_rows = [
        row
        for row in updated_ledger["predictions"]
        if row.get("epoch_id") == CURRENT_EPOCH_ID
    ]
    pending = sum(row.get("status") == "pending" for row in epoch_rows)
    resolved = [row for row in epoch_rows if row.get("status") == "resolved"]
    report = {
        "version": 1,
        "generated_at": now,
        "mode": "offline-public-input-shadow-only",
        "safety": {
            "network_calls": False,
            "private_endpoints": False,
            "credentials_accepted": False,
            "submission_writes": False,
            "predictions_are_operationalized": False,
        },
        "quarantined_baseline": {
            "candidate_count": QUARANTINED_STATELESS_CANDIDATES,
            "status": "rejected-and-quarantined",
            "evaluated_by_this_run": False,
            "reason": (
                "The prior cross-history stateless UUID/time/contract family is an audit "
                "baseline only and is never used to fit the configured current epoch."
            ),
        },
        "epoch_policy": dict(epoch_policy),
        "split_policy": {
            "strict_task_id_disjointness": True,
            "discovery": "model screening only",
            "holdout": "development holdout; never used to rescue or tune a failed model",
            "prospective": "new predictions only when seed label is absent; labeled rows only resolve an existing ledger entry",
            "minimum_discovery": min_discovery,
            "minimum_holdout": min_holdout,
            "counts": {
                "discovery": len(discovery),
                "holdout": len(holdout),
                "prospective": len(prospective),
            },
            "sufficient": sufficient,
            "epoch_gate_open": epoch_gate_open,
        },
        "registry": {
            "sha256": registry_sha256,
            "model_count": len(registry),
            "families": sorted({spec.family for spec in registry}),
            "models": [asdict(spec) for spec in registry],
        },
        "evaluation": {
            "models_registered": len(registry),
            "models_tested": fully_evaluable,
            "models_fully_evaluable": fully_evaluable,
            "models_partially_evaluable": partially_evaluable,
            "models_input_blocked": input_blocked,
            "models_discovery_rejected": fully_evaluable - len(discovery_pass),
            "discovery_pass": [spec.model_id for spec in discovery_pass],
            "holdout_pass": [spec.model_id for spec in holdout_pass],
            "unique_model_gate": len(holdout_pass) == 1,
            "prediction_gate_reason": (
                "exactly-one-model-passed-discovery-and-holdout"
                if len(holdout_pass) == 1
                else (
                    "blocked-by-epoch-change"
                    if not epoch_gate_open
                    else "blocked-unless-exactly-one-model-passes-both-gates"
                )
            ),
            "models": evaluations,
        },
        "stateful_sequence_diagnostics": stateful_sequence_diagnostics([*discovery, *holdout]),
        "prospective_ledger": {
            "epoch_id": CURRENT_EPOCH_ID,
            "predictions": len(epoch_rows),
            "pending": pending,
            "resolved": len(resolved),
            "exact_ordered": sum(row.get("exact_ordered") is True for row in resolved),
            "gate_open": len(holdout_pass) == 1,
        },
    }
    return report, updated_ledger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery-json", type=Path, required=True)
    parser.add_argument("--holdout-json", type=Path, required=True)
    parser.add_argument("--prospective-json", type=Path, required=True)
    parser.add_argument("--chain-json", type=Path, action="append", default=[])
    parser.add_argument("--registry-json", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--min-discovery", type=int, default=20)
    parser.add_argument("--min-holdout", type=int, default=5)
    parser.add_argument("--as-of", help="Fixed UTC timestamp for reproducible offline runs")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    chain_sources = [load_chain_context(path) for path in args.chain_json]
    discovery = attach_chain_context(load_tasks(args.discovery_json), chain_sources)
    holdout = attach_chain_context(load_tasks(args.holdout_json), chain_sources)
    prospective = attach_chain_context(load_tasks(args.prospective_json), chain_sources)
    registry = load_registry(args.registry_json)
    ledger = load_ledger(args.ledger)
    report, updated_ledger = run_lab(
        discovery=discovery,
        holdout=holdout,
        prospective=prospective,
        registry=registry,
        ledger=ledger,
        min_discovery=args.min_discovery,
        min_holdout=args.min_holdout,
        now=args.as_of,
    )
    # Write the evidence ledger first; the report describes that resulting state.
    atomic_json(args.ledger, updated_ledger)
    atomic_json(args.report, report)
    print(
        json.dumps(
            {
                "report": str(args.report.resolve()),
                "ledger": str(args.ledger.resolve()),
                "models_tested": report["evaluation"]["models_tested"],
                "holdout_pass": report["evaluation"]["holdout_pass"],
                "prospective": report["prospective_ledger"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
