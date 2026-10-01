import pytest

from tools.preseed_mt_xorsat_joint import (
    Cnf,
    add_alignment_lattice,
    add_permutation_prefix,
    add_permutation_prefix_forward,
    add_permutation_target,
    add_permutation_target_forward,
    add_permutation_first_hop_guard,
    add_permutation_zero_target_unary,
    add_observed_domain_subsequence,
    add_observed_singleton_traces,
    add_shuffle_network,
    add_sparse_mt_raw_bits,
    build_draw_layout,
    build_draws,
    choice_domain_targets,
    checkpoint_value_order,
    checkpoint_profile_order,
    checkpoint_profile_count,
    interval_bits,
    is_exact_observed_subsequence,
    matches_task_selector,
    parse_fixed_accepted_draws,
    replay_bounded_draws,
    replay_integer_unique_round,
    replay_shuffle,
    seed_duplicate_forbidden_values,
    shard_assumption_order,
    solve_checkpoint_assumptions,
    solve_checkpoint_profiles,
    statistical_rejection_checkpoints,
    profile_rejection_checkpoints,
    state_shard_variables,
    TEMPER_COEFFICIENT_MASKS,
    unsigned_at_most_literal,
)

from tools.preseed_mt19937_rank_audit import (
    STATE_BITS,
    SymbolicMT19937,
    evaluate_symbolic,
)


def test_interval_bits_matches_legacy_mask_width():
    assert interval_bits(1) == 1
    assert interval_bits(2) == 2
    assert interval_bits(255) == 8
    assert interval_bits(256) == 9
    assert interval_bits(899) == 10


def test_postsolve_round_replay_handles_mask_and_duplicate_rejections():
    # size 3 consumes maxima 2 then 1. The seed stream rejects 1000 (>899)
    # and a duplicate 7 before accepting three unique values.
    raw = [2, 1, 1000, 7, 7, 8, 9]
    replayed = replay_integer_unique_round(raw, 0, 3)
    assert replayed == ([2, 1], [107, 108, 109], 7, 2)


def test_build_draws_keeps_shuffle_then_seed_order():
    rows = [
        {
            "shuffle_size": 4,
            "discovery_seed_label": [101, 202, 303],
        }
    ]
    maxima, expected, slices = build_draws(rows, [654])
    assert maxima == [899, 3, 2, 1, 899, 899, 899]
    assert expected == [554, None, None, None, 1, 102, 203]
    assert slices == [(1, 4)]


def test_choice_layout_models_full_permutation_per_seed_event():
    rows = [{"shuffle_size": 4, "discovery_seed_label": [101, 202, 303]}]
    maxima, expected, shuffles, seeds = build_draw_layout(
        rows, [654, 347, 964], "choice-without-replacement"
    )
    assert maxima[:3] == [899, 898, 897]
    assert maxima[898] == 1
    assert shuffles == [(899, 902)]
    assert seeds == [(0, 899), (902, 1801)]
    assert all(value is None for value in expected)


def test_choice_layout_can_model_restart_prelude_without_a_task_row():
    maxima, expected, shuffles, seeds = build_draw_layout(
        [], [654, 347, 964], "choice-without-replacement"
    )
    assert len(maxima) == 899
    assert maxima[:2] == [899, 898]
    assert maxima[-1] == 1
    assert all(value is None for value in expected)
    assert shuffles == []
    assert seeds == [(0, 899)]


def test_profile_rejection_checkpoints_leave_within_window_paths_free():
    assert profile_rejection_checkpoints([0, 2, 1, 3, 0], 5, 2) == {
        2: (2, 2),
        4: (6, 6),
        5: (6, 6),
    }


def test_xorsat_state_shard_variables_are_evenly_spaced():
    assert state_shard_variables(2) == [1, 1 + STATE_BITS // 2]


def test_replay_bounded_draws_skips_masked_rejections():
    # maximum=2 uses mask 0b11, so raw 3 is rejected and raw 6 -> 2 accepted.
    replay = replay_bounded_draws([3, 6, 9], 0, [2, 1])
    assert replay == ([2, 1], 3, 1)


def test_replay_shuffle_and_subsequence_validation():
    shuffled = replay_shuffle(["a", "b", "c", "d"], [1, 0, 0])
    assert shuffled == ["d", "c", "a", "b"]
    assert is_exact_observed_subsequence(shuffled, ["d", "a", "b"])
    assert not is_exact_observed_subsequence(shuffled, ["a", "d", "b"])


def test_linear_domain_subsequence_accepts_exact_deletion_and_rejects_wrong_order():
    pycryptosat = __import__("pycryptosat")
    choices_values = [2, 1, 0, 0]
    initial = ["g0", "g1", "g0", "g3", "g4"]
    shuffled_positions = [int(value) for value in replay_shuffle(
        [str(value) for value in range(5)], choices_values
    )]
    shuffled = [initial[position] for position in shuffled_positions]
    observed = shuffled[:2] + shuffled[3:]

    def solve(target):
        solver = pycryptosat.Solver()
        cnf = Cnf(solver)
        choices = []
        for maximum, value in zip(range(4, 0, -1), choices_values):
            bits = [cnf.new() for _ in range(interval_bits(maximum))]
            for offset, bit in enumerate(bits):
                cnf.add([bit if (value >> offset) & 1 else -bit])
            choices.append(bits)
        output = add_shuffle_network(cnf, choices, initial)
        states, edges = add_observed_domain_subsequence(
            cnf, output, initial, target
        )
        assert states > 0 and edges > 0
        return solver.solve()[0]

    assert solve(observed) is True
    wrong = list(observed)
    wrong[0], wrong[-1] = wrong[-1], wrong[0]
    assert solve(wrong) is False


def test_linear_domain_subsequence_rejects_observed_multiset_overflow():
    pycryptosat = __import__("pycryptosat")
    solver = pycryptosat.Solver()
    cnf = Cnf(solver)
    choices = [[cnf.new() for _ in range(interval_bits(maximum))]
               for maximum in range(2, 0, -1)]
    output = add_shuffle_network(cnf, choices, ["a", "b", "c"])
    add_observed_domain_subsequence(cnf, output, ["a", "b", "c"], ["a", "a"])
    assert solver.solve()[0] is False


def test_task_selector_supports_exact_prefix_and_empty_default():
    task_id = "f05ef562-7cfb-4635-be86-148734f756df"
    assert matches_task_selector(task_id, [])
    assert matches_task_selector(task_id, [task_id])
    assert matches_task_selector(task_id, ["f05ef562"])
    assert not matches_task_selector(task_id, ["cb53c30c"])


def test_fixed_accepted_draw_parser_rejects_conflicts():
    assert parse_fixed_accepted_draws(["3:175", "4:10"]) == {3: 175, 4: 10}
    with pytest.raises(ValueError, match="conflicting"):
        parse_fixed_accepted_draws(["3:175", "3:28"])


def test_permutation_prefix_compact_trace_accepts_concrete_shuffle():
    pycryptosat = __import__("pycryptosat")
    choices_values = [2, 1, 0, 0]
    expected = [str(value) for value in replay_shuffle(
        [str(value) for value in range(5)], choices_values
    )[:3]]
    solver = pycryptosat.Solver()
    cnf = Cnf(solver)
    choices = []
    for maximum, value in zip(range(4, 0, -1), choices_values):
        bits = [cnf.new() for _ in range(interval_bits(maximum))]
        for offset, bit in enumerate(bits):
            cnf.add([bit if (value >> offset) & 1 else -bit])
        choices.append(bits)
    add_permutation_prefix(cnf, choices, [int(value) for value in expected])
    assert solver.solve()[0] is True


def test_forward_permutation_prefix_matches_concrete_shuffle_and_rejects_wrong_value():
    pycryptosat = __import__("pycryptosat")
    choices_values = [2, 1, 0, 0]
    expected = [int(value) for value in replay_shuffle(
        [str(value) for value in range(5)], choices_values
    )[:3]]

    def solve(targets):
        solver = pycryptosat.Solver()
        cnf = Cnf(solver)
        choices = []
        for maximum, value in zip(range(4, 0, -1), choices_values):
            bits = [cnf.new() for _ in range(interval_bits(maximum))]
            for offset, bit in enumerate(bits):
                cnf.add([bit if (value >> offset) & 1 else -bit])
            choices.append(bits)
        add_permutation_prefix_forward(cnf, choices, targets)
        return solver.solve()[0]

    assert solve(expected) is True
    wrong = list(expected)
    wrong[1] = next(value for value in range(5) if value not in expected)
    assert solve(wrong) is False


def test_single_permutation_targets_accumulate_without_rebinding_prior_positions():
    pycryptosat = __import__("pycryptosat")
    choices_values = [2, 1, 0, 0]
    expected = [int(value) for value in replay_shuffle(
        [str(value) for value in range(5)], choices_values
    )[:3]]

    for binder in (add_permutation_target, add_permutation_target_forward):
        solver = pycryptosat.Solver()
        cnf = Cnf(solver)
        choices = []
        for maximum, value in zip(range(4, 0, -1), choices_values):
            bits = [cnf.new() for _ in range(interval_bits(maximum))]
            for offset, bit in enumerate(bits):
                cnf.add([bit if (value >> offset) & 1 else -bit])
            choices.append(bits)
        for position, target in enumerate(expected):
            binder(cnf, choices, position, target)
            assert solver.solve()[0] is True
        wrong = next(value for value in range(5) if value not in expected)
        binder(cnf, choices, 1, wrong)
        assert solver.solve()[0] is False


def test_first_hop_guard_accepts_valid_chain_and_rejects_interference():
    pycryptosat = __import__("pycryptosat")

    def solve(values):
        solver = pycryptosat.Solver()
        cnf = Cnf(solver)
        choices = []
        for maximum, value in zip(range(4, 0, -1), values):
            bits = [cnf.new() for _ in range(interval_bits(maximum))]
            for offset, bit in enumerate(bits):
                cnf.add([bit if (value >> offset) & 1 else -bit])
            choices.append(bits)
        add_permutation_first_hop_guard(cnf, choices, 3)
        return solver.solve()[0]

    # i=3 sends value 3 to q=1; i=2 must not choose 1, and i=1 moves it to 0.
    assert solve([4, 1, 2, 0]) is True
    assert solve([4, 1, 1, 0]) is False


def test_unary_zero_target_matches_concrete_fisher_yates_outcome():
    pycryptosat = __import__("pycryptosat")

    def solve(values, target):
        solver = pycryptosat.Solver()
        cnf = Cnf(solver)
        choices = []
        for maximum, value in zip(range(4, 0, -1), values):
            bits = [cnf.new() for _ in range(interval_bits(maximum))]
            for offset, bit in enumerate(bits):
                cnf.add([bit if (value >> offset) & 1 else -bit])
            choices.append(bits)
        add_permutation_zero_target_unary(cnf, choices, target)
        return solver.solve()[0]

    values = [4, 1, 2, 0]
    final = [int(value) for value in replay_shuffle(
        [str(value) for value in range(5)], values
    )]
    assert solve(values, final[0]) is True
    assert solve(values, next(value for value in range(5) if value != final[0])) is False


def test_singleton_trace_accepts_incomplete_observed_shuffle():
    pycryptosat = __import__("pycryptosat")
    choices_values = [2, 1, 0, 0]
    shuffled = [int(value) for value in replay_shuffle(
        [str(value) for value in range(5)], choices_values
    )]
    solver = pycryptosat.Solver()
    cnf = Cnf(solver)
    choices = []
    for maximum, value in zip(range(4, 0, -1), choices_values):
        bits = [cnf.new() for _ in range(interval_bits(maximum))]
        for offset, bit in enumerate(bits):
            cnf.add([bit if (value >> offset) & 1 else -bit])
        choices.append(bits)
    observed = shuffled[:2] + shuffled[3:]
    row = {
        "initial_uids": list(range(5)),
        "ordered_uid_domains": [f"u{value}" for value in observed],
        "uid_domains": {f"u{value}": [value] for value in observed},
    }
    assert add_observed_singleton_traces(cnf, choices, row) == 4
    assert solver.solve()[0] is True


def test_domain_trace_accepts_repeated_ambiguous_group_subsequence():
    pycryptosat = __import__("pycryptosat")
    choices_values = [2, 1, 0, 0]
    shuffled = [int(value) for value in replay_shuffle(
        [str(value) for value in range(5)], choices_values
    )]
    groups = {0: "g0", 1: "g1", 2: "g0", 3: "g3", 4: "g4"}
    observed_uids = shuffled[:2] + shuffled[3:]
    solver = pycryptosat.Solver()
    cnf = Cnf(solver)
    choices = []
    for maximum, value in zip(range(4, 0, -1), choices_values):
        bits = [cnf.new() for _ in range(interval_bits(maximum))]
        for offset, bit in enumerate(bits):
            cnf.add([bit if (value >> offset) & 1 else -bit])
        choices.append(bits)
    row = {
        "initial_uids": list(range(5)),
        "ordered_uid_domains": [groups[value] for value in observed_uids],
        "uid_domains": {
            "g0": [0, 2],
            "g1": [1],
            "g3": [3],
            "g4": [4],
        },
    }
    assert add_observed_singleton_traces(
        cnf,
        choices,
        row,
        allow_ambiguous_domains=True,
    ) == len(observed_uids)
    assert solver.solve()[0] is True


def test_late_singleton_selection_builds_a_smaller_exact_trace_set():
    pycryptosat = __import__("pycryptosat")
    size = 12
    choices_values = [0] * (size - 1)
    shuffled = [int(value) for value in replay_shuffle(
        [str(value) for value in range(size)], choices_values
    )]
    row = {
        "initial_uids": list(range(size)),
        "ordered_uid_domains": [f"u{value}" for value in shuffled],
        "uid_domains": {f"u{value}": [value] for value in shuffled},
    }

    def build(selection):
        solver = pycryptosat.Solver()
        cnf = Cnf(solver)
        choices = []
        for maximum, value in zip(range(size - 1, 0, -1), choices_values):
            bits = [cnf.new() for _ in range(interval_bits(maximum))]
            for offset, bit in enumerate(bits):
                cnf.add([bit if (value >> offset) & 1 else -bit])
            choices.append(bits)
        assert add_observed_singleton_traces(
            cnf, choices, row, max_traces=3, selection=selection
        ) == 3
        assert solver.solve()[0] is True
        return cnf.next_variable - 1, cnf.clauses

    late = build("late")
    spread = build("spread")
    assert late[0] < spread[0]
    assert late[1] < spread[1]


def test_singleton_trace_cache_reuses_position_circuits_and_adds_pair_once():
    pycryptosat = __import__("pycryptosat")
    solver = pycryptosat.Solver()
    cnf = Cnf(solver)
    size = 6
    choices = [[cnf.new() for _ in range(interval_bits(maximum))]
               for maximum in range(size - 1, 0, -1)]
    row = {
        "initial_uids": list(range(size)),
        "ordered_uid_domains": [f"u{value}" for value in range(size - 1)],
        "uid_domains": {f"u{value}": [value] for value in range(size - 1)},
    }
    trace_cache = {}
    pair_cache = set()
    first_stats = {}
    assert add_observed_singleton_traces(
        cnf,
        choices,
        row,
        observed_positions={2},
        trace_cache=trace_cache,
        monotone_pair_cache=pair_cache,
        statistics=first_stats,
    ) == 1
    variables_after_first = cnf.next_variable
    second_stats = {}
    assert add_observed_singleton_traces(
        cnf,
        choices,
        row,
        observed_positions={2, 3},
        trace_cache=trace_cache,
        monotone_pair_cache=pair_cache,
        statistics=second_stats,
    ) == 1
    assert second_stats == {"traces_added": 1, "monotone_pairs_added": 1}
    assert cnf.next_variable > variables_after_first
    variables_after_second = cnf.next_variable
    third_stats = {}
    assert add_observed_singleton_traces(
        cnf,
        choices,
        row,
        observed_positions={2, 3},
        trace_cache=trace_cache,
        monotone_pair_cache=pair_cache,
        statistics=third_stats,
    ) == 0
    assert third_stats == {"traces_added": 0, "monotone_pairs_added": 0}
    assert cnf.next_variable == variables_after_second


def test_singleton_fixed_shift_is_an_exact_domain_shard():
    pycryptosat = __import__("pycryptosat")
    choices_values = [2, 1, 0, 0]
    shuffled = [int(value) for value in replay_shuffle(
        [str(value) for value in range(5)], choices_values
    )]
    observed = shuffled[:-1]
    row = {
        "initial_uids": list(range(5)),
        "ordered_uid_domains": [f"u{value}" for value in observed],
        "uid_domains": {f"u{value}": [value] for value in observed},
    }
    results = []
    for shift in (0, 1):
        solver = pycryptosat.Solver()
        cnf = Cnf(solver)
        choices = []
        for maximum, value in zip(range(4, 0, -1), choices_values):
            bits = [cnf.new() for _ in range(interval_bits(maximum))]
            for offset, bit in enumerate(bits):
                cnf.add([bit if (value >> offset) & 1 else -bit])
            choices.append(bits)
        add_observed_singleton_traces(
            cnf,
            choices,
            row,
            observed_positions={len(observed) - 1},
            fixed_shifts={len(observed) - 1: shift},
        )
        results.append(solver.solve()[0])
    assert results.count(True) == 1


def test_choice_domain_offset_applies_to_prelude_and_task_labels():
    # This guards the production mapping used by main(): 964 is a valid
    # published seed but not a valid zero-based index in a 900-item array.
    published = [[654, 347, 964], [491, 210, 379]]
    zero_based = [choice_domain_targets(triplet) for triplet in published]
    assert zero_based == [[554, 247, 864], [391, 110, 279]]
    assert all(0 <= value < 900 for triplet in zero_based for value in triplet)


def test_unsigned_comparator_and_lattice_replay_exact_rejection_path():
    pycryptosat = __import__("pycryptosat")
    solver = pycryptosat.Solver()
    cnf = Cnf(solver)
    raw_values = [3, 6, 9]
    raw_bits = []
    for value in raw_values:
        bits = [cnf.new() for _ in range(2)]
        for offset, bit in enumerate(bits):
            cnf.add([bit if (value >> offset) & 1 else -bit])
        raw_bits.append(bits)
    assert_literal = unsigned_at_most_literal(cnf, raw_bits[0], 2)
    cnf.add([-assert_literal])  # 3 > 2
    accepted, final, _layers = add_alignment_lattice(
        cnf,
        raw_bits,
        [2, 1],
        [2, 1],
        rejection_budget=1,
        rejection_checkpoints={2: (1, 1)},
        minimum_total_rejections=1,
    )
    cnf.add(list(final.values()))
    satisfiable, model = solver.solve()
    assert satisfiable is True
    assert [sum(bool(model[bit]) << index for index, bit in enumerate(bits)) for bits in accepted] == [2, 1]


def test_statistical_checkpoints_include_final_draw_and_expand_with_stream():
    short = statistical_rejection_checkpoints([899] * 64, interval=32, sigma=4)
    long = statistical_rejection_checkpoints([899] * 128, interval=32, sigma=4)
    assert set(short) == {32, 64}
    assert 128 in long
    assert long[128][1] >= short[64][1]


def test_checkpoint_value_order_is_mean_first_and_deterministic():
    values = [0, 1, 2, 3]
    # max=2 accepts 3/4, so one draw has expected 1/3 rejection.
    assert checkpoint_value_order([2], 1, values) == [0, 1, 2, 3]


def test_assumption_order_shards_are_disjoint_complete_and_mean_interleaved():
    ordered = list(range(11))
    shards = [shard_assumption_order(ordered, 3, index) for index in range(3)]
    assert shards == [[0, 3, 6, 9], [1, 4, 7, 10], [2, 5, 8]]
    assert sorted(value for shard in shards for value in shard) == ordered
    assert not (set(shards[0]) & set(shards[1]))


def test_assumption_order_shard_validation():
    with pytest.raises(ValueError, match="at least one"):
        shard_assumption_order([1], 0, 0)
    with pytest.raises(ValueError, match="within shard count"):
        shard_assumption_order([1], 2, 2)


def test_checkpoint_assumption_scan_reuses_solver_and_preserves_unknown_safety():
    pycryptosat = __import__("pycryptosat")
    solver = pycryptosat.Solver()
    solver.add_clause([1, 2])
    solver.add_clause([-1])
    status, model, records, complete = solve_checkpoint_assumptions(
        solver, {0: 1, 1: 2}, [0, 1], time_limit=1.0
    )
    assert status is True
    assert model[2] is True
    assert records == [
        {"cumulative_rejections": 0, "status": "unsat", "solve_seconds": records[0]["solve_seconds"]},
        {"cumulative_rejections": 1, "status": "sat", "solve_seconds": records[1]["solve_seconds"]},
    ]
    assert complete is False


def test_checkpoint_profile_scan_orders_joint_mean_and_finds_sat_pair():
    pycryptosat = __import__("pycryptosat")
    solver = pycryptosat.Solver()
    # Exactly state 2 at the first layer and state 4 at the second can hold.
    solver.add_clause([-1])
    solver.add_clause([2])
    solver.add_clause([-3])
    solver.add_clause([4])
    layers = {1: {0: 1, 1: 2}, 2: {1: 3, 2: 4}}
    profiles = checkpoint_profile_order([2, 2], layers)
    status, model, records, complete = solve_checkpoint_profiles(
        solver, layers, profiles, time_limit=1.0
    )
    assert status is True
    assert model[2] is True and model[4] is True
    assert records[-1]["checkpoint_rejections"] == {"1": 1, "2": 2}
    assert complete is False


def test_checkpoint_profile_order_uses_independent_increments():
    layers = {1: {0: 1, 1: 2}, 2: {0: 3, 1: 4, 2: 5}}
    profiles = checkpoint_profile_order([2, 2], layers)
    assert profiles[:3] == [(0, 0), (0, 1), (1, 1)]


def test_checkpoint_profile_top_k_matches_full_order_across_four_layers():
    layers = {
        1: {0: 1, 1: 2},
        2: {0: 3, 1: 4, 2: 5},
        3: {1: 6, 2: 7, 3: 8},
        4: {2: 9, 3: 10, 4: 11},
    }
    full = checkpoint_profile_order([2, 2, 2, 2], layers)
    assert checkpoint_profile_count(layers) == len(full)
    assert checkpoint_profile_order([2, 2, 2, 2], layers, limit=5) == full[:5]


def test_lattice_retries_duplicate_values_in_unique_seed_triplet():
    pycryptosat = __import__("pycryptosat")
    solver = pycryptosat.Solver()
    cnf = Cnf(solver)
    raw_values = [1, 1, 2]
    raw_bits = []
    for value in raw_values:
        bits = [cnf.new() for _ in range(2)]
        for offset, bit in enumerate(bits):
            cnf.add([bit if (value >> offset) & 1 else -bit])
        raw_bits.append(bits)
    forbidden = seed_duplicate_forbidden_values([(0, 2)], [1, 2])
    accepted, final, _layers = add_alignment_lattice(
        cnf,
        raw_bits,
        [3, 3],
        [1, 2],
        rejection_budget=1,
        rejection_checkpoints={2: (1, 1)},
        minimum_total_rejections=1,
        forbidden_values=forbidden,
    )
    cnf.add(list(final.values()))
    satisfiable, model = solver.solve()
    assert satisfiable is True
    values = [
        sum(bool(model[bit]) << index for index, bit in enumerate(bits))
        for bits in accepted
    ]
    assert values == [1, 2]


def test_sparse_mt_matches_dense_model_across_twist_boundary():
    pycryptosat = __import__("pycryptosat")
    solver = pycryptosat.Solver()
    cnf = Cnf(solver)
    sparse = add_sparse_mt_raw_bits(cnf, 2, exposed_bits=10, position=623)
    state = sum(
        1 << bit for bit in range(STATE_BITS) if (bit * 1103515245 + 12345) & 8
    )
    for bit in range(STATE_BITS):
        solver.add_clause([bit + 1 if (state >> bit) & 1 else -(bit + 1)])
    satisfiable, model = solver.solve()
    assert satisfiable is True
    symbolic = SymbolicMT19937(position=623)
    expected = [evaluate_symbolic(symbolic.next_word(), state) & 1023 for _ in range(2)]
    actual = [
        sum(bool(model[value]) << bit for bit, value in enumerate(word))
        for word in sparse
    ]
    assert actual == expected


def test_sparse_mt_stream_extends_without_rebuilding_prefix():
    pycryptosat = __import__("pycryptosat")
    from tools.preseed_mt_xorsat_joint import SparseMtCnfStream

    cnf = Cnf(pycryptosat.Solver())
    stream = SparseMtCnfStream(cnf)
    prefix = stream.ensure(10)
    prefix_ids = [list(bits) for bits in prefix]
    extended = stream.ensure(700)
    assert len(extended) == 700
    assert extended[:10] == prefix_ids
    assert stream.ensure(650) is extended


def test_direct_temper_matrix_has_nonzero_rows():
    assert len(TEMPER_COEFFICIENT_MASKS) == 32
    assert all(value != 0 for value in TEMPER_COEFFICIENT_MASKS)
