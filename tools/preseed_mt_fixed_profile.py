#!/usr/bin/env python3
"""Test exact rejection paths against one persistent NumPy MT19937 stream.

The broad alignment solvers spend most of their time choosing a raw index for
every bounded draw.  This shard fixes one empirically representative rejection
path, enforces every accepted and rejected masked value exactly, and combines
the public Discovery seed labels with public shuffle traces.  A SAT model is
then replayed against every selected public shuffle row before it can be
reported as a candidate.  Holdout/sealed seed labels are never opened or
predicted by this tool.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

import numpy as np

try:
    from tools.preseed_mt_cpsat_joint import atomic_json
    from tools.preseed_mt_xorsat_joint import (
        Cnf,
        STATE_BITS,
        SparseMtCnfStream,
        add_symbolic_raw_bits,
        add_choice_tuple_trie,
        add_permutation_prefix,
        add_permutation_prefix_forward,
        add_permutation_target,
        add_permutation_target_forward,
        add_permutation_first_hop_guard,
        add_permutation_zero_target_unary,
        add_observed_domain_subsequence,
        add_observed_singleton_traces,
        add_observed_subsequence,
        add_shuffle_network,
        build_draw_layout,
        choice_domain_targets,
        conjunction,
        interval_bits,
        is_exact_observed_subsequence,
        matches_task_selector,
        model_uint,
        replay_shuffle,
        seed_duplicate_forbidden_values,
        unsigned_at_most_literal,
    )
    from tools.preseed_shuffle_prefix_domains import uid_aware_sequences
except ModuleNotFoundError:
    from preseed_mt_cpsat_joint import atomic_json
    from preseed_mt_xorsat_joint import (
        Cnf,
        STATE_BITS,
        SparseMtCnfStream,
        add_symbolic_raw_bits,
        add_choice_tuple_trie,
        add_permutation_prefix,
        add_permutation_prefix_forward,
        add_permutation_target,
        add_permutation_target_forward,
        add_permutation_first_hop_guard,
        add_permutation_zero_target_unary,
        add_observed_domain_subsequence,
        add_observed_singleton_traces,
        add_observed_subsequence,
        add_shuffle_network,
        build_draw_layout,
        choice_domain_targets,
        conjunction,
        interval_bits,
        is_exact_observed_subsequence,
        matches_task_selector,
        model_uint,
        replay_shuffle,
        seed_duplicate_forbidden_values,
        unsigned_at_most_literal,
    )
    from preseed_shuffle_prefix_domains import uid_aware_sequences


def state_shard_variables(bits: int) -> list[int]:
    """Select deterministic, evenly spaced MT storage bits for exact sharding."""
    count = max(0, int(bits))
    if count > 20:
        raise ValueError("state sharding is capped at 20 bits")
    if not count:
        return []
    return [1 + (index * STATE_BITS) // count for index in range(count)]


def bind_state_shard(cnf: Cnf, bits: int, index: int) -> list[int]:
    """Bind one disjoint member of a complete 2**bits state partition."""
    variables = state_shard_variables(bits)
    if not 0 <= int(index) < (1 << len(variables)):
        raise ValueError("state shard index is outside the selected bit partition")
    for offset, variable in enumerate(variables):
        cnf.add([variable if (int(index) >> offset) & 1 else -variable])
    return variables


def parse_fixed_trace_shifts(values: list[str]) -> dict[int, dict[int, int]]:
    """Parse repeatable one-based ``round:position:shift`` trace shards."""
    parsed: dict[int, dict[int, int]] = {}
    for raw in values:
        parts = str(raw).split(":")
        if len(parts) != 3:
            raise ValueError("fixed trace shift must be ROUND:POSITION:SHIFT")
        round_number, position, shift = (int(part) for part in parts)
        if round_number < 1 or position < 0 or shift < 0:
            raise ValueError("fixed trace shift values are outside their valid range")
        row = parsed.setdefault(round_number - 1, {})
        if position in row and row[position] != shift:
            raise ValueError("fixed trace shift assigns two shifts to one position")
        row[position] = shift
    return parsed


def parse_fixed_accepted_draws(values: list[str]) -> dict[int, int]:
    """Parse repeatable zero-based ``DRAW:VALUE`` accepted-choice shards."""
    parsed: dict[int, int] = {}
    for raw in values:
        parts = str(raw).split(":")
        if len(parts) != 2:
            raise ValueError("fixed accepted draw must be DRAW:VALUE")
        draw, value = (int(part) for part in parts)
        if draw < 0 or value < 0:
            raise ValueError("fixed accepted draw values must be nonnegative")
        if draw in parsed and parsed[draw] != value:
            raise ValueError("fixed accepted draw has conflicting values")
        parsed[draw] = value
    return parsed


def bind_fixed_accepted_draws(
    cnf: Cnf,
    accepted_bits: list[list[int]],
    fixed_draws: dict[int, int],
    already_bound: set[int],
) -> int:
    """Bind every fixed draw currently present, retaining future draw shards."""
    added = 0
    for draw, value in sorted(fixed_draws.items()):
        if draw in already_bound or draw >= len(accepted_bits):
            continue
        for bit, variable in enumerate(accepted_bits[draw]):
            cnf.add([variable if (int(value) >> bit) & 1 else -variable])
        already_bound.add(draw)
        added += 1
    return added


def add_choice_seed_prefix(
    cnf: Cnf,
    choices: list[list[int]],
    labels: list[int],
    encoding: str,
) -> None:
    targets = choice_domain_targets(labels)
    if encoding == "reverse":
        add_permutation_prefix(cnf, choices, targets)
    elif encoding == "forward":
        add_permutation_prefix_forward(cnf, choices, targets)
    else:
        raise ValueError(f"unknown choice prefix encoding: {encoding}")


def add_choice_seed_target(
    cnf: Cnf,
    choices: list[list[int]],
    label: int,
    final_position: int,
    encoding: str,
) -> None:
    """Bind exactly one published choice seed position without duplication."""
    target = choice_domain_targets([label])[0]
    if encoding == "unary-zero":
        if int(final_position) != 0:
            raise ValueError("unary-zero encoding only supports final position zero")
        add_permutation_zero_target_unary(cnf, choices, target)
        return
    if encoding == "reverse":
        add_permutation_target(cnf, choices, final_position, target)
    elif encoding == "forward":
        add_permutation_target_forward(cnf, choices, final_position, target)
    else:
        raise ValueError(f"unknown choice prefix encoding: {encoding}")
    if int(final_position) == 0:
        add_permutation_first_hop_guard(cnf, choices, target)


def prebind_fixed_trace_shifts(
    cnf: Cnf,
    accepted_bits: list[list[int]],
    rows: list[dict[str, Any]],
    round_slices: list[tuple[int, int]],
    fixed_trace_shifts: dict[int, dict[int, int]],
    trace_caches: dict[int, dict[int, list[int]]],
    monotone_pair_caches: dict[int, set[tuple[int, int]]],
) -> tuple[int, int, int]:
    """Bind every fixed shift before the first solve.

    Fixed-shift shards are a complete partition only when the requested trace
    is present in the initial formula.  CEGAR used to add a missing trace only
    after the first model, so a timeout could incorrectly leave the shard
    untested.  Reuse all existing trace rows for the selected round here so the
    insertion shifts remain monotone around the newly fixed position as well.
    """
    traces_added = 0
    fixed_positions = 0
    monotone_pairs_added = 0
    for row_index, shifts in sorted(fixed_trace_shifts.items()):
        start, stop = round_slices[row_index]
        trace_cache = trace_caches.setdefault(row_index, {})
        requested_positions = set(shifts)
        observed_positions = set(trace_cache).union(requested_positions)
        statistics: dict[str, int] = {}
        added = add_observed_singleton_traces(
            cnf,
            accepted_bits[start:stop],
            rows[row_index],
            observed_positions=observed_positions,
            fixed_shifts=shifts,
            trace_cache=trace_cache,
            monotone_pair_cache=monotone_pair_caches.setdefault(row_index, set()),
            statistics=statistics,
            enforce_monotone=True,
            allow_ambiguous_domains=True,
        )
        traces_added += added
        fixed_positions += len(requested_positions)
        monotone_pairs_added += statistics.get("monotone_pairs_added", 0)
        missing_positions = requested_positions.difference(trace_cache)
        if missing_positions:
            raise ValueError(
                f"fixed trace positions could not be encoded: {sorted(missing_positions)}"
            )
    return traces_added, fixed_positions, monotone_pairs_added


def next_excluded_discovery(
    selected: list[dict[str, Any]], discovery: list[dict[str, Any]]
) -> dict[str, Any] | None:
    selected_ids = {str(row["task_id"]) for row in selected}
    return next(
        (row for row in discovery if str(row["task_id"]) not in selected_ids),
        None,
    )


def corridor_profile(plan: dict[str, Any], rank: int) -> list[int]:
    for item in plan.get("corridors") or []:
        if int(item.get("rank") or 0) == int(rank):
            gaps = [int(value) for value in item.get("rejection_gaps") or []]
            if not gaps:
                raise ValueError(
                    "corridor plan has no exact rejection path; regenerate it with v2"
                )
            return gaps
    raise ValueError(f"corridor rank {rank} is not present in the plan")


def profile_raw_indices(gaps: list[int]) -> tuple[list[int], list[list[int]]]:
    """Map per-accepted-draw gaps to accepted and rejected raw indices."""
    accepted: list[int] = []
    rejected: list[list[int]] = []
    cursor = 0
    for gap in gaps:
        if int(gap) < 0:
            raise ValueError("rejection gaps must be non-negative")
        rejected.append(list(range(cursor, cursor + int(gap))))
        cursor += int(gap)
        accepted.append(cursor)
        cursor += 1
    return accepted, rejected


def add_fixed_profile(
    cnf: Cnf,
    raw_bits: list[list[int]],
    maxima: list[int],
    expected: list[int | None],
    rejection_gaps: list[int],
    forbidden_values: dict[int, list[int]],
    *,
    draw_start: int = 0,
    draw_stop: int | None = None,
    enforce_rejected: bool = True,
) -> list[list[int]]:
    """Bind one exact rk_interval/unique-retry path to symbolic raw words."""
    if len(rejection_gaps) != len(maxima):
        raise ValueError("rejection path length does not match bounded draws")
    accepted_indices, rejected_indices = profile_raw_indices(rejection_gaps)
    start = max(0, int(draw_start))
    stop = len(maxima) if draw_stop is None else min(len(maxima), int(draw_stop))
    if start > stop:
        raise ValueError("draw_start must not exceed draw_stop")
    if stop > start and accepted_indices[stop - 1] >= len(raw_bits):
        raise ValueError("raw stream is shorter than the selected rejection-path slice")
    accepted_bits: list[list[int]] = []
    for draw in range(start, stop):
        maximum = maxima[draw]
        width = interval_bits(int(maximum))
        forbidden = sorted(set(int(value) for value in forbidden_values.get(draw) or []))

        def validity(raw_index: int) -> int:
            bits = raw_bits[raw_index][:width]
            conditions = [unsigned_at_most_literal(cnf, bits, int(maximum))]
            for value in forbidden:
                conditions.append(-cnf.exact_value(bits, value))
            return conjunction(cnf, conditions)

        if enforce_rejected:
            for raw_index in rejected_indices[draw]:
                cnf.add([-validity(raw_index)])
        raw_index = accepted_indices[draw]
        current = raw_bits[raw_index][:width]
        cnf.add([validity(raw_index)])
        value = expected[draw]
        if value is not None:
            for bit, variable in enumerate(current):
                cnf.add([variable if (int(value) >> bit) & 1 else -variable])
        accepted_bits.append(current)
    return accepted_bits


def parse_relaxed_rejection_windows(values: list[str]) -> list[tuple[int, int]]:
    """Parse repeatable zero-based half-open ``START:STOP`` draw windows."""
    windows: list[tuple[int, int]] = []
    for raw in values:
        parts = str(raw).split(":")
        if len(parts) != 2:
            raise ValueError("relaxed rejection window must be START:STOP")
        start, stop = (int(part) for part in parts)
        if start < 0 or stop <= start:
            raise ValueError("relaxed rejection window must be a nonempty positive range")
        windows.append((start, stop))
    windows.sort()
    for left, right in zip(windows, windows[1:]):
        if right[0] < left[1]:
            raise ValueError("relaxed rejection windows must not overlap")
    return windows


def weak_compositions(total: int, length: int, limit: int) -> list[tuple[int, ...]]:
    """Enumerate ordered nonnegative compositions with a fail-closed cap."""
    if total < 0 or length <= 0:
        raise ValueError("invalid weak-composition dimensions")
    cap = max(1, int(limit))
    rows: list[tuple[int, ...]] = []

    def visit(prefix: tuple[int, ...], remaining: int, slots: int) -> None:
        if len(rows) > cap:
            return
        if slots == 1:
            rows.append((*prefix, remaining))
            return
        for value in range(remaining + 1):
            visit((*prefix, value), remaining - value, slots - 1)
            if len(rows) > cap:
                return

    visit((), int(total), int(length))
    if len(rows) > cap:
        raise ValueError(
            f"relaxed rejection window exceeds alternative cap {cap}"
        )
    return rows


def add_relaxed_rejection_window(
    cnf: Cnf,
    raw_bits: list[list[int]],
    maxima: list[int],
    expected: list[int | None],
    rejection_gaps: list[int],
    forbidden_values: dict[int, list[int]],
    start: int,
    stop: int,
    *,
    max_alternatives: int,
) -> tuple[list[list[int]], int]:
    """Allow every rejection allocation with fixed cumulative window bounds.

    The total raw-word consumption is retained, so all exact-profile draws
    before and after the window keep their original raw indices.  A one-hot
    selector chooses one weak composition of the window's rejection total;
    accept/reject validity and accepted-bit multiplexing are conditional on it.
    """
    total = sum(int(value) for value in rejection_gaps[start:stop])
    alternatives = weak_compositions(total, stop - start, max_alternatives)
    selectors = [cnf.new() for _ in alternatives]
    cnf.add(selectors)
    cnf.at_most_one(selectors)
    accepted = [
        [cnf.new() for _ in range(interval_bits(int(maxima[draw])))]
        for draw in range(start, stop)
    ]
    raw_start = start + sum(int(value) for value in rejection_gaps[:start])
    raw_stop = stop + sum(int(value) for value in rejection_gaps[:stop])
    for selector, gaps in zip(selectors, alternatives):
        cursor = raw_start
        for local, gap in enumerate(gaps):
            draw = start + local
            maximum = int(maxima[draw])
            width = interval_bits(maximum)
            forbidden = sorted(
                set(int(value) for value in forbidden_values.get(draw) or [])
            )

            def validity(raw_index: int) -> int:
                bits = raw_bits[raw_index][:width]
                conditions = [unsigned_at_most_literal(cnf, bits, maximum)]
                for value in forbidden:
                    conditions.append(-cnf.exact_value(bits, value))
                return conjunction(cnf, conditions)

            for raw_index in range(cursor, cursor + int(gap)):
                cnf.add([-selector, -validity(raw_index)])
            cursor += int(gap)
            current = raw_bits[cursor][:width]
            cnf.add([-selector, validity(cursor)])
            for output, source in zip(accepted[local], current):
                cnf.imply_equal(selector, output, source)
            cursor += 1
        if cursor != raw_stop:
            raise AssertionError("relaxed rejection window changed raw consumption")
    for local, bits in enumerate(accepted):
        value = expected[start + local]
        if value is not None:
            for bit, variable in enumerate(bits):
                cnf.add([variable if (int(value) >> bit) & 1 else -variable])
    return accepted, len(alternatives)


def add_hybrid_profile(
    cnf: Cnf,
    raw_bits: list[list[int]],
    maxima: list[int],
    expected: list[int | None],
    rejection_gaps: list[int],
    forbidden_values: dict[int, list[int]],
    windows: list[tuple[int, int]],
    *,
    draw_stop: int,
    max_alternatives: int,
) -> tuple[list[list[int]], list[dict[str, int]]]:
    """Combine exact fixed-profile spans with independent relaxed windows."""
    accepted: list[list[int]] = []
    metadata: list[dict[str, int]] = []
    cursor = 0
    for start, stop in windows:
        accepted.extend(
            add_fixed_profile(
                cnf,
                raw_bits,
                maxima,
                expected,
                rejection_gaps,
                forbidden_values,
                draw_start=cursor,
                draw_stop=start,
            )
        )
        window_bits, alternatives = add_relaxed_rejection_window(
            cnf,
            raw_bits,
            maxima,
            expected,
            rejection_gaps,
            forbidden_values,
            start,
            stop,
            max_alternatives=max_alternatives,
        )
        accepted.extend(window_bits)
        metadata.append(
            {"start": start, "stop": stop, "alternatives": alternatives}
        )
        cursor = stop
    accepted.extend(
        add_fixed_profile(
            cnf,
            raw_bits,
            maxima,
            expected,
            rejection_gaps,
            forbidden_values,
            draw_start=cursor,
            draw_stop=draw_stop,
        )
    )
    return accepted, metadata


def rejected_profile_violations(
    raw_values: list[int],
    maxima: list[int],
    rejection_gaps: list[int],
    forbidden_values: dict[int, list[int]],
    *,
    draw_start: int = 0,
    draw_stop: int | None = None,
) -> list[tuple[int, int]]:
    """Return fixed-path rejected words that a concrete replay would accept.

    This is the exact oracle used by lazy rejection CEGAR. Accepted words are
    always range-bound by :func:`add_fixed_profile`; only rejected-word
    inequalities may be delayed.
    """
    if len(rejection_gaps) != len(maxima):
        raise ValueError("rejection path length does not match bounded draws")
    _accepted_indices, rejected_indices = profile_raw_indices(rejection_gaps)
    start = max(0, int(draw_start))
    stop = len(maxima) if draw_stop is None else min(len(maxima), int(draw_stop))
    violations: list[tuple[int, int]] = []
    for draw in range(start, stop):
        maximum = int(maxima[draw])
        mask = (1 << interval_bits(maximum)) - 1
        forbidden = set(int(value) for value in forbidden_values.get(draw) or [])
        for raw_index in rejected_indices[draw]:
            if raw_index >= len(raw_values):
                raise ValueError("raw stream is shorter than the rejection path")
            value = int(raw_values[raw_index]) & mask
            if value <= maximum and value not in forbidden:
                violations.append((draw, raw_index))
    return violations


def add_rejected_profile_constraints(
    cnf: Cnf,
    raw_bits: list[list[int]],
    maxima: list[int],
    forbidden_values: dict[int, list[int]],
    violations: list[tuple[int, int]],
) -> int:
    """Exclude concrete fixed-path rejection violations from future models."""
    added = 0
    for draw, raw_index in violations:
        maximum = int(maxima[int(draw)])
        width = interval_bits(maximum)
        bits = raw_bits[int(raw_index)][:width]
        conditions = [unsigned_at_most_literal(cnf, bits, maximum)]
        for value in sorted(
            set(int(value) for value in forbidden_values.get(int(draw)) or [])
        ):
            conditions.append(-cnf.exact_value(bits, value))
        cnf.add([-conjunction(cnf, conditions)])
        added += 1
    return added


def solve_with_deadline(
    solver: Any, deadline: float | None
) -> tuple[bool | None, list[Any] | None]:
    """Run one solve without allowing nested CEGAR calls to reset wall time."""
    if deadline is None:
        return solver.solve()
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        return None, None
    return solver.solve(time_limit=max(0.01, remaining))


def solve_bit_assumption_shards(
    solver: Any,
    bits: list[int],
    shard_bits: int,
    *,
    deadline: float | None,
    per_shard_time_limit: float,
) -> tuple[bool | None, list[Any] | None, int | None, list[dict[str, Any]]]:
    """Scan a complete high-bit partition while retaining learned clauses."""
    count = max(0, min(int(shard_bits), len(bits)))
    if count == 0:
        satisfiable, model = solve_with_deadline(solver, deadline)
        return satisfiable, model, None, []
    selected = bits[-count:]
    records: list[dict[str, Any]] = []
    saw_unknown = False
    for pattern in range(1 << count):
        remaining = (
            float(deadline) - time.monotonic() if deadline is not None else None
        )
        if remaining is not None and remaining <= 0:
            saw_unknown = True
            break
        limit = max(0.01, float(per_shard_time_limit))
        if remaining is not None:
            limit = min(limit, remaining)
        assumptions = [
            variable if (pattern >> offset) & 1 else -variable
            for offset, variable in enumerate(selected)
        ]
        started = time.monotonic()
        satisfiable, model = solver.solve(
            assumptions=assumptions,
            time_limit=limit,
        )
        status = (
            "sat"
            if satisfiable is True
            else "unsat"
            if satisfiable is False
            else "unknown"
        )
        records.append(
            {
                "pattern": pattern,
                "status": status,
                "solve_seconds": time.monotonic() - started,
            }
        )
        if satisfiable is True:
            for literal in assumptions:
                solver.add_clause([literal])
            return True, model, pattern, records
        if satisfiable is None:
            saw_unknown = True
    if len(records) < (1 << count) or saw_unknown:
        return None, None, None, records
    return False, None, None, records


def certify_lazy_rejections(
    solver: Any,
    cnf: Cnf,
    raw_bits: list[list[int]],
    maxima: list[int],
    rejection_gaps: list[int],
    forbidden_values: dict[int, list[int]],
    satisfiable: bool | None,
    model: list[Any] | None,
    *,
    draw_stop: int,
    batch_size: int,
    max_solves: int,
    deadline: float | None,
    seen: set[tuple[int, int]],
    steps: list[dict[str, Any]],
) -> tuple[bool | None, list[Any] | None, bool]:
    """Incrementally certify every rejected word used by an exact path."""
    solves = 0
    while satisfiable is True and model is not None:
        raw_values = concrete_raw_values_from_model(model, len(raw_bits))
        violations = [
            item
            for item in rejected_profile_violations(
                raw_values,
                maxima,
                rejection_gaps,
                forbidden_values,
                draw_stop=draw_stop,
            )
            if item not in seen
        ]
        if not violations:
            return satisfiable, model, True
        if int(max_solves) > 0 and solves >= int(max_solves):
            steps.append(
                {
                    "constraints_added": 0,
                    "total_constraints_added": len(seen),
                    "violations_pending": len(violations),
                    "status": "budget-exhausted",
                }
            )
            return satisfiable, model, False
        selected = violations[: max(1, int(batch_size))]
        added = add_rejected_profile_constraints(
            cnf, raw_bits, maxima, forbidden_values, selected
        )
        seen.update(selected)
        solve_started = time.monotonic()
        satisfiable, model = solve_with_deadline(solver, deadline)
        solves += 1
        steps.append(
            {
                "constraints_added": added,
                "total_constraints_added": len(seen),
                "violations_added": [list(item) for item in selected],
                "status": (
                    "sat"
                    if satisfiable is True
                    else "unsat"
                    if satisfiable is False
                    else "unknown"
                ),
                "solve_seconds": time.monotonic() - solve_started,
            }
        )
    return satisfiable, model, False


def replay_bounded_draws_with_forbidden(
    raw_values: list[int],
    maxima: list[int],
    forbidden_values: dict[int, list[int]],
) -> tuple[list[int], int, int] | None:
    """Replay rk_interval plus the generator's repeated-unique retry rule."""
    cursor = 0
    rejected = 0
    accepted: list[int] = []
    for draw, maximum in enumerate(maxima):
        mask = (1 << interval_bits(int(maximum))) - 1
        forbidden = set(int(value) for value in forbidden_values.get(draw) or [])
        while cursor < len(raw_values):
            value = int(raw_values[cursor]) & mask
            cursor += 1
            if value <= int(maximum) and value not in forbidden:
                accepted.append(value)
                break
            rejected += 1
        else:
            return None
    return accepted, cursor, rejected


def replay_rejection_gaps(
    raw_values: list[int],
    maxima: list[int],
    forbidden_values: dict[int, list[int]],
) -> list[int] | None:
    """Return the concrete rejected-word count before each accepted draw."""
    cursor = 0
    gaps: list[int] = []
    for draw, maximum in enumerate(maxima):
        mask = (1 << interval_bits(int(maximum))) - 1
        forbidden = set(int(value) for value in forbidden_values.get(draw) or [])
        gap = 0
        while cursor < len(raw_values):
            value = int(raw_values[cursor]) & mask
            cursor += 1
            if value <= int(maximum) and value not in forbidden:
                gaps.append(gap)
                break
            gap += 1
        else:
            return None
    return gaps


def replay_choice_seed_prefix(
    accepted: list[int], seed_slice: tuple[int, int], published: list[int]
) -> list[int]:
    """Replay NumPy's full 900-value permutation and return its seed prefix."""
    start, stop = (int(value) for value in seed_slice)
    choices = accepted[start:stop]
    if len(choices) != 899:
        raise ValueError("choice seed event must contain a full 900-value permutation")
    permutation = replay_shuffle([str(value) for value in range(900)], choices)
    return [int(value) + 100 for value in permutation[: len(published)]]


def concrete_raw_values_from_model(model: list[Any], count: int) -> list[int]:
    """Generate raw words from the 19,968 one-based symbolic state variables."""
    words = np.zeros(624, dtype=np.uint32)
    for word in range(624):
        value = 0
        for bit in range(32):
            variable = word * 32 + bit + 1
            if variable < len(model) and bool(model[variable]):
                value |= 1 << bit
        words[word] = value
    rng = np.random.RandomState()
    rng.set_state(("MT19937", words, 0, 0, 0.0))
    return [
        int(value)
        for value in rng.randint(0, 2**32, size=max(0, int(count)), dtype=np.uint32)
    ]


def validate_model(
    raw_values: list[int],
    rows: list[dict[str, Any]],
    maxima: list[int],
    expected: list[int | None],
    round_slices: list[tuple[int, int]],
    forbidden_values: dict[int, list[int]],
    *,
    seed_method: str = "integers-unique",
    seed_slices: list[tuple[int, int]] | None = None,
    seed_labels: list[list[int]] | None = None,
    counterexample_batch_size: int = 1,
    counterexample_low_position_threshold: int = -1,
    counterexample_low_batch_size: int = 1,
    counterexample_domain_batch_size: int = 1,
    counterexample_domain_low_position_threshold: int = -1,
    counterexample_domain_low_batch_size: int = 1,
) -> dict[str, Any]:
    replay = replay_bounded_draws_with_forbidden(
        raw_values, maxima, forbidden_values
    )
    if replay is None:
        return {"valid": False, "reason": "raw-cap-exhausted", "rows_passed": 0}
    accepted, cursor, rejected = replay
    for draw, value in enumerate(expected):
        if value is not None and accepted[draw] != int(value):
            return {
                "valid": False,
                "reason": "seed-label-mismatch",
                "failed_draw": draw,
                "rows_passed": 0,
            }
    if seed_method == "choice-without-replacement":
        if seed_slices is None or seed_labels is None:
            raise ValueError("choice validation requires seed slices and labels")
        if len(seed_slices) != len(seed_labels):
            raise ValueError("choice seed slices and labels are misaligned")
        for event, (seed_slice, published) in enumerate(zip(seed_slices, seed_labels)):
            predicted = replay_choice_seed_prefix(accepted, seed_slice, published)
            if predicted != [int(value) for value in published]:
                return {
                    "valid": False,
                    "reason": "seed-label-mismatch",
                    "failed_seed_event": event,
                    "rows_passed": 0,
                }
    elif seed_method != "integers-unique":
        raise ValueError(f"unknown seed method: {seed_method}")
    for row_index, (row, (start, stop)) in enumerate(zip(rows, round_slices)):
        initial, observed, _exact = uid_aware_sequences(row)
        full = replay_shuffle(initial, accepted[start:stop])
        if not is_exact_observed_subsequence(full, observed):
            trace_hint, trace_hint_kind = singleton_trace_counterexample_detail(
                row,
                full,
                max_positions=max(1, int(counterexample_batch_size)),
                low_position_threshold=int(counterexample_low_position_threshold),
                low_batch_size=max(1, int(counterexample_low_batch_size)),
                domain_batch_size=max(1, int(counterexample_domain_batch_size)),
                domain_low_position_threshold=int(
                    counterexample_domain_low_position_threshold
                ),
                domain_low_batch_size=max(1, int(counterexample_domain_low_batch_size)),
            )
            return {
                "valid": False,
                "reason": "public-shuffle-mismatch",
                "failed_round": row_index + 1,
                "failed_task_id": str(row["task_id"]),
                "rows_passed": row_index,
                "raw_words_consumed": cursor,
                "rejections": rejected,
                "singleton_trace_hint": trace_hint,
                "singleton_trace_hint_kind": trace_hint_kind,
            }
    return {
        "valid": True,
        "reason": "all-public-constraints-replayed",
        "rows_passed": len(rows),
        "raw_words_consumed": cursor,
        "rejections": rejected,
    }


def singleton_trace_counterexample(
    row: dict[str, Any],
    full: list[str],
    *,
    max_positions: int = 1,
    low_position_threshold: int = -1,
    low_batch_size: int = 1,
    domain_batch_size: int = 1,
    domain_low_position_threshold: int = -1,
    domain_low_batch_size: int = 1,
) -> list[int]:
    return singleton_trace_counterexample_detail(
        row,
        full,
        max_positions=max_positions,
        low_position_threshold=low_position_threshold,
        low_batch_size=low_batch_size,
        domain_batch_size=domain_batch_size,
        domain_low_position_threshold=domain_low_position_threshold,
        domain_low_batch_size=domain_low_batch_size,
    )[0]


def singleton_trace_counterexample_detail(
    row: dict[str, Any],
    full: list[str],
    *,
    max_positions: int = 1,
    low_position_threshold: int = -1,
    low_batch_size: int = 1,
    domain_batch_size: int = 1,
    domain_low_position_threshold: int = -1,
    domain_low_batch_size: int = 1,
) -> tuple[list[int], str | None]:
    """Return one or two singleton observations disproved by a full replay.

    Individual positions must lie in ``observed_position..position+missing``.
    Their missing-entry shifts must also be nondecreasing in observed order.
    Either violation gives a small exact trace constraint that excludes the
    current model without adding every known UID at once.
    """
    _initial, observed, _exact = uid_aware_sequences(row)
    missing = max(0, len(full) - len(observed))
    positions = {token: index for index, token in enumerate(full) if token.startswith("u")}
    shifts: list[tuple[int, int]] = []
    range_violations: list[int] = []
    for observed_position, token in enumerate(observed):
        if not token.startswith("u") or token not in positions:
            continue
        shift = positions[token] - observed_position
        if shift < 0 or shift > missing:
            range_violations.append(observed_position)
            continue
        shifts.append((observed_position, shift))
    if range_violations:
        # A late observed position skips the largest prefix of reverse swaps,
        # yielding a much smaller exact counterexample circuit.
        limit = max(1, int(max_positions))
        if (
            int(low_position_threshold) >= 0
            and max(range_violations) <= int(low_position_threshold)
        ):
            limit = max(1, int(low_batch_size))
        return (
            sorted(range_violations, reverse=True)[:limit],
            "range",
        )
    monotone_violations: list[tuple[int, int]] = []
    for previous, following in zip(shifts, shifts[1:]):
        if following[1] < previous[1]:
            monotone_violations.append((previous[0], following[0]))
    if monotone_violations:
        selected: set[int] = set()
        for previous, following in sorted(
            monotone_violations, key=lambda value: value[1], reverse=True
        ):
            selected.update((previous, following))
            if len(selected) >= max(1, int(max_positions)):
                break
        return sorted(selected), "monotone"
    # The exact UID constraints above intentionally ignore endpoint groups
    # with more than one registered UID.  Complete the same insertion-shift
    # argument over every public group token.  For observed position p, a
    # matching group member must occupy p..p+missing; choosing nondecreasing
    # shifts is exactly the subsequence condition and keeps repeated groups
    # on distinct, increasing final positions.
    initial, all_observed, _exact = uid_aware_sequences(row)
    positions_by_token: dict[str, list[int]] = {}
    for position, token in enumerate(full):
        positions_by_token.setdefault(token, []).append(position)
    allowed_by_position: list[tuple[int, list[int]]] = []
    domain_range_violations: list[int] = []
    for observed_position, token in enumerate(all_observed):
        allowed = [
            position - observed_position
            for position in positions_by_token.get(token, [])
            if 0 <= position - observed_position <= missing
        ]
        if not allowed:
            domain_range_violations.append(observed_position)
        else:
            allowed_by_position.append((observed_position, sorted(set(allowed))))
    if domain_range_violations:
        # Group-domain traces are larger than singleton traces because their
        # initial target is a disjunction.  Batch cheaper late positions while
        # retaining single-position solves for expensive early reverse traces.
        limit = max(1, int(domain_batch_size))
        if (
            int(domain_low_position_threshold) >= 0
            and max(domain_range_violations) <= int(domain_low_position_threshold)
        ):
            limit = max(1, int(domain_low_batch_size))
        return sorted(domain_range_violations, reverse=True)[:limit], "domain-range"
    previous_shift = 0
    previous_position: int | None = None
    for observed_position, allowed in allowed_by_position:
        feasible = [shift for shift in allowed if shift >= previous_shift]
        if not feasible:
            if previous_position is None:
                return [observed_position], "domain-range"
            return [previous_position, observed_position], "domain-monotone"
        previous_shift = min(feasible)
        previous_position = observed_position
    return [], None


def block_choice_slice(
    cnf: Cnf,
    model: list[Any],
    choices: list[list[int]],
    minimum_final_position: int,
) -> int:
    """Exclude the current concrete choice slice without a trace circuit.

    Undoing a token at final position p can only inspect Fisher--Yates swaps
    p..n-1.  Requiring at least one exposed choice bit in that slice to change
    is a sound lazy-CEGAR block for the current failing model.  It is not a
    proof of the desired final position, so every replacement model is replayed
    by the concrete oracle before promotion.
    """
    size = len(choices) + 1
    start_i = max(1, min(int(minimum_final_position), size - 1))
    literals: list[int] = []
    for i in range(start_i, size):
        choice = choices[size - 1 - i]
        for variable in choice:
            current = variable < len(model) and bool(model[variable])
            literals.append(-variable if current else variable)
    cnf.add(literals)
    return len(literals)


def write_cegar_checkpoint(
    path: Path,
    *,
    corridor_rank: int,
    rounds: int,
    status: str,
    traces: int,
    steps: list[dict[str, Any]],
    validation: dict[str, Any],
    rejection_steps: list[dict[str, Any]] | None = None,
    rejection_certified: bool = False,
    final: bool = False,
) -> None:
    """Persist CEGAR progress before the next potentially long SAT call."""
    atomic_json(
        path.resolve(),
        {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "numpy-mt19937-fixed-profile-cegar-checkpoint",
            "summary": {
                "corridor_rank": int(corridor_rank),
                "rounds_modelled": int(rounds),
                "status": status,
                "singleton_uid_traces_bound": int(traces),
                "cegar_iterations_completed": len(steps),
                "rejection_cegar_solves": len(rejection_steps or []),
                "rejection_profile_certified": bool(rejection_certified),
                "public_replay_reason": validation.get("reason"),
                "public_replay_rows_passed": int(validation.get("rows_passed") or 0),
                "final": bool(final),
            },
            "cegar_steps": list(steps),
            "rejection_cegar_steps": list(rejection_steps or []),
            "validation": dict(validation),
            "safety": {
                "holdout_labels_opened": False,
                "state_persisted": False,
                "raw_stream_persisted": False,
                "network_reads": False,
                "submission_writes": False,
            },
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--tuple-report", type=Path, required=True)
    parser.add_argument("--corridor-plan", type=Path, required=True)
    parser.add_argument("--corridor-rank", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument(
        "--seed-method",
        choices=("integers-unique", "choice-without-replacement"),
        default="integers-unique",
    )
    parser.add_argument(
        "--lazy-choice-seed-events",
        action="store_true",
        help=(
            "For choice-without-replacement, add each exact published seed "
            "prefix only after concrete replay exposes that event."
        ),
    )
    parser.add_argument(
        "--choice-prefix-encoding",
        choices=("reverse", "forward"),
        default="reverse",
        help="Trace final prefix positions backward or known values forward.",
    )
    parser.add_argument("--include-sealed-shuffles", action="store_true")
    parser.add_argument(
        "--validate-next-discovery",
        action="store_true",
        help="Keep the next labelled Discovery row out of the fit and test it afterwards.",
    )
    parser.add_argument(
        "--increment-next-discovery",
        action="store_true",
        help=(
            "Solve the included prefix first, then add the next Discovery row to the "
            "same solver so learned clauses are retained."
        ),
    )
    parser.add_argument(
        "--increment-seed-only",
        action="store_true",
        help="On the incremented row bind its seed labels but leave its shuffle for replay.",
    )
    parser.add_argument(
        "--increment-state-shard-bits",
        type=int,
        default=0,
        help=(
            "After adding an incremented integer-seed row, scan a complete "
            "partition of this many evenly spaced MT state bits with assumptions."
        ),
    )
    parser.add_argument(
        "--increment-state-shard-time-limit", type=float, default=120.0
    )
    parser.add_argument(
        "--increment-integer-prefix-depth",
        type=int,
        default=3,
        choices=(1, 2, 3),
        help="For an incremented integer seed event, bind only its first N labels.",
    )
    parser.add_argument(
        "--increment-integer-progressive",
        action="store_true",
        help=(
            "Add incremented integer seed labels one at a time in the same "
            "solver, retaining learned clauses between depths."
        ),
    )
    parser.add_argument(
        "--increment-choice-prefix-depth",
        type=int,
        default=3,
        choices=(1, 2, 3),
        help="For an incremented choice seed event, bind only its first N labels.",
    )
    parser.add_argument(
        "--increment-choice-progressive",
        action="store_true",
        help=(
            "Add and solve incremented choice labels one position at a time in "
            "the same solver, retaining learned clauses between depths."
        ),
    )
    parser.add_argument(
        "--increment-choice-anchor-shard-bits",
        type=int,
        default=0,
        help=(
            "For the first progressive seed position, partition the high bits "
            "of its structurally necessary j_target draw."
        ),
    )
    parser.add_argument(
        "--increment-choice-anchor-shard-time-limit", type=float, default=120.0
    )
    parser.add_argument(
        "--increment-choice-zero-target-encoding",
        choices=("binary", "unary"),
        default="binary",
    )
    parser.add_argument(
        "--shuffle-encoding",
        choices=("singleton-trace", "network"),
        default="singleton-trace",
    )
    parser.add_argument(
        "--mt-encoding",
        choices=("sparse", "dense"),
        default="sparse",
        help=(
            "Use incremental sparse MT gates or dense initial-state XOR rows. "
            "Dense mode prebuilds the selected raw prefix."
        ),
    )
    parser.add_argument("--max-singleton-traces", type=int, default=16)
    parser.add_argument(
        "--singleton-trace-selection",
        choices=("spread", "late"),
        default="spread",
        help="Choose evenly spread traces or the cheaper late permutation positions.",
    )
    parser.add_argument("--omit-singleton-traces", action="store_true")
    parser.add_argument(
        "--omit-rejected-inequalities",
        action="store_true",
        help="Diagnostic relaxation only; relaxed models can never be promoted.",
    )
    parser.add_argument(
        "--lazy-rejected-inequalities",
        action="store_true",
        help=(
            "Start without rejected-word inequalities, then add only concrete "
            "violations until the selected exact path is fully certified."
        ),
    )
    parser.add_argument("--rejection-cegar-batch-size", type=int, default=64)
    parser.add_argument(
        "--rejection-cegar-max-solves",
        type=int,
        default=0,
        help="Cap lazy-rejection re-solves per certification pass; zero is unlimited.",
    )
    parser.add_argument(
        "--wall-time-limit",
        type=float,
        default=0.0,
        help="One shared solve deadline across seed, rejection, and shuffle CEGAR stages.",
    )
    parser.add_argument("--max-network-missing", type=int, default=12)
    parser.add_argument(
        "--full-shuffle-task",
        action="append",
        default=[],
        metavar="TASK_ID_OR_PREFIX",
        help=(
            "Bind full network/domain automata only for selected task IDs or "
            "prefixes; with no selector all rows remain eligible."
        ),
    )
    parser.add_argument(
        "--max-linear-domain-rounds",
        type=int,
        default=0,
        help=(
            "Bind up to N large-missing rows with a full shuffle network and "
            "the exact linear delete-to-observed domain automaton."
        ),
    )
    parser.add_argument("--cegar-iterations", type=int, default=0)
    parser.add_argument(
        "--cegar-batch-size",
        type=int,
        default=1,
        help="Add up to this many public replay counterexample positions per solve.",
    )
    parser.add_argument(
        "--cegar-low-position-threshold",
        type=int,
        default=-1,
        help="At or below this reverse-trace position, use the smaller low batch.",
    )
    parser.add_argument("--cegar-low-batch-size", type=int, default=1)
    parser.add_argument("--cegar-domain-batch-size", type=int, default=1)
    parser.add_argument("--cegar-domain-low-position-threshold", type=int, default=-1)
    parser.add_argument("--cegar-domain-low-batch-size", type=int, default=1)
    parser.add_argument(
        "--cegar-strategy",
        choices=("singleton-trace", "block-choice-slice"),
        default="singleton-trace",
    )
    parser.add_argument(
        "--checkpoint-output",
        type=Path,
        help="Write an atomic progress report after every completed CEGAR solve.",
    )
    parser.add_argument(
        "--cegar-resume",
        type=Path,
        help="Preload singleton counterexamples from an earlier report/checkpoint.",
    )
    parser.add_argument("--omit-prefix-tuples", action="store_true")
    parser.add_argument("--time-limit", type=float, default=180.0)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument(
        "--state-shard-bits",
        type=int,
        default=0,
        help="Partition by this many evenly spaced initial MT storage bits.",
    )
    parser.add_argument("--state-shard-index", type=int, default=0)
    parser.add_argument(
        "--first-late-trace-shift",
        type=int,
        default=-1,
        help=(
            "Fix the missing-entry shift of the last exact singleton in the first "
            "round; -1 leaves its complete domain open."
        ),
    )
    parser.add_argument(
        "--fixed-trace-shift",
        action="append",
        default=[],
        metavar="ROUND:POSITION:SHIFT",
        help=(
            "Fix one observed trace's missing-entry shift. Repeat this option to "
            "build an exact, disjoint shift shard. ROUND is one-based."
        ),
    )
    parser.add_argument(
        "--fixed-accepted-draw",
        action="append",
        default=[],
        metavar="DRAW:VALUE",
        help="Fix one zero-based accepted bounded draw; repeat for exact shards.",
    )
    parser.add_argument(
        "--relax-rejection-window",
        action="append",
        default=[],
        metavar="START:STOP",
        help=(
            "Keep cumulative rejection fixed at window boundaries but allow all "
            "within-window allocations; repeat for disjoint windows."
        ),
    )
    parser.add_argument("--relax-rejection-max-alternatives", type=int, default=4096)
    parser.add_argument("--prelude", default="654,347,964")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_mt_fixed_profile.json"),
    )
    args = parser.parse_args()
    if args.omit_rejected_inequalities and args.lazy_rejected_inequalities:
        parser.error(
            "--omit-rejected-inequalities and --lazy-rejected-inequalities are exclusive"
        )
    if args.lazy_choice_seed_events and args.seed_method != "choice-without-replacement":
        parser.error("--lazy-choice-seed-events requires choice-without-replacement")
    if args.lazy_choice_seed_events and args.increment_next_discovery:
        parser.error(
            "--lazy-choice-seed-events does not yet support incremental Discovery rows"
        )
    if args.increment_choice_progressive and not (
        args.increment_next_discovery
        and args.increment_seed_only
        and args.seed_method == "choice-without-replacement"
    ):
        parser.error(
            "--increment-choice-progressive requires --increment-next-discovery, "
            "--increment-seed-only, and choice-without-replacement"
        )
    if args.increment_choice_anchor_shard_bits and not args.increment_choice_progressive:
        parser.error(
            "--increment-choice-anchor-shard-bits requires progressive choice increment"
        )
    if args.increment_integer_progressive and not (
        args.increment_next_discovery
        and args.increment_seed_only
        and args.seed_method == "integers-unique"
    ):
        parser.error(
            "--increment-integer-progressive requires --increment-next-discovery, "
            "--increment-seed-only, and integers-unique"
        )
    if args.increment_state_shard_bits and not args.increment_next_discovery:
        parser.error(
            "--increment-state-shard-bits requires --increment-next-discovery"
        )
    if args.increment_state_shard_bits and args.increment_choice_progressive:
        parser.error(
            "incremental state sharding and progressive choice sharding are exclusive"
        )
    if not 0 <= int(args.increment_state_shard_bits) <= 10:
        parser.error("--increment-state-shard-bits must be between 0 and 10")
    if not 0 <= int(args.increment_choice_anchor_shard_bits) <= 10:
        parser.error("--increment-choice-anchor-shard-bits must be between 0 and 10")
    try:
        fixed_trace_shifts = parse_fixed_trace_shifts(args.fixed_trace_shift)
    except ValueError as error:
        parser.error(str(error))
    try:
        fixed_accepted_draws = parse_fixed_accepted_draws(args.fixed_accepted_draw)
    except ValueError as error:
        parser.error(str(error))
    try:
        relaxed_rejection_windows = parse_relaxed_rejection_windows(
            args.relax_rejection_window
        )
    except ValueError as error:
        parser.error(str(error))
    if relaxed_rejection_windows and (
        args.omit_rejected_inequalities or args.lazy_rejected_inequalities
    ):
        parser.error(
            "relaxed rejection windows require eager rejected-word inequalities"
        )
    try:
        from pycryptosat import Solver
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with pycryptosat") from error

    payload = json.loads(args.constraints.read_text(encoding="utf-8"))
    tuple_payload = json.loads(args.tuple_report.read_text(encoding="utf-8"))
    plan = json.loads(args.corridor_plan.read_text(encoding="utf-8"))
    all_rows = [
        row for row in payload.get("rounds") or []
        if int(row.get("shuffle_size") or 0) == 256
    ]
    discovery_rows = [
        row for row in all_rows
        if row.get("seed_label_partition") == "discovery"
        and len(row.get("discovery_seed_label") or []) == 3
    ]
    rows = (
        all_rows[: max(0, args.rounds)]
        if args.include_sealed_shuffles
        else discovery_rows[: max(0, args.rounds)]
    )
    excluded_discovery = None
    if args.validate_next_discovery or args.increment_next_discovery:
        excluded_discovery = next_excluded_discovery(rows, discovery_rows)
    incremented_discovery = (
        excluded_discovery if args.increment_next_discovery else None
    )
    layout_rows = [*rows, incremented_discovery] if incremented_discovery else list(rows)
    for row_index, shifts in fixed_trace_shifts.items():
        # The incremental row is appended to the MT stream only after the
        # initial solve.  A shift shard must instead be present in that first
        # formula, otherwise UNKNOWN would not test the advertised branch.
        if row_index >= len(rows):
            raise ValueError(
                "fixed trace shift selects a round outside the initial model"
            )
        row = rows[row_index]
        observed = list(row.get("ordered_uid_domains") or [])
        size = len(row.get("initial_uids") or [])
        missing = max(0, size - len(observed))
        for position, shift in shifts.items():
            if position >= len(observed):
                raise ValueError("fixed trace shift selects an absent observation")
            if shift > missing or position + shift >= size:
                raise ValueError("fixed trace shift is outside its exact range")
    prelude = [int(value) for value in args.prelude.split(",") if value.strip()]
    maxima, expected, round_slices, seed_slices = build_draw_layout(
        layout_rows, prelude, args.seed_method
    )
    for draw, value in fixed_accepted_draws.items():
        if draw >= len(maxima):
            raise ValueError("fixed accepted draw is outside the model")
        if value > int(maxima[draw]):
            raise ValueError("fixed accepted draw value exceeds its bound")
    layout_seed_labels = [prelude] + [
        [int(value) for value in row.get("discovery_seed_label") or []]
        for row in layout_rows
    ]
    initial_draw_stop = (
        int(seed_slices[len(rows)][1]) if incremented_discovery else len(maxima)
    )
    if any(stop > initial_draw_stop for _start, stop in relaxed_rejection_windows):
        raise ValueError(
            "relaxed rejection windows must lie inside the initial solve prefix"
        )
    full_gaps = corridor_profile(plan, args.corridor_rank)
    if len(full_gaps) < len(maxima):
        raise ValueError(
            f"corridor path has {len(full_gaps)} draws but selected rows require {len(maxima)}"
        )
    # A plan made for the full 20-round corpus is also authoritative for every
    # prefix.  This enables incremental 1 -> 2 -> 4 -> ... boundary searches
    # without resampling a different rejection trajectory at each depth.
    gaps = full_gaps[: len(maxima)]
    accepted_indices, _ = profile_raw_indices(gaps)
    raw_cap = accepted_indices[-1] + 1
    tuple_tables = {
        str(row["task_id"]): [list(map(int, values)) for values in row.get("choice_tuples") or []]
        for row in tuple_payload.get("rounds") or []
    }

    started = time.monotonic()
    solver = Solver(
        verbose=0,
        time_limit=max(1.0, args.time_limit),
        threads=max(1, args.threads),
    )
    cnf = Cnf(solver)
    state_shard = bind_state_shard(
        cnf, max(0, args.state_shard_bits), int(args.state_shard_index)
    )
    initial_raw_cap = accepted_indices[initial_draw_stop - 1] + 1
    stream = SparseMtCnfStream(cnf) if args.mt_encoding == "sparse" else None
    raw_bits = (
        stream.ensure(initial_raw_cap)
        if stream is not None
        else add_symbolic_raw_bits(cnf, raw_cap)
    )
    forbidden_values = (
        seed_duplicate_forbidden_values(seed_slices, expected)
        if args.seed_method == "integers-unique"
        else {}
    )
    relaxed_rejection_metadata: list[dict[str, int]] = []
    if relaxed_rejection_windows:
        accepted_bits, relaxed_rejection_metadata = add_hybrid_profile(
            cnf,
            raw_bits,
            maxima,
            expected,
            gaps,
            forbidden_values,
            relaxed_rejection_windows,
            draw_stop=initial_draw_stop,
            max_alternatives=args.relax_rejection_max_alternatives,
        )
    else:
        accepted_bits = add_fixed_profile(
            cnf,
            raw_bits,
            maxima,
            expected,
            gaps,
            forbidden_values,
            draw_stop=initial_draw_stop,
            enforce_rejected=not (
                args.omit_rejected_inequalities or args.lazy_rejected_inequalities
            ),
        )
    fixed_accepted_draws_bound: set[int] = set()
    bind_fixed_accepted_draws(
        cnf,
        accepted_bits,
        fixed_accepted_draws,
        fixed_accepted_draws_bound,
    )
    choice_seed_events_bound_set: set[int] = set()
    incremental_choice_prefix_depth_bound = 0
    incremental_integer_prefix_depth_bound = 0
    if args.seed_method == "choice-without-replacement":
        seed_events_to_prebind = (
            list(zip(
                seed_slices[: len(rows) + 1],
                layout_seed_labels[: len(rows) + 1],
            ))
            if not args.lazy_choice_seed_events
            else [(seed_slices[0], layout_seed_labels[0])]
        )
        for event, (seed_slice, labels) in enumerate(seed_events_to_prebind):
            start, stop = seed_slice
            add_choice_seed_prefix(
                cnf,
                accepted_bits[start:stop],
                labels,
                args.choice_prefix_encoding,
            )
            choice_seed_events_bound_set.add(event)
    traces = tuple_rows = tuple_nodes = 0
    singleton_trace_caches: dict[int, dict[int, list[int]]] = {}
    singleton_pair_caches: dict[int, set[tuple[int, int]]] = {}
    full_shuffle_rounds = full_shuffle_positions = 0
    linear_domain_rounds = linear_domain_states = linear_domain_edges = 0
    fixed_first_trace_position: int | None = None
    for row_index, (row, (start, stop)) in enumerate(zip(rows, round_slices)):
        initial, observed, _exact = uid_aware_sequences(row)
        missing = len(initial) - len(observed)
        selected_for_network = matches_task_selector(
            str(row["task_id"]), args.full_shuffle_task
        )
        if (
            args.shuffle_encoding == "network"
            and selected_for_network
            and missing <= args.max_network_missing
        ):
            output = add_shuffle_network(cnf, accepted_bits[start:stop], initial)
            add_observed_subsequence(cnf, output, initial, observed)
            full_shuffle_rounds += 1
            full_shuffle_positions += len(observed)
        elif (
            args.shuffle_encoding == "network"
            and selected_for_network
            and linear_domain_rounds < max(0, args.max_linear_domain_rounds)
        ):
            output = add_shuffle_network(cnf, accepted_bits[start:stop], initial)
            states, edges = add_observed_domain_subsequence(
                cnf, output, initial, observed
            )
            full_shuffle_rounds += 1
            full_shuffle_positions += len(observed)
            linear_domain_rounds += 1
            linear_domain_states += states
            linear_domain_edges += edges
        else:
            if not args.omit_singleton_traces:
                fixed_shifts = dict(fixed_trace_shifts.get(row_index) or {})
                if row_index == 0 and args.first_late_trace_shift >= 0:
                    initial_uids = {int(value) for value in row.get("initial_uids") or []}
                    domains = {
                        str(key): [int(value) for value in values]
                        for key, values in (row.get("uid_domains") or {}).items()
                    }
                    positions = [
                        position
                        for position, token in enumerate(row.get("ordered_uid_domains") or [])
                        if len(domains.get(str(token)) or []) == 1
                        and domains[str(token)][0] in initial_uids
                    ]
                    if not positions:
                        raise ValueError("first round has no exact singleton to shift-shard")
                    fixed_first_trace_position = positions[-1]
                    requested_shift = int(args.first_late_trace_shift)
                    if (
                        fixed_first_trace_position in fixed_shifts
                        and fixed_shifts[fixed_first_trace_position] != requested_shift
                    ):
                        raise ValueError(
                            "first late trace shift conflicts with fixed trace shard"
                        )
                    fixed_shifts[fixed_first_trace_position] = requested_shift
                traces += add_observed_singleton_traces(
                    cnf,
                    accepted_bits[start:stop],
                    row,
                    max_traces=max(0, args.max_singleton_traces),
                    selection=args.singleton_trace_selection,
                    fixed_shifts=fixed_shifts or None,
                    trace_cache=singleton_trace_caches.setdefault(row_index, {}),
                    monotone_pair_cache=singleton_pair_caches.setdefault(row_index, set()),
                )
        if not args.omit_prefix_tuples:
            tuples = tuple_tables.get(str(row["task_id"])) or []
            depth, nodes = add_choice_tuple_trie(
                cnf, accepted_bits[start:stop], tuples
            )
            if depth:
                tuple_rows += len(tuples)
                tuple_nodes += nodes
    resumed_steps: list[dict[str, Any]] = []
    resume_seen: set[tuple[int, tuple[int, ...]]] = set()
    resume_payload: dict[str, Any] = {}
    if args.cegar_resume:
        resume_payload = json.loads(args.cegar_resume.read_text(encoding="utf-8"))
        task_to_index = {str(row["task_id"]): index for index, row in enumerate(rows)}
        for step in resume_payload.get("cegar_steps") or []:
            choice_event = step.get("choice_seed_event")
            if (
                args.lazy_choice_seed_events
                and choice_event is not None
                and str(step.get("constraint_added") or "") == "choice-seed-event"
            ):
                event = int(choice_event)
                if not 0 <= event < len(rows) + 1:
                    continue
                if event in choice_seed_events_bound_set:
                    continue
                seed_start, seed_stop = seed_slices[event]
                add_choice_seed_prefix(
                    cnf,
                    accepted_bits[seed_start:seed_stop],
                    layout_seed_labels[event],
                    args.choice_prefix_encoding,
                )
                choice_seed_events_bound_set.add(event)
                resumed_steps.append(dict(step))
                continue
            task_id = str(step.get("task_id") or "")
            row_index = task_to_index.get(task_id)
            hint = tuple(int(value) for value in step.get("observed_positions_added") or [])
            if row_index is None or not hint or (row_index, hint) in resume_seen:
                continue
            start, stop = round_slices[row_index]
            constraint_statistics: dict[str, int] = {}
            hint_kind = str(step.get("counterexample_kind") or "")
            added = add_observed_singleton_traces(
                cnf,
                accepted_bits[start:stop],
                rows[row_index],
                observed_positions=set(hint),
                fixed_shifts=fixed_trace_shifts.get(row_index) or None,
                trace_cache=singleton_trace_caches.setdefault(row_index, {}),
                monotone_pair_cache=singleton_pair_caches.setdefault(row_index, set()),
                statistics=constraint_statistics,
                enforce_monotone=hint_kind in {"monotone", "domain-monotone"},
                allow_ambiguous_domains=hint_kind.startswith("domain-"),
            )
            pairs_added = constraint_statistics.get("monotone_pairs_added", 0)
            if not added and not pairs_added:
                continue
            traces += added
            resume_seen.add((row_index, hint))
            resumed_steps.append(
                {
                    "round": row_index + 1,
                    "task_id": task_id,
                    "observed_positions_added": list(hint),
                    "traces_added": added,
                    "monotone_pairs_added": pairs_added,
                    "status": "resumed-constraint",
                }
            )
    resumed_rejection_steps: list[dict[str, Any]] = []
    resumed_rejection_seen: set[tuple[int, int]] = set()
    if args.lazy_rejected_inequalities:
        for step in resume_payload.get("rejection_cegar_steps") or []:
            selected = []
            for item in step.get("violations_added") or []:
                if not isinstance(item, list) or len(item) != 2:
                    continue
                violation = (int(item[0]), int(item[1]))
                if violation in resumed_rejection_seen:
                    continue
                draw, raw_index = violation
                if not (0 <= draw < initial_draw_stop and 0 <= raw_index < len(raw_bits)):
                    continue
                resumed_rejection_seen.add(violation)
                selected.append(violation)
            if not selected:
                continue
            add_rejected_profile_constraints(
                cnf, raw_bits, maxima, forbidden_values, selected
            )
            resumed_rejection_steps.append(
                {
                    "constraints_added": len(selected),
                    "total_constraints_added": len(resumed_rejection_seen),
                    "violations_added": [list(item) for item in selected],
                    "status": "resumed-constraint",
                }
            )
    (
        prebound_traces,
        fixed_trace_positions_prebound,
        fixed_trace_monotone_pairs_prebound,
    ) = prebind_fixed_trace_shifts(
        cnf,
        accepted_bits,
        rows,
        round_slices,
        fixed_trace_shifts,
        singleton_trace_caches,
        singleton_pair_caches,
    )
    traces += prebound_traces
    build_seconds = time.monotonic() - started
    solve_started = time.monotonic()
    solve_deadline = (
        solve_started + float(args.wall_time_limit)
        if float(args.wall_time_limit) > 0
        else None
    )
    satisfiable, model = solve_with_deadline(solver, solve_deadline)
    rejection_cegar_steps: list[dict[str, Any]] = list(resumed_rejection_steps)
    rejection_seen: set[tuple[int, int]] = set(resumed_rejection_seen)
    rejection_profile_certified = not args.omit_rejected_inequalities
    if args.lazy_rejected_inequalities:
        satisfiable, model, rejection_profile_certified = certify_lazy_rejections(
            solver,
            cnf,
            raw_bits,
            maxima,
            gaps,
            forbidden_values,
            satisfiable,
            model,
            draw_stop=initial_draw_stop,
            batch_size=args.rejection_cegar_batch_size,
            max_solves=args.rejection_cegar_max_solves,
            deadline=solve_deadline,
            seen=rejection_seen,
            steps=rejection_cegar_steps,
        )
    validation: dict[str, Any] = {
        "valid": False,
        "reason": "no-sat-model",
        "rows_passed": 0,
    }
    if satisfiable is True:
        raw_values = [model_uint(model, bits) for bits in raw_bits]
        validation = validate_model(
            raw_values,
            rows,
            maxima[:initial_draw_stop],
            expected[:initial_draw_stop],
            round_slices[: len(rows)],
            forbidden_values,
            seed_method=args.seed_method,
            seed_slices=seed_slices[: len(rows) + 1],
            seed_labels=layout_seed_labels[: len(rows) + 1],
            counterexample_batch_size=args.cegar_batch_size,
            counterexample_low_position_threshold=args.cegar_low_position_threshold,
            counterexample_low_batch_size=args.cegar_low_batch_size,
            counterexample_domain_batch_size=args.cegar_domain_batch_size,
            counterexample_domain_low_position_threshold=(
                args.cegar_domain_low_position_threshold
            ),
            counterexample_domain_low_batch_size=args.cegar_domain_low_batch_size,
        )
    cegar_steps: list[dict[str, Any]] = list(resumed_steps)
    cegar_seen: set[tuple[int, tuple[int, ...]]] = set(resume_seen)
    while (
        args.lazy_choice_seed_events
        and satisfiable is True
        and validation.get("reason") == "seed-label-mismatch"
    ):
        event = int(validation.get("failed_seed_event", -1))
        if not 0 <= event < len(rows) + 1 or event in choice_seed_events_bound_set:
            break
        seed_start, seed_stop = seed_slices[event]
        add_choice_seed_prefix(
            cnf,
            accepted_bits[seed_start:seed_stop],
            layout_seed_labels[event],
            args.choice_prefix_encoding,
        )
        choice_seed_events_bound_set.add(event)
        satisfiable, model = solve_with_deadline(solver, solve_deadline)
        if args.lazy_rejected_inequalities:
            satisfiable, model, rejection_profile_certified = certify_lazy_rejections(
                solver,
                cnf,
                raw_bits,
                maxima,
                gaps,
                forbidden_values,
                satisfiable,
                model,
                draw_stop=initial_draw_stop,
                batch_size=args.rejection_cegar_batch_size,
                max_solves=args.rejection_cegar_max_solves,
                deadline=solve_deadline,
                seen=rejection_seen,
                steps=rejection_cegar_steps,
            )
        status_now = (
            "sat"
            if satisfiable is True
            else "unsat"
            if satisfiable is False
            else "unknown"
        )
        if satisfiable is True:
            raw_values = [model_uint(model, bits) for bits in raw_bits]
            validation = validate_model(
                raw_values,
                rows,
                maxima[:initial_draw_stop],
                expected[:initial_draw_stop],
                round_slices[: len(rows)],
                forbidden_values,
                seed_method=args.seed_method,
                seed_slices=seed_slices[: len(rows) + 1],
                seed_labels=layout_seed_labels[: len(rows) + 1],
                counterexample_batch_size=args.cegar_batch_size,
                counterexample_low_position_threshold=(
                    args.cegar_low_position_threshold
                ),
                counterexample_low_batch_size=args.cegar_low_batch_size,
                counterexample_domain_batch_size=args.cegar_domain_batch_size,
                counterexample_domain_low_position_threshold=(
                    args.cegar_domain_low_position_threshold
                ),
                counterexample_domain_low_batch_size=(
                    args.cegar_domain_low_batch_size
                ),
            )
        else:
            validation = {
                "valid": False,
                "reason": "no-sat-model",
                "rows_passed": 0,
            }
        step = {
            "constraint_added": "choice-seed-event",
            "choice_seed_event": event,
            "published_seed": list(layout_seed_labels[event]),
            "status": status_now,
            "public_replay_reason": validation.get("reason"),
            "rows_passed": int(validation.get("rows_passed") or 0),
        }
        cegar_steps.append(step)
        if args.checkpoint_output:
            write_cegar_checkpoint(
                args.checkpoint_output,
                corridor_rank=args.corridor_rank,
                rounds=len(rows),
                status=status_now,
                traces=traces,
                steps=cegar_steps,
                validation=validation,
                rejection_steps=rejection_cegar_steps,
                rejection_certified=rejection_profile_certified,
            )
    if satisfiable is True and incremented_discovery is not None:
        if stream is not None:
            stream.ensure(raw_cap)
        incremental_expected = list(expected)
        if args.seed_method == "integers-unique":
            incremental_seed_start, incremental_seed_stop = seed_slices[len(rows) + 1]
            if (
                args.increment_integer_progressive
                or int(args.increment_integer_prefix_depth) < 3
            ):
                incremental_expected[incremental_seed_start:incremental_seed_stop] = [
                    None
                ] * (incremental_seed_stop - incremental_seed_start)
        future_bits = add_fixed_profile(
            cnf,
            raw_bits,
            maxima,
            incremental_expected,
            gaps,
            forbidden_values,
            draw_start=initial_draw_stop,
            enforce_rejected=not (
                args.omit_rejected_inequalities or args.lazy_rejected_inequalities
            ),
        )
        accepted_bits.extend(future_bits)
        bind_fixed_accepted_draws(
            cnf,
            accepted_bits,
            fixed_accepted_draws,
            fixed_accepted_draws_bound,
        )
        progressive_increment_steps: list[dict[str, Any]] = []
        incremental_solved_progressively = False
        incremental_integer_prefix_depth_bound = 0
        incremental_state_shard_pattern: int | None = None
        incremental_state_shard_scan: list[dict[str, Any]] = []
        if args.seed_method == "integers-unique":
            seed_start, seed_stop = seed_slices[len(rows) + 1]
            labels = layout_seed_labels[len(rows) + 1]
            requested_depth = min(
                len(labels), int(args.increment_integer_prefix_depth)
            )
            if args.increment_integer_progressive:
                incremental_solved_progressively = True
                for final_position in range(requested_depth):
                    value = int(labels[final_position]) - 100
                    bits = accepted_bits[seed_start + final_position]
                    for bit, variable in enumerate(bits):
                        cnf.add(
                            [variable if (value >> bit) & 1 else -variable]
                        )
                    incremental_integer_prefix_depth_bound = final_position + 1
                    stage_started = time.monotonic()
                    stage_state_pattern = None
                    stage_state_scan: list[dict[str, Any]] = []
                    if (
                        final_position == 0
                        and int(args.increment_state_shard_bits) > 0
                    ):
                        (
                            satisfiable,
                            model,
                            stage_state_pattern,
                            stage_state_scan,
                        ) = solve_bit_assumption_shards(
                            solver,
                            state_shard_variables(
                                int(args.increment_state_shard_bits)
                            ),
                            int(args.increment_state_shard_bits),
                            deadline=solve_deadline,
                            per_shard_time_limit=float(
                                args.increment_state_shard_time_limit
                            ),
                        )
                        incremental_state_shard_pattern = stage_state_pattern
                        incremental_state_shard_scan = stage_state_scan
                    else:
                        satisfiable, model = solve_with_deadline(
                            solver, solve_deadline
                        )
                    stage_status = (
                        "sat"
                        if satisfiable is True
                        else "unsat"
                        if satisfiable is False
                        else "unknown"
                    )
                    progressive_step = {
                        "round": len(rows) + 1,
                        "task_id": str(incremented_discovery["task_id"]),
                        "constraint_added": "incremental-integer-seed-position",
                        "integer_seed_position": final_position,
                        "published_seed_label": int(labels[final_position]),
                        "integer_prefix_depth_bound": (
                            incremental_integer_prefix_depth_bound
                        ),
                        "status": stage_status,
                        "solve_seconds": time.monotonic() - stage_started,
                        "state_shard_bits": (
                            int(args.increment_state_shard_bits)
                            if final_position == 0
                            else 0
                        ),
                        "state_shard_pattern": stage_state_pattern,
                        "state_shard_scan": stage_state_scan,
                    }
                    cegar_steps.append(progressive_step)
                    progressive_increment_steps.append(progressive_step)
                    if args.checkpoint_output:
                        write_cegar_checkpoint(
                            args.checkpoint_output,
                            corridor_rank=args.corridor_rank,
                            rounds=len(rows),
                            status=stage_status,
                            traces=traces,
                            steps=cegar_steps,
                            validation=validation,
                            rejection_steps=rejection_cegar_steps,
                            rejection_certified=rejection_profile_certified,
                        )
                    if satisfiable is not True:
                        break
            else:
                incremental_integer_prefix_depth_bound = requested_depth
                for final_position, label in enumerate(labels[:requested_depth]):
                    value = int(label) - 100
                    bits = accepted_bits[seed_start + final_position]
                    for bit, variable in enumerate(bits):
                        cnf.add(
                            [variable if (value >> bit) & 1 else -variable]
                        )
        if args.seed_method == "choice-without-replacement":
            seed_start, seed_stop = seed_slices[len(rows) + 1]
            labels = layout_seed_labels[len(rows) + 1]
            requested_depth = min(len(labels), int(args.increment_choice_prefix_depth))
            if args.increment_choice_progressive:
                incremental_solved_progressively = True
                for final_position in range(requested_depth):
                    add_choice_seed_target(
                        cnf,
                        accepted_bits[seed_start:seed_stop],
                        labels[final_position],
                        final_position,
                        (
                            "unary-zero"
                            if final_position == 0
                            and args.increment_choice_zero_target_encoding == "unary"
                            else args.choice_prefix_encoding
                        ),
                    )
                    incremental_choice_prefix_depth_bound = final_position + 1
                    stage_started = time.monotonic()
                    anchor_pattern = None
                    anchor_scan: list[dict[str, Any]] = []
                    if (
                        final_position == 0
                        and int(args.increment_choice_anchor_shard_bits) > 0
                    ):
                        target = choice_domain_targets(
                            [labels[final_position]]
                        )[0]
                        choices = accepted_bits[seed_start:seed_stop]
                        anchor_offset = len(choices) - int(target)
                        if not 0 <= anchor_offset < len(choices):
                            raise ValueError("choice target has no structural anchor draw")
                        (
                            satisfiable,
                            model,
                            anchor_pattern,
                            anchor_scan,
                        ) = solve_bit_assumption_shards(
                            solver,
                            choices[anchor_offset],
                            args.increment_choice_anchor_shard_bits,
                            deadline=solve_deadline,
                            per_shard_time_limit=(
                                args.increment_choice_anchor_shard_time_limit
                            ),
                        )
                    else:
                        satisfiable, model = solve_with_deadline(
                            solver, solve_deadline
                        )
                    if args.lazy_rejected_inequalities:
                        (
                            satisfiable,
                            model,
                            rejection_profile_certified,
                        ) = certify_lazy_rejections(
                            solver,
                            cnf,
                            raw_bits,
                            maxima,
                            gaps,
                            forbidden_values,
                            satisfiable,
                            model,
                            draw_stop=len(maxima),
                            batch_size=args.rejection_cegar_batch_size,
                            max_solves=args.rejection_cegar_max_solves,
                            deadline=solve_deadline,
                            seen=rejection_seen,
                            steps=rejection_cegar_steps,
                        )
                    stage_status = (
                        "sat"
                        if satisfiable is True
                        else "unsat"
                        if satisfiable is False
                        else "unknown"
                    )
                    progressive_step = {
                        "round": len(rows) + 1,
                        "task_id": str(incremented_discovery["task_id"]),
                        "constraint_added": "incremental-choice-seed-position",
                        "choice_seed_position": final_position,
                        "published_seed_label": int(labels[final_position]),
                        "choice_prefix_depth_bound": (
                            incremental_choice_prefix_depth_bound
                        ),
                        "status": stage_status,
                        "solve_seconds": time.monotonic() - stage_started,
                        "anchor_shard_bits": (
                            int(args.increment_choice_anchor_shard_bits)
                            if final_position == 0
                            else 0
                        ),
                        "anchor_shard_pattern": anchor_pattern,
                        "anchor_shard_scan": anchor_scan,
                    }
                    cegar_steps.append(progressive_step)
                    progressive_increment_steps.append(progressive_step)
                    if args.checkpoint_output:
                        write_cegar_checkpoint(
                            args.checkpoint_output,
                            corridor_rank=args.corridor_rank,
                            rounds=len(rows),
                            status=stage_status,
                            traces=traces,
                            steps=cegar_steps,
                            validation=validation,
                            rejection_steps=rejection_cegar_steps,
                            rejection_certified=rejection_profile_certified,
                        )
                    if satisfiable is not True:
                        break
            else:
                incremental_choice_prefix_depth_bound = requested_depth
                add_choice_seed_prefix(
                    cnf,
                    accepted_bits[seed_start:seed_stop],
                    labels[:incremental_choice_prefix_depth_bound],
                    args.choice_prefix_encoding,
                )
            if incremental_choice_prefix_depth_bound == len(labels):
                choice_seed_events_bound_set.add(len(rows) + 1)
        row_index = len(rows)
        start, stop = round_slices[row_index]
        added = 0
        if not args.increment_seed_only:
            initial, observed, _exact = uid_aware_sequences(incremented_discovery)
            missing = len(initial) - len(observed)
            if args.shuffle_encoding == "network" and missing <= args.max_network_missing:
                output = add_shuffle_network(cnf, accepted_bits[start:stop], initial)
                add_observed_subsequence(cnf, output, initial, observed)
                full_shuffle_rounds += 1
                full_shuffle_positions += len(observed)
            else:
                added = (
                    0
                    if args.omit_singleton_traces
                    else add_observed_singleton_traces(
                        cnf,
                        accepted_bits[start:stop],
                        incremented_discovery,
                        max_traces=max(0, args.max_singleton_traces),
                        fixed_shifts=fixed_trace_shifts.get(row_index) or None,
                        trace_cache=singleton_trace_caches.setdefault(row_index, {}),
                        monotone_pair_cache=singleton_pair_caches.setdefault(row_index, set()),
                    )
                )
                traces += added
        if not args.omit_prefix_tuples and not args.increment_seed_only:
            tuples = tuple_tables.get(str(incremented_discovery["task_id"])) or []
            depth, nodes = add_choice_tuple_trie(
                cnf, accepted_bits[start:stop], tuples
            )
            if depth:
                tuple_rows += len(tuples)
                tuple_nodes += nodes
        if not incremental_solved_progressively:
            if int(args.increment_state_shard_bits) > 0:
                (
                    satisfiable,
                    model,
                    incremental_state_shard_pattern,
                    incremental_state_shard_scan,
                ) = solve_bit_assumption_shards(
                    solver,
                    state_shard_variables(int(args.increment_state_shard_bits)),
                    int(args.increment_state_shard_bits),
                    deadline=solve_deadline,
                    per_shard_time_limit=float(
                        args.increment_state_shard_time_limit
                    ),
                )
            else:
                satisfiable, model = solve_with_deadline(solver, solve_deadline)
            if args.lazy_rejected_inequalities:
                satisfiable, model, rejection_profile_certified = certify_lazy_rejections(
                    solver,
                    cnf,
                    raw_bits,
                    maxima,
                    gaps,
                    forbidden_values,
                    satisfiable,
                    model,
                    draw_stop=len(maxima),
                    batch_size=args.rejection_cegar_batch_size,
                    max_solves=args.rejection_cegar_max_solves,
                    deadline=solve_deadline,
                    seen=rejection_seen,
                    steps=rejection_cegar_steps,
                )
        status_now = (
            "sat" if satisfiable is True else "unsat" if satisfiable is False else "unknown"
        )
        rows.append(incremented_discovery)
        step = {
            "round": len(rows),
            "task_id": str(incremented_discovery["task_id"]),
            "constraint_added": "incremental-discovery-row",
            "traces_added": added,
            "state_shard_bits": int(args.increment_state_shard_bits),
            "state_shard_pattern": incremental_state_shard_pattern,
            "state_shard_scan": incremental_state_shard_scan,
            "status": status_now,
        }
        if satisfiable is True:
            raw_values = [model_uint(model, bits) for bits in raw_bits]
            validation = validate_model(
                raw_values,
                rows,
                maxima,
                expected,
                round_slices,
                forbidden_values,
                seed_method=args.seed_method,
                seed_slices=seed_slices[: len(rows) + 1],
                seed_labels=layout_seed_labels[: len(rows) + 1],
                counterexample_batch_size=args.cegar_batch_size,
                counterexample_low_position_threshold=args.cegar_low_position_threshold,
                counterexample_low_batch_size=args.cegar_low_batch_size,
                counterexample_domain_batch_size=args.cegar_domain_batch_size,
                counterexample_domain_low_position_threshold=(
                    args.cegar_domain_low_position_threshold
                ),
                counterexample_domain_low_batch_size=args.cegar_domain_low_batch_size,
            )
            step["public_replay_reason"] = validation.get("reason")
            step["rows_passed"] = validation.get("rows_passed")
        else:
            validation = {
                "valid": False,
                "reason": "no-sat-model",
                "rows_passed": 0,
            }
        cegar_steps.append(step)
        excluded_discovery = next_excluded_discovery(rows, discovery_rows)
    if args.checkpoint_output:
        write_cegar_checkpoint(
            args.checkpoint_output,
            corridor_rank=args.corridor_rank,
            rounds=len(rows),
            status=(
                "sat" if satisfiable is True else "unsat" if satisfiable is False else "unknown"
            ),
            traces=traces,
            steps=cegar_steps,
            validation=validation,
            rejection_steps=rejection_cegar_steps,
            rejection_certified=rejection_profile_certified,
        )
    for _iteration in range(max(0, int(args.cegar_iterations))):
        if satisfiable is not True or validation.get("valid"):
            break
        failed_round = validation.get("failed_round")
        hint = tuple(int(value) for value in validation.get("singleton_trace_hint") or [])
        if not failed_round or not hint:
            break
        row_index = int(failed_round) - 1
        key = (row_index, hint)
        if args.cegar_strategy == "singleton-trace":
            if key in cegar_seen:
                break
            cegar_seen.add(key)
        row = rows[row_index]
        start, stop = round_slices[row_index]
        blocked_choice_bits = 0
        if args.cegar_strategy == "block-choice-slice":
            added = 0
            blocked_choice_bits = block_choice_slice(
                cnf,
                model,
                accepted_bits[start:stop],
                min(hint),
            )
        else:
            constraint_statistics = {}
            hint_kind = str(validation.get("singleton_trace_hint_kind") or "")
            added = add_observed_singleton_traces(
                cnf,
                accepted_bits[start:stop],
                row,
                observed_positions=set(hint),
                fixed_shifts=fixed_trace_shifts.get(row_index) or None,
                trace_cache=singleton_trace_caches.setdefault(row_index, {}),
                monotone_pair_cache=singleton_pair_caches.setdefault(row_index, set()),
                statistics=constraint_statistics,
                enforce_monotone=hint_kind in {"monotone", "domain-monotone"},
                allow_ambiguous_domains=hint_kind.startswith("domain-"),
            )
            monotone_pairs_added = constraint_statistics.get("monotone_pairs_added", 0)
            if not added and not monotone_pairs_added:
                break
            traces += added
        satisfiable, model = solve_with_deadline(solver, solve_deadline)
        if args.lazy_rejected_inequalities:
            satisfiable, model, rejection_profile_certified = certify_lazy_rejections(
                solver,
                cnf,
                raw_bits,
                maxima,
                gaps,
                forbidden_values,
                satisfiable,
                model,
                draw_stop=len(maxima),
                batch_size=args.rejection_cegar_batch_size,
                max_solves=args.rejection_cegar_max_solves,
                deadline=solve_deadline,
                seen=rejection_seen,
                steps=rejection_cegar_steps,
            )
        status_now = (
            "sat" if satisfiable is True else "unsat" if satisfiable is False else "unknown"
        )
        step: dict[str, Any] = {
            "round": int(failed_round),
            "task_id": str(row["task_id"]),
            "observed_positions_added": list(hint),
            "traces_added": added,
            "monotone_pairs_added": (
                0 if args.cegar_strategy == "block-choice-slice" else monotone_pairs_added
            ),
            "choice_bits_blocked": blocked_choice_bits,
            "cegar_strategy": args.cegar_strategy,
            "counterexample_kind": (
                None if args.cegar_strategy == "block-choice-slice" else hint_kind
            ),
            "status": status_now,
        }
        if satisfiable is True:
            raw_values = [model_uint(model, bits) for bits in raw_bits]
            validation = validate_model(
                raw_values,
                rows,
                maxima,
                expected,
                round_slices,
                forbidden_values,
                seed_method=args.seed_method,
                seed_slices=seed_slices[: len(rows) + 1],
                seed_labels=layout_seed_labels[: len(rows) + 1],
                counterexample_batch_size=args.cegar_batch_size,
                counterexample_low_position_threshold=args.cegar_low_position_threshold,
                counterexample_low_batch_size=args.cegar_low_batch_size,
                counterexample_domain_batch_size=args.cegar_domain_batch_size,
                counterexample_domain_low_position_threshold=(
                    args.cegar_domain_low_position_threshold
                ),
                counterexample_domain_low_batch_size=args.cegar_domain_low_batch_size,
            )
            step["public_replay_reason"] = validation.get("reason")
            step["rows_passed"] = validation.get("rows_passed")
        else:
            validation = {
                "valid": False,
                "reason": "no-sat-model",
                "rows_passed": 0,
            }
        cegar_steps.append(step)
        if args.checkpoint_output:
            write_cegar_checkpoint(
                args.checkpoint_output,
                corridor_rank=args.corridor_rank,
                rounds=len(rows),
                status=status_now,
                traces=traces,
                steps=cegar_steps,
                validation=validation,
                rejection_steps=rejection_cegar_steps,
                rejection_certified=rejection_profile_certified,
            )
    solve_seconds = time.monotonic() - solve_started
    status = "sat" if satisfiable is True else "unsat" if satisfiable is False else "unknown"
    public_fit = bool(satisfiable is True and validation.get("valid"))
    excluded_validation: dict[str, Any] | None = None
    if public_fit and excluded_discovery is not None:
        validation_rows = [*rows, excluded_discovery]
        (
            validation_maxima,
            validation_expected,
            validation_round_slices,
            validation_seed_slices,
        ) = build_draw_layout(validation_rows, prelude, args.seed_method)
        # The natural future rejection count is unknown, so retain generous
        # headroom while keeping the concrete stream entirely in memory.
        future_raw = concrete_raw_values_from_model(
            model, len(validation_maxima) + 10_000
        )
        excluded_validation = validate_model(
            future_raw,
            validation_rows,
            validation_maxima,
            validation_expected,
            validation_round_slices,
            seed_duplicate_forbidden_values(
                validation_seed_slices, validation_expected
            ) if args.seed_method == "integers-unique" else {},
            seed_method=args.seed_method,
            seed_slices=validation_seed_slices,
            seed_labels=[prelude] + [
                [int(value) for value in row.get("discovery_seed_label") or []]
                for row in validation_rows
            ],
        )
    excluded_predicted = bool(
        excluded_validation is not None and excluded_validation.get("valid")
    )
    promoted = (
        excluded_predicted
        and not args.omit_rejected_inequalities
        and rejection_profile_certified
    )
    selected_relaxed_rejection_windows: list[dict[str, Any]] = []
    if satisfiable is True and model is not None and relaxed_rejection_windows:
        selected_gaps = replay_rejection_gaps(
            [model_uint(model, bits) for bits in raw_bits],
            maxima[:initial_draw_stop],
            forbidden_values,
        )
        if selected_gaps is not None:
            selected_relaxed_rejection_windows = [
                {
                    "start": start,
                    "stop": stop,
                    "gaps": selected_gaps[start:stop],
                }
                for start, stop in relaxed_rejection_windows
            ]
    report = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "numpy-mt19937-fixed-rejection-profile-cegar",
        "summary": {
            "status": status,
            "seed_method": args.seed_method,
            "mt_encoding": args.mt_encoding,
            "choice_seed_events_bound": len(choice_seed_events_bound_set),
            "lazy_choice_seed_events": bool(args.lazy_choice_seed_events),
            "choice_prefix_encoding": args.choice_prefix_encoding,
            "incremental_choice_prefix_depth_bound": (
                incremental_choice_prefix_depth_bound
            ),
            "corridor_rank": int(args.corridor_rank),
            "rounds_modelled": len(rows),
            "discovery_seed_labels_bound": sum(
                len(row.get("discovery_seed_label") or []) for row in rows
            ),
            "sealed_shuffle_rounds_modelled": sum(
                row.get("seed_label_partition") != "discovery" for row in rows
            ),
            "sealed_seed_labels_opened": 0,
            "bounded_draws": len(maxima),
            "fixed_rejections": sum(gaps),
            "relaxed_rejection_windows": relaxed_rejection_metadata,
            "selected_relaxed_rejection_windows": (
                selected_relaxed_rejection_windows
            ),
            "relaxed_rejection_alternatives_product": (
                int(np.prod([
                    item["alternatives"] for item in relaxed_rejection_metadata
                ], dtype=object))
                if relaxed_rejection_metadata
                else 1
            ),
            "rejected_word_inequalities_enforced": bool(
                not args.omit_rejected_inequalities
                and rejection_profile_certified
            ),
            "lazy_rejection_cegar": bool(args.lazy_rejected_inequalities),
            "rejection_cegar_max_solves_per_pass": int(
                args.rejection_cegar_max_solves
            ),
            "wall_time_limit_seconds": float(args.wall_time_limit),
            "rejection_profile_certified": bool(rejection_profile_certified),
            "rejection_constraints_added": len(rejection_seen),
            "rejection_cegar_solves": len(rejection_cegar_steps),
            "raw_cap": raw_cap,
            "initial_raw_cap": initial_raw_cap,
            "singleton_uid_traces_bound": traces,
            "singleton_trace_selection": args.singleton_trace_selection,
            "state_shard_bits": len(state_shard),
            "state_shard_index": int(args.state_shard_index),
            "state_shard_variables": state_shard,
            "first_late_trace_position": fixed_first_trace_position,
            "first_late_trace_shift": (
                int(args.first_late_trace_shift)
                if args.first_late_trace_shift >= 0
                else None
            ),
            "fixed_trace_shifts": [
                {
                    "round": row_index + 1,
                    "position": position,
                    "shift": shift,
                }
                for row_index, shifts in sorted(fixed_trace_shifts.items())
                for position, shift in sorted(shifts.items())
            ],
            "fixed_trace_positions_prebound": fixed_trace_positions_prebound,
            "fixed_trace_monotone_pairs_prebound": (
                fixed_trace_monotone_pairs_prebound
            ),
            "fixed_accepted_draws": [
                {"draw": draw, "value": value}
                for draw, value in sorted(fixed_accepted_draws.items())
            ],
            "fixed_accepted_draws_bound": len(fixed_accepted_draws_bound),
            "shuffle_encoding": args.shuffle_encoding,
            "full_shuffle_task_selectors": list(args.full_shuffle_task),
            "full_shuffle_rounds_bound": full_shuffle_rounds,
            "full_shuffle_positions_bound": full_shuffle_positions,
            "linear_domain_rounds_bound": linear_domain_rounds,
            "linear_domain_automaton_states": linear_domain_states,
            "linear_domain_automaton_edges": linear_domain_edges,
            "cegar_iterations_completed": len(cegar_steps),
            "cegar_iterations_resumed": len(resumed_steps),
            "increment_next_discovery": bool(args.increment_next_discovery),
            "increment_seed_only": bool(args.increment_seed_only),
            "increment_state_shard_bits": int(args.increment_state_shard_bits),
            "increment_state_shard_time_limit": float(
                args.increment_state_shard_time_limit
            ),
            "increment_integer_progressive": bool(
                args.increment_integer_progressive
            ),
            "increment_integer_prefix_depth_bound": (
                incremental_integer_prefix_depth_bound
                if incremented_discovery is not None
                else 0
            ),
            "increment_choice_progressive": bool(args.increment_choice_progressive),
            "increment_choice_anchor_shard_bits": int(
                args.increment_choice_anchor_shard_bits
            ),
            "increment_choice_zero_target_encoding": (
                args.increment_choice_zero_target_encoding
            ),
            "increment_choice_progressive_steps": len(
                [
                    step
                    for step in cegar_steps
                    if step.get("constraint_added")
                    == "incremental-choice-seed-position"
                ]
            ),
            "cegar_strategy": args.cegar_strategy,
            "prefix_tuple_rows_bound": tuple_rows,
            "prefix_tuple_trie_nodes": tuple_nodes,
            "variables": cnf.next_variable - 1,
            "cnf_clauses": cnf.clauses,
            "xor_clauses": cnf.xor_clauses,
            "build_seconds": round(build_seconds, 6),
            "solve_seconds": round(solve_seconds, 6),
            "public_replay_rows_passed": int(validation.get("rows_passed") or 0),
            "public_replay_reason": validation.get("reason"),
            "included_public_fit": public_fit,
            "excluded_discovery_task_id": (
                str(excluded_discovery["task_id"]) if excluded_discovery else None
            ),
            "excluded_discovery_replay_reason": (
                excluded_validation.get("reason") if excluded_validation else None
            ),
            "excluded_discovery_rows_passed": (
                int(excluded_validation.get("rows_passed") or 0)
                if excluded_validation
                else 0
            ),
            "candidate_promoted": promoted,
            "mt19937_state_recovered": promoted,
            "excluded_discovery_predicted": excluded_predicted,
        },
        "validation": validation,
        "excluded_discovery_validation": excluded_validation,
        "cegar_steps": cegar_steps,
        "rejection_cegar_steps": rejection_cegar_steps,
        "interpretation": (
            (
                "This is a diagnostic relaxation with rejected-word inequalities omitted; "
                "neither SAT nor replay can promote a generator candidate."
                if args.omit_rejected_inequalities
                else "UNSAT rejects this exact rejection profile. SAT is retained only when "
                "its model replays every selected public shuffle. Promotion additionally "
                "requires an excluded Discovery row prediction; Holdout remains a separate "
                "future gate."
            )
        ),
        "safety": {
            "holdout_labels_opened": False,
            "sealed_seed_labels_opened": False,
            "state_persisted": False,
            "raw_stream_persisted": False,
            "network_reads": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    if args.checkpoint_output:
        write_cegar_checkpoint(
            args.checkpoint_output,
            corridor_rank=args.corridor_rank,
            rounds=len(rows),
            status=status,
            traces=traces,
            steps=cegar_steps,
            validation=validation,
            rejection_steps=rejection_cegar_steps,
            rejection_certified=rejection_profile_certified,
            final=True,
        )
    print(json.dumps({"output": str(args.output.resolve()), **report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
