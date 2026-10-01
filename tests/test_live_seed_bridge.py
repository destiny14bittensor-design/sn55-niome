import json
import os
import threading
import time

import pytest

import tools.live_seed_bridge as bridge_module
from tools.local_validator.artifacts import ArtifactBundle
from tools.live_seed_bridge import (
    SlowPut,
    _assert_contract_seed_only_changed,
    _choose_exact_consistency_candidate,
    _consistency_sample_from_payload,
    _envelope_seconds_remaining,
    _fetch_authorized_validator_contract,
    _fetch_refreshed_contract,
    _fetch_task_history_contract,
    _failure_category,
    _handle_envelope,
    _planned_stream_total_bytes,
    _quarantine_inherited_unknown_seed_tasks,
    _round_coordinates,
    _standby_start_delay,
    _submission_tail,
    _timing_probe,
    _wait_for_authoritative_contract_seed,
)


def test_authorized_validator_contract_uses_owned_wallet_and_exact_task(
    tmp_path, monkeypatch
):
    task_dir = tmp_path / "task-a"
    task_dir.mkdir()
    (task_dir / "contract.json").write_text(
        json.dumps({"seed": 0, "version": "v1"})
    )

    class FakeHotkey:
        ss58_address = "owned-validator-hotkey"

        @staticmethod
        def sign(message):
            assert b'"netuid":"55"' in message
            assert b'"hotkey":"owned-validator-hotkey"' in message
            return b"\xab\xcd"

    class FakeWallet:
        hotkey = FakeHotkey()

    monkeypatch.setattr(
        bridge_module, "AUTHORIZED_VALIDATOR_WALLET_NAME", "validator-cold"
    )
    monkeypatch.setattr(
        bridge_module, "AUTHORIZED_VALIDATOR_WALLET_HOTKEY", "validator-hot"
    )
    monkeypatch.setattr(
        bridge_module, "AUTHORIZED_VALIDATOR_WALLET_PATH", "/owned/wallets"
    )
    monkeypatch.setattr(
        bridge_module, "_AUTHORIZED_VALIDATOR_WALLET", FakeWallet()
    )

    requests = []
    payloads = iter(
        [
            {
                "task_id": "task-a",
                "contract_url": "https://bucket.example/private-contract.json",
            },
            {"seed": "218,229,395", "version": "v1"},
        ]
    )

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, **_kwargs):
        requests.append(request)
        return FakeResponse(next(payloads))

    monkeypatch.setattr(bridge_module, "urlopen", fake_urlopen)
    contract = _fetch_authorized_validator_contract(task_dir)

    assert contract["seed"] == "218,229,395"
    assert requests[0].full_url == bridge_module.TASK_URL
    assert requests[0].get_header("X-hotkey") == "owned-validator-hotkey"
    assert requests[0].get_header("X-netuid") == "55"
    assert requests[0].get_header("X-signature") == "abcd"
    assert requests[1].full_url == (
        "https://bucket.example/private-contract.json"
    )


def test_authorized_validator_contract_rejects_other_task(tmp_path, monkeypatch):
    task_dir = tmp_path / "task-a"
    task_dir.mkdir()

    class FakeHotkey:
        ss58_address = "owned-validator-hotkey"

        @staticmethod
        def sign(_message):
            return b"\x01"

    class FakeWallet:
        hotkey = FakeHotkey()

    monkeypatch.setattr(
        bridge_module, "AUTHORIZED_VALIDATOR_WALLET_NAME", "validator-cold"
    )
    monkeypatch.setattr(
        bridge_module, "AUTHORIZED_VALIDATOR_WALLET_HOTKEY", "validator-hot"
    )
    monkeypatch.setattr(
        bridge_module, "AUTHORIZED_VALIDATOR_WALLET_PATH", "/owned/wallets"
    )
    monkeypatch.setattr(
        bridge_module, "_AUTHORIZED_VALIDATOR_WALLET", FakeWallet()
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read():
            return json.dumps(
                {
                    "task_id": "task-b",
                    "contract_url": "https://bucket.example/contract.json",
                }
            ).encode()

    monkeypatch.setattr(
        bridge_module,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )
    with pytest.raises(ValueError, match="UUID"):
        _fetch_authorized_validator_contract(task_dir)


def test_same_round_overwrite_is_disabled_without_opening_put(tmp_path, monkeypatch):
    task_dir = tmp_path / "task-a"
    task_dir.mkdir()
    envelope = task_dir / "request_envelope.json"
    envelope.write_text("{}")

    monkeypatch.setattr(bridge_module, "SAME_ROUND_OVERWRITE_ENABLED", False)

    class ForbiddenSlowPut:
        def __init__(self, *_args, **_kwargs):
            pytest.fail("disabled same-round path must not open a PUT")

    monkeypatch.setattr(bridge_module, "SlowPut", ForbiddenSlowPut)
    _handle_envelope(envelope)

    status = json.loads((task_dir / "seed_bridge_status.json").read_text())
    assert status["state"] == "disabled_same_round_overwrite"
    assert status["active_put_count"] == 0
    assert status["submission_attempted"] is False
    assert "same_round_overwrite_disabled" in (
        task_dir / "seed_bridge_events.jsonl"
    ).read_text()


def test_consistency_history_accepts_only_contract_authoritative_exact_replay():
    payload = {
        "comparable_to_official": True,
        "seed_authority_epoch": bridge_module.SEED_AUTHORITY_EPOCH,
        "seed_policy": {"mode": "contract-authoritative"},
        "breakdown": {
            "total_weighted_score": 300.0,
            "distribution_fidelity_factor": 0.9,
            "consistency_factor": 0.75,
        },
    }

    accepted = _consistency_sample_from_payload("task", payload, 189.0)
    assert accepted is not None
    assert accepted.baseline_score == pytest.approx(270.0)
    assert accepted.normalized_top == pytest.approx(0.70)

    payload["seed_policy"]["mode"] = "chain-authoritative"
    assert _consistency_sample_from_payload("task", payload, 189.0) is None

    payload["seed_policy"]["mode"] = "contract-authoritative"
    payload.pop("seed_authority_epoch")
    assert _consistency_sample_from_payload("task", payload, 189.0) is None


def test_exact_consistency_search_selects_closest_candidate_above_target(
    monkeypatch,
):
    class Result:
        def __init__(self, consistency):
            self.final_score = 250.0 * consistency
            self.breakdown = {
                "consistency_factor": consistency,
                "total_weighted_score": 250.0,
                "distribution_fidelity_factor": 1.0,
            }

    class Artifacts:
        contract = {"seed": "old"}

    achieved = {"max": 1.0, "low": 0.72, "near": 0.77, "high": 0.84}

    def fake_evaluate(candidate, _artifacts, raw_submission_bytes):
        assert raw_submission_bytes
        return Result(achieved[candidate[0]["experiment_id"]])

    monkeypatch.setattr(bridge_module, "evaluate_submission", fake_evaluate)
    monkeypatch.setattr(
        bridge_module,
        "replace",
        lambda artifacts, contract: Artifacts(),
    )
    candidates = [
        ("managed-low", [{"experiment_id": "low"}]),
        ("managed-near", [{"experiment_id": "near"}]),
        ("managed-high", [{"experiment_id": "high"}]),
        ("max-score-fallback", [{"experiment_id": "max"}]),
    ]
    selected, diagnostics = _choose_exact_consistency_candidate(
        candidates=candidates,
        artifacts=Artifacts(),
        seeds=[1, 2, 3],
        target_consistency=0.76,
    )

    assert selected[0]["experiment_id"] == "near"
    assert diagnostics["selected_label"] == "managed-near"
    assert diagnostics["fallback_used"] is False


def test_cold_start_exact_search_stays_in_lower_band(monkeypatch):
    class Result:
        def __init__(self, consistency):
            self.final_score = 250.0 * consistency
            self.breakdown = {
                "consistency_factor": consistency,
                "total_weighted_score": 250.0,
                "distribution_fidelity_factor": 1.0,
            }

    class Artifacts:
        contract = {"seed": "old"}

    achieved = {"max": 1.0, "low": 0.62, "near": 0.73, "high": 0.81}

    def fake_evaluate(candidate, _artifacts, raw_submission_bytes):
        assert raw_submission_bytes
        return Result(achieved[candidate[0]["experiment_id"]])

    monkeypatch.setattr(bridge_module, "evaluate_submission", fake_evaluate)
    monkeypatch.setattr(
        bridge_module,
        "replace",
        lambda artifacts, contract: Artifacts(),
    )
    candidates = [
        ("managed-low", [{"experiment_id": "low"}]),
        ("managed-near", [{"experiment_id": "near"}]),
        ("managed-high", [{"experiment_id": "high"}]),
        ("max-score-fallback", [{"experiment_id": "max"}]),
    ]

    selected, diagnostics = _choose_exact_consistency_candidate(
        candidates=candidates,
        artifacts=Artifacts(),
        seeds=[1, 2, 3],
        target_consistency=0.77,
        allow_max_score_fallback=False,
        minimum_consistency=0.60,
        maximum_consistency=0.77,
    )

    assert selected[0]["experiment_id"] == "near"
    assert diagnostics["selected_label"] == "managed-near"
    assert diagnostics["selected"]["consistency_factor"] == pytest.approx(0.73)
    assert diagnostics["fallback_used"] is False
    assert diagnostics["target_met"] is False


def test_managed_exact_search_rejects_candidate_below_band(monkeypatch):
    class Result:
        final_score = 100.0
        breakdown = {
            "consistency_factor": 0.58,
            "total_weighted_score": 200.0,
            "distribution_fidelity_factor": 1.0,
        }

    class Artifacts:
        contract = {"seed": "old"}

    monkeypatch.setattr(
        bridge_module,
        "evaluate_submission",
        lambda *_args, **_kwargs: Result(),
    )
    monkeypatch.setattr(
        bridge_module,
        "replace",
        lambda artifacts, contract: Artifacts(),
    )

    with pytest.raises(RuntimeError, match="inside its consistency band"):
        _choose_exact_consistency_candidate(
            candidates=[("managed-low", [{"experiment_id": "low"}])],
            artifacts=Artifacts(),
            seeds=[1, 2, 3],
            target_consistency=0.77,
            allow_max_score_fallback=False,
            minimum_consistency=0.60,
            maximum_consistency=0.77,
        )


def test_fetch_refreshed_contract_uses_task_url_without_persisting_it(
    tmp_path, monkeypatch
):
    secret_url = "https://bucket.example/contract.json?signature=secret"
    (tmp_path / "task.json").write_text(json.dumps({"contract_url": secret_url}))
    observed = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read():
            return json.dumps({"seed": "50001,900000", "version": "v1"}).encode()

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(bridge_module, "urlopen", fake_urlopen)
    refreshed = _fetch_refreshed_contract(tmp_path)

    assert refreshed["seed"] == "50001,900000"
    assert observed["url"] == secret_url
    assert observed["timeout"] == bridge_module.CONTRACT_HTTP_TIMEOUT_SECONDS
    assert list(tmp_path.iterdir()) == [tmp_path / "task.json"]


def test_refreshed_contract_may_change_only_the_seed():
    original = {"seed": 0, "version": "v1", "rules": {"max_experiments": 250}}
    refreshed = {
        "seed": "50001,900000",
        "version": "v1",
        "rules": {"max_experiments": 250},
    }
    _assert_contract_seed_only_changed(original, refreshed)

    refreshed["rules"]["max_experiments"] = 500
    with pytest.raises(ValueError, match="other than seed"):
        _assert_contract_seed_only_changed(original, refreshed)


def test_fetch_task_history_contract_exact_matches_and_verifies_reference(
    tmp_path, monkeypatch
):
    task_id = tmp_path.name
    original = {"seed": 0, "version": "v1", "rules": {"max_experiments": 250}}
    original_reference = {
        "challenge": dict(original),
        "chromosome": "11",
        "window_id": "HBB_v1",
    }
    (tmp_path / "contract.json").write_text(json.dumps(original))
    (tmp_path / "hbb_reference.json").write_text(json.dumps(original_reference))
    refreshed = {**original, "seed": "519,253,867"}
    payload = {
        "items": [
            {"id": "another-task", "content": {}},
            {
                "id": task_id,
                "content": {
                    "contract": refreshed,
                    "hbb_reference": {
                        **original_reference,
                        "challenge": dict(refreshed),
                    },
                },
            },
        ]
    }
    observed = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read():
            return json.dumps(payload).encode()

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(bridge_module, "urlopen", fake_urlopen)

    assert _fetch_task_history_contract(tmp_path) == refreshed
    assert "page=1" in observed["url"]
    assert f"per_page={bridge_module.TASK_HISTORY_PAGE_SIZE}" in observed["url"]
    assert observed["timeout"] == bridge_module.CONTRACT_HTTP_TIMEOUT_SECONDS


def test_fetch_task_history_contract_rejects_non_seed_changes(tmp_path, monkeypatch):
    task_id = tmp_path.name
    original = {"seed": 0, "version": "v1"}
    original_reference = {"challenge": dict(original), "chromosome": "11"}
    (tmp_path / "contract.json").write_text(json.dumps(original))
    (tmp_path / "hbb_reference.json").write_text(json.dumps(original_reference))
    changed = {"seed": "1,2,3", "version": "v2"}
    payload = {
        "items": [
            {
                "id": task_id,
                "content": {
                    "contract": changed,
                    "hbb_reference": {
                        "challenge": dict(changed),
                        "chromosome": "11",
                    },
                },
            }
        ]
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        @staticmethod
        def read():
            return json.dumps(payload).encode()

    monkeypatch.setattr(
        bridge_module,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )
    with pytest.raises(ValueError, match="other than seed"):
        _fetch_task_history_contract(tmp_path)


def test_wait_for_authoritative_contract_seed_ignores_placeholder_then_returns(
    tmp_path, monkeypatch
):
    artifacts = ArtifactBundle(
        contract={"seed": 0, "version": "v1"},
        hbb_reference={},
        chromosome_11="A",
        cell_types={},
        manifest={},
    )
    responses = iter(
        [
            {"seed": 0, "version": "v1"},
            {"seed": "999,668,630", "version": "v1"},
        ]
    )
    (tmp_path / "task.json").write_text(
        json.dumps({"contract_url": "https://bucket.example/contract.json"})
    )

    monkeypatch.setattr(
        bridge_module,
        "_fetch_refreshed_contract",
        lambda _task_dir: next(responses),
    )
    monkeypatch.setattr(
        bridge_module,
        "_presigned_url_deadline",
        lambda _url: (time.monotonic() + 60.0, "test"),
    )
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    class FakeBridge:
        label = "64kib-primary"
        done = threading.Event()

        @staticmethod
        def snapshot():
            return {"label": "64kib-primary", "state": "streaming"}

    state = {}
    refreshed, seed_plan = _wait_for_authoritative_contract_seed(
        task_dir=tmp_path,
        artifacts=artifacts,
        bridges=[FakeBridge()],
        state=state,
        status_path=tmp_path / "seed_bridge_status.json",
        events_path=tmp_path / "seed_bridge_events.jsonl",
    )

    assert refreshed.contract["seed"] == "999,668,630"
    assert seed_plan.mode == "contract-authoritative"
    assert seed_plan.optimization_seeds == (999, 668, 630)
    assert json.loads((tmp_path / "refreshed_contract.json").read_text())["seed"] == (
        "999,668,630"
    )
    assert "authoritative_contract_seed_observed" in (
        tmp_path / "seed_bridge_events.jsonl"
    ).read_text()


def test_wait_prefers_authorized_validator_source_at_validation(
    tmp_path, monkeypatch
):
    artifacts = ArtifactBundle(
        contract={"seed": 0, "version": "v1"},
        hbb_reference={},
        chromosome_11="A",
        cell_types={},
        manifest={},
    )
    (tmp_path / "task.json").write_text(
        json.dumps({"contract_url": "https://bucket.example/contract.json"})
    )
    monkeypatch.setattr(
        bridge_module, "AUTHORIZED_VALIDATOR_WALLET_NAME", "validator-cold"
    )
    monkeypatch.setattr(
        bridge_module, "AUTHORIZED_VALIDATOR_WALLET_HOTKEY", "validator-hot"
    )
    monkeypatch.setattr(
        bridge_module, "AUTHORIZED_VALIDATOR_WALLET_PATH", "/owned/wallets"
    )
    monkeypatch.setattr(
        bridge_module,
        "_fetch_authorized_validator_contract",
        lambda _task_dir: {"seed": "729,862,298", "version": "v1"},
    )
    monkeypatch.setattr(
        bridge_module,
        "_fetch_refreshed_contract",
        lambda _task_dir: pytest.fail("authorized source must be preferred"),
    )
    monkeypatch.setattr(
        bridge_module,
        "_presigned_url_deadline",
        lambda _url: (time.monotonic() + 60.0, "test"),
    )

    class FakeSubtensor:
        block = 100

    class FakeBridge:
        label = "64kib-primary"
        done = threading.Event()

        @staticmethod
        def snapshot():
            return {"label": "64kib-primary", "state": "streaming"}

    state = {}
    observed, seed_plan = _wait_for_authoritative_contract_seed(
        task_dir=tmp_path,
        artifacts=artifacts,
        bridges=[FakeBridge()],
        subtensor=FakeSubtensor(),
        validation_block=100,
        state=state,
        status_path=tmp_path / "seed_bridge_status.json",
        events_path=tmp_path / "seed_bridge_events.jsonl",
    )

    assert observed.contract["seed"] == "729,862,298"
    assert seed_plan.source == "authorized-validator-current-task"
    assert state["contract_seed_source"] == "authorized-validator-current-task"
    assert state["authorized_validator_seed_source_attempts"] == 1


def test_wait_uses_verified_task_history_twice_after_signed_url_expiry(
    tmp_path, monkeypatch
):
    artifacts = ArtifactBundle(
        contract={"seed": 0, "version": "v1"},
        hbb_reference={"challenge": {"seed": 0, "version": "v1"}},
        chromosome_11="A",
        cell_types={},
        manifest={},
    )
    (tmp_path / "task.json").write_text(
        json.dumps({"contract_url": "https://bucket.example/expired-contract.json"})
    )
    refreshed = {"seed": "519,253,867", "version": "v1"}
    history_responses = iter(
        [refreshed, {"seed": 0, "version": "v1"}, refreshed, refreshed]
    )
    history_calls = []

    monkeypatch.setattr(
        bridge_module,
        "_presigned_url_deadline",
        lambda _url: (time.monotonic() - 1.0, "aws-query-v4"),
    )
    monkeypatch.setattr(
        bridge_module,
        "_fetch_refreshed_contract",
        lambda _task_dir: pytest.fail("expired signed URL must not be fetched"),
    )

    def fake_history(_task_dir):
        history_calls.append(True)
        return dict(next(history_responses))

    monkeypatch.setattr(bridge_module, "_fetch_task_history_contract", fake_history)
    monkeypatch.setattr(bridge_module.time, "sleep", lambda _seconds: None)

    class FakeBridge:
        label = "64kib-primary"
        done = threading.Event()

        @staticmethod
        def snapshot():
            return {"label": "64kib-primary", "state": "streaming"}

    state = {}
    observed_artifacts, seed_plan = _wait_for_authoritative_contract_seed(
        task_dir=tmp_path,
        artifacts=artifacts,
        bridges=[FakeBridge()],
        state=state,
        status_path=tmp_path / "seed_bridge_status.json",
        events_path=tmp_path / "seed_bridge_events.jsonl",
    )

    assert len(history_calls) == 4
    assert observed_artifacts.contract == refreshed
    assert seed_plan.optimization_seeds == (519, 253, 867)
    assert seed_plan.source == "task-history-expiry-fallback"
    assert state["contract_seed_source"] == "task-history-expiry-fallback"
    assert json.loads((tmp_path / "task_history_contract.json").read_text()) == refreshed
    events = (tmp_path / "seed_bridge_events.jsonl").read_text()
    assert "task_history_seed_fallback_started" in events
    assert '"source": "task-history-expiry-fallback"' in events


def test_submission_tail_completes_streamed_json_list():
    raw = json.dumps([{"experiment_id": "a"}], separators=(",", ":")).encode()
    streamed = b"[" + (b" " * 32) + _submission_tail(raw) + (b" " * 64)
    assert json.loads(streamed) == [{"experiment_id": "a"}]


def test_submission_tail_rejects_non_list_payload():
    with pytest.raises(ValueError, match="JSON list"):
        _submission_tail(b'{"not":"a-list"}')


def test_stream_profiles_cover_more_than_one_round():
    for _label, stream_bytes, total_bytes in bridge_module.STREAM_PROFILES:
        capacity_seconds = (
            total_bytes
            / stream_bytes
            * bridge_module.STREAM_INTERVAL_SECONDS
        )
        assert capacity_seconds >= 2.5 * 60 * 60


def test_stream_profiles_are_two_sequential_64kib_failover_paths():
    assert [profile[0] for profile in bridge_module.STREAM_PROFILES] == [
        "64kib-primary",
        "64kib-standby",
    ]
    assert {
        profile[1] for profile in bridge_module.STREAM_PROFILES
    } == {64 * 1024}


def test_standby_delay_preserves_url_opening_margin():
    assert _standby_start_delay(300.0) == 60.0
    assert _standby_start_delay(50.0) == 20.0
    assert _standby_start_delay(20.0) == 0.0


def test_round_coordinates_match_validator_seed_and_validation_windows():
    assert _round_coordinates(9_152_944) == (
        9_152_900,
        [9_153_330, 9_153_331, 9_153_332],
        9_153_336,
        9_153_350,
    )


def test_planned_stream_body_tracks_round_position_and_reserves_completion():
    early = _planned_stream_total_bytes(
        current_block=100,
        validation_block=500,
        stream_bytes=64 * 1024,
        maximum_total_bytes=576 * 1024 * 1024,
    )
    late = _planned_stream_total_bytes(
        current_block=490,
        validation_block=500,
        stream_bytes=64 * 1024,
        maximum_total_bytes=576 * 1024 * 1024,
    )

    assert early > late
    assert late >= bridge_module.PLANNED_COMPLETION_RESERVE_BYTES
    assert early % bridge_module.PADDING_CHUNK_BYTES == 0
    assert late % bridge_module.PADDING_CHUNK_BYTES == 0


def test_planned_stream_body_never_exceeds_profile_maximum():
    maximum = 80 * 1024 * 1024
    assert _planned_stream_total_bytes(
        current_block=0,
        validation_block=100_000,
        stream_bytes=64 * 1024,
        maximum_total_bytes=maximum,
    ) == maximum


def test_timing_probe_reports_required_completion_rate():
    class FakeBridge:
        label = "64kib-primary"

        @staticmethod
        def snapshot():
            return {
                "state": "streaming",
                "bytes_sent": 200,
                "total_bytes": 1_000,
            }

    probe = _timing_probe(
        current_block=440,
        validation_block=450,
        observed_seconds_per_block=6.0,
        bridges=[FakeBridge()],
    )

    assert probe["blocks_remaining_to_validation"] == 10
    assert probe["estimated_seconds_to_validation"] == 60.0
    assert probe["streams"][0]["remaining_bytes"] == 800
    assert probe["streams"][0]["required_body_bytes_per_second"] == pytest.approx(
        800 / 60
    )


def test_envelope_uses_signed_expiry(tmp_path):
    envelope = tmp_path / "request_envelope.json"
    envelope.write_text(
        json.dumps(
            {
                "presigned_url": (
                    "https://bucket.example/key?AWSAccessKeyId=test"
                    f"&Expires={time.time() + 60}&Signature=test"
                )
            }
        )
    )
    assert 55 < _envelope_seconds_remaining(envelope) <= 60


def test_envelope_fallback_ttl_is_anchored_to_file_receipt(tmp_path):
    envelope = tmp_path / "request_envelope.json"
    envelope.write_text(json.dumps({"presigned_url": "https://example.test/key"}))
    old = time.time() - 100
    os.utime(envelope, (old, old))
    assert 195 < _envelope_seconds_remaining(envelope) <= 200


def test_slow_put_sends_one_valid_fixed_length_json_body(monkeypatch):
    connections = []
    completion_sleeps = []

    class FakeResponse:
        status = 200
        reason = "OK"

        @staticmethod
        def read(_limit):
            return b""

    class FakeConnection:
        def __init__(self, host, port, timeout):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.request = None
            self.headers = []
            self.body = bytearray()
            self.send_sizes = []
            connections.append(self)

        def putrequest(self, method, target, **options):
            self.request = (method, target, options)

        def putheader(self, name, value):
            self.headers.append((name, value))

        def endheaders(self):
            pass

        def send(self, chunk):
            self.send_sizes.append(len(chunk))
            self.body.extend(chunk)

        @staticmethod
        def getresponse():
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(bridge_module.http.client, "HTTPSConnection", FakeConnection)
    monkeypatch.setattr(bridge_module.time, "sleep", completion_sleeps.append)
    submission = [{"experiment_id": "a"}]
    raw = json.dumps(submission, separators=(",", ":")).encode()
    total_bytes = 2 * bridge_module.PADDING_CHUNK_BYTES + 256
    put = SlowPut(
        "https://bucket.example/object.json?signature=secret",
        total_bytes=total_bytes,
        stream_bytes=8,
        stream_interval_seconds=0.001,
        completion_interval_seconds=0.25,
        label="test",
    )
    put.start()
    assert put.payload_ready.wait(0.02) is False
    put.finish_with(raw)
    assert put.done.wait(1)

    assert "error" not in put.result
    assert put.result["bytes_sent"] == total_bytes
    connection = connections[0]
    assert connection.request == (
        "PUT",
        "/object.json?signature=secret",
        {"skip_host": True, "skip_accept_encoding": True},
    )
    assert connection.headers.count(("Host", "bucket.example")) == 1
    assert ("Content-Length", str(total_bytes)) in connection.headers
    assert len(connection.body) == total_bytes
    assert max(connection.send_sizes) <= bridge_module.PADDING_CHUNK_BYTES
    assert json.loads(connection.body) == submission
    assert completion_sleeps
    assert set(completion_sleeps) == {0.25}
    assert put.result["completion_target_bytes_per_second"] == pytest.approx(
        bridge_module.PADDING_CHUNK_BYTES / 0.25
    )


def test_slow_put_records_actionable_remote_close_diagnostics(monkeypatch):
    class FakeConnection:
        sock = None

        def __init__(self, _host, _port, timeout):
            self.timeout = timeout
            self.send_calls = 0

        def putrequest(self, *_args, **_kwargs):
            pass

        def putheader(self, *_args):
            pass

        def endheaders(self):
            pass

        def send(self, _chunk):
            self.send_calls += 1
            if self.send_calls == 2:
                raise BrokenPipeError(32, "Broken pipe")

        def close(self):
            pass

    monkeypatch.setattr(bridge_module.http.client, "HTTPSConnection", FakeConnection)
    put = SlowPut(
        "https://bucket.example/object.json?signature=must-not-be-recorded",
        total_bytes=1024,
        stream_bytes=8,
        stream_interval_seconds=0.001,
        label="diagnostic-test",
    )
    put.start()
    assert put.done.wait(1)

    result = put.snapshot()
    assert result["state"] == "failed"
    assert result["failure_stage"] == "streaming"
    assert result["failure_category"] == "remote_connection_closed"
    assert result["error_type"] == "BrokenPipeError"
    assert result["error_errno"] == 32
    assert result["bytes_sent"] == 1
    assert result["last_successful_send_at"]
    assert "signature" not in json.dumps(result)


def test_startup_quarantines_inherited_placeholder_seed_bridge(tmp_path, monkeypatch):
    task = tmp_path / "task-a"
    task.mkdir()
    (task / "contract.json").write_text(json.dumps({"seed": 0}))
    (task / "seed_bridge_status.json").write_text(json.dumps({
        "state": "waiting_for_seeds",
        "process_pid": os.getpid() + 1000,
    }))
    monkeypatch.setattr(bridge_module, "ARTIFACT_ROOT", tmp_path)

    assert _quarantine_inherited_unknown_seed_tasks() == 1
    state = json.loads((task / "seed_bridge_status.json").read_text())
    assert state["state"] == "quarantined"
    assert state["seed_policy"]["mode"] == "robust-unknown"
    assert "bridge_quarantined_on_startup" in (
        task / "seed_bridge_events.jsonl"
    ).read_text()


@pytest.mark.parametrize(
    ("error", "cancelled", "status", "expected"),
    [
        (TimeoutError("timed out"), False, None, "socket_timeout"),
        (OSError(101, "network unreachable"), False, None, "socket_os_error"),
        (RuntimeError("slow PUT cancelled"), True, None, "cancelled_by_bridge"),
        (RuntimeError("denied"), False, 403, "s3_http_rejection"),
    ],
)
def test_failure_category_is_stable(error, cancelled, status, expected):
    assert _failure_category(error, cancelled=cancelled, status=status) == expected
