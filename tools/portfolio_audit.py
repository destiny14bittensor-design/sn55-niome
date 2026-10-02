#!/usr/bin/env python3
"""Write the four-lane canary/promotion evidence report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from niome_subnet.miner.portfolio_audit import build_portfolio_report


LANE_ROOTS = {
    "tao1": ROOT / "artifacts" / "miners" / "tao1",
    "tao2": ROOT / "artifacts" / "miners" / "tao2",
    "won1": ROOT / "artifacts" / "miners" / "won1",
    "won2": ROOT / "artifacts" / "miners" / "won2",
}
EXPECTED_POLICIES = {
    "tao1": "champion-v1",
    "tao2": "champion-reservoir001-cas55-v3",
    "won1": "champion-reservoir003-cas65-v3",
    "won2": "champion-reservoir005-v3",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts" / "portfolio" / "portfolio_state.json",
    )
    args = parser.parse_args()
    report = build_portfolio_report(
        LANE_ROOTS,
        expected_policies=EXPECTED_POLICIES,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "promotable" else 2


if __name__ == "__main__":
    raise SystemExit(main())
