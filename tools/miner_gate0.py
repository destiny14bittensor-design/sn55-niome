#!/usr/bin/env python3
"""Verify that all four hotkeys exist on SN55 before rollout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bittensor as bt

from niome_subnet.miner.registration_gate import audit_registration


TARGETS = {
    "tao1": "5GbhpWKt2SYHaZMHNy2WAsm5pzkGL5sRa7DFnYu9zJY3qYGC",
    "tao2": "5Ehx52VbhGyvmZVcvaRF2dG8JVJUMHHreRBLRsGMkJ695zih",
    "won1": "5CPsx7spR4FCr786VBTqxuNGaoWnMfe91fcQKEYig3eVbTbi",
    "won2": "5E6ttv44E9Ko8NAsYerT4atZummaYxYXGzkTNHNUgXmXEvb4",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--network", default="finney")
    parser.add_argument("--netuid", type=int, default=55)
    args = parser.parse_args()
    subtensor = bt.Subtensor(network=args.network)
    neurons = subtensor.neurons.neurons(netuid=args.netuid)
    report = audit_registration(neurons, TARGETS)
    report.update({"network": args.network, "netuid": args.netuid})
    print(json.dumps(report, indent=2))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
