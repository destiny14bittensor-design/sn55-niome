#!/usr/bin/env python3
"""Search public on-chain Drand pulse hypotheses against the fixed split.

Bittensor's Drand pallet retains public beacon pulses separately from block
hashes.  This tool collects only the pulse round and randomness, scans a full
NIOME round around each task, and opens holdout labels only for a 20/20 ordered
discovery match.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from tools.preseed_generator_lab import atomic_json, load_tasks
    from tools.preseed_hypothesis_search import (
        Ranking,
        _block_scalar,
        _digest_triplet,
        _numpy_triplet,
        _python_triplet,
        evaluate_predictions,
    )
except ModuleNotFoundError:
    from preseed_generator_lab import atomic_json, load_tasks
    from preseed_hypothesis_search import (
        Ranking,
        _block_scalar,
        _digest_triplet,
        _numpy_triplet,
        _python_triplet,
        evaluate_predictions,
    )


DRAND_GENESIS_TIME = 1_692_803_367
DRAND_PERIOD_SECONDS = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def task_base_round(record: Mapping[str, Any]) -> int:
    created = datetime.fromisoformat(
        str(record.get("created_at") or "").replace("Z", "+00:00")
    )
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return int((created.timestamp() - DRAND_GENESIS_TIME) // DRAND_PERIOD_SECONDS) + 1


def required_rounds(
    records: Sequence[Mapping[str, Any]], offset_start: int, offset_stop: int
) -> list[int]:
    return sorted(
        {
            task_base_round(record) + offset
            for record in records
            for offset in range(offset_start, offset_stop + 1)
        }
    )


def collect_pulses(
    cache: dict[str, Any],
    rounds: Sequence[int],
    *,
    network: str,
    batch_size: int,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    missing = [round_number for round_number in rounds if str(round_number) not in (cache.get("pulses") or {})]
    if not missing:
        return cache
    import bittensor as bt

    subtensor = bt.Subtensor(network=network)
    pulses = dict(cache.get("pulses") or {})
    try:
        substrate = subtensor._client._substrate
        for start in range(0, len(missing), max(1, batch_size)):
            batch = missing[start : start + max(1, batch_size)]
            values = subtensor._call(
                substrate.query_batch("Drand", "Pulses", [[value] for value in batch])
            )
            for requested, pulse in zip(batch, values):
                if not isinstance(pulse, Mapping):
                    continue
                randomness = pulse.get("randomness")
                actual_round = pulse.get("round")
                if (
                    int(actual_round or -1) == requested
                    and isinstance(randomness, str)
                    and randomness.startswith("0x")
                    and len(randomness) == 66
                ):
                    pulses[str(requested)] = randomness
            if on_progress is not None:
                on_progress(
                    {
                        "version": 1,
                        "network": network,
                        "source": "public-finney-drand-pallet",
                        "updated_at": utc_now(),
                        "pulses": pulses,
                    }
                )
    finally:
        subtensor.close()
    return {
        "version": 1,
        "network": network,
        "source": "public-finney-drand-pallet",
        "updated_at": utc_now(),
        "pulses": pulses,
    }


def _pulse_for(
    cache: Mapping[str, Any], record: Mapping[str, Any], offset: int
) -> str | None:
    return (cache.get("pulses") or {}).get(str(task_base_round(record) + offset))


def run_search(
    discovery: Sequence[Mapping[str, Any]],
    holdout: Sequence[Mapping[str, Any]],
    cache: Mapping[str, Any],
    *,
    offset_start: int,
    offset_stop: int,
    top_limit: int,
) -> dict[str, Any]:
    ranking = Ranking(limit=top_limit)
    holdout_results: list[dict[str, Any]] = []

    def add(
        model_id: str,
        family: str,
        predict: Callable[[Mapping[str, Any]], Sequence[int] | None],
    ) -> None:
        result = evaluate_predictions(model_id, family, discovery, predict)
        ranking.add(result)
        if result.exact_ordered == len(discovery):
            holdout_result = evaluate_predictions(model_id, family, holdout, predict)
            holdout_results.append(holdout_result.as_dict())

    scalar_variants = []
    for source in ("raw", "hash"):
        algorithms = ("sha256", "sha512", "blake2b") if source == "hash" else ("sha256",)
        for algorithm in algorithms:
            for endian in ("big", "little"):
                for width in ("full", "4", "8", "16"):
                    scalar_variants.append((source, algorithm, endian, width))
    for algorithm in ("sha256", "sha512", "blake2b"):
        for endian in ("big", "little"):
            scalar_variants.append(("official-counter", algorithm, endian, "full"))

    for offset in range(offset_start, offset_stop - 1):
        for source, algorithm, endian, width in scalar_variants:
            model_id = f"drand-consecutive:o{offset}:{source}:{algorithm}:{endian}:{width}"

            def predict(record: Mapping[str, Any], o=offset, s=source, a=algorithm, e=endian, w=width):
                values = [_pulse_for(cache, record, o + index) for index in range(3)]
                if any(value is None for value in values):
                    return None
                result: list[int] = []
                for value in values:
                    result.append(
                        _block_scalar(
                            value or "",
                            source=s,
                            algorithm=a,
                            endian=e,
                            width=w,
                            exclude=result,
                        )
                    )
                return result

            add(model_id, "drand-consecutive", predict)

    extractions = (
        "counter-big",
        "counter-little",
        "chunks-4-big",
        "chunks-4-little",
        "chunks-8-big",
        "chunks-8-little",
        "chunks-16-big",
        "chunks-16-little",
    )
    for offset in range(offset_start, offset_stop + 1):
        for material_name, material_fn in (
            ("raw", lambda value: bytes.fromhex(value.removeprefix("0x"))),
            ("hex", lambda value: value.removeprefix("0x").encode()),
            ("0xhex", lambda value: value.encode()),
        ):
            for algorithm in ("sha256", "sha512", "blake2b"):
                for extraction in extractions:
                    model_id = f"drand-one-digest:o{offset}:{material_name}:{algorithm}:{extraction}"

                    def predict(record: Mapping[str, Any], o=offset, mf=material_fn, a=algorithm, x=extraction):
                        value = _pulse_for(cache, record, o)
                        return None if value is None else _digest_triplet(mf(value), a, x)

                    add(model_id, "drand-one-digest", predict)

        for seed_name, seed_fn in (
            ("hex", lambda value: value.removeprefix("0x")),
            ("0xhex", lambda value: value),
            ("raw", lambda value: bytes.fromhex(value.removeprefix("0x"))),
            ("int-big", lambda value: int(value, 16)),
            ("first32-big", lambda value: int(value[2:10], 16)),
            ("last32-big", lambda value: int(value[-8:], 16)),
        ):
            for method in ("sample", "randint-unique"):
                model_id = f"drand-one-python:o{offset}:{seed_name}:{method}"

                def predict(record: Mapping[str, Any], o=offset, sf=seed_fn, m=method):
                    value = _pulse_for(cache, record, o)
                    return None if value is None else _python_triplet(sf(value), m)

                add(model_id, "drand-one-python", predict)

        for generator in ("random-state", "default-rng"):
            for method in ("choice", "integers-unique"):
                model_id = f"drand-round:o{offset}:{generator}:{method}"
                add(
                    model_id,
                    "drand-round-prng",
                    lambda record, o=offset, g=generator, m=method: _numpy_triplet(
                        task_base_round(record) + o, g, m
                    ),
                )
        for method in ("sample", "randint-unique"):
            add(
                f"drand-round-python:o{offset}:{method}",
                "drand-round-prng",
                lambda record, o=offset, m=method: _python_triplet(
                    task_base_round(record) + o, m
                ),
            )

        for field in ("task_id", "contract_material", "created_at"):
            for order in ("pulse-field", "field-pulse"):
                for delimiter in (b"", b":", b"|", b"\x1f"):
                    for algorithm in ("sha256", "blake2b"):
                        model_id = f"drand-task:o{offset}:{field}:{order}:d{delimiter.hex()}:{algorithm}"

                        def predict(record: Mapping[str, Any], o=offset, f=field, order_name=order, d=delimiter, a=algorithm):
                            value = _pulse_for(cache, record, o)
                            if value is None:
                                return None
                            pulse = bytes.fromhex(value.removeprefix("0x"))
                            other = str(record.get(f) or "").encode()
                            material = pulse + d + other if order_name == "pulse-field" else other + d + pulse
                            return _digest_triplet(material, a, "counter-big")

                        add(model_id, "drand-task-composite", predict)

    return {
        "version": 1,
        "generated_at": utc_now(),
        "mode": "offline-public-drand-discovery-search",
        "safety": {
            "public_chain_only": True,
            "credentials_accepted": False,
            "private_endpoints": False,
            "submission_writes": False,
        },
        "split": {
            "discovery_records": len(discovery),
            "holdout_records_reserved": len(holdout),
            "holdout_gate": "closed" if not ranking.discovery_exact else "opened-after-20-of-20",
        },
        "search": {
            "offset_start": offset_start,
            "offset_stop": offset_stop,
            "candidates_tested": ranking.tested,
            "discovery_exact_candidates": len(ranking.discovery_exact),
            "families": {
                key: {**value, "best_rank": list(value["best_rank"])}
                for key, value in sorted(ranking.by_family.items())
            },
            "top_candidates": [row.as_dict() for row in ranking.top()],
            "discovery_exact": [row.as_dict() for row in ranking.discovery_exact],
            "holdout": holdout_results,
        },
        "interpretation": (
            "Acceptance requires 20/20 ordered discovery, 5/5 ordered holdout, "
            "and a later prospective shadow confirmation."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--holdout", type=Path, required=True)
    parser.add_argument("--pulse-cache", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--network", default="finney")
    parser.add_argument("--offset-start", type=int, default=-32)
    parser.add_argument("--offset-stop", type=int, default=3_000)
    parser.add_argument("--batch-size", type=int, default=2_000)
    parser.add_argument("--top-limit", type=int, default=50)
    parser.add_argument("--no-collect", action="store_true")
    args = parser.parse_args()
    if args.offset_stop <= args.offset_start:
        raise SystemExit("invalid offset range")

    discovery = load_tasks(args.discovery.resolve())
    holdout = load_tasks(args.holdout.resolve())
    if len(discovery) != 20 or len(holdout) < 5:
        raise SystemExit("requires the fixed 20 discovery and at least 5 holdout tasks")
    cache: dict[str, Any] = {"version": 1, "network": args.network, "pulses": {}}
    if args.pulse_cache.exists():
        cache = json.loads(args.pulse_cache.read_text(encoding="utf-8"))
    rounds = required_rounds(discovery, args.offset_start, args.offset_stop + 2)
    if not args.no_collect:
        cache = collect_pulses(
            cache,
            rounds,
            network=args.network,
            batch_size=args.batch_size,
            on_progress=lambda value: atomic_json(args.pulse_cache.resolve(), value),
        )
        atomic_json(args.pulse_cache.resolve(), cache)
    missing = [value for value in rounds if str(value) not in (cache.get("pulses") or {})]
    if missing:
        raise SystemExit(f"pulse cache is missing {len(missing)} required public rounds")
    report = run_search(
        discovery,
        holdout,
        cache,
        offset_start=args.offset_start,
        offset_stop=args.offset_stop,
        top_limit=args.top_limit,
    )
    report["pulse_cache"] = {
        "network": args.network,
        "required_rounds": len(rounds),
        "cached_rounds": len(cache.get("pulses") or {}),
        "minimum_round": min(rounds),
        "maximum_round": max(rounds),
    }
    atomic_json(args.report.resolve(), report)
    print(
        json.dumps(
            {
                "event": "preseed_drand_search_complete",
                "candidates_tested": report["search"]["candidates_tested"],
                "discovery_exact_candidates": report["search"]["discovery_exact_candidates"],
                "best": (report["search"]["top_candidates"] or [None])[0],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
