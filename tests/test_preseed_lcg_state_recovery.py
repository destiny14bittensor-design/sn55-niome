import pytest

pytest.importorskip("z3")

from tools.preseed_lcg_state_recovery import SPECS, generate_lcg, solve_lcg


def test_direct_state_solver_recovers_small_lcg_stream_without_persisting_state():
    spec = next(item for item in SPECS if item.name == "ansi31-high15")
    observed = generate_lcg(0x1234_5678, 10, spec)
    result = solve_lcg(observed, spec, timeout_ms=30_000)
    assert result["status"] == "sat"
    assert result["exact_bucket_replay"] is True
    assert "state" not in result
