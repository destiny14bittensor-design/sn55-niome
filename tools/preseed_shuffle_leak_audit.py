#!/usr/bin/env python3
"""Measure public NumPy-shuffle leakage in validator W&B console logs.

Only ordering statistics are retained.  Endpoint strings, IP addresses, W&B
credentials, request headers, and score rows are deliberately excluded from the
artifact.  The report is a feasibility gate for a later MT19937 state solver;
it does not claim that benchmark seeds use the same NumPy generator.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.request import Request, urlopen


GRAPHQL_URL = "https://api.wandb.ai/graphql"
TASK_RE = re.compile(r"Fetched task ([0-9a-f-]{36})")
FORWARD_RE = re.compile(r"HTTP Request: POST http://([^ /]+)/forward")
QUERY_ERROR_RE = re.compile(r"Error querying miner ([0-9]+) at ([^ :]+(?::[0-9]+)?)")
SEED_RE = re.compile(r"Generated seeds:\s*([0-9, ]+)")


def fetch_log_nodes(entity: str, project: str, run: str) -> list[dict[str, Any]]:
    # The legacy resolver silently caps ``last`` at 10,000 lines and rewrites
    # cursors, which dropped the beginning of this long-lived validator run.
    # Improved pagination exposes stable line cursors. Walk backwards from the
    # newest page and prepend older pages so callers always receive chronology.
    query = """query Q($entity:String!,$project:String!,$run:String!,$before:String){project(name:$project,entityName:$entity){run(name:$run){logLines(last:10000,before:$before,useImprovedPagination:true){edges{node{line timestamp}} pageInfo{hasPreviousPage startCursor}}}}}"""
    before: str | None = None
    pages: list[list[dict[str, Any]]] = []
    while True:
        request = Request(
            GRAPHQL_URL,
            data=json.dumps(
                {
                    "query": query,
                    "variables": {
                        "entity": entity,
                        "project": project,
                        "run": run,
                        "before": before,
                    },
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "niome-shuffle-audit/2",
            },
        )
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
        connection = payload["data"]["project"]["run"]["logLines"]
        pages.append([edge.get("node") or {} for edge in connection["edges"]])
        page_info = connection["pageInfo"]
        if not page_info.get("hasPreviousPage"):
            break
        next_before = page_info.get("startCursor")
        if not next_before or next_before == before:
            raise RuntimeError("W&B log pagination did not advance")
        before = str(next_before)
    return [node for page in reversed(pages) for node in page]


def permutation_information_bits(tokens: list[str]) -> float:
    """Log2 of distinct permutations of a multiset of observable tokens."""
    counts = Counter(tokens)
    return (
        math.lgamma(len(tokens) + 1)
        - sum(math.lgamma(count + 1) for count in counts.values())
    ) / math.log(2)


def summarize_nodes(
    nodes: list[dict[str, Any]], minimum_observations: int = 1
) -> dict[str, Any]:
    sequences, seeds, fetched_at = extract_sequences(nodes)

    rounds = []
    incomplete_rounds = []
    for task_id, tokens in sequences.items():
        counts = Counter(tokens)
        row = {
                "task_id": task_id,
                "fetched_at": fetched_at.get(task_id),
                "forward_observations": len(tokens),
                "distinct_endpoint_tokens": len(counts),
                "duplicate_token_positions": len(tokens) - len(counts),
                "permutation_information_bits_upper_bound": round(
                    permutation_information_bits(tokens), 2
                ),
                "published_seed_triplet_observed": task_id in seeds,
            }
        if len(tokens) < max(1, int(minimum_observations)):
            incomplete_rounds.append(row)
        else:
            rounds.append(row)
    rounds.sort(key=lambda item: str(item.get("fetched_at") or ""))
    total_bits = sum(item["permutation_information_bits_upper_bound"] for item in rounds)
    return {
        "rounds": rounds,
        "round_count": len(rounds),
        "incomplete_round_count": len(incomplete_rounds),
        "incomplete_rounds": incomplete_rounds,
        "rounds_with_published_seed": sum(
            item["published_seed_triplet_observed"] for item in rounds
        ),
        "total_permutation_information_bits_upper_bound": round(total_bits, 2),
        "mt19937_state_bits": 19_937,
        "information_threshold_crossed": total_bits >= 19_937,
    }


def extract_sequences(
    nodes: list[dict[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, list[int]], dict[str, str]]:
    """Extract ephemeral endpoint order; callers must not persist raw tokens."""
    current = ""
    sequences: dict[str, list[str]] = defaultdict(list)
    seeds: dict[str, list[int]] = {}
    fetched_at: dict[str, str] = {}
    for node in nodes:
        line = str(node.get("line") or "")
        task_match = TASK_RE.search(line)
        if task_match:
            current = task_match.group(1)
            fetched_at[current] = str(node.get("timestamp") or "")
            continue
        forward_match = FORWARD_RE.search(line)
        if current and forward_match:
            sequences[current].append(forward_match.group(1))
            continue
        seed_match = SEED_RE.search(line)
        if current and seed_match:
            seeds[current] = [int(value) for value in seed_match.group(1).split(",")]

    return dict(sequences), seeds, fetched_at


def extract_order_events(nodes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Return chronological query events, retaining endpoints only in memory.

    Connection failures name the exact UID in the validator log.  HTTP status
    failures may first emit an httpx request line and then the exact-UID error;
    in that case the endpoint event is replaced instead of double-counted.
    Callers must sanitize endpoint events before persistence.
    """
    current = ""
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        line = str(node.get("line") or "")
        task_match = TASK_RE.search(line)
        if task_match:
            current = task_match.group(1)
            continue
        forward_match = FORWARD_RE.search(line)
        if current and forward_match:
            events[current].append(
                {
                    "kind": "endpoint",
                    "value": forward_match.group(1),
                    "timestamp": str(node.get("timestamp") or ""),
                }
            )
            continue
        error_match = QUERY_ERROR_RE.search(line)
        if current and error_match:
            uid = int(error_match.group(1))
            endpoint = error_match.group(2)
            if (
                events[current]
                and events[current][-1]["kind"] == "endpoint"
                and events[current][-1]["value"] == endpoint
            ):
                events[current][-1] = {
                    "kind": "uid",
                    "value": uid,
                    "endpoint": endpoint,
                    "timestamp": str(node.get("timestamp") or ""),
                    "source": "validator-error",
                }
            else:
                events[current].append(
                    {
                        "kind": "uid",
                        "value": uid,
                        "endpoint": endpoint,
                        "timestamp": str(node.get("timestamp") or ""),
                        "source": "validator-error",
                    }
                )
    return dict(events)


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity", default="genomes")
    parser.add_argument("--project", default="niome")
    parser.add_argument("--run", default="non2mca3")
    parser.add_argument(
        "--minimum-observations",
        type=int,
        default=200,
        help="Exclude a still-running sequential broadcast from leakage totals.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_shuffle_leak_audit.json"),
    )
    args = parser.parse_args()

    summary = summarize_nodes(
        fetch_log_nodes(args.entity, args.project, args.run),
        minimum_observations=args.minimum_observations,
    )
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "public-validator-shuffle-leak-feasibility",
        "source": {"entity": args.entity, "project": args.project, "run": args.run},
        "summary": summary,
        "gates": {
            "validator_source_uses_numpy_global_shuffle": True,
            "information_upper_bound_exceeds_mt19937_state": summary[
                "information_threshold_crossed"
            ],
            "seed_generator_uses_same_numpy_state": "unknown",
            "historical_uid_alignment_complete": False,
            "state_recovery_verified_on_future_round": False,
        },
        "interpretation": (
            "The multiset-permutation count is an upper bound, not directly usable raw MT output. "
            "Duplicate endpoints, missing historical UID labels, bounded-integer rejection draws, "
            "and the still-unproven coupling between shuffle and seed generation must be modeled."
        ),
        "safety": {
            "public_read_only": True,
            "stores_endpoint_or_ip": False,
            "stores_credentials": False,
            "stores_score_rows": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "round_count": summary["round_count"],
                "rounds_with_published_seed": summary["rounds_with_published_seed"],
                "permutation_bits_upper_bound": summary[
                    "total_permutation_information_bits_upper_bound"
                ],
                "threshold_crossed": summary["information_threshold_crossed"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
