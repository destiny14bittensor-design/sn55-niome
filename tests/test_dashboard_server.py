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


def test_tao2_is_the_only_rotating_top30_canary() -> None:
    lanes = {lane.lane_id: lane for lane in server.select_fleet_lanes("tao1,tao2")}

    assert lanes["tao1"].builder_policy == "champion-v1"
    assert lanes["tao1"].profile == "baseline"
    assert lanes["tao2"].builder_policy == "champion-reservoir003-cas65-v3"
    assert lanes["tao2"].expected_primary_cas_share == 0.65
    assert lanes["tao2"].profile == "exploration"
