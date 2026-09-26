import json
import os
import time

import pytest

import tools.live_seed_bridge as bridge_module
from tools.live_seed_bridge import (
    SlowPut,
    _assert_contract_seed_only_changed,
    _choose_exact_consistency_candidate,
    _consistency_sample_from_payload,
    _envelope_seconds_remaining,
    _fetch_refreshed_contract,
    _failure_category,
    _quarantine_inherited_unknown_seed_tasks,
    _round_coordinates,
    _standby_start_delay,
    _submission_tail,
    _timing_probe,
)


def test_consistency_history_accepts_only_chain_authoritative_exact_replay():
    payload = {
        "comparable_to_official": True,
        "seed_policy": {"mode": "chain-authoritative"},
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

    payload["seed_policy"]["mode"] = "contract-authoritative"
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
    submission = [{"experiment_id": "a"}]
    raw = json.dumps(submission, separators=(",", ":")).encode()
    total_bytes = 2 * bridge_module.PADDING_CHUNK_BYTES + 256
    put = SlowPut(
        "https://bucket.example/object.json?signature=secret",
        total_bytes=total_bytes,
        stream_bytes=8,
        stream_interval_seconds=0.001,
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
