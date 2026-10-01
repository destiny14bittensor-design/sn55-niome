import json
from pathlib import Path

import pytest

from tools.preseed_generator_lab import (
    ModelSpec,
    attach_chain_context,
    atomic_json,
    default_model_registry,
    load_chain_context,
    load_tasks,
    main,
    normalize_task,
    predict_model,
    run_lab,
    stateful_sequence_diagnostics,
    update_ledger,
    validate_splits,
)


NOW = "2026-09-29T04:00:00+00:00"
MODEL = ModelSpec(
    model_id="fixture-chain-created-task",
    family="public-digest-triplet-v1",
    components=("block.created.hash", "task.id"),
)


def task(index: int, seeds=None, *, block_hash: str | None = None):
    raw = {
        "id": f"00000000-0000-4000-8000-{index:012d}",
        "created_at": f"2026-09-29T{index:02d}:00:00Z",
        "content": {
            "contract": {
                "seed": seeds or "",
                "cell_type": "K562",
                "active_mutations": ["m1", "m2"],
            }
        },
    }
    if block_hash is not None:
        raw["block_context"] = {
            "created": {"number": 9000000 + index, "hash": block_hash}
        }
    return normalize_task(raw)


def labeled(index: int):
    record = task(index, block_hash=f"0x{index + 1:064x}")
    record["seeds"] = predict_model(MODEL, record)
    return record


def test_default_registry_is_chain_aware_and_quarantined_family_is_not_included():
    registry = default_model_registry()
    assert len(registry) == 640
    assert all(any(component.startswith("block.") for component in spec.components) for spec in registry)
    assert all("uuid-string" not in spec.model_id for spec in registry)
    assert len({spec.family for spec in registry}) == 2


def test_private_material_is_rejected_before_normalization(tmp_path: Path):
    source = tmp_path / "tasks.json"
    source.write_text(json.dumps({"items": [{"id": "x", "api_key": "secret"}]}))
    with pytest.raises(ValueError, match="private input key"):
        load_tasks(source)


def test_split_overlap_is_a_hard_error():
    row = labeled(0)
    with pytest.raises(ValueError, match="split contamination"):
        validate_splits([row], [row], [])


def test_pre_epoch_record_is_rejected_from_active_fit():
    row = labeled(0)
    row["created_at"] = "2026-09-25T20:30:35Z"
    with pytest.raises(ValueError, match="before configured epoch"):
        validate_splits([row], [], [])


def test_unique_discovery_and_holdout_winner_opens_shadow_prediction_gate():
    discovery = [labeled(index) for index in range(2)]
    holdout = [labeled(2)]
    prospective = [task(3, block_hash=f"0x{4:064x}")]
    report, ledger = run_lab(
        discovery=discovery,
        holdout=holdout,
        prospective=prospective,
        registry=[MODEL],
        ledger={"predictions": []},
        min_discovery=2,
        min_holdout=1,
        now=NOW,
    )
    assert report["evaluation"]["holdout_pass"] == [MODEL.model_id]
    assert report["evaluation"]["unique_model_gate"] is True
    assert report["quarantined_baseline"]["candidate_count"] == 363
    assert report["evaluation"]["models_registered"] == 1
    assert report["evaluation"]["models_fully_evaluable"] == 1
    assert report["evaluation"]["models_input_blocked"] == 0
    assert ledger["predictions"][0]["task_id"] == prospective[0]["task_id"]
    assert ledger["predictions"][0]["predicted_at"] == NOW
    assert ledger["predictions"][0]["status"] == "pending"


def test_two_identical_winners_keep_prediction_gate_closed():
    duplicate = ModelSpec(
        model_id="same-formula-different-id",
        family=MODEL.family,
        components=MODEL.components,
    )
    report, ledger = run_lab(
        discovery=[labeled(0)],
        holdout=[labeled(1)],
        prospective=[task(2, block_hash=f"0x{3:064x}")],
        registry=[MODEL, duplicate],
        ledger={"predictions": []},
        min_discovery=1,
        min_holdout=1,
        now=NOW,
    )
    assert len(report["evaluation"]["holdout_pass"]) == 2
    assert report["evaluation"]["unique_model_gate"] is False
    assert ledger["predictions"] == []


def test_missing_public_header_is_reported_as_input_blocked_not_tested():
    header_model = ModelSpec(
        model_id="fixture-header",
        family="public-digest-triplet-v1",
        components=("block.created.header", "task.id"),
    )
    report, _ = run_lab(
        discovery=[labeled(0)],
        holdout=[labeled(1)],
        prospective=[],
        registry=[header_model],
        ledger={"predictions": []},
        min_discovery=1,
        min_holdout=1,
        now=NOW,
    )
    assert report["evaluation"]["models_tested"] == 0
    assert report["evaluation"]["models_input_blocked"] == 1


def test_insufficient_split_sizes_keep_prediction_gate_closed():
    report, ledger = run_lab(
        discovery=[labeled(0)],
        holdout=[labeled(1)],
        prospective=[task(2, block_hash=f"0x{3:064x}")],
        registry=[MODEL],
        ledger={"predictions": []},
        min_discovery=2,
        min_holdout=1,
        now=NOW,
    )
    assert report["split_policy"]["sufficient"] is False
    assert report["evaluation"]["holdout_pass"] == []
    assert ledger["predictions"] == []


def test_epoch_change_gate_blocks_an_otherwise_exact_model():
    report, ledger = run_lab(
        discovery=[labeled(0)],
        holdout=[labeled(1)],
        prospective=[task(2, block_hash=f"0x{3:064x}")],
        registry=[MODEL],
        ledger={"predictions": []},
        min_discovery=1,
        min_holdout=1,
        now=NOW,
        epoch_policy={
            "id": "post-score-random-triple-100-999-v1",
            "gate_open": False,
            "status": "change-detected",
            "violation": {"task_id": "changed"},
        },
    )
    assert report["split_policy"]["sufficient"] is False
    assert report["evaluation"]["prediction_gate_reason"] == "blocked-by-epoch-change"
    assert ledger["predictions"] == []


def test_labeled_prospective_row_only_resolves_preexisting_prediction():
    record = labeled(3)
    previous = {
        "predictions": [
            {
                "task_id": record["task_id"],
                "predicted_seeds": record["seeds"],
                "predicted_at": "2026-09-29T03:00:00Z",
                "status": "pending",
            }
        ]
    }
    ledger = update_ledger(
        previous,
        [record],
        [],
        registry_sha256="registry",
        now=NOW,
    )
    assert ledger["predictions"][0]["status"] == "resolved"
    assert ledger["predictions"][0]["exact_ordered"] is True

    no_backfill = update_ledger(
        {"predictions": []},
        [record],
        [MODEL],
        registry_sha256="registry",
        now=NOW,
    )
    assert no_backfill["predictions"] == []


def test_already_published_score_blocks_new_prospective_prediction():
    prospective = task(3, block_hash=f"0x{4:064x}")
    prospective["score_published_at"] = "2026-09-29T03:59:00Z"
    ledger = update_ledger(
        {"predictions": []},
        [prospective],
        [MODEL],
        registry_sha256="registry",
        now=NOW,
    )
    assert ledger["predictions"] == []


def test_chain_context_is_attached_from_offline_json(tmp_path: Path):
    path = tmp_path / "chain.json"
    path.write_text(
        json.dumps(
            {
                "tasks": {
                    "t1": {
                        "created": {
                            "number": 1,
                            "hash": "0x01",
                            "header": {"parentHash": "0x00"},
                        }
                    }
                }
            }
        )
    )
    records = [
        {
            "task_id": "t1",
            "created_at": "",
            "seeds": [],
            "contract_material": "{}",
            "block_context": {},
        }
    ]
    attached = attach_chain_context(records, [load_chain_context(path)])
    assert attached[0]["block_context"]["created"]["hash"] == "0x01"
    assert predict_model(MODEL, attached[0]) is not None


def test_stateful_diagnostics_falsify_mismatched_direct_recurrences_without_prediction_claim():
    records = [task(0), task(1)]
    records[0]["seeds"] = [101, 234, 567]
    records[1]["seeds"] = [890, 345, 678]
    result = stateful_sequence_diagnostics(records)
    assert result["diagnostic_only"] is True
    assert "does not exclude hidden state" in result["limitations"]
    assert any(row["falsified_on_observed_sequence"] for row in result["xorshift_like"])


def test_atomic_json_replaces_file_without_leaving_temporary(tmp_path: Path):
    output = tmp_path / "result.json"
    atomic_json(output, {"a": 1})
    atomic_json(output, {"a": 2})
    assert json.loads(output.read_text()) == {"a": 2}
    assert not (tmp_path / ".result.json.tmp").exists()


def test_cli_uses_only_offline_files_and_writes_report_and_ledger(tmp_path: Path):
    discovery = [labeled(0)]
    holdout = [labeled(1)]
    prospective = [task(2, block_hash=f"0x{3:064x}")]
    registry = {"models": [{**MODEL.__dict__, "components": list(MODEL.components)}]}
    paths = {}
    for name, value in (
        ("discovery", discovery),
        ("holdout", holdout),
        ("prospective", prospective),
        ("registry", registry),
    ):
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(value))
    report = tmp_path / "report.json"
    ledger = tmp_path / "ledger.json"
    result = main(
        [
            "--discovery-json",
            str(paths["discovery"]),
            "--holdout-json",
            str(paths["holdout"]),
            "--prospective-json",
            str(paths["prospective"]),
            "--registry-json",
            str(paths["registry"]),
            "--report",
            str(report),
            "--ledger",
            str(ledger),
            "--min-discovery",
            "1",
            "--min-holdout",
            "1",
            "--as-of",
            NOW,
        ]
    )
    assert result == 0
    assert json.loads(report.read_text())["safety"]["network_calls"] is False
    assert len(json.loads(ledger.read_text())["predictions"]) == 1
