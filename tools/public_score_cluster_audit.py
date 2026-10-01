#!/usr/bin/env python3
"""Find repeated identical NIOME score fingerprints across public hotkeys.

The audit reads public task/score APIs and optional public chain ownership.  It
does not infer common ownership merely from an identical score; the output is
evidence for shared submissions/strategy, not proof of coordination or seed
knowledge.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import itertools
import json
from pathlib import Path
import statistics
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TASKS_URL = "https://niome-api.genomes.io/api/v3/tasks"
SCORES_URL = "https://niome-api.genomes.io/api/v3/miners/scores"
USER_AGENT = "niome-public-score-cluster-audit/1"


def read_json(url: str) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def fetch_recent_completed_tasks(limit: int) -> list[dict[str, Any]]:
    payload = read_json(f"{TASKS_URL}?{urlencode({'page': 1, 'per_page': 100})}")
    result = []
    seen = set()
    for task in payload.get("items") or []:
        task_id = str(task.get("id") or "")
        seed = (((task.get("content") or {}).get("contract") or {}).get("seed"))
        if not task_id or task_id in seen or seed in (None, "", 0, "0"):
            continue
        result.append({"task_id": task_id, "created_at": task.get("created_at")})
        seen.add(task_id)
        if len(result) >= limit:
            break
    return result


def fetch_task_scores(task_id: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    for page in range(1, 10):
        query = urlencode({"task_id": task_id, "page": page, "per_page": 100})
        batch = read_json(f"{SCORES_URL}?{query}").get("items") or []
        rows.extend(row for row in batch if str(row.get("task_id") or "") == task_id)
        if len(batch) < 100:
            break

    by_id = {str(row.get("id")): row for row in rows if row.get("id")}
    by_hotkey: dict[str, dict[str, Any]] = {}
    for row in by_id.values():
        hotkey = str(row.get("miner_hotkey") or "")
        if not hotkey:
            continue
        previous = by_hotkey.get(hotkey)
        if previous is None or str(row.get("created_at") or "") > str(
            previous.get("created_at") or ""
        ):
            by_hotkey[hotkey] = row
    return list(by_hotkey.values()), {
        "raw_rows": len(rows),
        "unique_score_ids": len(by_id),
        "unique_hotkeys": len(by_hotkey),
        "duplicate_hotkey_rows_removed": len(by_id) - len(by_hotkey),
    }


def score_fingerprint(row: dict[str, Any]) -> tuple[Any, ...]:
    breakdown = row.get("breakdown") or {}
    return (
        round(float(row.get("final_score") or 0), 12),
        round(float(row.get("weight") or 0), 12),
        round(float(breakdown.get("consistency_factor") or 0), 12),
        round(float(breakdown.get("consistency_score") or 0), 12),
        round(float(breakdown.get("distribution_fidelity_factor") or 0), 12),
        round(float(breakdown.get("distribution_fidelity_score") or 0), 12),
        round(float(breakdown.get("total_weighted_score") or 0), 9),
        int(breakdown.get("n_valid_experiments") or 0),
    )


def build_report(
    task_rows: list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]],
    owner_lookup: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    pair_tasks: dict[tuple[str, str], list[str]] = defaultdict(list)
    observations: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    summaries = []
    all_hotkeys = set()
    positive_hotkeys = set()
    for task, rows, diagnostics in task_rows:
        groups: dict[tuple[Any, ...], list[str]] = defaultdict(list)
        ranked_rows = sorted(
            rows, key=lambda row: float(row.get("final_score") or 0), reverse=True
        )
        for rank, row in enumerate(ranked_rows, 1):
            hotkey = str(row.get("miner_hotkey") or "")
            all_hotkeys.add(hotkey)
            if float(row.get("final_score") or 0) <= 0:
                continue
            positive_hotkeys.add(hotkey)
            groups[score_fingerprint(row)].append(hotkey)
            observations[task["task_id"]][hotkey] = {
                "rank": rank,
                "consistency": float(
                    (row.get("breakdown") or {}).get("consistency_factor") or 0
                ),
            }
        identical_groups = 0
        for members in groups.values():
            members = sorted(set(members))
            if len(members) < 2:
                continue
            identical_groups += 1
            for pair in itertools.combinations(members, 2):
                pair_tasks[pair].append(task["task_id"])
        summaries.append(
            {
                **task,
                **diagnostics,
                "first_score_at": min(
                    (
                        str(row.get("created_at"))
                        for row in rows
                        if row.get("created_at")
                    ),
                    default=None,
                ),
                "positive_score_rows": sum(
                    float(row.get("final_score") or 0) > 0 for row in rows
                ),
                "identical_fingerprint_groups": identical_groups,
            }
        )

    for summary in summaries:
        try:
            created = datetime.fromisoformat(
                str(summary["created_at"]).replace("Z", "+00:00")
            )
            scored = datetime.fromisoformat(
                str(summary["first_score_at"]).replace("Z", "+00:00")
            )
            delay = (scored - created).total_seconds()
        except (TypeError, ValueError):
            delay = None
        summary["score_delay_seconds"] = delay
        summary["estimated_score_phase_blocks_at_12s"] = (
            round(delay / 12, 3) if delay is not None else None
        )

    repeated = []
    relevant_hotkeys = {
        hotkey
        for pair, tasks in pair_tasks.items()
        if len(tasks) >= 2
        for hotkey in pair
    }
    owners = {
        hotkey: owner_lookup(hotkey) if owner_lookup else None
        for hotkey in sorted(relevant_hotkeys)
    }
    for pair, tasks in sorted(
        pair_tasks.items(), key=lambda item: (-len(item[1]), item[0])
    ):
        if len(tasks) < 2:
            continue
        first_owner, second_owner = owners[pair[0]], owners[pair[1]]
        pair_observations = [
            observations[task_id][pair[0]]
            for task_id in tasks
            if pair[0] in observations[task_id]
        ]
        consistency_values = [item["consistency"] for item in pair_observations]
        ranks = [item["rank"] for item in pair_observations]
        repeated.append(
            {
                "hotkeys": list(pair),
                "round_count": len(tasks),
                "task_ids": tasks,
                "coldkeys": [first_owner, second_owner],
                "same_coldkey": (
                    first_owner == second_owner
                    if first_owner is not None and second_owner is not None
                    else None
                ),
                "consistency": {
                    "minimum": min(consistency_values),
                    "median": statistics.median(consistency_values),
                    "maximum": max(consistency_values),
                    "exact_one_rounds": sum(
                        value == 1.0 for value in consistency_values
                    ),
                },
                "ranking": {
                    "best": min(ranks),
                    "top_five_rounds": sum(rank <= 5 for rank in ranks),
                },
            }
        )

    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "safety": {
            "public_only": True,
            "contains_credentials": False,
            "coordination_is_not_inferred": True,
        },
        "counts": {
            "tasks": len(task_rows),
            "unique_hotkeys": len(all_hotkeys),
            "unique_positive_score_hotkeys": len(positive_hotkeys),
            "repeated_identical_pairs": len(repeated),
        },
        "task_summaries": summaries,
        "repeated_identical_pairs": repeated,
    }


def chain_owner_lookup(netuid: int) -> Callable[[str], str | None]:
    import bittensor as bt

    subtensor = bt.Subtensor(network="finney")
    active = {neuron.hotkey: neuron.coldkey for neuron in subtensor.neurons.all(netuid)}

    def lookup(hotkey: str) -> str | None:
        return active.get(hotkey) or subtensor.neurons.hotkey_owner(hotkey)

    return lookup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=20)
    parser.add_argument("--netuid", type=int, default=55)
    parser.add_argument("--resolve-coldkeys", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    tasks = fetch_recent_completed_tasks(max(1, min(args.tasks, 100)))
    task_rows = []
    for task in tasks:
        rows, diagnostics = fetch_task_scores(task["task_id"])
        task_rows.append((task, rows, diagnostics))
    lookup = chain_owner_lookup(args.netuid) if args.resolve_coldkeys else None
    report = build_report(task_rows, lookup)
    encoded = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(json.dumps({"output": str(args.output.resolve()), **report["counts"]}))
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
