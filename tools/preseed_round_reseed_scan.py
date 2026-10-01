#!/usr/bin/env python3
"""Compile and run the exact multi-target uint32 reseed scanners.

The scan consumes only the fixed Discovery labels. Results are written
atomically so an interrupted 2**32 traversal never replaces a previous complete
artifact with an empty or partial file.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Mapping, Sequence


UINT32_MODULUS = 1 << 32
SOURCES = {
    "python": "tools/preseed_python_seed32_avx2.cpp",
    "numpy-randomstate": "tools/preseed_mt_seed32_avx2.cpp",
}


def discovery_targets(document: Mapping[str, Any]) -> list[list[int]]:
    records = document.get("records") or []
    if len(records) != 20:
        raise ValueError("requires the fixed 20-task Discovery set")
    targets = [[int(value) for value in row.get("seeds") or []] for row in records]
    if any(
        len(values) != 3
        or len(set(values)) != 3
        or any(value < 100 or value > 999 for value in values)
        for values in targets
    ):
        raise ValueError("every Discovery row requires three distinct seeds in 100..999")
    return targets


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--engine", choices=tuple(SOURCES), required=True)
    parser.add_argument("--threads", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--stop", type=int, default=UINT32_MODULUS)
    parser.add_argument("--build-dir", type=Path, default=Path(".build"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.start < 0 or args.start > args.stop or args.stop > UINT32_MODULUS:
        raise ValueError("scan range must be inside 0..2**32")
    if args.start % 8:
        raise ValueError("--start must be aligned to the eight-lane AVX2 batch")
    targets = discovery_targets(
        json.loads(args.discovery.read_text(encoding="utf-8"))
    )
    source = Path(SOURCES[args.engine]).resolve()
    args.build_dir.mkdir(parents=True, exist_ok=True)
    binary = (args.build_dir / f"{source.stem}-{args.engine}").resolve()
    if not binary.exists() or binary.stat().st_mtime_ns < source.stat().st_mtime_ns:
        subprocess.run(
            [
                "c++",
                "-O3",
                "-mavx2",
                "-fopenmp",
                "-std=c++17",
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
        )
    subprocess.run([str(binary), "--self-test"], check=True, capture_output=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
        for values in targets:
            handle.write(" ".join(map(str, values)) + "\n")
        handle.flush()
        completed = subprocess.run(
            [
                str(binary),
                "--multi-target",
                handle.name,
                str(args.start),
                str(args.stop),
                str(max(1, args.threads)),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    result = json.loads(completed.stdout)
    if len(result.get("targets") or []) != len(targets):
        raise RuntimeError("scanner returned the wrong target count")
    result["engine"] = args.engine
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["safety"] = {
        "discovery_only": True,
        "holdout_opened": False,
        "public_read_only": True,
        "submission_writes": False,
    }
    atomic_json(args.output.resolve(), result)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "engine": args.engine,
                "tested": result.get("tested"),
                "targets": len(result.get("targets") or []),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
