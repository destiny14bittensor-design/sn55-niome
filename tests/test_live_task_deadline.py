from datetime import datetime, timezone
import json
import time

import requests

from niome_subnet.genomics.submission_builder import build_submission
from niome_subnet.miner.task_processor import (
    _candidate_variants,
    _presigned_url_deadline,
    _score_candidates_until_deadline,
    _upload_with_deadline,
    pending_task_envelopes,
    persist_task_envelope,
)
from niome_subnet.genomics.model import Task
from niome_subnet.utils.seeds import seed_blocks, seeds_from_block_hashes
from tests.local_validator.test_clone import base_contract, build_submission_fixture


def test_uses_exact_aws_presigned_expiry():
    issued = datetime(2026, 9, 23, 16, 0, tzinfo=timezone.utc).timestamp()
    url = (
        "https://example.invalid/object?"
        "X-Amz-Date=20260923T160000Z&X-Amz-Expires=300"
    )

    deadline, source = _presigned_url_deadline(
        url,
        now_wall=issued + 60,
        now_monotonic=100.0,
    )

    assert source == "aws-query"
    assert deadline == 340.0


def test_falls_back_to_five_minutes_from_receipt():
    deadline, source = _presigned_url_deadline(
        "https://example.invalid/object",
        now_wall=1_000.0,
        now_monotonic=50.0,
    )

    assert source == "fallback-receipt"
    assert deadline == 350.0


def test_uses_legacy_aws_absolute_expiry():
    deadline, source = _presigned_url_deadline(
        "https://example.invalid/object?Expires=1300&Signature=test",
        now_wall=1_000.0,
        now_monotonic=50.0,
    )

    assert source == "aws-query-v2"
    assert deadline == 350.0


def test_seed_derivation_matches_known_vectors():
    assert seed_blocks(9_132_940) == [9_133_170, 9_133_171, 9_133_172]
    assert seeds_from_block_hashes(["00" * 32, "01" * 32, "02" * 32]) == [
        712,
        342,
        103,
    ]


def test_request_envelope_is_private_and_resumable(tmp_path):
    task = Task(
        id="live-task",
        contract_url="https://example.invalid/contract",
        hbb_ref_url="https://example.invalid/reference",
    )
    url = "https://example.invalid/upload"
    envelope = persist_task_envelope(
        task=task,
        presigned_url=url,
        caller_hotkey="validator",
        artifact_root=tmp_path,
    )

    assert envelope.stat().st_mode & 0o777 == 0o600
    assert pending_task_envelopes(tmp_path) == [(task, url, "validator")]


def test_same_task_id_is_isolated_between_miner_roots(tmp_path):
    task = Task(
        id="shared-live-task",
        contract_url="https://example.invalid/contract",
        hbb_ref_url="https://example.invalid/reference",
    )
    first_root = tmp_path / "dollar1"
    second_root = tmp_path / "dollar2"
    first_url = "https://example.invalid/first-upload"
    second_url = "https://example.invalid/second-upload"

    first = persist_task_envelope(
        task=task,
        presigned_url=first_url,
        caller_hotkey="validator-one",
        artifact_root=first_root,
    )
    second = persist_task_envelope(
        task=task,
        presigned_url=second_url,
        caller_hotkey="validator-two",
        artifact_root=second_root,
    )

    assert first != second
    assert first.parent.parent == first_root
    assert second.parent.parent == second_root
    assert json.loads(first.read_text())["presigned_url"] == first_url
    assert json.loads(second.read_text())["presigned_url"] == second_url
    assert pending_task_envelopes(first_root) == [
        (task, first_url, "validator-one")
    ]
    assert pending_task_envelopes(second_root) == [
        (task, second_url, "validator-two")
    ]


def test_builder_returns_immediate_valid_fallback_when_deadline_passed():
    contract = {
        "active_mutations": ["mutation-a"],
        "rules": {
            "base_padding": 10,
            "proximity_gate": True,
            "cas_systems": ["Cas9"],
            "max_experiments": 10,
        },
    }
    reference = {
        "mutation_map": {"mutation-a": 100},
        "gene_region": {"start": 50, "end": 150},
    }

    submission, diagnostics = build_submission(
        contract=contract,
        reference=reference,
        chromosome_11="A" * 500,
        cell_types={},
        deadline_monotonic=time.monotonic() - 1,
    )

    assert submission == []
    assert diagnostics["deadline_reached"] is True
    assert diagnostics["scanned_candidates"] == 0


def test_upload_retries_inside_deadline(monkeypatch):
    attempts = []

    class Response:
        def raise_for_status(self):
            return None

    def fake_put(*args, **kwargs):
        attempts.append(kwargs["timeout"])
        assert "headers" not in kwargs
        if len(attempts) == 1:
            raise requests.ConnectionError("transient")
        return Response()

    monkeypatch.setattr(requests, "put", fake_put)
    monkeypatch.setattr(time, "sleep", lambda _: None)

    _, attempt_count = _upload_with_deadline(
        "https://example.invalid/upload",
        b"[]",
        deadline_monotonic=time.monotonic() + 10,
    )

    assert attempt_count == 2
    assert len(attempts) == 2


def test_candidate_variants_keep_full_baseline_as_fallback():
    baseline = [
        {"experiment_id": str(index), "guideRNA": f"guide-{index}"}
        for index in range(20)
    ]

    variants = _candidate_variants(baseline)

    assert variants[0] == ("balanced-full", baseline)
    assert 2 <= len(variants) <= 5
    assert all(len(candidate) <= len(baseline) for _, candidate in variants)


def test_scoring_does_not_start_after_deadline():
    results, deadline_reached = _score_candidates_until_deadline(
        [("baseline", [])],
        {},
        deadline_monotonic=time.monotonic() - 1,
    )

    assert results == []
    assert deadline_reached is True


def test_scoring_worker_returns_result_before_deadline(tmp_path):
    sequence, submission = build_submission_fixture(10)
    contract_path = tmp_path / "contract.json"
    reference_path = tmp_path / "reference.json"
    chromosome_path = tmp_path / "chr11.fa"
    cell_types_path = tmp_path / "cell_types.json"
    contract_path.write_text(json.dumps(base_contract()), encoding="utf-8")
    reference_path.write_text(
        json.dumps(
            {
                "mutation_map": {"m1": 20},
                "gene_region": {"start": 0, "end": 650},
            }
        ),
        encoding="utf-8",
    )
    chromosome_path.write_text(f">chr11\n{sequence}\n", encoding="utf-8")
    cell_types_path.write_text("{}\n", encoding="utf-8")

    results, deadline_reached = _score_candidates_until_deadline(
        [("baseline", submission)],
        {
            "contract_path": str(contract_path),
            "hbb_reference_path": str(reference_path),
            "chromosome_11_path": str(chromosome_path),
            "cell_types_path": str(cell_types_path),
        },
        deadline_monotonic=time.monotonic() + 20,
    )

    assert deadline_reached is False
    assert len(results) == 1
    assert results[0]["label"] == "baseline"
    assert results[0]["final_score"] > 0
