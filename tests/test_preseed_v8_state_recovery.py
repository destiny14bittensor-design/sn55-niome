from tools.preseed_v8_state_recovery import (
    bin_bounds,
    common_prefix,
    generate_bins,
    output_bin,
    solve_legacy_linear,
    xorshift128_step_int,
)


def test_bin_bounds_match_integer_projection() -> None:
    denominator = 1 << 53
    for value in (100, 101, 417, 998, 999):
        low, high = bin_bounds(value, 53)
        assert 100 + low * 900 // denominator == value
        assert 100 + (high - 1) * 900 // denominator == value
        if low:
            assert 100 + (low - 1) * 900 // denominator < value


def test_common_prefix_is_shared_by_whole_bucket() -> None:
    for value in (100, 101, 417, 998, 999):
        low, high = bin_bounds(value, 53)
        length, prefix = common_prefix(value, 53)
        shift = 53 - length
        assert low >> shift == prefix
        assert (high - 1) >> shift == prefix


def test_integer_generator_is_deterministic_and_in_range() -> None:
    values, state = generate_bins(0x12345678, 0xABCDEF01, 30, "current-sum53")
    again, again_state = generate_bins(0x12345678, 0xABCDEF01, 30, "current-sum53")
    assert values == again
    assert state == again_state
    assert all(100 <= value <= 999 for value in values)


def test_legacy_projection_uses_new_state0() -> None:
    new0, _new1, _summed = xorshift128_step_int(7, 11)
    values, _ = generate_bins(7, 11, 1, "legacy-state0-52")
    assert values == [output_bin(new0, "legacy-state0-52")]


def test_legacy_linear_solver_recovers_synthetic_stream() -> None:
    observed, _ = generate_bins(
        0x123456789ABCDEF0,
        0x0FEDCBA987654321,
        30,
        "legacy-state0-52",
    )
    solved = solve_legacy_linear(observed)
    assert solved["status"] == "sat"
    assert solved["rank"] == 128
    replay, _ = generate_bins(
        solved["state0"], solved["state1"], len(observed), "legacy-state0-52"
    )
    assert replay == observed
