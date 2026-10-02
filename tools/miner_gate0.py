#!/usr/bin/env python3
"""Verify configured miner hotkeys on-chain without registering or writing."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bittensor as bt

from niome_subnet.miner.registration_gate import MinerIdentity, evaluate_registration


LANE_DEFAULTS = (
    ("tao1", "NIOME_TAO1_WALLET", "NIOME_TAO1_HOTKEY", 8091),
    ("tao2", "NIOME_TAO2_WALLET", "NIOME_TAO2_HOTKEY", 8092),
    ("won1", "NIOME_WON1_WALLET", "NIOME_WON1_HOTKEY", 8093),
    ("won2", "NIOME_WON2_WALLET", "NIOME_WON2_HOTKEY", 8094),
)


def parse_miner(raw: str) -> MinerIdentity:
    values = raw.split(",")
    if len(values) != 5:
        raise ValueError("--miner must be NAME,WALLET,HOTKEY,PORT,ARTIFACT_ROOT")
    name, wallet, hotkey, port, artifact_root = values
    return MinerIdentity(name, wallet, hotkey, int(port), artifact_root)


def configured_miners() -> list[MinerIdentity]:
    missing: list[str] = []
    identities: list[MinerIdentity] = []
    for name, wallet_key, hotkey_key, port in LANE_DEFAULTS:
        wallet = os.getenv(wallet_key)
        hotkey = os.getenv(hotkey_key)
        if not wallet:
            missing.append(wallet_key)
        if not hotkey:
            missing.append(hotkey_key)
        identities.append(
            MinerIdentity(
                name,
                wallet or "",
                hotkey or "",
                port,
                str(PROJECT_ROOT / "artifacts" / "miners" / name),
            )
        )
    if missing:
        raise ValueError(f"missing required environment variables: {', '.join(missing)}")
    return identities


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", default="finney")
    parser.add_argument("--netuid", type=int, default=55)
    parser.add_argument("--wallet-path", default=os.getenv("NIOME_WALLET_PATH"))
    parser.add_argument("--miner", action="append")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/miner_gate0.json"),
    )
    args = parser.parse_args(argv)
    if not args.wallet_path:
        parser.error("--wallet-path or NIOME_WALLET_PATH is required")
    identities = (
        [parse_miner(raw) for raw in args.miner]
        if args.miner
        else configured_miners()
    )
    subtensor = bt.Subtensor(network=args.network)
    neurons = subtensor.neurons.neurons(netuid=args.netuid)
    chain_uids = {str(neuron.hotkey): int(neuron.uid) for neuron in neurons}
    hotkey_addresses = {}
    for identity in identities:
        wallet = bt.Wallet(
            name=identity.wallet,
            hotkey=identity.hotkey,
            path=args.wallet_path,
        )
        hotkey_addresses[(identity.wallet, identity.hotkey)] = (
            wallet.hotkey.ss58_address
        )
    report = evaluate_registration(
        identities,
        hotkey_addresses,
        chain_uids,
        network=args.network,
        netuid=args.netuid,
    )
    atomic_json(args.output.resolve(), report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["all_registered"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
