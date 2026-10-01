#!/usr/bin/env python3
"""Audit whether NumPy shuffle outputs contain enough linear MT19937 evidence.

This is a *synthetic identifiability* test, not a recovery claim about the
validator.  It recreates NumPy RandomState's ``rk_interval`` exactly, records
which raw words were accepted or rejected, and feeds only the known low bits
of accepted words into a GF(2) model of MT19937.  If the synthetic state can be
recovered and future words predicted, the remaining real-data problem is
localized to permutation ambiguity and unknown rejection alignment.

No Holdout seed labels are read by this tool.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

import numpy as np


N = 624
M = 397
WORD_BITS = 32
STATE_BITS = N * WORD_BITS
# MT19937 stores 19,968 bits but its characteristic polynomial has degree
# 19,937.  Thirty-one storage directions are observationally redundant.
EFFECTIVE_STATE_BITS = 19937
MATRIX_A = 0x9908B0DF
UPPER_MASK = 0x80000000
LOWER_MASK = 0x7FFFFFFF


def interval_mask(maximum: int) -> int:
    if maximum < 0 or maximum > 0xFFFFFFFF:
        raise ValueError("maximum must fit uint32")
    mask = int(maximum)
    mask |= mask >> 1
    mask |= mask >> 2
    mask |= mask >> 4
    mask |= mask >> 8
    mask |= mask >> 16
    return mask


def raw_uint32(rng: np.random.RandomState) -> int:
    return int(rng.randint(0, 2**32, dtype=np.uint32))


def traced_interval(
    rng: np.random.RandomState, maximum: int
) -> tuple[int, list[tuple[int, bool, int]]]:
    """Return NumPy's bounded value plus (raw, accepted, exposed-bit-count)."""
    mask = interval_mask(maximum)
    bits = mask.bit_length()
    trace: list[tuple[int, bool, int]] = []
    while True:
        raw = raw_uint32(rng)
        value = raw & mask
        accepted = value <= maximum
        trace.append((raw, accepted, bits))
        if accepted:
            return value, trace


def traced_shuffle(
    rng: np.random.RandomState, size: int
) -> tuple[list[int], list[tuple[int, bool, int]]]:
    values = list(range(size))
    trace: list[tuple[int, bool, int]] = []
    for index in range(size - 1, 0, -1):
        choice, draws = traced_interval(rng, index)
        trace.extend(draws)
        values[index], values[choice] = values[choice], values[index]
    return values, trace


def _xor_words(left: list[int], right: list[int]) -> list[int]:
    return [a ^ b for a, b in zip(left, right)]


def _right_shift(word: list[int], amount: int) -> list[int]:
    return word[amount:] + [0] * amount


def _left_shift_mask(word: list[int], amount: int, mask: int) -> list[int]:
    return [
        word[bit - amount] if bit >= amount and ((mask >> bit) & 1) else 0
        for bit in range(WORD_BITS)
    ]


def temper_symbolic(word: list[int]) -> list[int]:
    value = _xor_words(word, _right_shift(word, 11))
    value = _xor_words(value, _left_shift_mask(value, 7, 0x9D2C5680))
    value = _xor_words(value, _left_shift_mask(value, 15, 0xEFC60000))
    return _xor_words(value, _right_shift(value, 18))


def twist_symbolic(state: list[list[int]]) -> list[list[int]]:
    # RandomState's legacy MT implementation twists in-place.  In the second
    # loop, ``index + M - N`` therefore refers to words already produced by the
    # first loop.  The final word also combines the old last word with the new
    # first word.  A modulo-old-state formula agrees only through word 226.
    result = list(state)

    def replace(index: int, source_index: int, following_index: int) -> None:
        following = result[following_index]
        mixed = following[:31] + [result[index][31]]
        shifted = mixed[1:] + [0]
        conditional = [mixed[0] if (MATRIX_A >> bit) & 1 else 0 for bit in range(32)]
        result[index] = _xor_words(
            result[source_index], _xor_words(shifted, conditional)
        )

    for index in range(N - M):
        replace(index, index + M, index + 1)
    for index in range(N - M, N - 1):
        replace(index, index + M - N, index + 1)
    replace(N - 1, M - 1, 0)
    return result


class SymbolicMT19937:
    """MT19937 outputs as bit-vectors linear in one 19968-bit state."""

    def __init__(self, position: int = 0) -> None:
        if position < 0 or position > N:
            raise ValueError("invalid MT position")
        self.state = [
            [1 << (word * WORD_BITS + bit) for bit in range(WORD_BITS)]
            for word in range(N)
        ]
        self.position = position

    def next_word(self) -> list[int]:
        if self.position >= N:
            self.state = twist_symbolic(self.state)
            self.position = 0
        result = temper_symbolic(self.state[self.position])
        self.position += 1
        return result


class LinearBasis:
    """Incremental GF(2) elimination with RHS values."""

    def __init__(self) -> None:
        self.rows: dict[int, tuple[int, int]] = {}
        self.inconsistent = False

    @property
    def rank(self) -> int:
        return len(self.rows)

    def add(self, coefficients: int, rhs: int) -> bool:
        row = int(coefficients)
        value = int(rhs) & 1
        while row:
            pivot = row.bit_length() - 1
            known = self.rows.get(pivot)
            if known is None:
                self.rows[pivot] = (row, value)
                return True
            row ^= known[0]
            value ^= known[1]
        if value:
            self.inconsistent = True
        return False

    def solve_full(self, variables: int) -> int:
        if self.inconsistent or self.rank != variables:
            raise ValueError("basis is not a consistent full-rank system")
        solution = 0
        for pivot in range(variables):
            row, rhs = self.rows[pivot]
            lower = row & ((1 << pivot) - 1)
            bit = rhs ^ ((lower & solution).bit_count() & 1)
            if bit:
                solution |= 1 << pivot
        return solution

    def solve_representative(self, variables: int) -> int:
        """Choose zero for free variables and solve all pivot variables."""
        if self.inconsistent:
            raise ValueError("basis is inconsistent")
        solution = 0
        for bit_index in range(variables):
            known = self.rows.get(bit_index)
            if known is None:
                continue
            row, rhs = known
            lower = row & ((1 << bit_index) - 1)
            bit = rhs ^ ((lower & solution).bit_count() & 1)
            if bit:
                solution |= 1 << bit_index
        return solution


def state_to_integer(words: Iterable[int]) -> int:
    result = 0
    for word_index, word in enumerate(words):
        for bit in range(WORD_BITS):
            if (int(word) >> bit) & 1:
                result |= 1 << (word_index * WORD_BITS + bit)
    return result


def evaluate_symbolic(word: list[int], state: int) -> int:
    value = 0
    for bit, coefficients in enumerate(word):
        if (coefficients & state).bit_count() & 1:
            value |= 1 << bit
    return value


def run_synthetic_audit(
    shuffle_sizes: list[int], seed: int, max_rounds: int | None = None
) -> dict[str, Any]:
    rng = np.random.RandomState(seed & 0xFFFFFFFF)
    # Advance once so get_state exposes the already-twisted state at position 1.
    first_word = raw_uint32(rng)
    state_info = rng.get_state()
    initial_words = [int(value) for value in state_info[1]]
    initial_position = int(state_info[2])
    symbolic = SymbolicMT19937(position=initial_position)
    basis = LinearBasis()
    raw_draws = accepted = rejected = equations = 0
    rejection_runs: list[int] = []
    full_rank_round: int | None = None
    per_round = []
    for round_index, size in enumerate(shuffle_sizes, start=1):
        _permutation, trace = traced_shuffle(rng, int(size))
        rank_before = basis.rank
        for raw, was_accepted, exposed_bits in trace:
            output = symbolic.next_word()
            raw_draws += 1
            if was_accepted:
                accepted += 1
                for bit in range(exposed_bits):
                    basis.add(output[bit], (raw >> bit) & 1)
                    equations += 1
            else:
                rejected += 1
        current_rejections = 0
        for _raw, was_accepted, _exposed_bits in trace:
            if was_accepted:
                rejection_runs.append(current_rejections)
                current_rejections = 0
            else:
                current_rejections += 1
        per_round.append(
            {
                "round": round_index,
                "shuffle_size": int(size),
                "raw_draws": len(trace),
                "rank_added": basis.rank - rank_before,
                "cumulative_rank": basis.rank,
            }
        )
        if basis.rank == EFFECTIVE_STATE_BITS and full_rank_round is None:
            full_rank_round = round_index
            break
        if max_rounds is not None and round_index >= max_rounds:
            break

    recovered = False
    future_words_verified = 0
    state_storage_exact = False
    if basis.rank == EFFECTIVE_STATE_BITS and not basis.inconsistent:
        recovered_state = basis.solve_representative(STATE_BITS)
        state_storage_exact = recovered_state == state_to_integer(initial_words)
        for _ in range(32):
            expected = raw_uint32(rng)
            predicted = evaluate_symbolic(symbolic.next_word(), recovered_state)
            if expected != predicted:
                break
            future_words_verified += 1
        recovered = future_words_verified == 32
    return {
        "seed": int(seed),
        "first_discarded_word": first_word,
        "initial_position": initial_position,
        "rounds_consumed": len(per_round),
        "raw_draws": raw_draws,
        "accepted_draws": accepted,
        "rejected_draws": rejected,
        "max_rejections_before_accept": max(rejection_runs, default=0),
        "rejection_run_histogram": {
            str(length): rejection_runs.count(length)
            for length in sorted(set(rejection_runs))
        },
        "linear_equations": equations,
        "rank": basis.rank,
        "state_storage_bits": STATE_BITS,
        "effective_state_bits": EFFECTIVE_STATE_BITS,
        "full_rank_round": full_rank_round,
        "state_storage_exact": state_storage_exact,
        "synthetic_predictive_state_recovered": recovered,
        "future_words_verified": future_words_verified,
        "per_round": per_round,
    }


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
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260929)
    parser.add_argument("--max-rounds", type=int)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_mt19937_rank_audit.json"),
    )
    args = parser.parse_args()
    constraints = json.loads(args.constraints.read_text(encoding="utf-8"))
    sizes = [int(row["shuffle_size"]) for row in constraints.get("rounds") or []]
    result = run_synthetic_audit(sizes, args.seed, args.max_rounds)
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "synthetic-mt19937-linear-identifiability-audit",
        "summary": result,
        "interpretation": {
            "proves": (
                "Whether exact accepted low bits with known raw-draw alignment identify and "
                "predict a synthetic MT19937 stream under the real NumPy interval algorithm."
            ),
            "does_not_prove": (
                "The validator used this RNG/state, or that real partial permutations reveal "
                "the accepted choices and rejection positions."
            ),
            "remaining_gates": [
                "resolve repeated endpoint groups and missing permutation positions",
                "resolve unknown rejected raw-word positions",
                "verify shuffle and three seed draws share one RandomState",
                "predict sealed Discovery labels before opening Holdout",
            ],
        },
        "safety": {
            "synthetic_only": True,
            "holdout_opened": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "rank": result["rank"],
                "effective_state_bits": result["effective_state_bits"],
                "full_rank_round": result["full_rank_round"],
                "synthetic_predictive_state_recovered": result[
                    "synthetic_predictive_state_recovered"
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
