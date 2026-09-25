"""Pure local clone of NIOME Stage 1 and Stage 2."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import math
from typing import Any


REGION_ENERGY_OFFSETS = {
    "5UTR_or_upstream": 0.05,
    "exon1": 0.03,
    "exon2": 0.03,
    "exon3": 0.03,
    "intron1": -0.03,
    "intron2": -0.03,
    "3UTR": 0.02,
}


def build_kmer_index(seq: str, k: int = 12) -> dict[str, list[int]]:
    index: dict[str, list[int]] = defaultdict(list)
    # The missing +1 intentionally preserves the production off-by-one.
    for i in range(len(seq) - k):
        index[seq[i : i + k]].append(i)
    return index


def hash_kmer_inputs(seq: str, k: int) -> str:
    h = hashlib.md5()
    h.update(seq.encode())
    h.update(str(k).encode())
    return h.hexdigest()


def gc_content(seq: str) -> float:
    return sum(base in "GC" for base in seq) / max(1, len(seq))


def hamming(a: str, b: str) -> int:
    return sum(left != right for left, right in zip(a, b))


_COMPLEMENT = str.maketrans(
    "ACGTRYMKBDHVNacgtrymkbdhvn",
    "TGCAYRKMVHDBNtgcayrkmvhdbn",
)


def reverse_complement(seq: str) -> str:
    return seq.translate(_COMPLEMENT)[::-1]


def check_pam(seq: str, start: int, length: int, cas: str, strand: str | None):
    if start < 0 or start + length >= len(seq):
        return False, "out_of_bounds"

    if cas == "Cas9":
        if strand == "+":
            if start + length + 3 > len(seq):
                return False, "out_of_bounds"
            pam = seq[start + length : start + length + 3]
        elif strand == "-":
            if start < 3:
                return False, "out_of_bounds"
            pam = reverse_complement(seq[start - 3 : start])
        else:
            return False, "invalid_strand"
        return (len(pam) == 3 and pam[1:] == "GG"), "ok"

    if cas == "Cas12a":
        if strand == "+":
            if start < 4:
                return False, "out_of_bounds"
            pam = seq[start - 4 : start]
        elif strand == "-":
            if start + length + 4 > len(seq):
                return False, "out_of_bounds"
            pam = reverse_complement(seq[start + length : start + length + 4])
        else:
            return False, "invalid_strand"
        return (len(pam) == 4 and pam[:3] == "TTT"), "ok"

    return False, "invalid_cas"


def stage1(
    exp: dict[str, Any],
    seq: str,
    mutation_map: dict[str, int],
    contract: dict[str, Any],
) -> tuple[float, str]:
    guide = exp["guideRNA"]
    cas = exp["cas_system"]
    start = exp["target_alignment_start"]
    mutation = exp["mutation"]

    if mutation not in contract["active_mutations"]:
        return 0.0, "mutation_not_allowed"
    contract_cell_type = contract.get("cell_type")
    if contract_cell_type is not None and exp.get("cell_type") != contract_cell_type:
        return 0.0, "cell_type_mismatch"
    if len(guide) not in (20, 23):
        return 0.0, "invalid_length"
    if exp.get("target_alignment_end") != start + len(guide):
        return 0.0, "invalid_alignment_end"
    if start < 0 or start + len(guide) >= len(seq):
        return 0.0, "out_of_bounds"

    pam_ok, pam_status = check_pam(seq, start, len(guide), cas, exp.get("strand"))
    if not pam_ok:
        return 0.0, f"pam_{pam_status}"

    target = seq[start : start + len(guide)]
    if exp["strand"] == "+":
        mismatches = hamming(guide, target)
    elif exp["strand"] == "-":
        mismatches = hamming(reverse_complement(guide), target)
    else:
        return 0.0, "invalid_strand"
    if mismatches > contract["rules"]["max_mismatches"]:
        return 0.0, "too_many_mismatches"

    if contract["rules"].get("proximity_gate", False):
        max_distance = contract["rules"]["base_padding"]
        if abs(start - mutation_map[mutation]) > max_distance:
            return 0.0, "mutation_too_far"
    return 1.0, "ok"


def offtarget_uniqueness(guide: str, cas: str, kmer_index: dict[str, list[int]]) -> float:
    seed = guide[-12:] if cas == "Cas9" else guide[:12]
    hits = len(kmer_index.get(seed, []))
    if hits == 0:
        return 1.0
    if hits <= 5:
        return 0.7
    if hits <= 20:
        return 0.4
    return 0.1


def stage2(
    cell_types: dict[str, Any],
    exp: dict[str, Any],
    mutation_map: dict[str, int],
    contract: dict[str, Any],
    kmer_index: dict[str, list[int]],
) -> tuple[float, dict[str, Any]]:
    guide = exp["guideRNA"]
    cas = exp["cas_system"]
    start = exp["target_alignment_start"]
    mutation = exp["mutation"]
    mutation_coordinate = mutation_map[mutation]

    gc = gc_content(guide)
    distance = abs(start - mutation_coordinate)
    gc_score = max(0.0, 1.0 - abs(gc - 0.5) * 2)
    base_padding = contract["rules"]["base_padding"]
    dist_score = math.exp(-distance / base_padding)
    consistency = 1.0 if distance < base_padding else 0.3
    base_structural_score = 0.625 * gc_score + 0.375 * dist_score
    offtarget_factor = offtarget_uniqueness(guide, cas, kmer_index)
    structural_score = base_structural_score * offtarget_factor
    mutation_weight = contract.get("mutation_weights", {}).get(mutation, 1.0)
    weighted_score = structural_score * mutation_weight
    cell_type = contract.get("cell_type")
    accessibility = cell_types.get(cell_type, {}).get("accessibility", 1.0)
    mutation_region = contract.get("mutation_regions", {}).get(mutation)
    region_energy_offset = REGION_ENERGY_OFFSETS.get(mutation_region, 0.0)
    return structural_score, {
        "gc": gc,
        "distance": distance,
        "gc_score": gc_score,
        "dist_score": dist_score,
        "consistency": consistency,
        "offtarget_factor": offtarget_factor,
        "mutation_weight": mutation_weight,
        "weighted_score": weighted_score,
        "cell_type": cell_type,
        "cell_type_accessibility": accessibility,
        "mutation_region": mutation_region,
        "region_energy_offset": region_energy_offset,
    }


def run_stage12(
    submission: list[dict[str, Any]],
    *,
    contract: dict[str, Any],
    reference: dict[str, Any],
    chromosome_11: str,
    cell_types: dict[str, Any],
    offtarget_flank: int = 50_000,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    mutation_map = reference["mutation_map"]
    win_start = reference["gene_region"]["start"]
    win_end = reference["gene_region"]["end"]
    idx_start = max(0, win_start - offtarget_flank)
    idx_end = min(len(chromosome_11), win_end + offtarget_flank)
    index_sequence = chromosome_11[idx_start:idx_end]
    kmer_index = build_kmer_index(index_sequence, k=12)

    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    seen_valid_keys: set[tuple[Any, ...]] = set()
    for exp in submission:
        stage1_score, reason = stage1(exp, chromosome_11, mutation_map, contract)
        if stage1_score == 1.0:
            duplicate_key = (
                exp["cas_system"],
                exp["target_alignment_start"],
                exp.get("strand"),
                exp["guideRNA"],
            )
            if duplicate_key in seen_valid_keys:
                stage1_score, reason = 0.0, "duplicate_experiment"
            else:
                seen_valid_keys.add(duplicate_key)
        if stage1_score == 0.0:
            invalid.append({"experiment": exp, "stage1_pass": False, "reason": reason})
            continue

        structural_score, info = stage2(
            cell_types, exp, mutation_map, contract, kmer_index
        )
        valid.append(
            {
                "experiment": exp,
                "features": {
                    "gc": info["gc"],
                    "distance_to_mutation": info["distance"],
                    "gc_score": info["gc_score"],
                    "dist_score": info["dist_score"],
                    "consistency": info["consistency"],
                    "offtarget_factor": info["offtarget_factor"],
                    "mutation_weight": info["mutation_weight"],
                    "cell_type": info["cell_type"],
                    "cell_type_accessibility": info["cell_type_accessibility"],
                    "mutation_region": info["mutation_region"],
                    "region_energy_offset": info["region_energy_offset"],
                },
                "stage1": {"valid": True},
                "stage2": {
                    "structural_score": structural_score,
                    "weighted_score": info["weighted_score"],
                },
            }
        )
    provenance = {
        "kmer_k": 12,
        "kmer_window": [idx_start, idx_end],
        "kmer_input_md5": hash_kmer_inputs(index_sequence, 12),
        "kmer_unique_keys": len(kmer_index),
    }
    return valid, invalid, provenance
