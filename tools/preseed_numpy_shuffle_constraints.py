#!/usr/bin/env python3
"""Build a sanitized constraint corpus for NumPy MT19937 shuffle recovery.

The validator's public W&B order is converted to an ordered subsequence of
historical SN55 UIDs whose axon endpoint was unique in both the metagraph and
the log.  Endpoint/IP strings never leave memory.  Seed labels are included
only for the fixed Discovery partition; Holdout and prospective labels remain
sealed.
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
    from tools.preseed_shuffle_leak_audit import (
        extract_order_events,
        extract_sequences,
        fetch_log_nodes,
    )
    from tools.preseed_shuffle_metagraph_align import endpoint_uid_map, load_records
except ModuleNotFoundError:
    from preseed_shuffle_leak_audit import (
        extract_order_events,
        extract_sequences,
        fetch_log_nodes,
    )
    from preseed_shuffle_metagraph_align import endpoint_uid_map, load_records


def build_round_constraint(
    task_id: str,
    block: int,
    observed_endpoints: list[str],
    neurons: Iterable[Any],
    discovery_seed: list[int] | None,
    observed_order: list[dict[str, Any]] | None = None,
    *,
    endpoint_uids_override: dict[str, list[int]] | None = None,
) -> dict[str, Any]:
    neurons = list(neurons)
    initial_uids = [int(neuron.uid) for neuron in neurons if float(neuron.trust) <= 0]
    endpoint_uids = (
        {
            str(endpoint): sorted(int(uid) for uid in uids)
            for endpoint, uids in endpoint_uids_override.items()
        }
        if endpoint_uids_override is not None
        else endpoint_uid_map(neurons)
    )
    if observed_order is None:
        observed_order = [
            {"kind": "endpoint", "value": endpoint}
            for endpoint in observed_endpoints
        ]
    ordered_endpoints = [
        str(event["value"])
        for event in observed_order
        if event.get("kind") == "endpoint"
    ]
    observed_counts = Counter(ordered_endpoints)
    # Preserve repeated/shared endpoint information without persisting the
    # endpoint itself.  Every domain is a public UID set and every row is an
    # ordered observation from the final shuffled array.  When the historical
    # metagraph has fewer members for a token than the log observed, the token
    # is excluded: endpoint churn means it cannot be mapped soundly.
    domain_ids = {
        endpoint: f"g{index}"
        for index, endpoint in enumerate(
            sorted(
                (
                    endpoint
                    for endpoint, uids in endpoint_uids.items()
                    if uids and observed_counts[endpoint] <= len(uids)
                ),
                key=lambda endpoint: tuple(sorted(endpoint_uids[endpoint])),
            )
        )
    }
    uid_domains = {
        domain_ids[endpoint]: sorted(int(uid) for uid in endpoint_uids[endpoint])
        for endpoint in domain_ids
    }
    uid_to_domain = {
        int(uid): domain_ids[endpoint]
        for endpoint, uids in endpoint_uids.items()
        if endpoint in domain_ids
        for uid in uids
    }
    initial_domain_sequence = [
        uid_to_domain.get(uid, f"m{uid}") for uid in initial_uids
    ]
    ordered_uid_domains: list[str] = []
    ordered_group_domains: list[str] = []
    exact_error_uids: list[int] = []
    exact_owned_uids: list[int] = []
    for event in observed_order:
        if event.get("kind") == "uid":
            uid = int(event["value"])
            if uid not in initial_uids:
                continue
            token = f"u{uid}"
            uid_domains[token] = [uid]
            ordered_uid_domains.append(token)
            if event.get("source") == "owned-request-envelope":
                exact_owned_uids.append(uid)
            else:
                exact_error_uids.append(uid)
            endpoint = str(event.get("endpoint") or "")
            ordered_group_domains.append(
                domain_ids.get(endpoint, uid_to_domain.get(uid, f"m{uid}"))
            )
        else:
            endpoint = str(event.get("value") or "")
            if endpoint in domain_ids:
                ordered_uid_domains.append(domain_ids[endpoint])
                ordered_group_domains.append(domain_ids[endpoint])
    exact_endpoint_to_uid = {
        endpoint: uids[0]
        for endpoint, uids in endpoint_uids.items()
        if len(uids) == 1 and observed_counts[endpoint] == 1
    }
    unique_uid_subsequence = [
        exact_endpoint_to_uid[endpoint]
        for endpoint in observed_endpoints
        if endpoint in exact_endpoint_to_uid
    ]
    value = {
        "task_id": task_id,
        "block": block,
        "initial_uids": initial_uids,
        "unique_uid_subsequence": unique_uid_subsequence,
        "exact_error_uid_subsequence": exact_error_uids,
        "exact_owned_uid_subsequence": exact_owned_uids,
        "uid_domains": uid_domains,
        "ordered_uid_domains": ordered_uid_domains,
        "initial_domain_sequence": initial_domain_sequence,
        "ordered_group_domains": ordered_group_domains,
        "shuffle_size": len(initial_uids),
        "observed_query_count": len(observed_order),
        "exact_error_uid_constraints": len(exact_error_uids),
        "exact_owned_uid_constraints": len(exact_owned_uids),
        "domain_order_constraints": len(ordered_uid_domains),
        "group_order_constraints": len(ordered_group_domains),
        "group_sequence_missing_positions": len(initial_domain_sequence)
        - len(ordered_group_domains),
        "ambiguous_domain_positions": sum(
            len(uid_domains[domain_id]) > 1 for domain_id in ordered_uid_domains
        ),
        "unique_uid_constraints": len(unique_uid_subsequence),
        "seed_label_partition": "discovery" if discovery_seed is not None else "sealed",
    }
    if discovery_seed is not None:
        value["discovery_seed_label"] = [int(seed) for seed in discovery_seed]
    return value


def build_event_index_constraint(
    task_id: str,
    block: int,
    neurons: Iterable[Any],
    discovery_seed: list[int] | None,
    observed_order: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a sound order corpus without historical endpoint attribution.

    A validator broadcast spans many blocks and ``self.metagraph`` can change
    while the async request loop is running.  Mapping every later HTTP endpoint
    through the task-start metagraph can therefore assign a request to the
    wrong UID.  This representation preserves each query event's ordinal
    position but labels it only when the validator error log or one of our own
    request envelopes proves the UID.  All other positions share the anonymous
    ``?`` token.  This keeps the exact insertion bound from non-queryable UIDs
    while making no stale endpoint claim.
    """
    neurons = list(neurons)
    initial_uids = [int(neuron.uid) for neuron in neurons if float(neuron.trust) <= 0]
    initial_uid_set = set(initial_uids)
    exact_error_uids: list[int] = []
    exact_owned_uids: list[int] = []
    exact_uid_order: list[int] = []
    observed_tokens: list[str] = []
    exact_seen: set[int] = set()
    for event in observed_order:
        if event.get("kind") != "uid" or int(event.get("value")) not in initial_uid_set:
            observed_tokens.append("?")
            continue
        uid = int(event["value"])
        if uid in exact_seen:
            raise ValueError(
                f"task {task_id} has duplicate exact UID observation {uid}"
            )
        exact_seen.add(uid)
        exact_uid_order.append(uid)
        token = f"u{uid}"
        observed_tokens.append(token)
        if event.get("source") == "owned-request-envelope":
            exact_owned_uids.append(uid)
        else:
            exact_error_uids.append(uid)

    if len(observed_tokens) > len(initial_uids):
        raise ValueError(
            f"task {task_id} has more query events than shuffled UIDs"
        )
    initial_tokens = [f"u{uid}" if uid in exact_seen else "?" for uid in initial_uids]
    uid_domains = {f"u{uid}": [uid] for uid in sorted(exact_seen)}
    uid_domains["?"] = [uid for uid in initial_uids if uid not in exact_seen]
    value = {
        "task_id": task_id,
        "block": block,
        "identity_mode": "event-index-exact-only",
        "initial_uids": initial_uids,
        "unique_uid_subsequence": exact_uid_order,
        "exact_error_uid_subsequence": exact_error_uids,
        "exact_owned_uid_subsequence": exact_owned_uids,
        "uid_domains": uid_domains,
        "ordered_uid_domains": observed_tokens,
        "initial_domain_sequence": initial_tokens,
        "ordered_group_domains": observed_tokens,
        "shuffle_size": len(initial_uids),
        "observed_query_count": len(observed_tokens),
        "exact_error_uid_constraints": len(exact_error_uids),
        "exact_owned_uid_constraints": len(exact_owned_uids),
        "domain_order_constraints": len(observed_tokens),
        "group_order_constraints": len(observed_tokens),
        "group_sequence_missing_positions": len(initial_uids) - len(observed_tokens),
        "ambiguous_domain_positions": observed_tokens.count("?"),
        "unique_uid_constraints": len(exact_seen),
        "seed_label_partition": "discovery" if discovery_seed is not None else "sealed",
    }
    if discovery_seed is not None:
        value["discovery_seed_label"] = [int(seed) for seed in discovery_seed]
    return value


def stable_endpoint_uid_map(
    snapshots: Iterable[Iterable[Any]],
) -> dict[str, list[int]]:
    """Keep only endpoint domains identical in every public snapshot."""
    maps = [endpoint_uid_map(neurons) for neurons in snapshots]
    if not maps:
        return {}
    common = set(maps[0])
    for mapping in maps[1:]:
        common.intersection_update(mapping)
    return {
        endpoint: sorted(int(uid) for uid in maps[0][endpoint])
        for endpoint in common
        if all(mapping[endpoint] == maps[0][endpoint] for mapping in maps[1:])
    }


def metagraph_window_blocks(
    task_block: int,
    fetched_at: str,
    events: list[dict[str, Any]],
    *,
    snapshots: int = 5,
    finality_lag: int = 4,
    block_seconds: float = 12.0,
) -> list[int]:
    """Estimate evenly spaced public snapshots covering one broadcast."""
    start = max(1, int(task_block) - max(0, int(finality_lag)))
    timestamps = [str(event.get("timestamp") or "") for event in events]
    timestamps = [value for value in timestamps if value]
    if not fetched_at or not timestamps:
        return [start]
    duration = max(
        0.0,
        (_parse_timestamp(max(timestamps)) - _parse_timestamp(fetched_at)).total_seconds(),
    )
    stop = max(start, int(task_block) + int(duration // max(0.1, block_seconds)) - max(0, int(finality_lag)))
    count = max(2, int(snapshots))
    return sorted(
        {
            start + round(index * (stop - start) / (count - 1))
            for index in range(count)
        }
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def apply_owned_capture_observations(
    events: list[dict[str, Any]],
    captures: list[tuple[str, int, str]],
    *,
    expected_endpoints: dict[int, str] | None = None,
    maximum_delay_seconds: float = 30.0,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Replace safely matched endpoint events with an owned miner's exact UID.

    A capture timestamp alone is insufficient: miner and W&B clocks can be
    offset, and selecting the first later event produced false exact UIDs in
    the historical corpus.  Require both the public metagraph endpoint for the
    owned UID and a small time window.  Endpoint strings remain ephemeral and
    are never added to the persisted report.
    """
    enriched = [dict(event) for event in events]
    used: set[int] = set()
    matched = 0
    rejected = 0
    endpoints = expected_endpoints or {}
    for _lane, uid, captured_at in sorted(
        captures, key=lambda item: _parse_timestamp(item[2])
    ):
        capture_time = _parse_timestamp(captured_at)
        expected_endpoint = str(endpoints.get(int(uid)) or "")
        if not expected_endpoint:
            rejected += 1
            continue
        candidates: list[tuple[float, int]] = []
        for index, event in enumerate(enriched):
            if index in used or event.get("kind") not in {"endpoint", "uid"}:
                continue
            event_endpoint = (
                str(event.get("value") or "")
                if event.get("kind") == "endpoint"
                else str(event.get("endpoint") or "")
            )
            if event_endpoint != expected_endpoint:
                continue
            timestamp = str(event.get("timestamp") or "")
            if not timestamp:
                continue
            delay = (_parse_timestamp(timestamp) - capture_time).total_seconds()
            if 0 <= delay <= float(maximum_delay_seconds):
                candidates.append((delay, index))
        if not candidates:
            rejected += 1
            continue
        _delay, index = min(candidates)
        event = enriched[index]
        if event.get("kind") == "uid" and int(event.get("value")) != int(uid):
            rejected += 1
            continue
        endpoint = (
            str(event.get("value") or "")
            if event.get("kind") == "endpoint"
            else str(event.get("endpoint") or "")
        )
        enriched[index] = {
            "kind": "uid",
            "value": int(uid),
            "endpoint": endpoint,
            "timestamp": str(event.get("timestamp") or ""),
            "source": "owned-request-envelope",
        }
        used.add(index)
        matched += 1
    return enriched, {"matched": matched, "rejected": rejected}


def load_owned_captures(
    artifact_root: Path, task_id: str, lane_uids: dict[str, int]
) -> list[tuple[str, int, str]]:
    captures: list[tuple[str, int, str]] = []
    for lane, uid in sorted(lane_uids.items()):
        path = artifact_root / lane / task_id / "request_envelope.json"
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        captured_at = str(payload.get("captured_at") or "")
        task = payload.get("task") or {}
        if not isinstance(task, dict):
            task = {}
        envelope_task_id = str(task.get("id") or task.get("task_id") or "")
        if not captured_at or (envelope_task_id and envelope_task_id != task_id):
            continue
        captures.append((lane, int(uid), captured_at))
    return captures


def is_complete_observation_round(
    observed_events: list[dict[str, Any]], minimum_observations: int = 200
) -> bool:
    """Fail closed while the sequential broadcast is still in progress."""
    return len(observed_events) >= max(1, int(minimum_observations))


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
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--network", default="finney")
    parser.add_argument("--netuid", type=int, default=55)
    parser.add_argument("--entity", default="genomes")
    parser.add_argument("--project", default="niome")
    parser.add_argument("--run", default="non2mca3")
    parser.add_argument(
        "--owned-artifact-root",
        type=Path,
        help="Optional local miner artifact root containing lane/task/request_envelope.json.",
    )
    parser.add_argument(
        "--owned-lane-hotkey",
        action="append",
        default=[],
        metavar="LANE=SS58",
        help="Map an owned artifact lane to its public hotkey; repeatable.",
    )
    parser.add_argument("--owned-match-max-delay", type=float, default=30.0)
    parser.add_argument(
        "--identity-mode",
        choices=("endpoint-snapshot", "endpoint-window-stable", "event-index"),
        default="endpoint-snapshot",
        help=(
            "Use historical endpoint attribution or retain only exact UID events at "
            "their raw query index."
        ),
    )
    parser.add_argument("--window-snapshots", type=int, default=5)
    parser.add_argument("--metagraph-finality-lag", type=int, default=4)
    parser.add_argument("--block-seconds", type=float, default=12.0)
    parser.add_argument(
        "--minimum-observations",
        type=int,
        default=200,
        help="Exclude an in-progress broadcast from solver constraints.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_numpy_shuffle_constraints.json"),
    )
    args = parser.parse_args()

    owned_hotkeys: dict[str, str] = {}
    for item in args.owned_lane_hotkey:
        lane, separator, hotkey = str(item).partition("=")
        if not separator or not lane.strip() or not hotkey.strip():
            parser.error("--owned-lane-hotkey must be LANE=SS58")
        owned_hotkeys[lane.strip()] = hotkey.strip()

    discovery_payload = json.loads(args.discovery.read_text(encoding="utf-8"))
    discovery_labels = {
        str(record["task_id"]): list(record["seeds"])
        for record in discovery_payload.get("records") or []
    }
    records = load_records(path.resolve() for path in args.dataset)
    nodes = fetch_log_nodes(args.entity, args.project, args.run)
    sequences, _published_seeds, fetched_at = extract_sequences(nodes)
    order_events = extract_order_events(nodes)
    subtensor = bt.Subtensor(network=args.network)
    rounds = []
    incomplete_rounds = []
    owned_matches = 0
    owned_rejections = 0
    for task_id, observed in sequences.items():
        record = records.get(task_id)
        if not record:
            continue
        block = int(((record.get("block_context") or {}).get("created") or {}).get("number") or 0)
        if block <= 0:
            continue
        observed_events = order_events.get(task_id) or []
        minimum_observations = max(1, args.minimum_observations)
        if len(observed_events) < minimum_observations:
            incomplete_rounds.append(
                {
                    "task_id": task_id,
                    "observed_query_count": len(observed_events),
                    "minimum_required": minimum_observations,
                }
            )
            continue
        if args.identity_mode == "endpoint-window-stable":
            snapshot_blocks = metagraph_window_blocks(
                block,
                fetched_at.get(task_id) or "",
                observed_events,
                snapshots=max(2, int(args.window_snapshots)),
                finality_lag=max(0, int(args.metagraph_finality_lag)),
                block_seconds=max(0.1, float(args.block_seconds)),
            )
            metagraphs = [
                subtensor.subnets.metagraph(
                    args.netuid, block=snapshot_block, commitments=False
                )
                for snapshot_block in snapshot_blocks
            ]
            metagraph = metagraphs[0]
            stable_endpoints = stable_endpoint_uid_map(
                snapshot.neurons for snapshot in metagraphs
            )
        else:
            snapshot_blocks = [block]
            metagraph = subtensor.subnets.metagraph(
                args.netuid, block=block, commitments=False
            )
            stable_endpoints = None
        if args.owned_artifact_root and owned_hotkeys:
            uid_by_hotkey = {
                str(neuron.hotkey): int(neuron.uid) for neuron in metagraph.neurons
            }
            lane_uids = {
                lane: uid_by_hotkey[hotkey]
                for lane, hotkey in owned_hotkeys.items()
                if hotkey in uid_by_hotkey
            }
            captures = load_owned_captures(
                args.owned_artifact_root.resolve(), task_id, lane_uids
            )
            observed_events, owned_stats = apply_owned_capture_observations(
                observed_events,
                captures,
                expected_endpoints={
                    int(neuron.uid): str(neuron.axon)
                    for neuron in metagraph.neurons
                    if float(neuron.trust) <= 0 and neuron.axon is not None
                },
                maximum_delay_seconds=max(0.0, args.owned_match_max_delay),
            )
            owned_matches += owned_stats["matched"]
            owned_rejections += owned_stats["rejected"]
        if args.identity_mode == "event-index":
            row = build_event_index_constraint(
                task_id,
                block,
                metagraph.neurons,
                discovery_labels.get(task_id),
                observed_events,
            )
        else:
            row = build_round_constraint(
                task_id,
                block,
                observed,
                metagraph.neurons,
                discovery_labels.get(task_id),
                observed_events,
                endpoint_uids_override=stable_endpoints,
            )
        if args.identity_mode == "endpoint-window-stable":
            row["metagraph_snapshot_blocks"] = snapshot_blocks
            row["stable_endpoint_domains"] = len(stable_endpoints or {})
        row["fetched_at"] = fetched_at.get(task_id)
        rounds.append(row)
    rounds.sort(key=lambda row: str(row.get("fetched_at") or ""))

    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "numpy-shuffle-partial-order-constraints",
        "identity_mode": str(args.identity_mode),
        "generator_hypothesis": {
            "shuffle_api": "numpy.random.shuffle",
            "candidate_state": "legacy global RandomState / MT19937",
            "inter_round_seed_draws": "unknown; enumerate separately",
        },
        "partition_policy": {
            "discovery_labels_included": sum(
                row["seed_label_partition"] == "discovery" for row in rounds
            ),
            "sealed_labels_included": 0,
            "holdout_labels_opened": False,
            "prospective_labels_opened": False,
        },
        "summary": {
            "rounds": len(rounds),
            "shuffle_calls": len(rounds),
            "unique_uid_order_constraints": sum(
                row["unique_uid_constraints"] for row in rounds
            ),
            "domain_order_constraints": sum(
                row["domain_order_constraints"] for row in rounds
            ),
            "group_order_constraints": sum(
                row["group_order_constraints"] for row in rounds
            ),
            "group_sequence_missing_positions": sum(
                row["group_sequence_missing_positions"] for row in rounds
            ),
            "ambiguous_domain_positions": sum(
                row["ambiguous_domain_positions"] for row in rounds
            ),
            "exact_error_uid_constraints": sum(
                row["exact_error_uid_constraints"] for row in rounds
            ),
            "exact_owned_uid_constraints": sum(
                row.get("exact_owned_uid_constraints", 0) for row in rounds
            ),
            "owned_capture_matches": owned_matches,
            "owned_capture_rejections": owned_rejections,
            "incomplete_rounds_excluded": len(incomplete_rounds),
            "minimum_round_constraints": min(
                (row["unique_uid_constraints"] for row in rounds), default=0
            ),
        },
        "rounds": rounds,
        "incomplete_rounds": incomplete_rounds,
        "solver_contract": {
            "order_semantics": (
                "unique_uid_subsequence is an ordered subsequence of the full shuffled "
                "initial_uids array; it is not an absolute-position vector"
            ),
            "domain_order_semantics": (
                "ordered_uid_domains is a stronger ordered subsequence. Each token names "
                "a UID domain from uid_domains; repeated tokens require distinct members "
                "of that domain in occurrence order but do not expose endpoint strings"
            ),
            "group_order_semantics": (
                "In endpoint-snapshot mode the sequences encode historical endpoint "
                "equivalence. In event-index mode only proven UIDs are named and every "
                "other query is the same anonymous token; no endpoint string is persisted"
            ),
            "promotion_gate": (
                "recover one state, reproduce all included Discovery labels under one "
                "fixed seed-draw API, then open the five sealed Holdout labels"
            ),
        },
        "safety": {
            "public_read_only": True,
            "stores_endpoint_or_ip": False,
            "stores_credentials": False,
            "stores_owned_hotkeys": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **report["summary"], **report["partition_policy"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
