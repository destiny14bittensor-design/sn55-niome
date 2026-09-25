from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from niome_subnet.dashboard.collector import collect_pm2


def test_collect_pm2_maps_isolated_processes_to_dashboard_roles(
    monkeypatch,
    tmp_path: Path,
):
    miner_log = tmp_path / "miner.log"
    bridge_log = tmp_path / "bridge.log"
    miner_log.write_text(
        "guide_variants=72 primary_cas_share=0.60 artifact_root=/isolated/dollar2\n"
    )
    bridge_log.write_text(
        "builder_guide_variants=72 primary_cas_share=0.60\n"
    )
    payload = [
        {
            "name": "niome-dollar2",
            "pid": 201,
            "pm2_env": {
                "status": "online",
                "restart_time": 0,
                "pm_uptime": 1_790_000_000_000,
                "pm_out_log_path": str(miner_log),
            },
            "monit": {"cpu": 1.0, "memory": 123},
        },
        {
            "name": "niome-seed-bridge-dollar2",
            "pid": 202,
            "pm2_env": {
                "status": "online",
                "restart_time": 0,
                "pm_uptime": 1_790_000_000_000,
                "pm_out_log_path": str(bridge_log),
            },
            "monit": {"cpu": 0.0, "memory": 456},
        },
    ]

    monkeypatch.setattr(
        "niome_subnet.dashboard.collector.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(payload)),
    )

    processes, loaded = collect_pm2(
        miner_process_name="niome-dollar2",
        bridge_process_name="niome-seed-bridge-dollar2",
    )

    assert processes["niome-dollar1"]["name"] == "niome-dollar2"
    assert processes["niome-seed-bridge"]["name"] == (
        "niome-seed-bridge-dollar2"
    )
    assert loaded == {
        "niome-dollar1": {"guide_variants": 72, "primary_cas_share": 0.60},
        "niome-seed-bridge": {
            "guide_variants": 72,
            "primary_cas_share": 0.60,
        },
    }
