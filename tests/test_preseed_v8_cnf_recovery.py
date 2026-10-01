import pytest

Solver = pytest.importorskip("pycryptosat").Solver

from tools.preseed_v8_cnf_recovery import (
    CnfCircuit,
    greater_equal_constant,
    less_equal_constant,
    solve_incremental,
)
from tools.preseed_v8_state_recovery import generate_bins


def _assignment(bits, value):
    return [variable if (value >> index) & 1 else -variable for index, variable in enumerate(bits)]


def test_constant_comparators_accept_exact_unsigned_range():
    for width in (3, 4):
        for lower, upper in ((0, 0), (1, 3), (2, (1 << width) - 2)):
            circuit = CnfCircuit()
            bits = circuit.variables(width)
            clauses = [
                *circuit.clauses,
                *greater_equal_constant(bits, lower),
                *less_equal_constant(bits, upper),
            ]
            solver = Solver()
            solver.add_clauses(clauses)
            for value in range(1 << width):
                outcome, _ = solver.solve(assumptions=_assignment(bits, value))
                assert outcome is (lower <= value <= upper)


def test_cnf_recovers_synthetic_v8_stream_and_predicts_suffix():
    expected, _ = generate_bins(0x123456789ABCDEF0, 0x0FEDCBA987654321, 24, "current-sum53")
    result = solve_incremental(expected, start_outputs=8, time_limit=30.0)
    assert result["status"] == "sat"
    assert result["full_stream_exact"] is True
    assert result["constrained_outputs"] <= len(expected)
