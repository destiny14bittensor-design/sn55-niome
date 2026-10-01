#!/usr/bin/env python3
"""Automate the public/owned-artifact NIOME seed research workflow.

The orchestrator never uploads a submission.  It combines public task history,
owned probe-inversion results and prospective shadow predictions into one safe
dashboard snapshot.  A prediction is written only when one deterministic model
has passed both the discovery and historical holdout gates.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import time
from typing import Any, Iterable

try:
    from tools.seed_prng_fingerprint import (
        build_report,
        candidate_generators,
        candidate_results,
        fetch_tasks,
        safe_record,
        seed_regime,
    )
except ModuleNotFoundError:  # Direct execution sets sys.path to tools/.
    from seed_prng_fingerprint import (
        build_report,
        candidate_generators,
        candidate_results,
        fetch_tasks,
        safe_record,
        seed_regime,
    )

try:
    from tools.seed_epoch_policy import (
        CURRENT_EPOCH_ID,
        CURRENT_EPOCH_STARTED_AT,
        configured_epoch_view,
        is_current_epoch_time,
        public_policy_metadata,
    )
except ModuleNotFoundError:
    from seed_epoch_policy import (
        CURRENT_EPOCH_ID,
        CURRENT_EPOCH_STARTED_AT,
        configured_epoch_view,
        is_current_epoch_time,
        public_policy_metadata,
    )


DISCOVERY_TARGET = 20
HOLDOUT_TARGET = 5
PROBE_VERIFICATION_TARGET = 5
FORWARD_TARGET = 5


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


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def latest_normal_epoch(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return labels from the explicitly configured current epoch only.

    Historical records are never folded into this result.  A post-boundary
    shape violation fails closed instead of silently starting another epoch.
    """
    view = configured_epoch_view(records)
    if view["gate_open"] is not True:
        return []
    return list(view["labeled_records"])


def load_probe_documents(roots: Iterable[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    solutions_by_task: dict[str, dict[str, Any]] = {}
    statuses_by_task: dict[str, dict[str, Any]] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*.json"):
            name = path.name
            if "probe" not in name:
                continue
            document = read_json(path)
            if not isinstance(document, dict):
                continue
            task_id = str(document.get("task_id") or "")
            if not task_id:
                continue
            wrapped = {**document, "artifact_path": str(path)}
            if "solution" in name and document.get("candidates") is not None:
                previous = solutions_by_task.get(task_id)
                current_at = str(document.get("captured_at") or "")
                previous_at = str((previous or {}).get("captured_at") or "")
                if previous is None or (current_at and current_at < previous_at):
                    solutions_by_task[task_id] = wrapped
            elif "status" in name:
                previous = statuses_by_task.get(task_id)
                current_at = str(document.get("updated_at") or "")
                previous_at = str((previous or {}).get("updated_at") or "")
                if previous is None or current_at > previous_at:
                    statuses_by_task[task_id] = wrapped
    return list(solutions_by_task.values()), list(statuses_by_task.values())


def load_predictions(path: Path) -> dict[str, Any]:
    value = read_json(path, {"version": 1, "predictions": []})
    if not isinstance(value, dict) or not isinstance(value.get("predictions"), list):
        return {"version": 1, "predictions": []}
    return value


def _model_prediction(model: str, record: dict[str, Any]) -> list[int] | None:
    generator = candidate_generators(record).get(model)
    if generator is None:
        return None
    try:
        return [int(value) for value in generator()]
    except (TypeError, ValueError):
        return None


def accepted_models(
    discovery: list[dict[str, Any]], holdout: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, int]], list[str], list[str]]:
    stats = candidate_results(discovery) if discovery else {}
    discovery_pass = [
        name
        for name, values in stats.items()
        if len(discovery) >= DISCOVERY_TARGET
        and values["exact_triplets"] == len(discovery)
    ]
    holdout_pass = []
    if len(holdout) >= HOLDOUT_TARGET:
        for name in discovery_pass:
            if all(_model_prediction(name, record) == record["seeds"] for record in holdout):
                holdout_pass.append(name)
    return stats, discovery_pass, holdout_pass


def update_prediction_ledger(
    ledger: dict[str, Any],
    records: list[dict[str, Any]],
    holdout_models: list[str],
    *,
    now: str,
) -> dict[str, Any]:
    predictions = [dict(item) for item in ledger.get("predictions") or []]
    by_task = {str(item.get("task_id")): item for item in predictions}
    records_by_task = {record["task_id"]: record for record in records}

    for item in predictions:
        if not item.get("epoch_id") and is_current_epoch_time(item.get("task_created_at")):
            item["epoch_id"] = CURRENT_EPOCH_ID
        if item.get("epoch_id") != CURRENT_EPOCH_ID:
            continue
        actual = records_by_task.get(str(item.get("task_id")))
        if actual and actual["seeds"] and not item.get("resolved_at"):
            item["actual_seeds"] = list(actual["seeds"])
            item["exact"] = list(item.get("predicted_seeds") or []) == actual["seeds"]
            item["resolved_at"] = now

    if len(holdout_models) == 1:
        model = holdout_models[0]
        for record in sorted(records, key=lambda value: value["created_at"], reverse=True):
            if (
                not is_current_epoch_time(record.get("created_at"))
                or record["seeds"]
                or not record["task_id"]
                or record["task_id"] in by_task
            ):
                continue
            prediction = _model_prediction(model, record)
            if prediction is not None:
                predictions.append(
                    {
                        "task_id": record["task_id"],
                        "task_created_at": record["created_at"],
                        "epoch_id": CURRENT_EPOCH_ID,
                        "model": model,
                        "predicted_seeds": prediction,
                        "predicted_at": now,
                        "actual_seeds": None,
                        "exact": None,
                        "resolved_at": None,
                    }
                )
            break

    return {"version": 1, "updated_at": now, "predictions": predictions}


def consecutive_exact(predictions: list[dict[str, Any]]) -> int:
    resolved = sorted(
        (item for item in predictions if item.get("resolved_at")),
        key=lambda item: str(item.get("task_created_at") or ""),
    )
    streak = 0
    for item in reversed(resolved):
        if item.get("exact") is True:
            streak += 1
        else:
            break
    return streak


def consecutive_ordered_exact(predictions: list[dict[str, Any]]) -> int:
    """Count only resolved, prospectively timestamped ordered-exact rows."""
    resolved = sorted(
        (
            item
            for item in predictions
            if item.get("resolved_at") and item.get("predicted_at")
        ),
        key=lambda item: str(item.get("task_created_at") or item.get("predicted_at") or ""),
    )
    streak = 0
    for item in reversed(resolved):
        if item.get("exact_ordered") is True:
            streak += 1
        else:
            break
    return streak


def _generator_track(
    report: dict[str, Any],
    ledger: dict[str, Any],
    legacy: dict[str, Any],
    supplemental_reports: Iterable[dict[str, Any]] = (),
    shuffle_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize the isolated Track A worker into the dashboard contract."""
    split = report.get("split_policy") or {}
    counts = split.get("counts") or {}
    registry = report.get("registry") or {}
    evaluation = report.get("evaluation") or {}
    quarantined = report.get("quarantined_baseline") or {}
    epoch_policy = report.get("epoch_policy") or {}
    rows = list(ledger.get("predictions") or [])
    if epoch_policy.get("id"):
        rows = [
            row for row in rows if row.get("epoch_id") == epoch_policy.get("id")
        ]
    streak = consecutive_ordered_exact(rows)
    pending = sum(row.get("status") == "pending" for row in rows)
    accepted = list(evaluation.get("holdout_pass") or [])
    unique = evaluation.get("unique_model_gate") is True and len(accepted) == 1
    discovery = int(counts.get("discovery") or 0)
    holdout = int(counts.get("holdout") or 0)
    discovery_target = int(split.get("minimum_discovery") or DISCOVERY_TARGET)
    holdout_target = int(split.get("minimum_holdout") or HOLDOUT_TARGET)
    if not report:
        hypothesis = legacy.get("hypothesis") or {}
        forward = legacy.get("forward") or {}
        discovery = int((legacy.get("dataset") or {}).get("discovery_tasks") or 0)
        holdout = int((legacy.get("dataset") or {}).get("holdout_tasks") or 0)
        tested = int(hypothesis.get("tested_models") or 0)
        accepted_model = hypothesis.get("accepted_model")
        streak = int(forward.get("consecutive_exact") or 0)
        unique = bool(accepted_model)
        accepted = [accepted_model] if accepted_model else []
        pending = max(
            0,
            int(forward.get("recorded_predictions") or 0)
            - int(forward.get("resolved_predictions") or 0),
        )
    else:
        registered = int(
            evaluation.get("models_registered") or registry.get("model_count") or 0
        )
        tested = int(
            evaluation.get("models_fully_evaluable")
            if evaluation.get("models_fully_evaluable") is not None
            else evaluation.get("models_tested") or 0
        )
        input_blocked = int(evaluation.get("models_input_blocked") or 0)
        partially_evaluable = int(evaluation.get("models_partially_evaluable") or 0)
    if not report:
        registered = tested
        input_blocked = 0
        partially_evaluable = 0
    labels = (discovery + holdout) * 3
    if epoch_policy and epoch_policy.get("gate_open") is not True:
        status = "changed"
        title = "시드 체제 변경 감지 — 예측 중지"
        violation = epoch_policy.get("violation") or {}
        detail = (
            f"{violation.get('task_id') or '새 task'}에서 개수·범위·중복 조건이 달라져 "
            "현 epoch 모델과 신규 shadow prediction을 차단했습니다."
        )
    elif discovery < discovery_target or holdout < holdout_target:
        status = "collecting"
        title = "공개 chain/task 결합 라벨 수집"
        detail = (
            f"엄격 분리 데이터가 discovery {discovery}/{discovery_target}, "
            f"holdout {holdout}/{holdout_target}입니다."
        )
    elif not unique:
        status = "searching"
        title = "공개 chain 기반 생성기 가설 탐색"
        detail = (
            f"등록 {registered}개 중 {tested}개 평가 완료, 입력 부족 {input_blocked}개입니다. "
            "양쪽 split을 exact 통과한 단일 모델은 없습니다."
        )
    elif streak < FORWARD_TARGET:
        status = "validating"
        title = "채점 전 shadow prediction 검증"
        detail = f"승인 모델을 고정한 뒤 미래 task ordered-exact {streak}/{FORWARD_TARGET}를 검증합니다."
    else:
        status = "complete"
        title = "신뢰성 게이트 통과"
        detail = "채점 전에 고정한 seed가 미래 5개 task에서 연속 ordered-exact였습니다."
    quarantine_count = int(quarantined.get("candidate_count") or 363)
    supplemental = []
    for item in supplemental_reports:
        search = item.get("search") or {}
        tested_count = int(
            search.get("candidates_tested")
            or search.get("candidate_initializations")
            or search.get("tested")
            or 0
        )
        if tested_count <= 0:
            continue
        exact_count = search.get("discovery_exact_candidates")
        if exact_count is None:
            exact_count = len(search.get("exact_candidates") or [])
        families = search.get("families")
        if families is None:
            families = search.get("configurations") or {}
        if not families and tested_count > 0 and search.get("engine"):
            families = {
                f"{search.get('engine')}:{search.get('method') or 'unknown'}": {}
            }
        supplemental.append(
            {
                "mode": str(item.get("mode") or "extended-search"),
                "candidates_tested": tested_count,
                "discovery_exact_candidates": int(exact_count or 0),
                "families": len(families),
            }
        )
    campaign_tested = quarantine_count + tested + sum(
        item["candidates_tested"] for item in supplemental
    )
    campaign_exact = sum(
        item["discovery_exact_candidates"] for item in supplemental
    )
    campaign_families = len(registry.get("families") or []) + sum(
        item["families"] for item in supplemental
    )
    shuffle_evidence = shuffle_evidence or {}
    leak_summary = (shuffle_evidence.get("leak") or {}).get("summary") or {}
    alignment_summary = (shuffle_evidence.get("alignment") or {}).get("summary") or {}
    constraint_summary = (shuffle_evidence.get("constraints") or {}).get("summary") or {}
    fisher_summary = (shuffle_evidence.get("fisher_yates") or {}).get("summary") or {}
    prefix_summary = (shuffle_evidence.get("prefix_domains") or {}).get("summary") or {}
    tuple_summary = (shuffle_evidence.get("prefix_tuples") or {}).get("summary") or {}
    prefix_cpsat_summary = (shuffle_evidence.get("prefix_cpsat") or {}).get("summary") or {}
    joint_cpsat_summary = (shuffle_evidence.get("joint_cpsat") or {}).get("summary") or {}
    z3_joint_summary = (shuffle_evidence.get("z3_joint") or {}).get("summary") or {}
    xorsat_joint_summary = (shuffle_evidence.get("xorsat_joint") or {}).get("summary") or {}
    fixed_profile_summary = (shuffle_evidence.get("fixed_profile") or {}).get("summary") or {}
    incremental_label_summary = (
        (shuffle_evidence.get("incremental_labels") or {}).get("summary") or {}
    )
    postgres_double_summary = (shuffle_evidence.get("postgres_double") or {}).get("summary") or {}
    postgres_range_summary = (shuffle_evidence.get("postgres_range") or {}).get("summary") or {}
    identifiability_summary = (shuffle_evidence.get("identifiability") or {}).get("summary") or {}
    mt_rank_summary = (shuffle_evidence.get("mt_rank_audit") or {}).get("summary") or {}
    shuffle_ready = alignment_summary.get("symbolic_solver_input_ready") is True
    return {
        "status": status,
        "status_label": {
            "changed": "체제 변경 · 중지",
            "collecting": "공개 입력 수집",
            "searching": "가설 탐색",
            "validating": "미래 검증",
            "complete": "목표 검증",
        }[status],
        "current_action": {"title": title, "detail": detail, "task_id": None},
        "metrics": {
            "labels": labels,
            "labels_detail": (
                f"{epoch_policy.get('id')} · discovery {discovery} · holdout {holdout}"
                if epoch_policy.get("id")
                else f"discovery {discovery} · holdout {holdout}"
            ),
            "tested_models": (
                f"누적 {campaign_tested:,} · 계열 {campaign_families} · "
                f"기본평가 {tested} · 입력부족 {input_blocked}"
                + (f" · 부분평가 {partially_evaluable}" if partially_evaluable else "")
            ),
            "model_status": accepted[0] if unique else "승인 모델 없음",
            "forward_exact": f"{streak} / {FORWARD_TARGET}",
            "forward_detail": f"pending {pending} · 공개 전 기록만 인정",
            "incremental_label_status": incremental_label_summary.get("status"),
            "incremental_label_target_rounds": incremental_label_summary.get(
                "target_rounds"
            ),
            "incremental_label_completed_rounds": incremental_label_summary.get(
                "completed_rounds"
            ),
            "incremental_label_sat_rounds": (
                incremental_label_summary.get("sat_rounds")
                if incremental_label_summary.get("sat_rounds") is not None
                else (
                    incremental_label_summary.get("completed_rounds")
                    if incremental_label_summary.get("status") == "sat"
                    else None
                )
            ),
            "incremental_label_seed_labels_bound": incremental_label_summary.get(
                "seed_labels_bound"
            ),
            "incremental_label_sat_seed_labels_bound": (
                incremental_label_summary.get("sat_seed_labels_bound")
                if incremental_label_summary.get("sat_seed_labels_bound") is not None
                else (
                    incremental_label_summary.get("seed_labels_bound")
                    if incremental_label_summary.get("status") == "sat"
                    else None
                )
            ),
            "incremental_label_public_shuffle_rows_passed": incremental_label_summary.get(
                "public_shuffle_rows_passed"
            ),
        },
        "evidence": {
            "summary": (
                "기존 363개 direct-map 가설은 격리했습니다. 공개 후 probe 역산은 라벨 확인일 뿐 "
                "사전예측 적중 수에 포함하지 않습니다."
            ),
            "prospective_consecutive_ordered_exact": streak,
            "accepted_model": accepted[0] if unique else None,
            "epoch_policy": epoch_policy,
            "campaign": {
                "candidates_tested": campaign_tested,
                "discovery_exact_candidates": campaign_exact,
                "supplemental_searches": supplemental,
            },
            "shuffle_state_recovery": {
                "permutation_information_bits_upper_bound": leak_summary.get(
                    "total_permutation_information_bits_upper_bound"
                ),
                "exact_unique_uid_positions": alignment_summary.get(
                    "exact_unique_uid_positions"
                ),
                "group_order_constraints": constraint_summary.get(
                    "group_order_constraints"
                ),
                "group_sequence_missing_positions": constraint_summary.get(
                    "group_sequence_missing_positions"
                ),
                "incomplete_shuffle_rounds_excluded": constraint_summary.get(
                    "incomplete_rounds_excluded"
                ),
                "fisher_yates_witness_rounds": fisher_summary.get("sat_rounds"),
                "prefix_choice_domains": prefix_summary.get("choice_domains"),
                "prefix_singleton_choice_domains": prefix_summary.get(
                    "singleton_choice_domains"
                ),
                "prefix_domain_reduction_bits": prefix_summary.get(
                    "domain_reduction_bits"
                ),
                "correlated_prefix_choice_tuples": tuple_summary.get("choice_tuples"),
                "correlated_prefix_maximum_depth": tuple_summary.get(
                    "maximum_completed_depth"
                ),
                "prefix_cpsat_status": prefix_cpsat_summary.get("status"),
                "prefix_cpsat_rounds_modelled": prefix_cpsat_summary.get(
                    "rounds_modelled"
                ),
                "joint_cpsat_status": joint_cpsat_summary.get("status"),
                "joint_cpsat_rounds_modelled": joint_cpsat_summary.get(
                    "rounds_modelled"
                ),
                "z3_joint_status": z3_joint_summary.get("status"),
                "z3_joint_full_group_positions_bound": z3_joint_summary.get(
                    "full_group_positions_bound"
                ),
                "xorsat_joint_status": xorsat_joint_summary.get("status"),
                "xorsat_joint_rounds_modelled": xorsat_joint_summary.get(
                    "rounds_modelled"
                ),
                "xorsat_joint_full_shuffle_rounds_bound": xorsat_joint_summary.get(
                    "full_shuffle_rounds_bound"
                ),
                "xorsat_joint_full_group_positions_bound": xorsat_joint_summary.get(
                    "full_group_positions_bound"
                ),
                "xorsat_joint_shuffle_encoding": xorsat_joint_summary.get(
                    "shuffle_encoding"
                ),
                "xorsat_joint_singleton_trace_rounds": xorsat_joint_summary.get(
                    "singleton_trace_rounds_bound"
                ),
                "xorsat_joint_singleton_uid_traces": xorsat_joint_summary.get(
                    "singleton_uid_traces_bound"
                ),
                "xorsat_joint_sealed_shuffle_rounds": xorsat_joint_summary.get(
                    "sealed_shuffle_rounds_modelled"
                ),
                "xorsat_joint_sealed_seed_labels_opened": xorsat_joint_summary.get(
                    "sealed_seed_labels_opened"
                ),
                "xorsat_joint_rejected_words_enforced": xorsat_joint_summary.get(
                    "rejected_word_inequalities_enforced"
                ) is True,
                "xorsat_joint_excluded_discovery_predicted": xorsat_joint_summary.get(
                    "excluded_discovery_predicted"
                ) is True,
                "fixed_profile_status": fixed_profile_summary.get("status"),
                "fixed_profile_rounds_modelled": fixed_profile_summary.get(
                    "rounds_modelled"
                ),
                "fixed_profile_included_public_fit": fixed_profile_summary.get(
                    "included_public_fit"
                ) is True,
                "fixed_profile_excluded_discovery_predicted": fixed_profile_summary.get(
                    "excluded_discovery_predicted"
                ) is True,
                "fixed_profile_excluded_replay_reason": fixed_profile_summary.get(
                    "excluded_discovery_replay_reason"
                ),
                "incremental_label_status": incremental_label_summary.get("status"),
                "incremental_label_target_rounds": incremental_label_summary.get(
                    "target_rounds"
                ),
                "incremental_label_completed_rounds": incremental_label_summary.get(
                    "completed_rounds"
                ),
                "incremental_label_sat_rounds": (
                    incremental_label_summary.get("sat_rounds")
                    if incremental_label_summary.get("sat_rounds") is not None
                    else (
                        incremental_label_summary.get("completed_rounds")
                        if incremental_label_summary.get("status") == "sat"
                        else None
                    )
                ),
                "incremental_label_seed_labels_bound": incremental_label_summary.get(
                    "seed_labels_bound"
                ),
                "incremental_label_sat_seed_labels_bound": (
                    incremental_label_summary.get("sat_seed_labels_bound")
                    if incremental_label_summary.get("sat_seed_labels_bound") is not None
                    else (
                        incremental_label_summary.get("seed_labels_bound")
                        if incremental_label_summary.get("status") == "sat"
                        else None
                    )
                ),
                "incremental_label_public_shuffle_rows_passed": incremental_label_summary.get(
                    "public_shuffle_rows_passed"
                ),
                "postgres_double_status": postgres_double_summary.get("status"),
                "postgres_double_outputs_constrained": postgres_double_summary.get(
                    "constrained_outputs"
                ),
                "postgres_range_status": postgres_range_summary.get("status"),
                "postgres_range_outputs_constrained": postgres_range_summary.get(
                    "constrained_outputs"
                ),
                "discovery_label_information_bits": identifiability_summary.get(
                    "maximum_label_information_bits"
                ),
                "mt19937_unresolved_bits_from_labels_only": identifiability_summary.get(
                    "minimum_unresolved_mt19937_bits_from_labels_only"
                ),
                "labels_only_mt19937_identifiable": identifiability_summary.get(
                    "labels_only_mt19937_identifiable"
                ),
                "mt19937_state_recovered": fisher_summary.get(
                    "mt19937_state_recovered"
                ) is True,
                "synthetic_effective_rank": mt_rank_summary.get("rank"),
                "synthetic_effective_state_bits": mt_rank_summary.get(
                    "effective_state_bits"
                ),
                "synthetic_full_rank_round": mt_rank_summary.get("full_rank_round"),
                "synthetic_predictive_state_recovered": mt_rank_summary.get(
                    "synthetic_predictive_state_recovered"
                ) is True,
                "synthetic_future_words_verified": mt_rank_summary.get(
                    "future_words_verified"
                ),
                "symbolic_solver_input_ready": shuffle_ready,
                "shuffle_rng_producer": "validator-process:numpy-global-randomstate",
                "seed_rng_producer": "uncommitted-validator-patch:exact-api-unobserved",
                "same_validator_process_evidence": True,
                "same_rng_coupling_verified": False,
                "same_rng_coupling_status": "unverified",
                "fixed_rejection_profile_empirically_observed": False,
                "corridor_coverage_is_not_exact_path_probability": True,
            },
        },
        "gates": [
            {"label": "Discovery 라벨", "detail": f"{discovery}/{discovery_target}", "complete": discovery >= discovery_target},
            {"label": "독립 Holdout", "detail": f"{holdout}/{holdout_target}", "complete": holdout >= holdout_target},
            {"label": "단일 가설 exact", "detail": accepted[0] if unique else "승인 없음", "complete": unique},
            {
                "label": "NumPy 상태복원 입력",
                "detail": (
                    f"고유 UID {int(alignment_summary.get('exact_unique_uid_positions') or 0):,}개 · "
                    f"그룹 순서 {int(constraint_summary.get('group_order_constraints') or 0):,}개 · "
                    f"합성 rank {int(mt_rank_summary.get('rank') or 0):,}/"
                    f"{int(mt_rank_summary.get('effective_state_bits') or 19_937):,} · "
                    f"진행 중 제외 {int(constraint_summary.get('incomplete_rounds_excluded') or 0)} · "
                    f"prefix domain {int(prefix_summary.get('choice_domains') or 0):,}개 · "
                    "실데이터 MT 상태 미복원"
                ),
                "complete": shuffle_ready,
            },
            {"label": "공개 전 예측 기록", "detail": f"{len(rows)}회", "complete": bool(rows)},
            {"label": "미래 ordered-exact", "detail": f"{streak}/{FORWARD_TARGET}", "complete": streak >= FORWARD_TARGET},
        ],
        "safety": {"label": "PUBLIC INPUT · SHADOW ONLY", "authorized": True},
        "qualified": (
            streak >= FORWARD_TARGET
            and (not epoch_policy or epoch_policy.get("gate_open") is True)
        ),
    }


def _early_score_documents(roots: Iterable[Path]) -> list[dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*/early_score_summary.json"):
            value = read_json(path)
            if not isinstance(value, dict) or value.get("mode") != "observation-only":
                continue
            task_id = str(value.get("task_id") or "")
            if not task_id:
                continue
            previous = documents.get(task_id)
            if previous is None or str(value.get("updated_at") or "") > str(previous.get("updated_at") or ""):
                documents[task_id] = value
    return list(documents.values())


def _batch_signal_task_ids(roots: Iterable[Path]) -> set[str]:
    """Count legacy public score observations only as batch baselines."""
    task_ids: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.glob("*/seed_signal_race_summary.json"):
            value = read_json(path)
            if not isinstance(value, dict):
                continue
            task_id = str(value.get("task_id") or "")
            signals = (
                value.get("signals")
                or value.get("observations")
                or value.get("first")
                or {}
            )
            encoded = json.dumps(signals, sort_keys=True).lower()
            if task_id and ("score" in encoded or value.get("score_observed_at")):
                task_ids.add(task_id)
    return task_ids


def _early_score_track(
    summaries: list[dict[str, Any]],
    legacy_batch_ids: set[str],
    supervisor: dict[str, Any],
) -> dict[str, Any]:
    confirmed: list[dict[str, Any]] = []
    individual_candidates = 0
    batch_ids = set(legacy_batch_ids)
    for summary in summaries:
        task_id = str(summary.get("task_id") or "")
        publication = summary.get("score_publication") or {}
        decision = summary.get("decision") or {}
        lead = summary.get("lead_time") or {}
        gates = decision.get("gates") or {}
        if int(publication.get("cohort_size") or 0) > 0:
            batch_ids.add(task_id)
        if gates.get("early_individual_score") is True:
            individual_candidates += 1
        if decision.get("actionable") is True and lead.get("status") in {
            "confirmed_before_batch",
            "confirmed_before_batch_completion",
        }:
            confirmed.append(summary)
    latest = max(summaries, key=lambda row: str(row.get("updated_at") or ""), default={})
    latest_lead = latest.get("lead_time") or {}
    active_task = supervisor.get("active_task_id")
    reproduced = len(confirmed)
    if reproduced >= 3:
        status = "complete"
        title = "조기 개별 점수 경로 재현 완료"
        detail = "독립 task 3회에서 batch 이전성, writable 채널, 독립성과 양의 시간예산을 모두 확인했습니다."
    elif active_task:
        status = "active"
        title = "현재 task 조기 개별 점수 감시"
        detail = "정확한 task+소유 hotkey 행과 전체 cohort를 같은 시계로 병렬 관측합니다."
    else:
        status = "searching"
        title = "다음 소유 task 관측 대기"
        detail = "공개 또는 명시적으로 허가된 관측면만 사용하며 batch 행은 조기 신호로 세지 않습니다."
    confirmed_leads = [
        float((row.get("lead_time") or {}).get("seconds"))
        for row in confirmed
        if (row.get("lead_time") or {}).get("seconds") is not None
    ]
    lead_seconds = max(confirmed_leads) if confirmed_leads else None
    latest_gates = (latest.get("decision") or {}).get("gates") or {}
    return {
        "status": status,
        "status_label": {"active": "실시간 관측", "searching": "경로 탐색", "complete": "목표 검증"}[status],
        "current_action": {"title": title, "detail": detail, "task_id": active_task},
        "metrics": {
            "actionable_individual_scores": reproduced,
            "hits_detail": f"후보 {individual_candidates} · 확정/실행가능 {reproduced}",
            "lead_time_seconds": lead_seconds,
            "lead_detail": "확정된 batch 이전 선행시간" if lead_seconds is not None else "입증된 시간창 없음",
            "batch_observations": len(batch_ids),
            "batch_detail": "공개 batch 기준선 · 조기 신호 아님",
            "precedes_batch": reproduced > 0,
            "actionable_window_verified": any((row.get("decision") or {}).get("actionable") is True for row in summaries),
            "reproduced_rounds": reproduced,
            "reproducible": reproduced >= 3,
        },
        "evidence": {
            "summary": (
                "조기 후보는 batch 공개 뒤 확정하고, 소유 채널의 쓰기 가능성·독립성·deadline 예산을 "
                "동시에 통과한 라운드만 실행 가능 증거로 인정합니다."
            ),
            "latest_lead_status": latest_lead.get("status"),
        },
        "gates": [
            {"label": "일괄 공개 기준선", "detail": f"{len(batch_ids)} task", "complete": bool(batch_ids)},
            {"label": "개별 점수 신호", "detail": f"후보 {individual_candidates}회", "complete": individual_candidates > 0, "active": bool(active_task)},
            {"label": "batch 이전성", "detail": f"확정 {reproduced}회", "complete": reproduced > 0},
            {"label": "실행 가능 시간창", "detail": "writable·독립·예산", "complete": latest_gates.get("positive_build_upload_budget") is True and latest_gates.get("owned_channel_writable") is True and latest_gates.get("channel_independent_of_score_source") is True},
            {"label": "독립 라운드 재현", "detail": f"{reproduced}/3", "complete": reproduced >= 3},
        ],
        "safety": {"label": "READ ONLY · AUTHORIZED SURFACES", "authorized": True},
        "qualified": reproduced >= 3,
    }


def build_snapshot(
    *,
    tasks: list[dict[str, Any]],
    probe_solutions: list[dict[str, Any]],
    probe_statuses: list[dict[str, Any]],
    predictions: dict[str, Any],
    supervisor: dict[str, Any],
    generator_report: dict[str, Any] | None = None,
    generator_ledger: dict[str, Any] | None = None,
    supplemental_generator_reports: list[dict[str, Any]] | None = None,
    shuffle_evidence: dict[str, Any] | None = None,
    early_score_summaries: list[dict[str, Any]] | None = None,
    early_score_supervisor: dict[str, Any] | None = None,
    legacy_batch_ids: set[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    records = sorted(
        (safe_record(task) for task in tasks), key=lambda item: item["created_at"]
    )
    epoch_view = configured_epoch_view(records)
    reported_policy = (generator_report or {}).get("epoch_policy") or {}
    if (
        reported_policy.get("id") == CURRENT_EPOCH_ID
        and reported_policy.get("gate_open") is False
    ):
        epoch_view["status"] = "change-detected"
        epoch_view["gate_open"] = False
        epoch_view["violation"] = reported_policy.get("violation")
    epoch = latest_normal_epoch(records)
    if epoch_view["gate_open"] is not True:
        epoch = []
    discovery = epoch[:DISCOVERY_TARGET]
    holdout = epoch[DISCOVERY_TARGET : DISCOVERY_TARGET + HOLDOUT_TARGET]
    stats, discovery_models, holdout_models = accepted_models(discovery, holdout)
    official_by_task = {record["task_id"]: record for record in epoch}

    probe_rows = []
    for solution in probe_solutions:
        task_id = str(solution.get("task_id") or "")
        candidates = solution.get("candidates") or []
        recovered = list((candidates[0] or {}).get("seeds") or []) if candidates else []
        official = list((official_by_task.get(task_id) or {}).get("seeds") or [])
        observation = next(iter((solution.get("observations") or {}).values()), {})
        score_at = parse_time(observation.get("created_at"))
        recovered_at = parse_time(solution.get("captured_at"))
        latency = (
            (recovered_at - score_at).total_seconds()
            if score_at is not None and recovered_at is not None
            else None
        )
        probe_rows.append(
            {
                "task_id": task_id,
                "recovered_seeds": recovered,
                "official_seeds": official,
                "candidate_count": int(solution.get("candidate_count") or 0),
                "unique": int(solution.get("candidate_count") or 0) == 1,
                "verified": bool(official),
                "exact_unordered": bool(official) and sorted(recovered) == sorted(official),
                "exact_ordered": bool(official) and recovered == official,
                "score_at": observation.get("created_at"),
                "recovered_at": solution.get("captured_at"),
                "recovery_latency_seconds": latency,
                "partial_table": bool((solution.get("watch") or {}).get("partial_table_match")),
            }
        )
    probe_rows.sort(key=lambda item: str(item.get("recovered_at") or ""), reverse=True)
    verified_probes = [item for item in probe_rows if item["verified"]]
    exact_probes = [item for item in verified_probes if item["exact_unordered"]]

    prediction_rows = [
        item
        for item in (predictions.get("predictions") or [])
        if item.get("epoch_id") == CURRENT_EPOCH_ID
        or (
            not item.get("epoch_id")
            and is_current_epoch_time(item.get("task_created_at"))
        )
    ]
    resolved_predictions = [item for item in prediction_rows if item.get("resolved_at")]
    streak = consecutive_exact(prediction_rows)

    active_status = None
    supervisor_active_task = str(supervisor.get("active_task_id") or "")
    for status in sorted(
        probe_statuses, key=lambda item: str(item.get("updated_at") or ""), reverse=True
    ):
        if (
            supervisor_active_task
            and str(status.get("task_id") or "") == supervisor_active_task
            and status.get("state") in {"building", "fingerprints_complete"}
        ):
            active_status = status
            break

    if epoch_view["gate_open"] is not True:
        phase = "epoch_change_detected"
        violation = epoch_view.get("violation") or {}
        current_action = {
            "code": phase,
            "title": "시드 체제 변경 감지 — 연구 epoch 잠금",
            "detail": (
                f"{violation.get('task_id') or '새 task'}에서 현행 3개·100–999 조건이 "
                "깨졌습니다. 과거·신규 체제를 섞지 않고 새 경계를 확정할 때까지 예측을 중지합니다."
            ),
            "task_id": violation.get("task_id"),
            "progress": 0.0,
        }
    elif active_status and active_status.get("state") == "building":
        phase = "fingerprint_build"
        current_action = {
            "code": phase,
            "title": "현재 task fingerprint 생성",
            "detail": (
                f"seed {active_status.get('completed', 0)}/{active_status.get('total', 900)} "
                "계산 후 공식 점수를 기다립니다."
            ),
            "task_id": active_status.get("task_id"),
            "progress": (
                float(active_status.get("completed") or 0)
                / max(1.0, float(active_status.get("total") or 900))
            ),
        }
    elif active_status and active_status.get("state") == "fingerprints_complete":
        phase = "probe_score_wait"
        current_action = {
            "code": phase,
            "title": "현재 task 공식 점수 감시",
            "detail": "900개 fingerprint 계산을 마쳤고 공개 점수가 생기면 즉시 seed를 역산합니다.",
            "task_id": active_status.get("task_id"),
            "progress": 1.0,
        }
    elif len(discovery) < DISCOVERY_TARGET:
        phase = "dataset_discovery"
        current_action = {
            "code": phase,
            "title": "안정 epoch seed 라벨 수집",
            "detail": f"discovery 데이터 {len(discovery)}/{DISCOVERY_TARGET} task 확보 중입니다.",
            "task_id": None,
            "progress": len(discovery) / DISCOVERY_TARGET,
        }
    elif len(holdout) < HOLDOUT_TARGET:
        phase = "historical_holdout"
        current_action = {
            "code": phase,
            "title": "historical holdout 구성",
            "detail": f"학습에서 분리한 검증 task {len(holdout)}/{HOLDOUT_TARGET}개입니다.",
            "task_id": None,
            "progress": len(holdout) / HOLDOUT_TARGET,
        }
    elif not holdout_models:
        phase = "generator_search"
        current_action = {
            "code": phase,
            "title": "seed 생성기 가설 확장 탐색",
            "detail": (
                f"기본 결정식 {len(stats)}개를 검사했으며 승인 후보는 없습니다. "
                "block phase/hash 및 상태형 PRNG 계열을 계속 좁힙니다."
            ),
            "task_id": None,
            "progress": min(0.95, len(exact_probes) / PROBE_VERIFICATION_TARGET),
        }
    elif streak < FORWARD_TARGET:
        phase = "shadow_prediction"
        current_action = {
            "code": phase,
            "title": "채점 전 shadow prediction 검증",
            "detail": f"연속 exact {streak}/{FORWARD_TARGET}; 제출 경로에는 연결하지 않습니다.",
            "task_id": None,
            "progress": streak / FORWARD_TARGET,
        }
    else:
        phase = "eligible_for_review"
        current_action = {
            "code": phase,
            "title": "사전 예측 승인 검토 가능",
            "detail": "5회 연속 exact를 통과했습니다. 운영 연결은 별도 검토 대상입니다.",
            "task_id": None,
            "progress": 1.0,
        }

    recent_rounds = []
    probe_by_task = {row["task_id"]: row for row in probe_rows}
    prediction_by_task = {
        str(row.get("task_id")): row for row in prediction_rows if row.get("task_id")
    }
    for index, record in reversed(list(enumerate(epoch))[-8:]):
        if index < DISCOVERY_TARGET:
            partition = "discovery"
        elif index < DISCOVERY_TARGET + HOLDOUT_TARGET:
            partition = "holdout"
        else:
            partition = "observed"
        prediction = prediction_by_task.get(record["task_id"])
        if prediction:
            partition = "forward"
        recent_rounds.append(
            {
                "task_id": record["task_id"],
                "created_at": record["created_at"],
                "official_seeds": record["seeds"],
                "partition": partition,
                "probe": probe_by_task.get(record["task_id"]),
                "prediction": prediction,
            }
        )

    supervisor_updated = parse_time(supervisor.get("updated_at"))
    supervisor_age = (
        max(0.0, (now - supervisor_updated).total_seconds())
        if supervisor_updated
        else None
    )
    snapshot = {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "phase": phase,
        "current_action": current_action,
        "targets": {
            "discovery": DISCOVERY_TARGET,
            "holdout": HOLDOUT_TARGET,
            "probe_verification": PROBE_VERIFICATION_TARGET,
            "forward_exact": FORWARD_TARGET,
        },
        "dataset": {
            "epoch_id": CURRENT_EPOCH_ID,
            "configured_epoch_started_at": CURRENT_EPOCH_STARTED_AT,
            "epoch_status": epoch_view["status"],
            "epoch_gate_open": epoch_view["gate_open"],
            "epoch_violation": epoch_view["violation"],
            "historical_records_excluded": epoch_view["historical_records_excluded"],
            "latest_epoch_tasks": len(epoch),
            "latest_epoch_seed_values": len(epoch) * 3,
            "epoch_started_at": epoch[0]["created_at"] if epoch else None,
            "epoch_ended_at": epoch[-1]["created_at"] if epoch else None,
            "discovery_tasks": len(discovery),
            "holdout_tasks": len(holdout),
        },
        "probe": {
            "captured_tasks": len(probe_rows),
            "verified_tasks": len(verified_probes),
            "exact_tasks": len(exact_probes),
            "unique_tasks": sum(item["unique"] for item in probe_rows),
            "latest": probe_rows[0] if probe_rows else None,
        },
        "hypothesis": {
            "tested_models": len(stats),
            "discovery_pass_models": discovery_models,
            "holdout_pass_models": holdout_models,
            "accepted_model": holdout_models[0] if len(holdout_models) == 1 else None,
            "ambiguous": len(holdout_models) > 1,
        },
        "forward": {
            "recorded_predictions": len(prediction_rows),
            "resolved_predictions": len(resolved_predictions),
            "exact_predictions": sum(item.get("exact") is True for item in resolved_predictions),
            "consecutive_exact": streak,
            "target": FORWARD_TARGET,
            "eligible": streak >= FORWARD_TARGET,
        },
        "automation": {
            "supervisor_active_task_id": supervisor.get("active_task_id"),
            "supervisor_last_task_id": supervisor.get("last_completed_task_id"),
            "supervisor_last_return_code": supervisor.get("last_return_code"),
            "supervisor_updated_at": supervisor.get("updated_at"),
            "supervisor_age_seconds": supervisor_age,
            "supervisor_healthy": supervisor_age is not None and supervisor_age <= 30,
        },
        "recent_rounds": recent_rounds,
        "safety": {
            "public_and_owned_artifacts_only": True,
            "submission_writes": False,
            "prediction_requires_holdout_pass": True,
            "forward_requires_pre_score_record": True,
        },
    }
    generator_track = _generator_track(
        generator_report or {},
        generator_ledger or {},
        snapshot,
        supplemental_generator_reports or [],
        shuffle_evidence or {},
    )
    if phase == "generator_search":
        campaign = (generator_track.get("evidence") or {}).get("campaign") or {}
        campaign_tested = int(campaign.get("candidates_tested") or 0)
        supplemental = campaign.get("supplemental_searches") or []
        family_count = sum(int(item.get("families") or 0) for item in supplemental)
        snapshot["current_action"]["detail"] = (
            f"고정 Discovery {len(discovery)}개로 누적 {campaign_tested:,}개 후보와 "
            f"확장 계열 {family_count}개를 검사했습니다. exact 생성기는 아직 없으며 "
            f"Holdout {len(holdout)}개는 봉인한 채 상태형·호출순서 가설을 계속 좁힙니다."
        )
    early_track = _early_score_track(
        early_score_summaries or [],
        legacy_batch_ids or set(),
        early_score_supervisor or {},
    )
    snapshot["tracks"] = {
        "generator": generator_track,
        "early_score": early_track,
        "combined": {
            "ready": generator_track["qualified"] and early_track["qualified"],
            "generator_qualified": generator_track["qualified"],
            "early_score_qualified": early_track["qualified"],
            "submission_automation_enabled": False,
            "note": "두 독립 증명 트랙이 모두 통과해도 제출 연결은 별도 승인 대상입니다.",
        },
    }
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--prng-report", type=Path, required=True)
    parser.add_argument("--probe-root", type=Path, action="append", required=True)
    parser.add_argument("--supervisor-state", type=Path, required=True)
    parser.add_argument("--generator-report", type=Path)
    parser.add_argument("--generator-ledger", type=Path)
    parser.add_argument(
        "--extended-generator-report",
        type=Path,
        action="append",
        default=[],
        help="Append a bounded search report to the Track A campaign totals.",
    )
    parser.add_argument("--shuffle-leak-report", type=Path)
    parser.add_argument("--shuffle-alignment-report", type=Path)
    parser.add_argument("--shuffle-constraints-report", type=Path)
    parser.add_argument("--fisher-yates-report", type=Path)
    parser.add_argument("--shuffle-prefix-report", type=Path)
    parser.add_argument("--shuffle-prefix-tuple-report", type=Path)
    parser.add_argument("--mt-prefix-cpsat-report", type=Path)
    parser.add_argument("--mt-joint-cpsat-report", type=Path)
    parser.add_argument("--mt-z3-joint-report", type=Path)
    parser.add_argument("--mt-xorsat-joint-report", type=Path)
    parser.add_argument("--mt-fixed-profile-report", type=Path)
    parser.add_argument("--mt-incremental-label-report", type=Path)
    parser.add_argument("--postgres-double-report", type=Path)
    parser.add_argument("--postgres-range-report", type=Path)
    parser.add_argument("--identifiability-report", type=Path)
    parser.add_argument("--mt-rank-audit-report", type=Path)
    parser.add_argument("--early-score-root", type=Path, action="append", default=[])
    parser.add_argument("--early-score-supervisor-state", type=Path)
    parser.add_argument("--signal-root", type=Path, action="append", default=[])
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    cached_tasks: list[dict[str, Any]] = []
    last_seed_signature = ""
    candidate_cache: tuple[dict[str, dict[str, int]], list[str], list[str]] | None = None

    while not stopping:
        try:
            tasks = fetch_tasks(max_pages=max(1, args.max_pages))
            cached_tasks = tasks
        except OSError as error:
            if not cached_tasks:
                print(json.dumps({"event": "task_fetch_failed", "error": type(error).__name__}), flush=True)
                if args.once:
                    return 1
                time.sleep(max(2.0, args.poll_interval))
                continue
            tasks = cached_tasks

        records = sorted(
            (safe_record(task) for task in tasks), key=lambda item: item["created_at"]
        )
        epoch = latest_normal_epoch(records)
        signature = "|".join(
            f"{record['task_id']}:{','.join(map(str, record['seeds']))}" for record in epoch
        )
        if signature != last_seed_signature:
            report = build_report(tasks)
            atomic_json(args.prng_report.resolve(), report)
            candidate_cache = accepted_models(
                epoch[:DISCOVERY_TARGET],
                epoch[DISCOVERY_TARGET : DISCOVERY_TARGET + HOLDOUT_TARGET],
            )
            last_seed_signature = signature

        if candidate_cache is None:
            candidate_cache = accepted_models([], [])
        _stats, _discovery_models, holdout_models = candidate_cache
        now_text = utc_now()
        ledger = update_prediction_ledger(
            load_predictions(args.predictions.resolve()),
            records,
            holdout_models,
            now=now_text,
        )
        atomic_json(args.predictions.resolve(), ledger)
        solutions, statuses = load_probe_documents(
            path.resolve() for path in args.probe_root
        )
        supervisor = read_json(args.supervisor_state.resolve(), {}) or {}
        generator_report = (
            read_json(args.generator_report.resolve(), {}) if args.generator_report else {}
        ) or {}
        generator_ledger = (
            read_json(args.generator_ledger.resolve(), {}) if args.generator_ledger else {}
        ) or {}
        supplemental_generator_reports = [
            read_json(path.resolve(), {}) or {}
            for path in args.extended_generator_report
        ]
        shuffle_evidence = {
            "leak": (
                read_json(args.shuffle_leak_report.resolve(), {})
                if args.shuffle_leak_report
                else {}
            ),
            "alignment": (
                read_json(args.shuffle_alignment_report.resolve(), {})
                if args.shuffle_alignment_report
                else {}
            ),
            "constraints": (
                read_json(args.shuffle_constraints_report.resolve(), {})
                if args.shuffle_constraints_report
                else {}
            ),
            "fisher_yates": (
                read_json(args.fisher_yates_report.resolve(), {})
                if args.fisher_yates_report
                else {}
            ),
            "prefix_domains": (
                read_json(args.shuffle_prefix_report.resolve(), {})
                if args.shuffle_prefix_report
                else {}
            ),
            "prefix_tuples": (
                read_json(args.shuffle_prefix_tuple_report.resolve(), {})
                if args.shuffle_prefix_tuple_report
                else {}
            ),
            "prefix_cpsat": (
                read_json(args.mt_prefix_cpsat_report.resolve(), {})
                if args.mt_prefix_cpsat_report
                else {}
            ),
            "joint_cpsat": (
                read_json(args.mt_joint_cpsat_report.resolve(), {})
                if args.mt_joint_cpsat_report
                else {}
            ),
            "z3_joint": (
                read_json(args.mt_z3_joint_report.resolve(), {})
                if args.mt_z3_joint_report
                else {}
            ),
            "xorsat_joint": (
                read_json(args.mt_xorsat_joint_report.resolve(), {})
                if args.mt_xorsat_joint_report
                else {}
            ),
            "fixed_profile": (
                read_json(args.mt_fixed_profile_report.resolve(), {})
                if args.mt_fixed_profile_report
                else {}
            ),
            "incremental_labels": (
                read_json(args.mt_incremental_label_report.resolve(), {})
                if args.mt_incremental_label_report
                else {}
            ),
            "postgres_double": (
                read_json(args.postgres_double_report.resolve(), {})
                if args.postgres_double_report
                else {}
            ),
            "postgres_range": (
                read_json(args.postgres_range_report.resolve(), {})
                if args.postgres_range_report
                else {}
            ),
            "identifiability": (
                read_json(args.identifiability_report.resolve(), {})
                if args.identifiability_report
                else {}
            ),
            "mt_rank_audit": (
                read_json(args.mt_rank_audit_report.resolve(), {})
                if args.mt_rank_audit_report
                else {}
            ),
        }
        early_score_summaries = _early_score_documents(
            path.resolve() for path in args.early_score_root
        )
        early_score_supervisor = (
            read_json(args.early_score_supervisor_state.resolve(), {})
            if args.early_score_supervisor_state
            else {}
        ) or {}
        legacy_batch_ids = _batch_signal_task_ids(
            path.resolve() for path in args.signal_root
        )
        snapshot = build_snapshot(
            tasks=tasks,
            probe_solutions=solutions,
            probe_statuses=statuses,
            predictions=ledger,
            supervisor=supervisor,
            generator_report=generator_report,
            generator_ledger=generator_ledger,
            supplemental_generator_reports=supplemental_generator_reports,
            shuffle_evidence=shuffle_evidence,
            early_score_summaries=early_score_summaries,
            early_score_supervisor=early_score_supervisor,
            legacy_batch_ids=legacy_batch_ids,
        )
        atomic_json(args.output.resolve(), snapshot)
        print(
            json.dumps(
                {
                    "event": "research_state_updated",
                    "phase": snapshot["phase"],
                    "epoch_tasks": snapshot["dataset"]["latest_epoch_tasks"],
                    "probe_exact": snapshot["probe"]["exact_tasks"],
                    "forward_streak": snapshot["forward"]["consecutive_exact"],
                }
            ),
            flush=True,
        )
        if args.once:
            return 0
        time.sleep(max(2.0, args.poll_interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
