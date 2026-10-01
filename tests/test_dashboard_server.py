from __future__ import annotations

import json

from niome_subnet.dashboard import server


def test_seed_research_state_falls_back_when_file_is_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "SEED_RESEARCH_STATE", tmp_path / "missing.json")

    state = server.load_seed_research_state()

    assert state["phase"] == "waiting_for_automation"
    assert state["safety"]["submission_writes"] is False


def test_seed_research_state_reads_worker_snapshot(tmp_path, monkeypatch) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"schema_version": 1, "phase": "generator_search"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "SEED_RESEARCH_STATE", path)

    assert server.load_seed_research_state()["phase"] == "generator_search"
