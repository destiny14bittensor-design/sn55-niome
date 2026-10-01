from datetime import datetime, timedelta, timezone

from tools.seed_research_orchestrator import (
    _early_score_track,
    _generator_track,
    build_snapshot,
    consecutive_ordered_exact,
    consecutive_exact,
    latest_normal_epoch,
    update_prediction_ledger,
)
from tools.seed_prng_fingerprint import safe_record


NOW = datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc)


def task(index: int, seed="100,200,300"):
    created = datetime(2026, 9, 26, tzinfo=timezone.utc) + timedelta(hours=index)
    return {
        "id": f"00000000-0000-4000-8000-{index:012d}",
        "created_at": created.isoformat(),
        "content": {
            "contract": {
                "seed": seed,
                "active_mutations": ["m1", "m2"],
                "cell_type": "K562",
            },
            "hbb_reference": {},
        },
    }


def test_current_epoch_ignores_seedless_publication_gap():
    records = [
        safe_record(task(0, "101,202,303")),
        safe_record(task(1, 0)),
        safe_record(task(2, "404,505,606")),
    ]
    assert [record["seeds"] for record in latest_normal_epoch(records)] == [
        [101, 202, 303],
        [404, 505, 606],
    ]


def test_current_epoch_fails_closed_on_post_boundary_shape_change():
    records = [
        safe_record(task(0, "101,202,303")),
        safe_record(task(1, "9000,8000,7000")),
        safe_record(task(2, "404,505,606")),
    ]
    assert latest_normal_epoch(records) == []


def test_snapshot_separates_discovery_holdout_and_forward():
    tasks = [task(index) for index in range(26)]
    snapshot = build_snapshot(
        tasks=tasks,
        probe_solutions=[],
        probe_statuses=[],
        predictions={"predictions": []},
        supervisor={"updated_at": NOW.isoformat(), "active_task_id": "active-task"},
        now=NOW,
    )
    assert snapshot["dataset"]["discovery_tasks"] == 20
    assert snapshot["dataset"]["holdout_tasks"] == 5
    assert snapshot["forward"]["resolved_predictions"] == 0
    assert snapshot["phase"] == "generator_search"


def test_probe_verification_is_order_insensitive_but_retains_order_result():
    current = task(0, "229,274,353")
    solution = {
        "task_id": current["id"],
        "captured_at": "2026-09-01T02:00:04Z",
        "candidate_count": 1,
        "candidates": [{"seeds": [353, 229, 274]}],
        "observations": {"lane": {"created_at": "2026-09-01T02:00:00Z"}},
        "watch": {"partial_table_match": True},
    }
    snapshot = build_snapshot(
        tasks=[current],
        probe_solutions=[solution],
        probe_statuses=[],
        predictions={"predictions": []},
        supervisor={},
        now=NOW,
    )
    assert snapshot["probe"]["exact_tasks"] == 1
    assert snapshot["probe"]["latest"]["exact_ordered"] is False
    assert snapshot["probe"]["latest"]["recovery_latency_seconds"] == 4


def test_forward_streak_stops_at_latest_failure():
    predictions = [
        {"task_created_at": "1", "resolved_at": "1", "exact": True},
        {"task_created_at": "2", "resolved_at": "2", "exact": False},
        {"task_created_at": "3", "resolved_at": "3", "exact": True},
        {"task_created_at": "4", "resolved_at": "4", "exact": True},
    ]
    assert consecutive_exact(predictions) == 2


def test_prediction_is_not_written_without_holdout_model():
    seedless = safe_record(task(0, 0))
    ledger = update_prediction_ledger(
        {"predictions": []}, [seedless], [], now=NOW.isoformat()
    )
    assert ledger["predictions"] == []


def test_fingerprint_complete_reports_public_score_wait_as_current_action():
    tasks = [task(index) for index in range(26)]
    snapshot = build_snapshot(
        tasks=tasks,
        probe_solutions=[],
        probe_statuses=[
            {
                "task_id": "active-task",
                "state": "fingerprints_complete",
                "updated_at": NOW.isoformat(),
            }
        ],
        predictions={"predictions": []},
        supervisor={"updated_at": NOW.isoformat(), "active_task_id": "active-task"},
        now=NOW,
    )

    assert snapshot["phase"] == "probe_score_wait"
    assert snapshot["current_action"]["task_id"] == "active-task"


def test_stale_probe_status_is_not_reported_as_active():
    snapshot = build_snapshot(
        tasks=[task(index) for index in range(26)],
        probe_solutions=[],
        probe_statuses=[
            {"task_id": "stale", "state": "fingerprints_complete", "updated_at": NOW.isoformat()}
        ],
        predictions={"predictions": []},
        supervisor={"updated_at": NOW.isoformat(), "active_task_id": None},
        now=NOW,
    )
    assert snapshot["phase"] == "generator_search"


def test_track_a_counts_only_prospective_ordered_exact():
    ledger = {
        "predictions": [
            {"task_created_at": "1", "predicted_at": "0", "resolved_at": "1", "exact_ordered": True},
            {"task_created_at": "2", "predicted_at": "1", "resolved_at": "2", "exact_ordered": False},
            {"task_created_at": "3", "predicted_at": "2", "resolved_at": "3", "exact_ordered": True},
        ]
    }
    assert consecutive_ordered_exact(ledger["predictions"]) == 1
    track = _generator_track(
        {
            "split_policy": {"counts": {"discovery": 20, "holdout": 5}, "minimum_discovery": 20, "minimum_holdout": 5},
            "registry": {"model_count": 640},
            "evaluation": {"holdout_pass": ["model"], "unique_model_gate": True},
            "quarantined_baseline": {"candidate_count": 363},
        },
        ledger,
        {},
    )
    assert track["qualified"] is False
    assert track["metrics"]["forward_exact"] == "1 / 5"


def test_track_a_aggregates_bounded_extended_searches():
    track = _generator_track(
        {
            "split_policy": {
                "counts": {"discovery": 20, "holdout": 5},
                "minimum_discovery": 20,
                "minimum_holdout": 5,
            },
            "registry": {"model_count": 640, "families": ["base-a", "base-b"]},
            "evaluation": {
                "models_fully_evaluable": 640,
                "holdout_pass": [],
                "unique_model_gate": False,
            },
            "quarantined_baseline": {"candidate_count": 363},
        },
        {"predictions": []},
        {},
        [
            {
                "mode": "block-offset-search",
                "search": {
                    "candidates_tested": 100,
                    "discovery_exact_candidates": 0,
                    "families": {"one": {}, "two": {}},
                },
            },
            {
                "mode": "drand-search",
                "search": {
                    "candidates_tested": 200,
                    "discovery_exact_candidates": 1,
                    "families": {"three": {}},
                },
            },
            {
                "mode": "independent-clock-search",
                "search": {
                    "candidate_initializations": 300,
                    "exact_candidates": [],
                    "configurations": [{"name": "seconds"}, {"name": "milliseconds"}],
                },
            },
            {
                "mode": "numpy-pcg64-persistent-uint32-exhaustive",
                "search": {
                    "tested": 7,
                    "engine": "numpy-pcg64",
                    "method": "choice",
                    "discovery_exact_candidates": 0,
                },
            },
        ],
        {
            "leak": {
                "summary": {"total_permutation_information_bits_upper_bound": 21000}
            },
            "alignment": {
                "summary": {
                    "exact_unique_uid_positions": 2000,
                    "symbolic_solver_input_ready": True,
                }
            },
            "constraints": {
                "summary": {
                    "group_order_constraints": 4200,
                    "group_sequence_missing_positions": 80,
                    "incomplete_rounds_excluded": 1,
                }
            },
            "fisher_yates": {
                "summary": {
                    "sat_rounds": 3,
                    "mt19937_state_recovered": False,
                }
            },
            "mt_rank_audit": {
                "summary": {
                    "rank": 19937,
                    "effective_state_bits": 19937,
                    "full_rank_round": 17,
                    "synthetic_predictive_state_recovered": True,
                    "future_words_verified": 32,
                }
            },
            "xorsat_joint": {
                "summary": {
                    "status": "sat",
                    "rounds_modelled": 5,
                    "full_shuffle_rounds_bound": 2,
                    "full_group_positions_bound": 501,
                    "rejected_word_inequalities_enforced": True,
                    "excluded_discovery_predicted": False,
                }
            },
            "incremental_labels": {
                "summary": {
                    "status": "sat",
                    "target_rounds": 8,
                    "completed_rounds": 7,
                    "seed_labels_bound": 21,
                    "public_shuffle_rows_passed": 0,
                }
            },
            "postgres_double": {
                "summary": {"status": "unknown", "constrained_outputs": 10}
            },
            "postgres_range": {
                "summary": {"status": "unknown", "constrained_outputs": 16}
            },
        },
    )
    assert track["evidence"]["campaign"]["candidates_tested"] == 1610
    assert track["evidence"]["campaign"]["discovery_exact_candidates"] == 1
    assert track["evidence"]["campaign"]["supplemental_searches"][0]["mode"] == "block-offset-search"
    assert track["metrics"]["incremental_label_sat_rounds"] == 7
    assert track["metrics"]["incremental_label_sat_seed_labels_bound"] == 21
    assert track["evidence"]["campaign"]["supplemental_searches"][2] == {
        "mode": "independent-clock-search",
        "candidates_tested": 300,
        "discovery_exact_candidates": 0,
        "families": 2,
    }
    assert track["evidence"]["campaign"]["supplemental_searches"][3]["families"] == 1
    assert "누적 1,610" in track["metrics"]["tested_models"]
    assert track["evidence"]["shuffle_state_recovery"]["symbolic_solver_input_ready"] is True
    assert track["evidence"]["shuffle_state_recovery"]["same_rng_coupling_verified"] is False
    assert track["evidence"]["shuffle_state_recovery"]["group_order_constraints"] == 4200
    assert track["evidence"]["shuffle_state_recovery"]["incomplete_shuffle_rounds_excluded"] == 1
    assert track["evidence"]["shuffle_state_recovery"]["fisher_yates_witness_rounds"] == 3
    assert track["evidence"]["shuffle_state_recovery"]["synthetic_effective_rank"] == 19937
    assert track["evidence"]["shuffle_state_recovery"]["synthetic_full_rank_round"] == 17
    assert track["evidence"]["shuffle_state_recovery"]["synthetic_predictive_state_recovered"] is True
    assert track["evidence"]["shuffle_state_recovery"]["xorsat_joint_status"] == "sat"
    assert track["evidence"]["shuffle_state_recovery"]["xorsat_joint_rounds_modelled"] == 5
    assert track["evidence"]["shuffle_state_recovery"]["xorsat_joint_rejected_words_enforced"] is True
    assert track["evidence"]["shuffle_state_recovery"]["xorsat_joint_excluded_discovery_predicted"] is False
    assert track["evidence"]["shuffle_state_recovery"]["postgres_double_outputs_constrained"] == 10
    assert track["evidence"]["shuffle_state_recovery"]["postgres_range_outputs_constrained"] == 16


def test_track_b_requires_confirmed_actionable_rounds_not_batch_count():
    batch_only = {
        "task_id": "batch",
        "updated_at": NOW.isoformat(),
        "score_publication": {"cohort_size": 8},
        "lead_time": {"status": "not_early", "seconds": 0},
        "decision": {"actionable": False, "gates": {}},
    }
    track = _early_score_track([batch_only], {"legacy"}, {})
    assert track["metrics"]["batch_observations"] == 2
    assert track["metrics"]["actionable_individual_scores"] == 0
    assert track["qualified"] is False
