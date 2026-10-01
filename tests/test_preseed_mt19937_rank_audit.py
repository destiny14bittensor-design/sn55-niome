from __future__ import annotations

import numpy as np

from tools.preseed_mt19937_rank_audit import (
    LinearBasis,
    SymbolicMT19937,
    evaluate_symbolic,
    interval_mask,
    raw_uint32,
    state_to_integer,
    traced_shuffle,
)


def test_interval_masks_cover_maximum() -> None:
    assert interval_mask(1) == 1
    assert interval_mask(2) == 3
    assert interval_mask(127) == 127
    assert interval_mask(128) == 255


def test_traced_shuffle_matches_numpy() -> None:
    expected = np.arange(256)
    native = np.random.RandomState(42)
    traced = np.random.RandomState(42)
    native.shuffle(expected)
    actual, trace = traced_shuffle(traced, 256)
    assert actual == expected.tolist()
    assert len(trace) > 255
    assert sum(accepted for _raw, accepted, _bits in trace) == 255


def test_linear_basis_solves_full_system() -> None:
    basis = LinearBasis()
    # x2=1, x2+x1=0, x1+x0=1 -> 110b
    basis.add(0b100, 1)
    basis.add(0b110, 0)
    basis.add(0b011, 1)
    assert basis.rank == 3
    assert basis.solve_full(3) == 0b110


def test_linear_basis_representative_sets_free_bits_to_zero() -> None:
    basis = LinearBasis()
    basis.add(0b110, 0)
    basis.add(0b011, 1)
    solution = basis.solve_representative(3)
    assert solution == 0b110
    assert solution & 1 == 0


def test_symbolic_mt_matches_multiple_twists() -> None:
    rng = np.random.RandomState(42)
    raw_uint32(rng)
    state = rng.get_state()
    symbolic = SymbolicMT19937(position=int(state[2]))
    state_value = state_to_integer(state[1])
    for _ in range(1_500):
        assert evaluate_symbolic(symbolic.next_word(), state_value) == raw_uint32(rng)
