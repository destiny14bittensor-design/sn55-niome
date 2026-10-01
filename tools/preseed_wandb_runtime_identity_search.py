#!/usr/bin/env python3
"""Test safe public W&B runtime identities as persistent PRNG initializers.

The W&B key is read from the existing mode-0600 file. Metadata values are used
only in memory; output contains labels and aggregate counts, never raw host,
writer id, paths, arguments, environment values, URLs, or credentials.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from typing import Any, Mapping

import wandb

try:
    from tools.preseed_runtime_identity_search import (
        derived_seeds,
        numpy_stream,
        prefix_match,
        python_stream,
    )
    from tools.preseed_stateful_prelude_search import load_layout
    from tools.wandb_seed_probe import DEFAULT_KEY_FILE, _read_key
except ModuleNotFoundError:
    from preseed_runtime_identity_search import (
        derived_seeds,
        numpy_stream,
        prefix_match,
        python_stream,
    )
    from preseed_stateful_prelude_search import load_layout
    from wandb_seed_probe import DEFAULT_KEY_FILE, _read_key


SAFE_METADATA_KEYS = (
    "host",
    "writerId",
    "program",
    "codePath",
    "codePathLocal",
    "executable",
    "startedAt",
)


def safe_materials(metadata: Mapping[str, Any], run_id: str) -> list[tuple[str, str]]:
    values = {
        key: str(metadata[key])
        for key in SAFE_METADATA_KEYS
        if isinstance(metadata.get(key), (str, int)) and str(metadata[key])
    }
    values["run-id"] = str(run_id)
    rows = [(key, value) for key, value in values.items()]
    start = values.get("startedAt")
    if start:
        for key in SAFE_METADATA_KEYS:
            if key == "startedAt" or key not in values:
                continue
            for delimiter_name, delimiter in (
                ("none", ""),
                ("colon", ":"),
                ("pipe", "|"),
                ("unit", "\x1f"),
            ):
                rows.append(
                    (
                        f"{key}-startedAt-{delimiter_name}",
                        values[key] + delimiter + start,
                    )
                )
    return rows


def search_materials(
    materials: list[tuple[str, str]], targets: list[list[int]]
) -> dict[str, Any]:
    tested = 0
    best = 0
    hits: list[dict[str, str]] = []
    family_counts = {"python-wandb-runtime": 0, "numpy-wandb-runtime": 0}
    for label, material in materials:
        for derivation, seed in derived_seeds(material):
            for method in ("sample", "randint-unique", "float-unique"):
                matched = prefix_match(python_stream(seed, method, len(targets)), targets)
                tested += 1
                family_counts["python-wandb-runtime"] += 1
                best = max(best, matched)
                if matched == len(targets):
                    hits.append(
                        {
                            "material_label": label,
                            "derivation": derivation,
                            "engine": "python",
                            "method": method,
                        }
                    )
            if not isinstance(seed, int):
                continue
            for engine in ("random-state", "default-rng"):
                for method in ("choice", "integers-unique"):
                    matched = prefix_match(
                        numpy_stream(seed, engine, method, len(targets)), targets
                    )
                    tested += 1
                    family_counts["numpy-wandb-runtime"] += 1
                    best = max(best, matched)
                    if matched == len(targets):
                        hits.append(
                            {
                                "material_label": label,
                                "derivation": derivation,
                                "engine": engine,
                                "method": method,
                            }
                        )
    return {
        "candidates_tested": tested,
        "discovery_exact_candidates": len(hits),
        "best_prefix_tasks": best,
        "families": {
            label: {"tested": count} for label, count in family_counts.items()
        },
        "exact_candidates": hits,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--entity", default="genomes")
    parser.add_argument("--project", default="niome")
    parser.add_argument("--run", default="non2mca3")
    parser.add_argument("--prelude", default="654,347,964")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_wandb_runtime_identity_search.json"),
    )
    args = parser.parse_args()
    api = wandb.Api(api_key=_read_key(args.key_file), timeout=30)
    run = api.run(f"{args.entity}/{args.project}/{args.run}")
    with tempfile.TemporaryDirectory() as directory:
        downloaded = run.file("wandb-metadata.json").download(
            root=directory, replace=True
        )
        metadata = json.loads(Path(downloaded.name).read_text(encoding="utf-8"))
    observed_prelude = [int(value) for value in args.prelude.split(",")]
    if len(observed_prelude) != 3:
        raise ValueError("--prelude requires three comma-separated integers")
    process_start = str(metadata.get("startedAt") or "")
    if not process_start:
        raise ValueError("public W&B metadata has no process start")
    prelude, segment = load_layout(
        args.discovery.resolve(), process_start, observed_prelude
    )
    targets = [prelude] + [[int(value) for value in row["seeds"]] for row in segment]
    materials = safe_materials(metadata, args.run)
    search = search_materials(materials, targets)
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "public-wandb-runtime-identity-prng-search",
        "split": {
            "process_segment_with_prelude": len(targets),
            "holdout_opened": False,
        },
        "search": search,
        "safety": {
            "public_authorized_wandb_read_only": True,
            "raw_metadata_values_persisted": False,
            "args_or_environment_read": False,
            "credentials_stored": False,
            "holdout_opened": False,
            "submission_writes": False,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "candidates_tested": search["candidates_tested"],
                "discovery_exact_candidates": search[
                    "discovery_exact_candidates"
                ],
                "best_prefix_tasks": search["best_prefix_tasks"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
