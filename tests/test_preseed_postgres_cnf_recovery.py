from tools.preseed_postgres_cnf_recovery import replay_bins, xor_to_cnf
from tools.preseed_postgres_prng_recovery import concrete_step


def test_replay_bins_uses_current_output_before_transition():
    output, _next0, _next1 = concrete_step(1, 2)
    expected = 100 + ((output >> 12) * 900) // (1 << 52)
    assert replay_bins(1, 2, 1) == [expected]


def test_xor_to_cnf_rejects_wrong_parity_assignments():
    clauses = xor_to_cnf([1, 2, 3], False)
    assert len(clauses) == 4
    assert [-1, 2, 3] in clauses
