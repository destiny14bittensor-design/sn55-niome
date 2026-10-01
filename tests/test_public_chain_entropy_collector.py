from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from tools.public_chain_entropy_collector import collect_range, header_record


def info(block: int):
    return SimpleNamespace(
        number=block,
        hash="0x" + f"{block:064x}",
        timestamp=datetime(2026, 9, 29, 1, 2, 3, tzinfo=timezone.utc),
        header={
            "number": block,
            "parentHash": "0x" + f"{max(0, block - 1):064x}",
            "digest": {"logs": []},
        },
    )


def test_header_record_contains_only_public_header_fields():
    record = header_record(info(12), "2026-09-29T01:02:04+00:00")
    assert record["block"] == 12
    assert record["hash"].endswith("0c")
    assert set(record) == {"block", "hash", "header", "timestamp", "observed_at", "source"}
    assert record["header"]["number"] == 12


def test_header_record_rejects_invalid_hash():
    with pytest.raises(ValueError):
        header_record(SimpleNamespace(number=1, hash="bad"), "now")


def test_collect_range_appends_and_checkpoints(tmp_path):
    output = tmp_path / "headers.jsonl"
    state_path = tmp_path / "state.json"

    state = collect_range(
        start=7,
        end=9,
        block_info=info,
        output=output,
        state_path=state_path,
    )

    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert [row["block"] for row in rows] == [7, 8, 9]
    assert state["last_block"] == 9
    assert state["records_written"] == 3
    assert state["safety"]["submission_writes"] is False
