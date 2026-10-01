from tools.preseed_mt_fixed_profile import (
    add_fixed_profile,
    add_hybrid_profile,
    add_rejected_profile_constraints,
    bind_state_shard,
    bind_fixed_accepted_draws,
    block_choice_slice,
    corridor_profile,
    concrete_raw_values_from_model,
    next_excluded_discovery,
    parse_fixed_trace_shifts,
    parse_fixed_accepted_draws,
    parse_relaxed_rejection_windows,
    prebind_fixed_trace_shifts,
    profile_raw_indices,
    rejected_profile_violations,
    replay_choice_seed_prefix,
    replay_rejection_gaps,
    replay_bounded_draws_with_forbidden,
    singleton_trace_counterexample,
    singleton_trace_counterexample_detail,
    solve_with_deadline,
    solve_bit_assumption_shards,
    state_shard_variables,
    weak_compositions,
)


def test_choice_replay_matches_numpy_randomstate_stream_exactly():
    import numpy as np

    expected_rng = np.random.RandomState(123456789)
    expected = expected_rng.choice(
        np.arange(100, 1000), size=3, replace=False
    ).tolist()
    expected_following = expected_rng.randint(
        0, 2**32, size=16, dtype=np.uint32
    )

    raw_rng = np.random.RandomState(123456789)
    accepted = []
    for maximum in range(899, 0, -1):
        mask = (1 << maximum.bit_length()) - 1
        while True:
            raw = int(raw_rng.randint(0, 2**32, dtype=np.uint32))
            value = raw & mask
            if value <= maximum:
                accepted.append(value)
                break
    actual = replay_choice_seed_prefix(accepted, (0, 899), [0, 0, 0])
    actual_following = raw_rng.randint(0, 2**32, size=16, dtype=np.uint32)
    assert actual == expected
    assert np.array_equal(actual_following, expected_following)


def test_replay_choice_seed_prefix_maps_permutation_indices_to_public_domain():
    accepted = list(range(899, 0, -1))
    assert replay_choice_seed_prefix(accepted, (0, 899), [100, 101, 102]) == [
        100,
        101,
        102,
    ]


def test_parse_fixed_trace_shifts_builds_disjoint_round_map():
    assert parse_fixed_trace_shifts(["1:67:0", "1:77:5", "2:4:1"]) == {
        0: {67: 0, 77: 5},
        1: {4: 1},
    }


def test_parse_fixed_trace_shifts_rejects_conflicts_and_invalid_values():
    for values in (["1:2"], ["0:2:1"], ["1:2:-1"], ["1:2:0", "1:2:1"]):
        try:
            parse_fixed_trace_shifts(values)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid fixed trace shard accepted: {values}")


def test_fixed_accepted_draw_parser_and_binder_form_exact_shards():
    pycryptosat = __import__("pycryptosat")
    from tools.preseed_mt_xorsat_joint import Cnf

    assert parse_fixed_accepted_draws(["3:2", "9:7", "3:2"]) == {3: 2, 9: 7}
    for values in (["3"], ["-1:0"], ["1:-2"], ["3:2", "3:1"]):
        try:
            parse_fixed_accepted_draws(values)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid accepted draw shard accepted: {values}")

    solver = pycryptosat.Solver()
    cnf = Cnf(solver)
    draws = [[cnf.new(), cnf.new()], [cnf.new(), cnf.new()]]
    bound = set()
    assert bind_fixed_accepted_draws(cnf, draws, {1: 2, 3: 1}, bound) == 1
    assert bound == {1}
    cnf.add([draws[1][0]])  # Fixed value 2 requires this low bit to be false.
    assert solver.solve()[0] is False


def test_relaxed_rejection_windows_and_compositions_are_fail_closed():
    assert parse_relaxed_rejection_windows(["4:6", "1:3"]) == [(1, 3), (4, 6)]
    assert weak_compositions(2, 2, 3) == [(0, 2), (1, 1), (2, 0)]
    for values in (["3:3"], ["-1:2"], ["1"], ["1:4", "3:5"]):
        try:
            parse_relaxed_rejection_windows(values)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid relaxed window accepted: {values}")
    try:
        weak_compositions(4, 3, 2)
    except ValueError:
        pass
    else:
        raise AssertionError("alternative cap was not enforced")


def test_hybrid_profile_selects_a_valid_within_window_rejection_path():
    pycryptosat = __import__("pycryptosat")
    from tools.preseed_mt_xorsat_joint import Cnf

    solver = pycryptosat.Solver()
    cnf = Cnf(solver)
    raw_bits = [[cnf.new(), cnf.new()] for _ in range(3)]
    # raw values 3,1,2 force gaps (1,0) for maxima (2,2).
    for bits, value in zip(raw_bits, [3, 1, 2]):
        for offset, variable in enumerate(bits):
            cnf.add([variable if (value >> offset) & 1 else -variable])
    accepted, metadata = add_hybrid_profile(
        cnf,
        raw_bits,
        [2, 2],
        [1, 2],
        [1, 0],
        {},
        [(0, 2)],
        draw_stop=2,
        max_alternatives=8,
    )
    assert len(accepted) == 2
    assert metadata == [{"start": 0, "stop": 2, "alternatives": 2}]
    assert solver.solve()[0] is True
    assert replay_rejection_gaps([3, 1, 2], [2, 2], {}) == [1, 0]


def test_fixed_trace_shift_is_bound_before_first_solve():
    pycryptosat = __import__("pycryptosat")
    from tools.preseed_mt_xorsat_joint import Cnf

    row = {
        "initial_uids": [0, 1],
        "ordered_uid_domains": ["g0"],
        "uid_domains": {"g0": [0]},
    }

    def solve(choice: int) -> tuple[bool, tuple[int, int, int]]:
        solver = pycryptosat.Solver()
        cnf = Cnf(solver)
        choice_bit = cnf.new()
        cnf.add([choice_bit if choice else -choice_bit])
        statistics = prebind_fixed_trace_shifts(
            cnf,
            [[choice_bit]],
            [row],
            [(0, 1)],
            {0: {0: 0}},
            {},
            {},
        )
        return bool(solver.solve()[0]), statistics

    # j=1 leaves UID 0 at final position 0; j=0 swaps it to position 1.
    assert solve(1) == (True, (1, 1, 0))
    assert solve(0) == (False, (1, 1, 0))


def test_expired_shared_deadline_never_starts_another_solver_call():
    class NeverCalled:
        def solve(self, **_kwargs):
            raise AssertionError("expired deadline must not call solver")

    assert solve_with_deadline(NeverCalled(), 0.0) == (None, None)


def test_bit_assumption_shards_find_and_pin_the_sat_partition():
    pycryptosat = __import__("pycryptosat")
    from tools.preseed_mt_xorsat_joint import Cnf

    solver = pycryptosat.Solver()
    cnf = Cnf(solver)
    bits = [cnf.new(), cnf.new(), cnf.new()]
    # High bits 01, so the complete two-bit scan must find pattern one.
    cnf.add([bits[1]])
    cnf.add([-bits[2]])
    satisfiable, model, pattern, records = solve_bit_assumption_shards(
        solver,
        bits,
        2,
        deadline=None,
        per_shard_time_limit=1.0,
    )
    assert satisfiable is True
    assert model is not None
    assert pattern == 1
    assert [row["status"] for row in records] == ["unsat", "sat"]
    # The winning assumptions were promoted to permanent unit clauses.
    assert solver.solve(assumptions=[-bits[1]])[0] is False


def test_rejected_profile_violations_detects_only_words_that_would_accept():
    # max=2 uses a two-bit mask. Zero would be accepted and therefore cannot
    # occupy the fixed path's rejected slot; three is genuinely rejected.
    assert rejected_profile_violations([0, 2], [2], [1], {}) == [(0, 0)]
    assert rejected_profile_violations([3, 2], [2], [1], {}) == []
    # A repeated unique value is also a valid reason to reject the word.
    assert rejected_profile_violations([1, 2], [3], [1], {0: [1]}) == []


def test_lazy_rejection_clause_excludes_a_concrete_accepting_word():
    pycryptosat = __import__("pycryptosat")
    from tools.preseed_mt_xorsat_joint import Cnf

    solver = pycryptosat.Solver()
    cnf = Cnf(solver)
    raw_bits = [[cnf.new(), cnf.new()]]
    cnf.add([-raw_bits[0][0]])
    cnf.add([-raw_bits[0][1]])
    assert solver.solve()[0] is True
    assert add_rejected_profile_constraints(cnf, raw_bits, [2], {}, [(0, 0)]) == 1
    assert solver.solve()[0] is False


def test_next_excluded_discovery_selects_first_row_outside_fit():
    discovery = [{"task_id": "a"}, {"task_id": "b"}, {"task_id": "c"}]
    assert next_excluded_discovery(discovery[:2], discovery) == {"task_id": "c"}
    assert next_excluded_discovery(discovery, discovery) is None


def test_profile_raw_indices_tracks_rejections_before_acceptance():
    accepted, rejected = profile_raw_indices([0, 2, 1])
    assert accepted == [0, 3, 5]
    assert rejected == [[], [1, 2], [4]]


def test_state_shards_are_disjoint_and_cover_selected_bits():
    pycryptosat = __import__("pycryptosat")
    from tools.preseed_mt_xorsat_joint import Cnf, STATE_BITS

    variables = state_shard_variables(3)
    assert variables == [1, 1 + STATE_BITS // 3, 1 + (2 * STATE_BITS) // 3]
    models = set()
    for index in range(8):
        solver = pycryptosat.Solver()
        cnf = Cnf(solver)
        assert bind_state_shard(cnf, 3, index) == variables
        satisfiable, model = solver.solve()
        assert satisfiable is True
        models.add(tuple(bool(model[variable]) for variable in variables))
    assert len(models) == 8


def test_fixed_profile_slice_checks_only_selected_raw_prefix():
    pycryptosat = __import__("pycryptosat")
    from tools.preseed_mt_xorsat_joint import Cnf

    cnf = Cnf(pycryptosat.Solver())
    raw_bits = [[cnf.new(), cnf.new()]]
    accepted = add_fixed_profile(
        cnf,
        raw_bits,
        [3, 3],
        [None, None],
        [0, 10],
        {},
        draw_stop=1,
    )
    assert len(accepted) == 1


def test_fixed_profile_can_omit_rejected_inequalities_for_diagnostics():
    pycryptosat = __import__("pycryptosat")
    from tools.preseed_mt_xorsat_joint import Cnf

    strict_solver = pycryptosat.Solver()
    strict = Cnf(strict_solver)
    # The profile says raw[0] is rejected for maximum 2, but pin it to 0,
    # which is valid.  Strict mode must reject this contradictory trajectory.
    strict_raw = [[strict.new(), strict.new()], [strict.new(), strict.new()]]
    strict.add([-strict_raw[0][0]])
    strict.add([-strict_raw[0][1]])
    add_fixed_profile(strict, strict_raw, [2], [1], [1], {})
    assert strict_solver.solve()[0] is False

    relaxed_solver = pycryptosat.Solver()
    relaxed = Cnf(relaxed_solver)
    relaxed_raw = [[relaxed.new(), relaxed.new()], [relaxed.new(), relaxed.new()]]
    relaxed.add([-relaxed_raw[0][0]])
    relaxed.add([-relaxed_raw[0][1]])
    add_fixed_profile(
        relaxed,
        relaxed_raw,
        [2],
        [1],
        [1],
        {},
        enforce_rejected=False,
    )
    assert relaxed_solver.solve()[0] is True


def test_fixed_profile_single_validity_constraint_bounds_unknown_acceptance():
    pycryptosat = __import__("pycryptosat")
    from tools.preseed_mt_xorsat_joint import Cnf

    results = []
    for value in range(4):
        solver = pycryptosat.Solver()
        cnf = Cnf(solver)
        raw = [[cnf.new(), cnf.new()]]
        for bit, variable in enumerate(raw[0]):
            cnf.add([variable if (value >> bit) & 1 else -variable])
        add_fixed_profile(cnf, raw, [2], [None], [0], {})
        results.append(solver.solve()[0])
    assert results == [True, True, True, False]


def test_corridor_profile_requires_v2_exact_path():
    plan = {"corridors": [{"rank": 2, "rejection_gaps": [0, 1, 0]}]}
    assert corridor_profile(plan, 2) == [0, 1, 0]


def test_corridor_profile_rejects_checkpoint_only_plan():
    plan = {"corridors": [{"rank": 1, "checkpoints": {"10": [2, 4]}}]}
    try:
        corridor_profile(plan, 1)
    except ValueError as error:
        assert "regenerate" in str(error)
    else:
        raise AssertionError("checkpoint-only plan must not become a fixed path")


def test_replay_retries_in_range_values_for_unique_triplet():
    # Draw 1 sees a repeated zero first.  It must be rejected even though it
    # is inside the ordinary 0..3 rk_interval range.
    replay = replay_bounded_draws_with_forbidden(
        [0, 0, 2],
        [3, 3],
        {1: [0]},
    )
    assert replay == ([0, 2], 3, 1)


def test_singleton_counterexample_finds_out_of_range_uid_position():
    row = {
        "initial_domain_sequence": ["g0", "g1", "g2", "g3"],
        "initial_uids": [0, 1, 2, 3],
        "ordered_uid_domains": ["g0", "g1", "g2"],
        "uid_domains": {"g0": [0], "g1": [1], "g2": [2], "g3": [3]},
    }
    # Several singleton ranges fail; choose the latest observation so its
    # reverse trace can skip the largest provably irrelevant swap prefix.
    assert singleton_trace_counterexample(row, ["u3", "u2", "u0", "u1"]) == [2]


def test_singleton_counterexample_batches_latest_range_violations():
    row = {
        "initial_domain_sequence": ["g0", "g1", "g2", "g3", "g4"],
        "initial_uids": [0, 1, 2, 3, 4],
        "ordered_uid_domains": ["g0", "g1", "g2", "g3"],
        "uid_domains": {f"g{value}": [value] for value in range(5)},
    }
    assert singleton_trace_counterexample(
        row,
        ["u4", "u3", "u2", "u1", "u0"],
        max_positions=3,
    ) == [3, 1, 0]
    assert singleton_trace_counterexample_detail(
        row,
        ["u4", "u3", "u2", "u1", "u0"],
        max_positions=3,
    ) == ([3, 1, 0], "range")
    assert singleton_trace_counterexample_detail(
        row,
        ["u4", "u3", "u2", "u1", "u0"],
        max_positions=3,
        low_position_threshold=3,
        low_batch_size=1,
    ) == ([3], "range")


def test_counterexample_falls_through_to_ambiguous_group_domains():
    row = {
        "initial_domain_sequence": ["g0", "g1", "g0", "g2"],
        "initial_uids": [0, 1, 2, 3],
        "ordered_uid_domains": ["g0", "g1", "g0"],
        "uid_domains": {"g0": [0, 2], "g1": [1], "g2": [3]},
    }
    hint, kind = singleton_trace_counterexample_detail(
        row,
        ["g0", "g0", "u1", "g2"],
    )
    assert hint == [2]
    assert kind == "domain-range"


def test_concrete_raw_values_uses_symbolic_state_variable_numbering():
    from tools.preseed_mt19937_rank_audit import (
        SymbolicMT19937,
        evaluate_symbolic,
        state_to_integer,
    )

    words = [((index + 1) * 0x9E3779B9) & 0xFFFFFFFF for index in range(624)]
    state = state_to_integer(words)
    model = [None] + [bool((state >> bit) & 1) for bit in range(624 * 32)]
    concrete = concrete_raw_values_from_model(model, 3)
    symbolic = SymbolicMT19937(position=0)
    expected = [evaluate_symbolic(symbolic.next_word(), state) for _ in range(3)]
    assert concrete == expected


def test_choice_slice_block_excludes_only_current_slice_assignment():
    pycryptosat = __import__("pycryptosat")
    from tools.preseed_mt_xorsat_joint import Cnf

    solver = pycryptosat.Solver()
    cnf = Cnf(solver)
    choices = [[cnf.new()] for _ in range(4)]
    sat, model = solver.solve()
    assert sat is True
    # size=5 and p=3 inspects swaps 3..4, i.e. choice offsets 1 and 0.
    assert block_choice_slice(cnf, model, choices, 3) == 2
    sat, changed = solver.solve()
    assert sat is True
    assert any(
        bool(changed[value]) != (value < len(model) and bool(model[value]))
        for value in (choices[0][0], choices[1][0])
    )
