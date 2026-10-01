#!/usr/bin/env python3
"""Record finalized public Finney block headers for prospective seed research.

The collector stores only public block number, hash, chain timestamp and local
observation time.  It does not read wallets, validator state or task secrets.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sys
import time
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from niome_subnet.utils.misc import FINALITY_LAG
from tools.public_block_header import public_header


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def header_record(info: Any, observed_at: str) -> dict[str, Any]:
    number = int(info.number)
    block_hash = str(info.hash)
    if not block_hash.startswith("0x") or len(block_hash) != 66:
        raise ValueError("unexpected public block hash")
    timestamp = getattr(info, "timestamp", None)
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp_text = timestamp.astimezone(timezone.utc).isoformat()
    else:
        timestamp_text = str(timestamp or "") or None
    record = {
        "block": number,
        "hash": block_hash,
        "timestamp": timestamp_text,
        "observed_at": observed_at,
        "source": "finney-public-finalized-header",
    }
    header = public_header(info)
    if header is not None:
        record["header"] = header
    return record


def collect_range(
    *,
    start: int,
    end: int,
    block_info: Callable[[int], Any],
    output: Path,
    state_path: Path,
) -> dict[str, Any]:
    state = read_json(state_path, {})
    written = 0
    for block in range(max(0, start), max(0, end) + 1):
        info = block_info(block)
        if info is None:
            continue
        record = header_record(info, utc_now())
        append_jsonl(output, record)
        written += 1
        state.update(
            {
                "last_block": record["block"],
                "last_hash": record["hash"],
                "updated_at": record["observed_at"],
                "healthy": True,
                "records_written": int(state.get("records_written") or 0) + 1,
                "safety": {
                    "public_chain_only": True,
                    "credentials": False,
                    "submission_writes": False,
                },
            }
        )
        atomic_json(state_path, state)
    state["last_batch_written"] = written
    atomic_json(state_path, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--network", default="finney")
    parser.add_argument("--poll-interval", type=float, default=6.0)
    parser.add_argument("--initial-backfill", type=int, default=30)
    parser.add_argument("--finality-lag", type=int, default=FINALITY_LAG)
    args = parser.parse_args()

    import bittensor as bt

    output = args.output.resolve()
    state_path = args.state.resolve()
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    subtensor = bt.Subtensor(network=args.network)
    try:
        while not stopping:
            state = read_json(state_path, {})
            current = max(0, int(subtensor.block) - max(0, args.finality_lag))
            last = state.get("last_block")
            start = (
                int(last) + 1
                if isinstance(last, int)
                else max(0, current - max(0, args.initial_backfill))
            )
            end = current
            if start <= end:
                try:
                    collect_range(
                        start=start,
                        end=end,
                        block_info=subtensor.block_info,
                        output=output,
                        state_path=state_path,
                    )
                except Exception as error:
                    failed = read_json(state_path, {})
                    failed.update(
                        {
                            "healthy": False,
                            "updated_at": utc_now(),
                            "last_error": type(error).__name__,
                        }
                    )
                    atomic_json(state_path, failed)
            time.sleep(max(1.0, args.poll_interval))
    finally:
        close = getattr(subtensor, "close", None)
        if callable(close):
            close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
