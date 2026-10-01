from pathlib import Path

import pytest

from tools.wandb_seed_probe import _read_key, _safe_timing_lines


def test_read_key_requires_private_permissions(tmp_path: Path):
    key_file = tmp_path / "wandb_api_key"
    key_file.write_text("test-key\n", encoding="utf-8")
    key_file.chmod(0o644)

    with pytest.raises(PermissionError):
        _read_key(key_file)

    key_file.chmod(0o600)
    assert _read_key(key_file) == "test-key"


def test_safe_timing_lines_redacts_credentials():
    edges = [
        {
            "node": {
                "timestamp": "2026-09-28T00:00:00Z",
                "line": "Generated seeds: 123,417,715",
            }
        },
        {
            "node": {
                "timestamp": "2026-09-28T00:00:01Z",
                "line": "Generated seeds: 123,417,715 api_key=do-not-print",
            }
        },
        {
            "node": {
                "timestamp": "2026-09-28T00:00:02Z",
                "line": "ordinary unrelated line",
            }
        },
    ]

    assert _safe_timing_lines(edges) == [
        {
            "timestamp": "2026-09-28T00:00:00Z",
            "line": "Generated seeds: 123,417,715",
        }
    ]
