import pytest

cp_model = pytest.importorskip("ortools.sat.python.cp_model")

from tools.preseed_mt_cpsat_joint import SymbolicMtStream


def test_symbolic_mt_low_bits_match_fixed_numpy_state():
    import numpy as np

    rng = np.random.RandomState(12345)
    expected = [int(rng.randint(0, 2**32, dtype=np.uint32)) for _ in range(4)]
    state = rng.get_state()[1]
    # The state array exposed after four draws is the same post-twist array
    # whose words 0..3 produced ``expected``.
    model = cp_model.CpModel()
    symbolic = SymbolicMtStream(model, 4, exposed_bits=10)
    for word_index, word in enumerate(state):
        for bit in range(32):
            model.add(symbolic.initial_state[word_index][bit] == ((int(word) >> bit) & 1))
    for output_index, value in enumerate(expected):
        for bit in range(10):
            model.add(symbolic.raw_bits[output_index][bit] == ((value >> bit) & 1))
    solver = cp_model.CpSolver()
    assert solver.solve(model) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
