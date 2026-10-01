#!/usr/bin/env python3
"""Align public validator shuffle order with historical public SN55 metagraphs.

Raw axon endpoints are used only in memory and are never written.  The output
contains counts needed to decide whether a symbolic Fisher–Yates/MT19937 solver
has enough correctly labelled positions to be worth building.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

import bittensor as bt

try:
    from tools.preseed_shuffle_leak_audit import extract_sequences, fetch_log_nodes
except ModuleNotFoundError:
    from preseed_shuffle_leak_audit import extract_sequences, fetch_log_nodes


def load_records(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for record in payload.get("records") or []:
            records[str(record["task_id"])] = record
    return records


def alignment_stats(
    observed: list[str], expected_endpoint_uids: dict[str, list[int]]
) -> dict[str, Any]:
    observed_counts = Counter(observed)
    expected_counts = Counter(
        {endpoint: len(uids) for endpoint, uids in expected_endpoint_uids.items()}
    )
    common = observed_counts & expected_counts
    unique_labelled = sum(
        observed_counts[endpoint] == 1 and expected_counts[endpoint] == 1
        for endpoint in common
    )
    ambiguous_positions = sum(
        common[endpoint]
        for endpoint in common
        if observed_counts[endpoint] > 1 or expected_counts[endpoint] > 1
    )
    return {
        "observed_positions": len(observed),
        "expected_queryable_uids": sum(expected_counts.values()),
        "matched_multiset_positions": sum(common.values()),
        "missing_expected_positions": sum((expected_counts - observed_counts).values()),
        "extra_observed_positions": sum((observed_counts - expected_counts).values()),
        "exact_unique_uid_positions": unique_labelled,
        "ambiguous_shared_endpoint_positions": ambiguous_positions,
        "counter_exact": observed_counts == expected_counts,
    }


def endpoint_uid_map(neurons: Iterable[Any]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for neuron in neurons:
        if float(neuron.trust) > 0 or neuron.axon is None:
            continue
        result.setdefault(str(neuron.axon), []).append(int(neuron.uid))
    return result


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def offset_summary(rounds: list[dict[str, Any]], offset: int) -> dict[str, Any]:
    selected = [item for item in rounds if int(item["block_offset"]) == offset]
    return {
        "block_offset": offset,
        "aligned_rounds": len(selected),
        "rounds_with_exact_endpoint_counter": sum(
            bool(item["counter_exact"]) for item in selected
        ),
        "matched_multiset_positions": sum(
            int(item["matched_multiset_positions"]) for item in selected
        ),
        "exact_unique_uid_positions": sum(
            int(item["exact_unique_uid_positions"]) for item in selected
        ),
        "missing_expected_positions": sum(
            int(item["missing_expected_positions"]) for item in selected
        ),
        "extra_observed_positions": sum(
            int(item["extra_observed_positions"]) for item in selected
        ),
    }


def offset_rank_key(summary: dict[str, Any]) -> tuple[int, int, int, int, int]:
    """Prefer exact counters, then maximum overlap with minimum disagreement."""
    return (
        int(summary["rounds_with_exact_endpoint_counter"]),
        int(summary["matched_multiset_positions"]),
        int(summary["exact_unique_uid_positions"]),
        -int(summary["missing_expected_positions"]),
        -abs(int(summary["block_offset"])),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--network", default="finney")
    parser.add_argument("--netuid", type=int, default=55)
    parser.add_argument("--entity", default="genomes")
    parser.add_argument("--project", default="niome")
    parser.add_argument("--run", default="non2mca3")
    parser.add_argument(
        "--offset-min",
        type=int,
        default=0,
        help="Smallest metagraph block offset relative to the recorded task block.",
    )
    parser.add_argument(
        "--offset-max",
        type=int,
        default=0,
        help="Largest metagraph block offset relative to the recorded task block.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_shuffle_metagraph_alignment.json"),
    )
    args = parser.parse_args()
    if args.offset_min > args.offset_max:
        parser.error("--offset-min must be <= --offset-max")

    records = load_records(path.resolve() for path in args.dataset)
    sequences, seeds, _fetched = extract_sequences(
        fetch_log_nodes(args.entity, args.project, args.run)
    )
    subtensor = bt.Subtensor(network=args.network)
    candidates = []
    for task_id, observed in sequences.items():
        record = records.get(task_id)
        if not record:
            continue
        block = int(((record.get("block_context") or {}).get("created") or {}).get("number") or 0)
        if block <= 0:
            continue
        for offset in range(args.offset_min, args.offset_max + 1):
            lookup_block = block + offset
            if lookup_block <= 0:
                continue
            metagraph = subtensor.subnets.metagraph(
                args.netuid, block=lookup_block, commitments=False
            )
            stats = alignment_stats(observed, endpoint_uid_map(metagraph.neurons))
            stats.update(
                {
                    "task_id": task_id,
                    "task_block": block,
                    "metagraph_block": lookup_block,
                    "block_offset": offset,
                    "published_seed_triplet_observed": task_id in seeds,
                }
            )
            candidates.append(stats)

    offset_summaries = [
        offset_summary(candidates, offset)
        for offset in range(args.offset_min, args.offset_max + 1)
    ]
    best = max(offset_summaries, key=offset_rank_key) if offset_summaries else None
    best_offset = int(best["block_offset"]) if best else 0
    rounds = [item for item in candidates if item["block_offset"] == best_offset]
    total_unique = sum(item["exact_unique_uid_positions"] for item in rounds)
    total_matched = sum(item["matched_multiset_positions"] for item in rounds)
    report = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "public-shuffle-historical-metagraph-alignment",
        "summary": {
            "aligned_rounds": len(rounds),
            "rounds_with_exact_endpoint_counter": sum(item["counter_exact"] for item in rounds),
            "matched_multiset_positions": total_matched,
            "exact_unique_uid_positions": total_unique,
            "symbolic_solver_input_ready": len(rounds) >= 16 and total_unique >= 1_800,
            "selected_block_offset": best_offset,
        },
        "offset_summaries": offset_summaries,
        "rounds": rounds,
        "next_gate": (
            "Model partial Fisher-Yates positions and NumPy bounded draws; prove same-state "
            "coupling only by predicting seed triplets excluded from the solve."
        ),
        "safety": {
            "public_read_only": True,
            "stores_endpoint_or_ip": False,
            "stores_credentials": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
