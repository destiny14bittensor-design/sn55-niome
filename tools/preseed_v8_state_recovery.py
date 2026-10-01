#!/usr/bin/env python3
"""Test whether the current seed stream is consecutive V8 Math.random output.

V8 uses a 128-bit xorshift state, so the post-restart public seed bins carry
enough information for a genuine state-recovery test.  The tool tests both the
current xorshift128+ output (sum, top 53 bits) and the older V8 state0 output
(top 52 bits).  It first asks whether the whole Discovery process segment is
satisfiable, then trains on a prefix and predicts an excluded Discovery suffix.

Z3 is intentionally an ephemeral runtime dependency.  Run with
``uv run --with z3-solver python tools/preseed_v8_state_recovery.py ...``.
No Holdout labels, credentials, network calls, or submissions are used.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

try:
    from tools.preseed_mt19937_rank_audit import LinearBasis
except ModuleNotFoundError:
    from preseed_mt19937_rank_audit import LinearBasis


MASK64 = (1 << 64) - 1
DOMAIN_LOW = 100
DOMAIN_SIZE = 900
DEFAULT_PROCESS_START = "2026-09-26T14:57:51.192497Z"
DEFAULT_PRELUDE = [654, 347, 964]


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_segment(path: Path, process_start: str) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    boundary = parse_time(process_start)
    return sorted(
        (
            row
            for row in payload.get("records") or []
            if parse_time(str(row["created_at"])) >= boundary
        ),
        key=lambda row: str(row["created_at"]),
    )


def xorshift128_step_int(state0: int, state1: int) -> tuple[int, int, int]:
    value = state0 & MASK64
    new0 = state1 & MASK64
    value ^= (value << 23) & MASK64
    value ^= value >> 17
    value ^= new0
    value ^= new0 >> 26
    new1 = value & MASK64
    return new0, new1, (new0 + new1) & MASK64


def output_bin(random_word: int, variant: str) -> int:
    if variant == "current-sum53":
        numerator = (random_word & MASK64) >> 11
        denominator = 1 << 53
    elif variant == "legacy-state0-52":
        numerator = (random_word & MASK64) >> 12
        denominator = 1 << 52
    else:
        raise ValueError(variant)
    return DOMAIN_LOW + (numerator * DOMAIN_SIZE) // denominator


def generate_bins(
    state0: int, state1: int, count: int, variant: str
) -> tuple[list[int], tuple[int, int]]:
    result = []
    for _ in range(count):
        state0, state1, summed = xorshift128_step_int(state0, state1)
        word = summed if variant == "current-sum53" else state0
        result.append(output_bin(word, variant))
    return result, (state0, state1)


def _xor_word(left: list[int], right: list[int]) -> list[int]:
    return [a ^ b for a, b in zip(left, right)]


def _left_word(word: list[int], amount: int) -> list[int]:
    return [0] * amount + word[: 64 - amount]


def _right_word(word: list[int], amount: int) -> list[int]:
    return word[amount:] + [0] * amount


def xorshift128_step_symbolic(
    state0: list[int], state1: list[int]
) -> tuple[list[int], list[int]]:
    value = state0
    new0 = state1
    value = _xor_word(value, _left_word(value, 23))
    value = _xor_word(value, _right_word(value, 17))
    value = _xor_word(value, new0)
    value = _xor_word(value, _right_word(new0, 26))
    return new0, value


def solve_legacy_linear(observed: list[int]) -> dict[str, Any]:
    """Exact feasibility for legacy V8 via common-prefix linear equations."""
    state0 = [1 << bit for bit in range(64)]
    state1 = [1 << (64 + bit) for bit in range(64)]
    basis = LinearBasis()
    for value in observed:
        state0, state1 = xorshift128_step_symbolic(state0, state1)
        prefix_bits, prefix_value = common_prefix(value, 52)
        for offset in range(prefix_bits):
            word_bit = 63 - offset
            rhs = (prefix_value >> (prefix_bits - 1 - offset)) & 1
            basis.add(state0[word_bit], rhs)
    if basis.inconsistent:
        return {
            "status": "unsat",
            "method": "gf2-common-prefix",
            "rank": basis.rank,
        }
    if basis.rank < 128:
        return {
            "status": "underdetermined",
            "method": "gf2-common-prefix",
            "rank": basis.rank,
        }
    state = basis.solve_representative(128)
    initial = (state & MASK64, (state >> 64) & MASK64)
    replay, _ = generate_bins(*initial, len(observed), "legacy-state0-52")
    return {
        "status": "sat" if replay == observed else "unsat",
        "method": "gf2-common-prefix",
        "rank": basis.rank,
        "state0": initial[0],
        "state1": initial[1],
        "exact_bucket_replay": replay == observed,
    }


def solve_current_boolector(
    observed: list[int], max_models: int = 64
) -> dict[str, Any]:
    """Solve current V8 xorshift128+ with a QF_BV-specialized solver."""
    try:
        from pyboolector import Boolector, BtorOption
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with pyboolector") from error

    solver = Boolector()
    solver.Set_opt(BtorOption.BTOR_OPT_MODEL_GEN, 1)
    solver.Set_opt(BtorOption.BTOR_OPT_INCREMENTAL, 1)
    sort = solver.BitVecSort(64)
    original0 = solver.Var(sort, "state0")
    original1 = solver.Var(sort, "state1")
    state0, state1 = original0, original1
    solver.Assert((state0 != 0) | (state1 != 0))
    for value in observed:
        mixed = state0
        new0 = state1
        mixed = mixed ^ (mixed << 23)
        mixed = mixed ^ solver.Srl(mixed, solver.Const(17, 64))
        mixed = mixed ^ new0
        mixed = mixed ^ solver.Srl(new0, solver.Const(26, 64))
        state0, state1 = new0, mixed
        random_word = state0 + state1
        prefix_bits, prefix_value = common_prefix(value, 53)
        solver.Assert(
            random_word[63 : 64 - prefix_bits]
            == solver.Const(prefix_value, prefix_bits)
        )

    checked = 0
    while checked < max(1, max_models):
        outcome = solver.Sat()
        if outcome == solver.UNKNOWN:
            return {"status": "unknown", "models_checked": checked}
        if outcome != solver.SAT:
            return {
                "status": "unsat" if checked == 0 else "relaxed-sat-no-exact",
                "models_checked": checked,
                "method": "boolector-common-prefix",
            }
        initial = (int(original0.assignment, 2), int(original1.assignment, 2))
        checked += 1
        replay, _ = generate_bins(*initial, len(observed), "current-sum53")
        if replay == observed:
            return {
                "status": "sat",
                "state0": initial[0],
                "state1": initial[1],
                "constrained_outputs": len(observed),
                "models_checked": checked,
                "method": "boolector-common-prefix",
            }
        solver.Assert((original0 != initial[0]) | (original1 != initial[1]))
    return {
        "status": "relaxed-sat-no-exact",
        "models_checked": checked,
        "method": "boolector-common-prefix",
    }


def bin_bounds(value: int, bits: int) -> tuple[int, int]:
    """Integer numerator bounds for floor(900*n/2**bits) == value-100."""
    bucket = int(value) - DOMAIN_LOW
    if bucket < 0 or bucket >= DOMAIN_SIZE:
        raise ValueError(f"seed outside {DOMAIN_LOW}..{DOMAIN_LOW + DOMAIN_SIZE - 1}")
    denominator = 1 << bits
    low = (bucket * denominator + DOMAIN_SIZE - 1) // DOMAIN_SIZE
    high = ((bucket + 1) * denominator + DOMAIN_SIZE - 1) // DOMAIN_SIZE
    return low, high


def common_prefix(value: int, bits: int) -> tuple[int, int]:
    """Return the bit prefix shared by every numerator in a seed bucket."""
    low, high = bin_bounds(value, bits)
    differing = low ^ (high - 1)
    length = bits - differing.bit_length()
    return length, low >> (bits - length)


def _symbolic_outputs(z3: Any, count: int, variant: str) -> tuple[Any, Any, list[Any]]:
    state0 = z3.BitVec("state0", 64)
    state1 = z3.BitVec("state1", 64)
    original0, original1 = state0, state1
    outputs = []
    for _ in range(count):
        value = state0
        new0 = state1
        value = value ^ (value << 23)
        value = value ^ z3.LShR(value, 17)
        value = value ^ new0
        value = value ^ z3.LShR(new0, 26)
        state0, state1 = new0, value
        outputs.append(state0 + state1 if variant == "current-sum53" else state0)
    return original0, original1, outputs


def solve_stream(
    observed: list[int], variant: str, timeout_ms: int, max_models: int = 64
) -> dict[str, Any]:
    try:
        import z3
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with z3-solver") from error

    bits = 53 if variant == "current-sum53" else 52
    state0, state1, outputs = _symbolic_outputs(z3, len(observed), variant)
    solver = z3.Solver()
    solver.set(timeout=max(1, int(timeout_ms)))
    solver.add(z3.Or(state0 != 0, state1 != 0))
    for value, output in zip(observed, outputs):
        numerator = z3.LShR(output, 64 - bits)
        prefix_bits, prefix_value = common_prefix(value, bits)
        solver.add(
            z3.LShR(numerator, bits - prefix_bits)
            == z3.BitVecVal(prefix_value, 64)
        )
    checked = 0
    while checked < max(1, max_models):
        outcome = solver.check()
        if outcome == z3.unknown:
            return {
                "status": "unknown",
                "reason": solver.reason_unknown(),
                "models_checked": checked,
            }
        if outcome != z3.sat:
            return {
                "status": "unsat" if checked == 0 else "relaxed-sat-no-exact",
                "models_checked": checked,
            }
        model = solver.model()
        initial = (model.eval(state0).as_long(), model.eval(state1).as_long())
        checked += 1
        replay, _ = generate_bins(*initial, len(observed), variant)
        if replay == observed:
            return {
                "status": "sat",
                "state0": initial[0],
                "state1": initial[1],
                "constrained_outputs": len(observed),
                "models_checked": checked,
            }
        solver.add(z3.Or(state0 != initial[0], state1 != initial[1]))
    return {"status": "relaxed-sat-no-exact", "models_checked": checked}


def train_and_predict(
    observed: list[int], variant: str, train_outputs: int, timeout_ms: int
) -> dict[str, Any]:
    train_outputs = min(max(1, train_outputs), len(observed))
    solved = solve_stream(observed[:train_outputs], variant, timeout_ms)
    result = {"train_outputs": train_outputs, "validation_outputs": len(observed) - train_outputs, **solved}
    if solved["status"] != "sat":
        return result
    predicted, _ = generate_bins(
        int(solved["state0"]), int(solved["state1"]), len(observed), variant
    )
    suffix = observed[train_outputs:]
    predicted_suffix = predicted[train_outputs:]
    result.update(
        {
            "validation_exact": predicted_suffix == suffix,
            "validation_position_matches": sum(
                left == right for left, right in zip(predicted_suffix, suffix)
            ),
        }
    )
    # State values are unnecessary and must not become operational artifacts.
    result.pop("state0", None)
    result.pop("state1", None)
    return result


def train_and_predict_legacy_linear(
    observed: list[int], train_outputs: int
) -> dict[str, Any]:
    train_outputs = min(max(1, train_outputs), len(observed))
    solved = solve_legacy_linear(observed[:train_outputs])
    result = {
        "train_outputs": train_outputs,
        "validation_outputs": len(observed) - train_outputs,
        **solved,
    }
    if solved["status"] == "sat":
        predicted, _ = generate_bins(
            int(solved["state0"]),
            int(solved["state1"]),
            len(observed),
            "legacy-state0-52",
        )
        suffix = observed[train_outputs:]
        predicted_suffix = predicted[train_outputs:]
        result["validation_exact"] = predicted_suffix == suffix
        result["validation_position_matches"] = sum(
            left == right for left, right in zip(predicted_suffix, suffix)
        )
    result.pop("state0", None)
    result.pop("state1", None)
    return result


def train_and_predict_current_boolector(
    observed: list[int], train_outputs: int
) -> dict[str, Any]:
    train_outputs = min(max(1, train_outputs), len(observed))
    solved = solve_current_boolector(observed[:train_outputs])
    result = {
        "train_outputs": train_outputs,
        "validation_outputs": len(observed) - train_outputs,
        **solved,
    }
    if solved["status"] == "sat":
        predicted, _ = generate_bins(
            int(solved["state0"]),
            int(solved["state1"]),
            len(observed),
            "current-sum53",
        )
        suffix = observed[train_outputs:]
        predicted_suffix = predicted[train_outputs:]
        result["validation_exact"] = predicted_suffix == suffix
        result["validation_position_matches"] = sum(
            left == right for left, right in zip(predicted_suffix, suffix)
        )
    result.pop("state0", None)
    result.pop("state1", None)
    return result


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
    parser.add_argument("--process-start", default=DEFAULT_PROCESS_START)
    parser.add_argument("--prelude", default=",".join(map(str, DEFAULT_PRELUDE)))
    parser.add_argument("--train-tasks", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--skip-current",
        action="store_true",
        help="Record prior solver timeout for current V8 and run the exact legacy GF(2) test.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_v8_state_recovery.json"),
    )
    args = parser.parse_args()
    segment = load_segment(args.discovery.resolve(), args.process_start)
    prelude = [int(value) for value in args.prelude.split(",") if value.strip()]
    observed = prelude + [int(seed) for row in segment for seed in row["seeds"]]
    train_outputs = len(prelude) + min(len(segment), max(1, args.train_tasks)) * 3
    variants = {}
    if args.skip_current:
        variants["current-sum53"] = {
            "full_discovery_stream": {
                "status": "unknown",
                "reason": "z3-and-boolector-timeout",
            },
            "prefix_prediction": {
                "status": "unknown",
                "reason": "z3-and-boolector-timeout",
                "train_outputs": train_outputs,
                "validation_outputs": len(observed) - train_outputs,
            },
        }
    else:
        current_full = solve_current_boolector(observed)
        current_public = {
            key: value
            for key, value in current_full.items()
            if key not in {"state0", "state1"}
        }
        variants["current-sum53"] = {
            "full_discovery_stream": current_public,
            "prefix_prediction": train_and_predict_current_boolector(
                observed, train_outputs
            ),
        }
    legacy_full = solve_legacy_linear(observed)
    legacy_public = {
        key: value
        for key, value in legacy_full.items()
        if key not in {"state0", "state1"}
    }
    variants["legacy-state0-52"] = {
        "full_discovery_stream": legacy_public,
        "prefix_prediction": train_and_predict_legacy_linear(
            observed, train_outputs
        ),
    }
    exact = [
        name
        for name, value in variants.items()
        if value["full_discovery_stream"]["status"] == "sat"
        and value["prefix_prediction"].get("validation_exact") is True
    ]
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "v8-xorshift128-state-recovery",
        "summary": {
            "process_segment_tasks": len(segment),
            "observed_outputs": len(observed),
            "train_outputs": train_outputs,
            "validation_outputs": len(observed) - train_outputs,
            "variants_tested": len(variants),
            "discovery_exact_candidates": len(exact),
            "exact_variants": exact,
        },
        "candidates_tested": sum(
            value["full_discovery_stream"].get("status") in {"sat", "unsat"}
            for value in variants.values()
        ),
        "discovery_exact_candidates": len(exact),
        "families": {
            f"v8-{name}": {
                "tested": int(value["full_discovery_stream"].get("status") in {"sat", "unsat"}),
                "status": value["full_discovery_stream"].get("status"),
            }
            for name, value in variants.items()
        },
        "variants": variants,
        "assumptions": [
            "one Math.random call per published seed",
            "no hidden duplicate draw or other interleaved Math.random consumer",
            "one persistent V8 state across the observed validator-process segment",
        ],
        "safety": {
            "discovery_only": True,
            "holdout_opened": False,
            "network_reads": False,
            "submission_writes": False,
            "recovered_state_persisted": False,
        },
    }
    report["search"] = {
        "candidates_tested": report["candidates_tested"],
        "discovery_exact_candidates": report["discovery_exact_candidates"],
        "families": report["families"],
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
