#!/usr/bin/env python3
"""Read-only audit of W&B surfaces that could expose NIOME seeds.

The API key is read from a mode-0600 file.  It is never accepted on the command
line, copied into the environment, or included in output.  The probe lists runs
visible to the supplied identity and extracts only task/validation/seed timing
lines from each run's console log.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

import wandb


DEFAULT_KEY_FILE = Path.home() / ".config" / "niome" / "wandb_api_key"
DEFAULT_ENTITY = "genomes"
DEFAULT_PROJECT = "niome"

SAFE_LINE_PATTERNS = (
    re.compile(r"Fetched task\s+[0-9a-f-]{36}", re.IGNORECASE),
    re.compile(r"Validating miners", re.IGNORECASE),
    re.compile(r"Generated seeds?\s*:", re.IGNORECASE),
    re.compile(r"Benchmarking this round on seeds", re.IGNORECASE),
    re.compile(r"Submitted validation results for task", re.IGNORECASE),
    re.compile(r"Finished validation", re.IGNORECASE),
)

# Never echo lines likely to contain credentials, signed URLs, or authorization
# material, even if they also contain a task/seed keyword.
SENSITIVE_LINE_PATTERN = re.compile(
    r"api[_ -]?key|authorization|bearer|secret|password|x-amz-|signature",
    re.IGNORECASE,
)


def _read_key(path: Path) -> str:
    resolved = path.expanduser().resolve()
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"W&B key path is not a regular file: {resolved}")
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(
            f"W&B key file must not be accessible by group/other: {resolved}"
        )
    key = resolved.read_text(encoding="utf-8").strip()
    if not key or "\n" in key or "\r" in key:
        raise ValueError("W&B key file must contain exactly one non-empty line")
    return key


def _safe_timing_lines(edges: list[dict[str, Any]]) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    for edge in edges:
        node = edge.get("node") or {}
        line = str(node.get("line") or "")
        if SENSITIVE_LINE_PATTERN.search(line):
            continue
        if not any(pattern.search(line) for pattern in SAFE_LINE_PATTERNS):
            continue
        selected.append(
            {
                "timestamp": str(node.get("timestamp") or ""),
                "line": line,
            }
        )
    return selected


def _inventory(api: wandb.Api, entity: str, project: str, log_limit: int) -> dict:
    query = """
    query ProbeProject($entity: String!, $project: String!, $logLimit: Int!) {
      project(name: $project, entityName: $entity) {
        id
        name
        runs(first: 500, order: "-createdAt") {
          edges {
            node {
              name
              displayName
              state
              createdAt
              updatedAt
              heartbeatAt
              logLineCount
              eventsLineCount
              historyKeys
              files {
                edges { node { name sizeBytes updatedAt } }
              }
              logLines(last: $logLimit) {
                edges { node { line timestamp } }
              }
            }
          }
          pageInfo { hasNextPage endCursor }
        }
        artifactCollections(first: 500) {
          edges {
            node {
              name
              createdAt
              updatedAt
              artifacts(first: 10) {
                totalCount
                edges {
                  node { versionIndex createdAt updatedAt size fileCount }
                }
              }
            }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """
    data = api._service_api.execute_graphql(  # noqa: SLF001 - official SDK transport
        query,
        variables={
            "entity": entity,
            "project": project,
            "logLimit": log_limit,
        },
    )
    project_data = data.get("project")
    if not isinstance(project_data, dict):
        raise RuntimeError(f"W&B project is not visible: {entity}/{project}")

    runs = []
    for edge in project_data["runs"]["edges"]:
        node = edge["node"]
        history_keys = node.get("historyKeys") or {}
        runs.append(
            {
                "name": node.get("name"),
                "display_name": node.get("displayName"),
                "state": node.get("state"),
                "created_at": node.get("createdAt"),
                "updated_at": node.get("updatedAt"),
                "heartbeat_at": node.get("heartbeatAt"),
                "log_line_count": node.get("logLineCount"),
                "events_line_count": node.get("eventsLineCount"),
                "history_keys": sorted((history_keys.get("keys") or {}).keys()),
                "files": [
                    {
                        "name": file_edge["node"].get("name"),
                        "size_bytes": file_edge["node"].get("sizeBytes"),
                        "updated_at": file_edge["node"].get("updatedAt"),
                    }
                    for file_edge in (node.get("files") or {}).get("edges", [])
                ],
                "timing_lines": _safe_timing_lines(
                    (node.get("logLines") or {}).get("edges", [])
                ),
            }
        )

    collections = []
    for edge in project_data["artifactCollections"]["edges"]:
        node = edge["node"]
        collections.append(
            {
                "name": node.get("name"),
                "created_at": node.get("createdAt"),
                "updated_at": node.get("updatedAt"),
                "versions": [
                    {
                        "version_index": artifact_edge["node"].get("versionIndex"),
                        "created_at": artifact_edge["node"].get("createdAt"),
                        "updated_at": artifact_edge["node"].get("updatedAt"),
                        "size": artifact_edge["node"].get("size"),
                        "file_count": artifact_edge["node"].get("fileCount"),
                    }
                    for artifact_edge in node["artifacts"]["edges"]
                ],
            }
        )

    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "entity": entity,
        "project": project,
        "run_count": len(runs),
        "artifact_collection_count": len(collections),
        "runs_page_truncated": bool(project_data["runs"]["pageInfo"]["hasNextPage"]),
        "artifacts_page_truncated": bool(
            project_data["artifactCollections"]["pageInfo"]["hasNextPage"]
        ),
        "runs": runs,
        "artifact_collections": collections,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--key-file",
        type=Path,
        default=Path(os.environ.get("NIOME_WANDB_KEY_FILE", DEFAULT_KEY_FILE)),
    )
    parser.add_argument("--entity", default=DEFAULT_ENTITY)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--log-limit", type=int, default=10_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    key = _read_key(args.key_file)
    api = wandb.Api(api_key=key, timeout=30)
    result = _inventory(
        api,
        entity=args.entity,
        project=args.project,
        log_limit=max(1, min(args.log_limit, 50_000)),
    )
    encoded = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
        print(
            json.dumps(
                {
                    "output": str(args.output.resolve()),
                    "run_count": result["run_count"],
                    "artifact_collection_count": result[
                        "artifact_collection_count"
                    ],
                }
            )
        )
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
