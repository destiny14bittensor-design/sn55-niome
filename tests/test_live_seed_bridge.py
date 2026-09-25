import json
import os
import time

import pytest

import tools.live_seed_bridge as bridge_module
from tools.live_seed_bridge import (
    SlowPut,
    _envelope_seconds_remaining,
    _failure_category,
    _submission_tail,
)


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
