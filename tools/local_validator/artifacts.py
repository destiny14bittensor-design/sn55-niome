"""Immutable input loading and provenance for local validator runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
from typing import Any


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> tuple[Any, bytes]:
    raw = path.read_bytes()
    return json.loads(raw), raw


def read_first_fasta(path: Path) -> tuple[str, bytes]:
    """Read the first FASTA record, matching the validator's first-record behavior."""
    raw = path.read_bytes()
    lines = raw.decode("utf-8").splitlines()
    sequence: list[str] = []
    in_first_record = False
    for line in lines:
        if line.startswith(">"):
            if in_first_record:
                break
            in_first_record = True
            continue
        if in_first_record:
            sequence.append(line.strip())
    if not in_first_record or not sequence:
        raise ValueError(f"No FASTA record found in {path}")
    return "".join(sequence), raw


@dataclass(frozen=True)
class ArtifactBundle:
    contract: dict[str, Any]
    hbb_reference: dict[str, Any]
    chromosome_11: str
    cell_types: dict[str, Any]
    manifest: dict[str, Any]

    @classmethod
    def from_paths(
        cls,
        *,
        contract_path: str | Path,
        hbb_reference_path: str | Path,
        chromosome_11_path: str | Path,
        cell_types_path: str | Path,
    ) -> "ArtifactBundle":
        paths = {
            "contract": Path(contract_path).resolve(),
            "hbb_reference": Path(hbb_reference_path).resolve(),
            "chromosome_11": Path(chromosome_11_path).resolve(),
            "cell_types": Path(cell_types_path).resolve(),
        }
        contract, contract_raw = _read_json(paths["contract"])
        reference, reference_raw = _read_json(paths["hbb_reference"])
        chromosome, chromosome_raw = read_first_fasta(paths["chromosome_11"])
        cell_types, cell_types_raw = _read_json(paths["cell_types"])
        raw_by_name = {
            "contract": contract_raw,
            "hbb_reference": reference_raw,
            "chromosome_11": chromosome_raw,
            "cell_types": cell_types_raw,
        }
        manifest = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(),
            "files": {
                name: {
                    "path": str(path),
                    "sha256": sha256_bytes(raw_by_name[name]),
                    "bytes": len(raw_by_name[name]),
                }
                for name, path in paths.items()
            },
        }
        return cls(
            contract=contract,
            hbb_reference=reference,
            chromosome_11=chromosome,
            cell_types=cell_types,
            manifest=manifest,
        )
