#!/usr/bin/env python3
"""Write an auditable official-score gap report for a target rank."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from niome_subnet.miner.rank_target import analyze_rank_target


SCORES_URL = "https://niome-api.genomes.io/api/v3/miners/scores"
DEFAULT_HOTKEYS = {
    "won1": "5CPsx7spR4FCr786VBTqxuNGaoWnMfe91fcQKEYig3eVbTbi",
    "won2": "5E6ttv44E9Ko8NAsYerT4atZummaYxYXGzkTNHNUgXmXEvb4",
}


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--target-rank", type=int, default=30)
    parser.add_argument("--safety-margin", type=float, default=0.03)
    parser.add_argument("--score-url", default=SCORES_URL)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts" / "portfolio" / "rank-target-latest.json",
    )
    args = parser.parse_args()
    response = httpx.get(
        args.score_url,
        params={"task_id": args.task_id},
        timeout=20.0,
    )
    response.raise_for_status()
    payload = response.json()
    items = payload.get("items") if isinstance(payload, dict) else None
    report = analyze_rank_target(
        [item for item in (items or []) if isinstance(item, dict)],
        DEFAULT_HOTKEYS,
        target_rank=args.target_rank,
        safety_margin=args.safety_margin,
    )
    report.update(
        {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "task_id": args.task_id,
            "method": "official cutoff with one-factor consistency counterfactual",
        }
    )
    atomic_write(args.output.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
