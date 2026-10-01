#!/usr/bin/env python3
"""Native XOR-SAT over MT19937, bounded-draw alignment, and full shuffles.

With ``--enforce-rejected-values`` the model applies NumPy ``rk_interval``
accept/reject inequalities exactly within the selected rejection shard. SAT is
still only a compatibility milestone until a model predicts an excluded row.

The model uses public Discovery labels and public shuffle order only.  It never
reads Holdout labels and never persists an MT state or exact UID permutation.
Run through ``uv run --with pycryptosat``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import heapq
import json
import math
from pathlib import Path
import time
from typing import Any

try:
    from tools.preseed_mt19937_rank_audit import STATE_BITS, SymbolicMT19937
    from tools.preseed_mt_cpsat_joint import atomic_json
    from tools.preseed_shuffle_prefix_domains import missing_tokens, uid_aware_sequences
except ModuleNotFoundError:
    from preseed_mt19937_rank_audit import STATE_BITS, SymbolicMT19937
    from preseed_mt_cpsat_joint import atomic_json
    from preseed_shuffle_prefix_domains import missing_tokens, uid_aware_sequences


def interval_bits(maximum: int) -> int:
    mask = int(maximum)
    mask |= mask >> 1
    mask |= mask >> 2
    mask |= mask >> 4
    mask |= mask >> 8
    return max(1, mask.bit_length())


class Cnf:
    def __init__(self, solver: Any) -> None:
        self.solver = solver
        self.next_variable = STATE_BITS + 1
        self.clauses = 0
        self.xor_clauses = 0
        self.true = self.new()
        self.add([self.true])

    def new(self) -> int:
        value = self.next_variable
        self.next_variable += 1
        return value

    def add(self, values: list[int]) -> None:
        self.solver.add_clause(values)
        self.clauses += 1

    def xor(self, values: list[int], rhs: bool = False) -> None:
        self.solver.add_xor_clause(values, rhs)
        self.xor_clauses += 1

    def imply_equal(self, condition: int, left: int, right: int) -> None:
        self.add([-condition, -left, right])
        self.add([-condition, left, -right])

    def at_most_one(self, values: list[int]) -> None:
        for offset, left in enumerate(values):
            for right in values[offset + 1 :]:
                self.add([-left, -right])

    def exact_value(self, bits: list[int], value: int, name: str = "") -> int:
        del name
        selector = self.new()
        expected = [bit if (value >> index) & 1 else -bit for index, bit in enumerate(bits)]
        for literal in expected:
            self.add([-selector, literal])
        self.add([selector, *[-literal for literal in expected]])
        return selector


def add_symbolic_raw_bits(cnf: Cnf, raw_cap: int, exposed_bits: int = 10) -> list[list[int]]:
    symbolic = SymbolicMT19937(position=0)
    raw_bits: list[list[int]] = []
    for _raw_index in range(raw_cap):
        expressions = symbolic.next_word()
        current = []
        for bit in range(exposed_bits):
            output = cnf.new()
            coefficients = int(expressions[bit])
            variables = [output]
            while coefficients:
                lowest = coefficients & -coefficients
                variables.append(lowest.bit_length())
                coefficients ^= lowest
            cnf.xor(variables, False)
            current.append(output)
        raw_bits.append(current)
    return raw_bits


def xor_output(cnf: Cnf, inputs: list[int]) -> int:
    """Return a variable equal to the XOR of a small positive-literal list."""
    if not inputs:
        return -cnf.true
    if len(inputs) == 1:
        return inputs[0]
    output = cnf.new()
    cnf.xor([output, *inputs], False)
    return output


def temper_coefficient_masks() -> tuple[int, ...]:
    """Return the direct 32x32 GF(2) transform for MT tempering."""
    word = [1 << bit for bit in range(32)]
    stage1 = [
        word[bit] ^ word[bit + 11] if bit + 11 < 32 else word[bit]
        for bit in range(32)
    ]
    stage2 = [
        stage1[bit] ^ stage1[bit - 7]
        if bit >= 7 and (0x9D2C5680 >> bit) & 1
        else stage1[bit]
        for bit in range(32)
    ]
    stage3 = [
        stage2[bit] ^ stage2[bit - 15]
        if bit >= 15 and (0xEFC60000 >> bit) & 1
        else stage2[bit]
        for bit in range(32)
    ]
    return tuple(
        stage3[bit] ^ stage3[bit + 18]
        if bit + 18 < 32
        else stage3[bit]
        for bit in range(32)
    )


TEMPER_COEFFICIENT_MASKS = temper_coefficient_masks()


def temper_sparse(cnf: Cnf, word: list[int]) -> list[int]:
    """Connect tempered bits directly to one state word without stage gates."""
    outputs = []
    for coefficients in TEMPER_COEFFICIENT_MASKS:
        inputs = []
        value = int(coefficients)
        while value:
            lowest = value & -value
            inputs.append(word[lowest.bit_length() - 1])
            value ^= lowest
        outputs.append(xor_output(cnf, inputs))
    return outputs


def twist_sparse(cnf: Cnf, state: list[list[int]]) -> list[list[int]]:
    """Encode legacy RandomState's in-place MT19937 twist with local XORs."""
    result = list(state)

    def replace(index: int, source_index: int, following_index: int) -> None:
        current = result[index]
        following = result[following_index]
        mixed = following[:31] + [current[31]]
        word = []
        for bit in range(32):
            inputs = [result[source_index][bit]]
            if bit < 31:
                inputs.append(mixed[bit + 1])
            if (0x9908B0DF >> bit) & 1:
                inputs.append(mixed[0])
            word.append(xor_output(cnf, inputs))
        result[index] = word

    for index in range(624 - 397):
        replace(index, index + 397, index + 1)
    for index in range(624 - 397, 623):
        replace(index, index + 397 - 624, index + 1)
    replace(623, 396, 0)
    return result


class SparseMtCnfStream:
    """Incrementally expose one sparse MT stream without rebuilding its prefix."""

    def __init__(self, cnf: Cnf, exposed_bits: int = 10, position: int = 0) -> None:
        if position < 0 or position > 624:
            raise ValueError("invalid MT position")
        self.cnf = cnf
        self.exposed_bits = int(exposed_bits)
        self.state = [
            [word * 32 + bit + 1 for bit in range(32)]
            for word in range(624)
        ]
        self.cursor = int(position)
        self.raw_bits: list[list[int]] = []

    def ensure(self, raw_cap: int) -> list[list[int]]:
        while len(self.raw_bits) < max(0, int(raw_cap)):
            if self.cursor >= 624:
                self.state = twist_sparse(self.cnf, self.state)
                self.cursor = 0
            self.raw_bits.append(
                temper_sparse(self.cnf, self.state[self.cursor])[: self.exposed_bits]
            )
            self.cursor += 1
        return self.raw_bits


def add_sparse_mt_raw_bits(
    cnf: Cnf, raw_cap: int, exposed_bits: int = 10, position: int = 0
) -> list[list[int]]:
    """Expose MT words through sparse twist/temper gates instead of dense XOR rows."""
    return SparseMtCnfStream(cnf, exposed_bits, position).ensure(raw_cap)


def imply_unsigned_greater(
    cnf: Cnf, condition: int, bits: list[int], maximum: int
) -> None:
    """Encode ``condition -> unsigned(bits) > maximum`` with prefix witnesses."""
    witnesses = []
    for bit in range(len(bits) - 1, -1, -1):
        if (maximum >> bit) & 1:
            continue
        witness = cnf.new()
        witnesses.append(witness)
        cnf.add([-witness, bits[bit]])
        for higher in range(bit + 1, len(bits)):
            expected = bits[higher] if (maximum >> higher) & 1 else -bits[higher]
            cnf.add([-witness, expected])
    cnf.add([-condition, *witnesses])


def constrain_unsigned_at_most(cnf: Cnf, bits: list[int], maximum: int) -> None:
    """Forbid only the unused masked values instead of enumerating valid ones."""
    largest = (1 << len(bits)) - 1
    for invalid in range(int(maximum) + 1, largest + 1):
        cnf.add(
            [
                -bit if (invalid >> offset) & 1 else bit
                for offset, bit in enumerate(bits)
            ]
        )


def conjunction(cnf: Cnf, literals: list[int]) -> int:
    result = cnf.new()
    for literal in literals:
        cnf.add([-result, literal])
    cnf.add([result, *[-literal for literal in literals]])
    return result


def disjunction(cnf: Cnf, literals: list[int]) -> int:
    if not literals:
        return -cnf.true
    result = cnf.new()
    for literal in literals:
        cnf.add([-literal, result])
    cnf.add([-result, *literals])
    return result


def unsigned_at_most_literal(cnf: Cnf, bits: list[int], maximum: int) -> int:
    """Return a literal equivalent to ``unsigned(bits) <= maximum``."""
    equal_prefix = cnf.true
    greater_witnesses: list[int] = []
    for bit_index in range(len(bits) - 1, -1, -1):
        bit = bits[bit_index]
        maximum_bit = (int(maximum) >> bit_index) & 1
        if not maximum_bit:
            greater_witnesses.append(conjunction(cnf, [equal_prefix, bit]))
        equal_prefix = conjunction(
            cnf, [equal_prefix, bit if maximum_bit else -bit]
        )
    greater = disjunction(cnf, greater_witnesses)
    return -greater


def rejection_statistics(maxima: list[int]) -> tuple[float, float]:
    """Mean and variance of cumulative masked-sampler rejections."""
    mean = variance = 0.0
    for maximum in maxima:
        probability = (int(maximum) + 1) / (1 << interval_bits(maximum))
        mean += (1.0 - probability) / probability
        variance += (1.0 - probability) / (probability * probability)
    return mean, variance


def checkpoint_value_order(
    maxima: list[int], draw: int, available: list[int]
) -> list[int]:
    """Try cumulative rejection states nearest the distribution mean first."""
    mean, _variance = rejection_statistics(maxima[: int(draw)])
    return sorted((int(value) for value in available), key=lambda value: (abs(value - mean), value))


def shard_assumption_order(
    ordered: list[Any], shard_count: int, shard_index: int
) -> list[Any]:
    """Return one exact, disjoint strided partition of an assumption order.

    Striding preserves the useful mean-first ordering inside every worker while
    allowing independent solver processes to cover the global scan without
    duplicate assumptions. A shard may prove SAT, but local exhaustion must
    not be promoted to global UNSAT without aggregating every shard.
    """
    count = int(shard_count)
    index = int(shard_index)
    if count < 1:
        raise ValueError("assumption shard count must be at least one")
    if index < 0 or index >= count:
        raise ValueError("assumption shard index must be within shard count")
    return list(ordered[index::count])


def solve_checkpoint_assumptions(
    solver: Any,
    states: dict[int, int],
    ordered_values: list[int],
    *,
    time_limit: float,
    max_values: int = 0,
) -> tuple[bool | None, Any, list[dict[str, Any]], bool]:
    """Solve one exact alignment state at a time without rebuilding the CNF.

    The returned ``complete`` flag is true only if every reachable value in
    ``states`` was decided.  Therefore a set of UNSAT shards is promoted to a
    global UNSAT result only when the scan is complete; timeouts remain
    UNKNOWN and can never accidentally reject the generator family.
    """
    records: list[dict[str, Any]] = []
    model = None
    attempted = 0
    for value in ordered_values:
        if int(value) not in states:
            continue
        if max_values > 0 and attempted >= int(max_values):
            break
        attempted += 1
        started = time.monotonic()
        satisfiable, candidate_model = solver.solve(
            assumptions=[states[int(value)]],
            time_limit=max(0.01, float(time_limit)),
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
                "cumulative_rejections": int(value),
                "status": status,
                "solve_seconds": round(time.monotonic() - started, 6),
            }
        )
        if satisfiable is True:
            model = candidate_model
            return True, model, records, False
    scanned = {record["cumulative_rejections"] for record in records}
    complete = scanned == set(states)
    if complete and all(record["status"] == "unsat" for record in records):
        return False, None, records, True
    return None, None, records, complete


def checkpoint_profile_order(
    maxima: list[int],
    layers: dict[int, dict[int, int]],
    *,
    limit: int = 0,
) -> list[tuple[int, ...]]:
    """Order profiles by independent-increment standardized distance.

    Cumulative rejection counts at nested checkpoints are strongly correlated.
    Ranking every cumulative value as if it were independent wastes the early
    solver budget. Rejection increments over disjoint draw windows are
    independent, so score each ``value - previous_value`` against the moments
    of the corresponding slice instead.
    """
    draws = sorted(layers)
    moments: dict[int, tuple[float, float]] = {}
    previous_draw = 0
    for draw in draws:
        moments[draw] = rejection_statistics(maxima[previous_draw:draw])
        previous_draw = draw

    keep = max(0, int(limit))
    partials: dict[int, list[tuple[float, tuple[int, ...]]]] = {
        0: [(0.0, ())]
    }
    for draw in draws:
        mean, variance = moments[draw]
        following: dict[int, list[tuple[float, tuple[int, ...]]]] = {}
        for value in sorted(layers[draw]):
            candidates: list[tuple[float, tuple[int, ...]]] = []
            for previous_value, ranked in partials.items():
                if int(value) < int(previous_value):
                    continue
                increment = int(value) - int(previous_value)
                added = ((increment - mean) ** 2) / max(variance, 1e-9)
                candidates.extend(
                    (score + added, profile + (int(value),))
                    for score, profile in ranked
                )
            following[int(value)] = (
                heapq.nsmallest(keep, candidates)
                if keep > 0 and len(candidates) > keep
                else sorted(candidates)
            )
        partials = following
    ranked_profiles = [item for ranked in partials.values() for item in ranked]
    if keep > 0:
        ranked_profiles = heapq.nsmallest(keep, ranked_profiles)
    else:
        ranked_profiles.sort()
    return [profile for _score, profile in ranked_profiles]


def checkpoint_profile_count(layers: dict[int, dict[int, int]]) -> int:
    """Count every monotone cumulative-rejection profile without materializing it."""
    counts = {0: 1}
    for draw in sorted(layers):
        following: dict[int, int] = {}
        for value in layers[draw]:
            following[int(value)] = sum(
                count
                for previous, count in counts.items()
                if int(previous) <= int(value)
            )
        counts = following
    return sum(counts.values())


def solve_checkpoint_profiles(
    solver: Any,
    layers: dict[int, dict[int, int]],
    ordered_profiles: list[tuple[int, ...]],
    *,
    time_limit: float,
    max_profiles: int = 0,
) -> tuple[bool | None, Any, list[dict[str, Any]], bool]:
    """Solve exact cumulative-rejection profiles with temporary assumptions."""
    draws = sorted(layers)
    records: list[dict[str, Any]] = []
    model = None
    for profile in ordered_profiles:
        if max_profiles > 0 and len(records) >= int(max_profiles):
            break
        started = time.monotonic()
        assumptions = [
            layers[draw][int(value)] for draw, value in zip(draws, profile)
        ]
        satisfiable, candidate_model = solver.solve(
            assumptions=assumptions,
            time_limit=max(0.01, float(time_limit)),
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
                "checkpoint_rejections": {
                    str(draw): int(value) for draw, value in zip(draws, profile)
                },
                "status": status,
                "solve_seconds": round(time.monotonic() - started, 6),
            }
        )
        if satisfiable is True:
            model = candidate_model
            return True, model, records, False
    complete = len(records) == len(ordered_profiles)
    if complete and all(record["status"] == "unsat" for record in records):
        return False, None, records, True
    return None, None, records, complete


def statistical_rejection_checkpoints(
    maxima: list[int], *, interval: int = 64, sigma: float = 4.0
) -> dict[int, tuple[int, int]]:
    """High-coverage cumulative rejection bands for a bounded-draw stream."""
    checkpoints: dict[int, tuple[int, int]] = {}
    step = max(1, int(interval))
    for stop in range(step, len(maxima) + step, step):
        draw = min(stop, len(maxima))
        mean, variance = rejection_statistics(maxima[:draw])
        radius = max(1.0, float(sigma) * math.sqrt(variance))
        checkpoints[draw] = (
            max(0, math.floor(mean - radius)),
            math.ceil(mean + radius),
        )
        if draw == len(maxima):
            break
    return checkpoints


def profile_rejection_checkpoints(
    rejection_gaps: list[int], draw_count: int, interval: int
) -> dict[int, tuple[int, int]]:
    """Convert one exact gap path into coarser cumulative checkpoint shards."""
    draws = max(0, int(draw_count))
    step = max(1, int(interval))
    if len(rejection_gaps) < draws:
        raise ValueError("fixed rejection profile is shorter than the draw layout")
    checkpoints: dict[int, tuple[int, int]] = {}
    cumulative = 0
    for draw, gap in enumerate(rejection_gaps[:draws], start=1):
        if int(gap) < 0:
            raise ValueError("fixed rejection profile contains a negative gap")
        cumulative += int(gap)
        if draw % step == 0 or draw == draws:
            checkpoints[draw] = (cumulative, cumulative)
    return checkpoints


def state_shard_variables(bits: int) -> list[int]:
    """Select deterministic, evenly spaced MT state bits for exact sharding."""
    count = max(0, int(bits))
    if count > 20:
        raise ValueError("state sharding is capped at 20 bits")
    if not count:
        return []
    return [1 + (index * STATE_BITS) // count for index in range(count)]


def bind_state_shard(cnf: Cnf, bits: int, index: int) -> list[int]:
    variables = state_shard_variables(bits)
    if not 0 <= int(index) < (1 << len(variables)):
        raise ValueError("state shard index is outside the selected partition")
    for offset, variable in enumerate(variables):
        cnf.add([variable if (int(index) >> offset) & 1 else -variable])
    return variables


def equal_vectors(cnf: Cnf, left: list[int], right: list[int]) -> int:
    if len(left) != len(right):
        raise ValueError("bit vectors must have equal width")
    equal_bits = []
    for a, b in zip(left, right):
        equal = cnf.new()
        cnf.add([-equal, -a, b])
        cnf.add([-equal, a, -b])
        cnf.add([equal, a, b])
        cnf.add([equal, -a, -b])
        equal_bits.append(equal)
    return conjunction(cnf, equal_bits)


def add_permutation_target(
    cnf: Cnf,
    choices: list[list[int]],
    final_position: int,
    target: int,
) -> None:
    """Bind one final Fisher--Yates position by tracing it backwards."""
    size = len(choices) + 1
    width = interval_bits(size - 1)
    true = cnf.true
    if not 0 <= int(final_position) < size or not 0 <= int(target) < size:
        cnf.add([])
        return
    # If an initial value t must finish below t, no already-processed higher
    # swap i>t may choose t, and swap i=t must move it away from t. Express
    # this necessary condition directly; the trace circuit implies it, but a
    # single inequality clause per draw propagates far more strongly.
    if int(final_position) < int(target):
        for i in range(int(target), size):
            choice = choices[size - 1 - i]
            cnf.add(
                [
                    -bit if (int(target) >> offset) & 1 else bit
                    for offset, bit in enumerate(choice)
                ]
            )
    position = [
        true if (int(final_position) >> bit) & 1 else -true
        for bit in range(width)
    ]
    # Forward choices are i=n-1..1. Undo swaps in the opposite order.
    for i in range(1, size):
        choice = list(choices[size - 1 - i])
        choice.extend([-true] * (width - len(choice)))
        at_i = cnf.exact_value(position, i)
        at_choice = equal_vectors(cnf, position, choice)
        use_i = at_i
        use_choice = conjunction(cnf, [-at_i, at_choice])
        unchanged = conjunction(cnf, [-at_i, -at_choice])
        following = [cnf.new() for _ in range(width)]
        for bit, output in enumerate(following):
            cnf.imply_equal(use_i, output, choice[bit])
            i_literal = true if (i >> bit) & 1 else -true
            cnf.imply_equal(use_choice, output, i_literal)
            cnf.imply_equal(unchanged, output, position[bit])
        position = following
    cnf.add([cnf.exact_value(position, int(target))])


def add_permutation_prefix(
    cnf: Cnf,
    choices: list[list[int]],
    targets: list[int],
) -> None:
    """Bind the leading values of a Fisher--Yates permutation compactly.

    Rather than materialising all 900 array cells, trace each known final
    prefix position backwards through the swaps. A three-value NumPy choice
    therefore costs O(3*n*log(n)) instead of O(n^2).
    """
    for final_position, target in enumerate(targets):
        add_permutation_target(cnf, choices, final_position, target)


def add_permutation_target_forward(
    cnf: Cnf,
    choices: list[list[int]],
    final_position: int,
    target: int,
) -> None:
    """Bind one final position by tracking its target in NumPy call order."""
    size = len(choices) + 1
    width = interval_bits(size - 1)
    true = cnf.true
    if not 0 <= int(final_position) < size or not 0 <= int(target) < size:
        cnf.add([])
        return
    if int(final_position) < int(target):
        for i in range(int(target), size):
            choice = choices[size - 1 - i]
            cnf.add(
                [
                    -bit if (int(target) >> offset) & 1 else bit
                    for offset, bit in enumerate(choice)
                ]
            )
    position = [
        true if (int(target) >> bit) & 1 else -true
        for bit in range(width)
    ]
    for offset, i in enumerate(range(size - 1, 0, -1)):
        choice = list(choices[offset])
        choice.extend([-true] * (width - len(choice)))
        at_i = cnf.exact_value(position, i)
        at_choice = equal_vectors(cnf, position, choice)
        use_choice = conjunction(cnf, [-at_i, at_choice])
        unchanged = conjunction(cnf, [-at_i, -at_choice])
        following = [cnf.new() for _ in range(width)]
        for bit, output in enumerate(following):
            cnf.imply_equal(at_i, output, choice[bit])
            i_literal = true if (i >> bit) & 1 else -true
            cnf.imply_equal(use_choice, output, i_literal)
            cnf.imply_equal(unchanged, output, position[bit])
        position = following
    cnf.add([cnf.exact_value(position, int(final_position))])


def add_permutation_prefix_forward(
    cnf: Cnf,
    choices: list[list[int]],
    targets: list[int],
) -> None:
    """Bind a known prefix by tracking each target in NumPy call order.

    This is logically equivalent to :func:`add_permutation_prefix`, but starts
    from each known array value and follows the descending Fisher--Yates swaps
    forward. Keeping the circuit direction aligned with the bounded draws can
    produce a different SAT propagation profile even though its asymptotic
    size is the same.
    """
    for final_position, target in enumerate(targets):
        add_permutation_target_forward(cnf, choices, final_position, target)


def add_permutation_first_hop_guard(
    cnf: Cnf,
    choices: list[list[int]],
    target: int,
) -> None:
    """Strengthen a final-position-zero target after its first descending hop.

    If swap ``i=target`` sends the target value to ``q``, any intervening swap
    ``i=q+1..target-1`` choosing q would move it to an already processed index,
    where it could never reach zero. Swap i=q must also move it below q. These
    implications are already entailed by the full trace, but exposing them as
    short clauses materially improves propagation under an exact j_target
    assumption.
    """
    size = len(choices) + 1
    t = int(target)
    if not 0 < t < size:
        return
    anchor = choices[size - 1 - t]
    for q in range(1, t):
        anchor_is_q = cnf.exact_value(anchor, q)
        for i in range(q, t):
            choice = choices[size - 1 - i]
            not_q = [
                -bit if (q >> offset) & 1 else bit
                for offset, bit in enumerate(choice)
            ]
            cnf.add([-anchor_is_q, *not_q])


def add_permutation_zero_target_unary(
    cnf: Cnf,
    choices: list[list[int]],
    target: int,
) -> None:
    """Bind final position zero to target with a monotone unary trace.

    While undoing Fisher--Yates swaps in ascending i order, the current source
    position is always below i. It either stays put or jumps to i when j_i
    equals that position. A unary state machine exposes this exact monotone
    transition and propagates exact anchor assumptions more strongly than the
    generic binary-position circuit.
    """
    size = len(choices) + 1
    t = int(target)
    if not 0 <= t < size:
        cnf.add([])
        return
    state = [cnf.true]
    for i in range(1, t + 1):
        choice = choices[size - 1 - i]
        next_state: list[int] = []
        hits: list[int] = []
        for position, active in enumerate(state):
            equal = cnf.exact_value(choice, position)
            hits.append(conjunction(cnf, [active, equal]))
            next_state.append(conjunction(cnf, [active, -equal]))
        next_state.append(disjunction(cnf, hits))
        state = next_state
    cnf.add([state[t]])
    for i in range(t + 1, size):
        choice = choices[size - 1 - i]
        cnf.add(
            [
                -bit if (t >> offset) & 1 else bit
                for offset, bit in enumerate(choice)
            ]
        )


def trace_final_position_to_initial(
    cnf: Cnf,
    choices: list[list[int]],
    final_position_bits: list[int],
    *,
    minimum_final_position: int = 0,
) -> list[int]:
    """Undo a Fisher--Yates shuffle for one symbolic final array position."""
    size = len(choices) + 1
    width = interval_bits(size - 1)
    true = cnf.true
    position = list(final_position_bits)
    position.extend([-true] * (width - len(position)))
    # If the final position is known to be at least p, swaps 1..p-1 cannot
    # touch it while undoing the shuffle.  Skipping that prefix is exact and
    # makes late-position singleton traces dramatically smaller.
    for i in range(max(1, int(minimum_final_position)), size):
        choice = list(choices[size - 1 - i])
        choice.extend([-true] * (width - len(choice)))
        at_i = cnf.exact_value(position, i)
        at_choice = equal_vectors(cnf, position, choice)
        use_choice = conjunction(cnf, [-at_i, at_choice])
        unchanged = conjunction(cnf, [-at_i, -at_choice])
        following = [cnf.new() for _ in range(width)]
        for bit, output in enumerate(following):
            cnf.imply_equal(at_i, output, choice[bit])
            i_literal = true if (i >> bit) & 1 else -true
            cnf.imply_equal(use_choice, output, i_literal)
            cnf.imply_equal(unchanged, output, position[bit])
        position = following
    return position


def add_observed_singleton_traces(
    cnf: Cnf,
    choices: list[list[int]],
    row: dict[str, Any],
    *,
    max_traces: int = 0,
    observed_positions: set[int] | None = None,
    selection: str = "spread",
    fixed_shifts: dict[int, int] | None = None,
    trace_cache: dict[int, list[int]] | None = None,
    monotone_pair_cache: set[tuple[int, int]] | None = None,
    statistics: dict[str, int] | None = None,
    enforce_monotone: bool = True,
    allow_ambiguous_domains: bool = False,
) -> int:
    """Bind singleton observed UID domains without a full permutation network.

    If ``m`` query log entries are missing, observed position ``p`` can occupy
    any final position in ``p..p+m``.  The shift (missing entries before the
    token) is nondecreasing across the observed sequence; enforcing that shared
    insertion path removes the exponential independent-position overestimate.
    Each selected singleton is then traced back through the swaps to its exact
    position in the initial UID array.
    """
    initial_uids = [int(value) for value in row.get("initial_uids") or []]
    observed = [str(value) for value in row.get("ordered_uid_domains") or []]
    uid_domains = {
        str(key): [int(value) for value in values]
        for key, values in (row.get("uid_domains") or {}).items()
    }
    size = len(initial_uids)
    if size != len(choices) + 1 or not observed:
        return 0
    initial_positions = {uid: index for index, uid in enumerate(initial_uids)}
    missing = max(0, size - len(observed))
    candidates: list[tuple[int, list[int]]] = []
    for observed_position, token in enumerate(observed):
        if observed_positions is not None and observed_position not in observed_positions:
            continue
        domain = uid_domains.get(token) or []
        if not domain or (len(domain) != 1 and not allow_ambiguous_domains):
            continue
        targets = [initial_positions[uid] for uid in domain if uid in initial_positions]
        if not targets:
            continue
        candidates.append((observed_position, targets))
    if observed_positions is None and max_traces > 0 and len(candidates) > max_traces:
        if selection == "late":
            # Reverse tracing position p touches only swaps p..n-1.  The late
            # observations therefore provide the cheapest exact constraints
            # and let broad multi-round scans bind many more rounds before a
            # solver timeout.  Restore observation order for the shared
            # missing-entry monotonicity constraints below.
            candidates = candidates[-max_traces:]
        elif selection == "spread":
            # Preserve coverage across the full permutation rather than taking
            # a dense prefix, whose MT information is highly correlated.
            selected = []
            for index in range(max_traces):
                offset = round(index * (len(candidates) - 1) / max(1, max_traces - 1))
                selected.append(candidates[offset])
            candidates = list({position: targets for position, targets in selected}.items())
        else:
            raise ValueError(f"unknown singleton trace selection: {selection}")
    width = interval_bits(size - 1)
    shift_rows: list[tuple[int, list[int]]] = []
    traces_added = 0
    monotone_pairs_added = 0
    for observed_position, target_initial_positions in candidates:
        selectors = trace_cache.get(observed_position) if trace_cache is not None else None
        shifts = list(range(0, min(missing, size - 1 - observed_position) + 1))
        if selectors is None:
            final_bits = [cnf.new() for _ in range(width)]
            selectors = [
                cnf.exact_value(final_bits, observed_position + shift)
                for shift in shifts
            ]
            cnf.add(selectors)
            # Pad impossible high shifts with false literals so every row uses
            # the common 0..missing index space below.
            selectors.extend([-cnf.true] * (missing + 1 - len(selectors)))
            if trace_cache is not None:
                trace_cache[observed_position] = selectors
            traced = trace_final_position_to_initial(
                cnf,
                choices,
                final_bits,
                minimum_final_position=observed_position,
            )
            cnf.add(
                [
                    cnf.exact_value(traced, initial_position)
                    for initial_position in target_initial_positions
                ]
            )
            traces_added += 1
        if fixed_shifts is not None and observed_position in fixed_shifts:
            fixed = int(fixed_shifts[observed_position])
            if fixed not in shifts:
                raise ValueError("fixed singleton shift is outside its exact range")
            cnf.add([selectors[fixed]])
        shift_rows.append((observed_position, selectors))
    adjacent_shift_rows = zip(shift_rows, shift_rows[1:]) if enforce_monotone else ()
    for (previous_position, previous), (following_position, following) in adjacent_shift_rows:
        pair = (previous_position, following_position)
        if monotone_pair_cache is not None and pair in monotone_pair_cache:
            continue
        for previous_shift in range(missing + 1):
            for following_shift in range(previous_shift):
                cnf.add([-previous[previous_shift], -following[following_shift]])
        if monotone_pair_cache is not None:
            monotone_pair_cache.add(pair)
        monotone_pairs_added += 1
    if statistics is not None:
        statistics["traces_added"] = traces_added
        statistics["monotone_pairs_added"] = monotone_pairs_added
    return traces_added


def add_alignment(
    cnf: Cnf,
    raw_bits: list[list[int]],
    maxima: list[int],
    expected: list[int | None],
    *,
    max_rejections: int,
    rejection_budget: int,
    enforce_rejected_values: bool,
    rejection_checkpoints: dict[int, tuple[int, int]],
    minimum_total_rejections: int,
) -> tuple[list[list[int]], dict[int, int], list[dict[int, int]]]:
    """Return accepted low bits and possible final cumulative rejections."""
    draws = len(maxima)
    checkpoints = dict(rejection_checkpoints)
    final_low, final_high = checkpoints.get(draws, (0, rejection_budget))
    checkpoints[draws] = (
        max(final_low, int(minimum_total_rejections)),
        min(final_high, rejection_budget),
    )
    constraints = [(0, 0, 0), *[(draw, *bounds) for draw, bounds in checkpoints.items()]]
    layer_ranges: list[tuple[int, int]] = []
    for layer in range(draws + 1):
        low, high = 0, rejection_budget
        for checkpoint_draw, checkpoint_low, checkpoint_high in constraints:
            if checkpoint_draw <= layer:
                low = max(low, checkpoint_low)
                high = min(
                    high,
                    checkpoint_high + (layer - checkpoint_draw) * max_rejections,
                )
            else:
                low = max(
                    low,
                    checkpoint_low - (checkpoint_draw - layer) * max_rejections,
                )
                high = min(high, checkpoint_high)
        layer_ranges.append((max(0, low), min(rejection_budget, high)))

    states: list[dict[int, int]] = []
    greater_cache: dict[tuple[int, int], int] = {}
    start = cnf.new()
    cnf.add([start])
    states.append({0: start})
    accepted = [
        [cnf.new() for _ in range(interval_bits(maximum))] for maximum in maxima
    ]
    for draw_index, maximum in enumerate(maxima):
        following: dict[int, int] = {}
        incoming: dict[int, list[int]] = {}
        outgoing: dict[int, list[int]] = {}
        for rejected, source in states[-1].items():
            edges = []
            for gap in range(max_rejections + 1):
                next_rejected = rejected + gap
                raw_index = draw_index + next_rejected
                next_low, next_high = layer_ranges[draw_index + 1]
                if (
                    next_rejected < next_low
                    or next_rejected > next_high
                    or raw_index >= len(raw_bits)
                ):
                    continue
                edge = cnf.new()
                edges.append(edge)
                destination = following.setdefault(next_rejected, cnf.new())
                incoming.setdefault(next_rejected, []).append(edge)
                cnf.add([-edge, source])
                cnf.add([-edge, destination])
                for bit, accepted_bit in enumerate(accepted[draw_index]):
                    cnf.imply_equal(edge, accepted_bit, raw_bits[raw_index][bit])
                if enforce_rejected_values:
                    exposed = interval_bits(maximum)
                    for attempt in range(gap):
                        rejected_raw_index = draw_index + rejected + attempt
                        key = (rejected_raw_index, maximum)
                        greater = greater_cache.get(key)
                        if greater is None:
                            greater = cnf.new()
                            greater_cache[key] = greater
                            imply_unsigned_greater(
                                cnf,
                                greater,
                                raw_bits[rejected_raw_index][:exposed],
                                maximum,
                            )
                        cnf.add([-edge, greater])
            if not edges:
                cnf.add([-source])
                continue
            outgoing[rejected] = edges
            cnf.add([-source, *edges])
            cnf.at_most_one(edges)
        for rejected, destination in following.items():
            edges = incoming[rejected]
            cnf.add([-destination, *edges])
            for edge in edges:
                cnf.add([-edge, destination])
        states.append(following)

        checkpoint = rejection_checkpoints.get(draw_index + 1)
        if checkpoint is not None:
            low, high = checkpoint
            cnf.add(
                [
                    state
                    for rejected, state in following.items()
                    if low <= rejected <= high
                ]
            )

        value = expected[draw_index]
        if value is not None:
            for offset, bit in enumerate(accepted[draw_index]):
                cnf.add([bit if (int(value) >> offset) & 1 else -bit])
        else:
            constrain_unsigned_at_most(cnf, accepted[draw_index], maximum)
    return accepted, states[-1], states


def add_alignment_lattice(
    cnf: Cnf,
    raw_bits: list[list[int]],
    maxima: list[int],
    expected: list[int | None],
    *,
    rejection_budget: int,
    rejection_checkpoints: dict[int, tuple[int, int]],
    minimum_total_rejections: int,
    forbidden_values: dict[int, list[int]] | None = None,
) -> tuple[list[list[int]], dict[int, int], list[dict[int, int]]]:
    """Exact accept/reject lattice with two transitions per reachable state.

    The older jump encoding creates one edge for every possible rejection gap.
    This lattice consumes one raw word at a time, so each state has only an
    accept and a reject transition while enforcing both inequalities exactly.
    """
    draws = len(maxima)
    checkpoints = dict(rejection_checkpoints)
    final_low, final_high = checkpoints.get(draws, (0, rejection_budget))
    checkpoints[draws] = (
        max(final_low, int(minimum_total_rejections)),
        min(final_high, rejection_budget),
    )
    constraints = [(0, 0, 0), *[(draw, *bounds) for draw, bounds in checkpoints.items()]]
    layer_ranges: list[tuple[int, int]] = []
    for layer in range(draws + 1):
        low, high = 0, rejection_budget
        for checkpoint_draw, checkpoint_low, checkpoint_high in constraints:
            if checkpoint_draw <= layer:
                low = max(low, checkpoint_low)
                high = min(high, checkpoint_high + (layer - checkpoint_draw) * rejection_budget)
            else:
                low = max(low, checkpoint_low - (checkpoint_draw - layer) * rejection_budget)
                high = min(high, checkpoint_high)
        layer_ranges.append((max(0, low), min(rejection_budget, high)))

    # A layer checkpoint applies immediately *after* an accepted draw. While
    # retrying the next draw, cumulative rejections may temporarily exceed the
    # current layer's high bound up to the following layer's permitted high.
    state_ranges = list(layer_ranges)
    for draw in range(draws):
        low, high = state_ranges[draw]
        state_ranges[draw] = (low, max(high, layer_ranges[draw + 1][1]))
    states: list[dict[int, int]] = []
    for low, high in state_ranges:
        states.append({rejected: cnf.new() for rejected in range(low, high + 1)})
    start = states[0].get(0)
    if start is None:
        cnf.add([])
    else:
        cnf.add([start])
    accepted = [[cnf.new() for _ in range(interval_bits(maximum))] for maximum in maxima]
    incoming: list[dict[int, list[int]]] = [dict() for _ in range(draws + 1)]
    comparator_cache: dict[tuple[int, int, tuple[int, ...]], int] = {}
    forbidden_values = forbidden_values or {}
    for draw, maximum in enumerate(maxima):
        for rejected, source in states[draw].items():
            raw_index = draw + rejected
            if raw_index >= len(raw_bits):
                cnf.add([-source])
                continue
            forbidden = tuple(sorted(set(forbidden_values.get(draw) or [])))
            key = (raw_index, int(maximum), forbidden)
            valid = comparator_cache.get(key)
            if valid is None:
                at_most = unsigned_at_most_literal(
                    cnf, raw_bits[raw_index][: interval_bits(maximum)], maximum
                )
                allowed = [at_most]
                for value in forbidden:
                    equal = cnf.exact_value(
                        raw_bits[raw_index][: interval_bits(maximum)], int(value)
                    )
                    allowed.append(-equal)
                valid = conjunction(cnf, allowed)
                comparator_cache[key] = valid
            edges: list[int] = []
            next_low, next_high = layer_ranges[draw + 1]
            accept_destination = (
                states[draw + 1].get(rejected)
                if next_low <= rejected <= next_high
                else None
            )
            if accept_destination is not None:
                edge = conjunction(cnf, [source, valid])
                edges.append(edge)
                cnf.add([-edge, accept_destination])
                incoming[draw + 1].setdefault(rejected, []).append(edge)
                for bit, accepted_bit in enumerate(accepted[draw]):
                    cnf.imply_equal(edge, accepted_bit, raw_bits[raw_index][bit])
            else:
                cnf.add([-source, -valid])
            reject_destination = states[draw].get(rejected + 1)
            if reject_destination is not None:
                edge = conjunction(cnf, [source, -valid])
                edges.append(edge)
                cnf.add([-edge, reject_destination])
                incoming[draw].setdefault(rejected + 1, []).append(edge)
            else:
                cnf.add([-source, valid])
            cnf.add([-source, *edges])
        # All same-layer reject predecessors have been visited in ascending
        # rejection order, so close this layer before moving to the next draw.
        for rejected, destination in states[draw].items():
            if draw == 0 and rejected == 0:
                continue
            edges = incoming[draw].get(rejected) or []
            cnf.add([-destination, *edges])
    for rejected, destination in states[-1].items():
        edges = incoming[-1].get(rejected) or []
        cnf.add([-destination, *edges])
    for draw, value in enumerate(expected):
        if value is None:
            continue
        for offset, bit in enumerate(accepted[draw]):
            cnf.add([bit if (int(value) >> offset) & 1 else -bit])
    return accepted, states[-1], states


def add_shuffle_network(
    cnf: Cnf,
    choices: list[list[int]],
    initial_tokens: list[str],
) -> list[list[int]]:
    size = len(initial_tokens)
    true = cnf.true
    pool: list[list[int]] = [
        [true if (uid >> bit) & 1 else -true for bit in range(8)]
        for uid in range(size)
    ]
    output: list[list[int] | None] = [None] * size
    for offset, shuffle_index in enumerate(range(size - 1, 0, -1)):
        bits = choices[offset]
        selectors = [cnf.exact_value(bits, value) for value in range(shuffle_index + 1)]
        cnf.add(selectors)
        # The selectors are exact, distinct encodings of the same bit-vector;
        # mutual exclusion is implied and pairwise clauses would add roughly
        # 2.8 million redundant clauses per 256-element round.
        selected = [cnf.new() for _ in range(8)]
        for position, selector in enumerate(selectors):
            for bit in range(8):
                cnf.imply_equal(selector, selected[bit], pool[position][bit])
        output[shuffle_index] = selected
        following: list[list[int]] = []
        for position in range(shuffle_index):
            word = [cnf.new() for _ in range(8)]
            selector = selectors[position]
            for bit in range(8):
                cnf.imply_equal(selector, word[bit], pool[shuffle_index][bit])
                cnf.imply_equal(-selector, word[bit], pool[position][bit])
            following.append(word)
        pool = following
    output[0] = pool[0]
    return [value for value in output if value is not None]


def add_observed_subsequence(
    cnf: Cnf,
    output: list[list[int]],
    initial_tokens: list[str],
    observed_tokens: list[str],
) -> int:
    omitted = list(missing_tokens(initial_tokens, observed_tokens))
    if len(omitted) > 12:
        raise ValueError("missing-token subset automaton is intentionally capped at 12")
    uids_by_token: dict[str, list[int]] = {}
    for uid, token in enumerate(initial_tokens):
        uids_by_token.setdefault(token, []).append(uid)
    # Create UID equalities lazily.  A position can only consume the observed
    # token at one of ``missing+1`` nearby indices or one of the omitted
    # tokens, so materialising all 256 identities at every position wastes the
    # majority of the SAT instance.
    identity: list[dict[int, int]] = [{} for _ in output]

    def is_uid(position: int, uid: int) -> int:
        known = identity[position].get(uid)
        if known is None:
            known = cnf.exact_value(output[position], uid)
            identity[position][uid] = known
        return known
    layers: list[dict[int, int]] = [{0: cnf.new()}]
    cnf.add([layers[0][0]])
    full_mask = (1 << len(omitted)) - 1
    for position in range(len(output)):
        following: dict[int, int] = {}
        incoming: dict[int, list[int]] = {}
        for mask, source in layers[-1].items():
            observed_index = position - mask.bit_count()
            transitions: list[tuple[int, str]] = []
            if 0 <= observed_index < len(observed_tokens):
                transitions.append((mask, observed_tokens[observed_index]))
            for missing_index, token in enumerate(omitted):
                if not (mask >> missing_index) & 1:
                    transitions.append((mask | (1 << missing_index), token))
            edges = []
            for following_mask, token in transitions:
                edge = cnf.new()
                edges.append(edge)
                destination = following.setdefault(following_mask, cnf.new())
                incoming.setdefault(following_mask, []).append(edge)
                cnf.add([-edge, source])
                cnf.add([-edge, destination])
                allowed = uids_by_token.get(token) or []
                if not allowed:
                    cnf.add([-edge])
                else:
                    cnf.add([-edge, *[is_uid(position, uid) for uid in allowed]])
            cnf.add([-source, *edges])
            cnf.at_most_one(edges)
        for mask, destination in following.items():
            cnf.add([-destination, *incoming[mask]])
        layers.append(following)
    final = layers[-1].get(full_mask)
    if final is None:
        cnf.add([])
    else:
        cnf.add([final])
    return len(omitted)


def add_observed_domain_subsequence(
    cnf: Cnf,
    output: list[list[int]],
    initial_tokens: list[str],
    observed_tokens: list[str],
) -> tuple[int, int]:
    """Bind an exact delete-to-observed domain subsequence in linear space.

    ``output`` is a permutation of the positions in ``initial_tokens``.  Every
    output position is either omitted or consumes the next observed domain.
    Requiring the final cursor to equal ``len(observed_tokens)`` is therefore
    exactly the statement that deleting the unobserved positions yields the
    complete observed domain sequence.  Unlike ``add_observed_subsequence``,
    this automaton does not identify the omitted token multiset, so its state
    count is O(n*m) instead of O(2**missing).
    """
    size = len(output)
    observed_size = len(observed_tokens)
    if len(initial_tokens) != size:
        raise ValueError("shuffle output and initial token lengths differ")
    if observed_size > size:
        cnf.add([])
        return 0, 0

    positions_by_token: dict[str, list[int]] = {}
    for initial_position, token in enumerate(initial_tokens):
        positions_by_token.setdefault(token, []).append(initial_position)
    identity: list[dict[int, int]] = [{} for _ in output]

    def is_initial_position(position: int, initial_position: int) -> int:
        known = identity[position].get(initial_position)
        if known is None:
            known = cnf.exact_value(output[position], initial_position)
            identity[position][initial_position] = known
        return known

    layers: list[dict[int, int]] = [{0: cnf.new()}]
    cnf.add([layers[0][0]])
    states_created = 1
    edges_created = 0
    for position in range(size):
        following: dict[int, int] = {}
        incoming: dict[int, list[int]] = {}
        remaining_after = size - position - 1
        for cursor, source in layers[-1].items():
            edges: list[int] = []

            # Delete this output position only if the remaining suffix can
            # still consume every outstanding observation.
            if remaining_after >= observed_size - cursor:
                destination = following.get(cursor)
                if destination is None:
                    destination = cnf.new()
                    following[cursor] = destination
                    states_created += 1
                edge = cnf.new()
                edges.append(edge)
                incoming.setdefault(cursor, []).append(edge)
                cnf.add([-edge, source])
                cnf.add([-edge, destination])
                edges_created += 1

            if cursor < observed_size:
                token = observed_tokens[cursor]
                allowed_positions = positions_by_token.get(token) or []
                if allowed_positions:
                    destination_cursor = cursor + 1
                    destination = following.get(destination_cursor)
                    if destination is None:
                        destination = cnf.new()
                        following[destination_cursor] = destination
                        states_created += 1
                    allowed = disjunction(
                        cnf,
                        [
                            is_initial_position(position, initial_position)
                            for initial_position in allowed_positions
                        ],
                    )
                    edge = cnf.new()
                    edges.append(edge)
                    incoming.setdefault(destination_cursor, []).append(edge)
                    cnf.add([-edge, source])
                    cnf.add([-edge, allowed])
                    cnf.add([-edge, destination])
                    edges_created += 1

            cnf.add([-source, *edges])
            cnf.at_most_one(edges)

        for cursor, destination in following.items():
            cnf.add([-destination, *incoming.get(cursor, [])])
        layers.append(following)

    final = layers[-1].get(observed_size)
    if final is None:
        cnf.add([])
    else:
        cnf.add([final])
    return states_created, edges_created


def add_choice_tuple_trie(
    cnf: Cnf, choices: list[list[int]], allowed_tuples: list[list[int]]
) -> tuple[int, int]:
    """Bind correlated prefix choices without flattening a large table to DNF."""
    if not choices or not allowed_tuples:
        return 0, 0
    depth = min(len(choices), min(len(values) for values in allowed_tuples))
    tuples = {tuple(int(value) for value in values[:depth]) for values in allowed_tuples}
    children_by_prefix: dict[tuple[int, ...], set[int]] = {}
    for values in tuples:
        for offset in range(depth):
            children_by_prefix.setdefault(values[:offset], set()).add(values[offset])
    active: dict[tuple[int, ...], int] = {(): cnf.new()}
    cnf.add([active[()]])
    nodes = 1
    for offset in range(depth):
        following: dict[tuple[int, ...], int] = {}
        for prefix, source in active.items():
            values = sorted(children_by_prefix.get(prefix) or ())
            children = []
            for value in values:
                child_prefix = prefix + (value,)
                child = cnf.new()
                following[child_prefix] = child
                children.append(child)
                cnf.add([-child, source])
                selector = cnf.exact_value(choices[offset], value)
                cnf.add([-child, selector])
            cnf.add([-source, *children])
        active = following
        nodes += len(active)
    return depth, nodes


def build_draw_layout(
    rows: list[dict[str, Any]], prelude: list[int], seed_method: str
):
    maxima: list[int] = []
    expected: list[int | None] = []
    round_slices: list[tuple[int, int]] = []
    seed_slices: list[tuple[int, int]] = []

    def add_seed_event(labels: list[int | None]) -> None:
        start = len(maxima)
        if seed_method == "integers-unique":
            maxima.extend([899] * len(labels))
            expected.extend(
                [int(value) - 100 if value is not None else None for value in labels]
            )
        elif seed_method == "choice-without-replacement":
            maxima.extend(range(899, 0, -1))
            expected.extend([None] * 899)
        else:
            raise ValueError(seed_method)
        seed_slices.append((start, len(maxima)))

    add_seed_event([int(value) for value in prelude])
    for row in rows:
        start = len(maxima)
        size = int(row["shuffle_size"])
        maxima.extend(range(size - 1, 0, -1))
        expected.extend([None] * (size - 1))
        shuffle_stop = len(maxima)
        labels = row.get("discovery_seed_label") or [None, None, None]
        round_slices.append((start, shuffle_stop))
        add_seed_event(labels)
    return maxima, expected, round_slices, seed_slices


def build_draws(rows: list[dict[str, Any]], prelude: list[int]):
    maxima, expected, round_slices, _seed_slices = build_draw_layout(
        rows, prelude, "integers-unique"
    )
    return maxima, expected, round_slices


def choice_domain_targets(labels: list[int]) -> list[int]:
    """Map published 100..999 seed values to permutation positions 0..899."""
    targets = [int(value) - 100 for value in labels]
    if any(value < 0 or value >= 900 for value in targets):
        raise ValueError("choice seed labels must be in 100..999")
    return targets


def seed_duplicate_forbidden_values(
    seed_slices: list[tuple[int, int]], expected: list[int | None]
) -> dict[int, list[int]]:
    """Values that NumPy's repeated-unique triplets must retry per draw."""
    forbidden: dict[int, list[int]] = {}
    for start, stop in seed_slices:
        previous: list[int] = []
        for draw in range(start, stop):
            if previous:
                forbidden[draw] = list(previous)
            value = expected[draw]
            if value is not None:
                previous.append(int(value))
    return forbidden


def model_uint(model: list[Any], bits: list[int]) -> int:
    value = 0
    for offset, variable in enumerate(bits):
        if variable < len(model) and bool(model[variable]):
            value |= 1 << offset
    return value


def replay_bounded_draws(
    raw_values: list[int], start: int, maxima: list[int]
) -> tuple[list[int], int, int] | None:
    """Replay NumPy's masked rejection draws from exposed low raw bits."""
    cursor = int(start)
    rejected = 0
    accepted: list[int] = []
    for maximum in maxima:
        mask = (1 << interval_bits(maximum)) - 1
        while cursor < len(raw_values):
            value = int(raw_values[cursor]) & mask
            cursor += 1
            if value <= maximum:
                accepted.append(value)
                break
            rejected += 1
        else:
            return None
    return accepted, cursor, rejected


def replay_integer_unique_round(
    raw_values: list[int], start: int, shuffle_size: int
) -> tuple[list[int], list[int], int, int] | None:
    """Replay one shuffle followed by a repeated-unique integer seed event."""
    replayed = replay_bounded_draws(
        raw_values,
        int(start),
        list(range(int(shuffle_size) - 1, 0, -1)),
    )
    if replayed is None:
        return None
    choices, cursor, rejected = replayed
    seed_values: list[int] = []
    mask = (1 << interval_bits(899)) - 1
    while len(seed_values) < 3:
        if cursor >= len(raw_values):
            return None
        value = int(raw_values[cursor]) & mask
        cursor += 1
        if value > 899 or value in seed_values:
            rejected += 1
            continue
        seed_values.append(value)
    return choices, [value + 100 for value in seed_values], cursor, rejected


def replay_shuffle(initial: list[str], choices: list[int]) -> list[str]:
    values = list(initial)
    if len(choices) != max(0, len(values) - 1):
        raise ValueError("choice count does not match shuffle size")
    for maximum, choice in zip(range(len(values) - 1, 0, -1), choices):
        values[maximum], values[int(choice)] = values[int(choice)], values[maximum]
    return values


def is_exact_observed_subsequence(full: list[str], observed: list[str]) -> bool:
    cursor = 0
    for token in full:
        if cursor < len(observed) and token == observed[cursor]:
            cursor += 1
    return (
        cursor == len(observed)
        and len(missing_tokens(full, observed)) == len(full) - len(observed)
    )


def matches_task_selector(task_id: str, selectors: list[str]) -> bool:
    """Return whether a task is selected by an exact ID or unambiguous prefix.

    Prefixes are convenient for reproducible command lines, while keeping the
    selection decision independent of the hidden seed labels.
    """
    if not selectors:
        return True
    return any(task_id == selector or task_id.startswith(selector) for selector in selectors)


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--tuple-report", type=Path)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument(
        "--include-sealed-shuffles",
        action="store_true",
        help=(
            "Model later public pre-score shuffle rows while keeping their seed "
            "labels sealed. This is incompatible with excluded-row validation."
        ),
    )
    parser.add_argument("--raw-cap", type=int, default=500)
    parser.add_argument("--max-rejections", type=int, default=2)
    parser.add_argument("--min-total-rejections", type=int, default=0)
    parser.add_argument("--enforce-rejected-values", action="store_true")
    parser.add_argument(
        "--alignment-encoding",
        choices=("jump", "lattice"),
        default="jump",
        help="Use the compact exact raw-word lattice for broad rejection bands.",
    )
    parser.add_argument(
        "--rejection-checkpoint",
        action="append",
        default=[],
        metavar="DRAW:LOW:HIGH",
        help="Constrain cumulative rejected words after DRAW bounded draws.",
    )
    parser.add_argument(
        "--auto-mode91-checkpoints",
        action="store_true",
        help="Add the high-probability 32-draw checkpoint shard ending at 91 per round.",
    )
    parser.add_argument("--rejection-budget", type=int, default=160)
    parser.add_argument("--auto-statistical-checkpoints", action="store_true")
    parser.add_argument("--checkpoint-interval", type=int, default=64)
    parser.add_argument("--checkpoint-sigma", type=float, default=4.0)
    parser.add_argument(
        "--fixed-rejection-profile",
        type=Path,
        help="Corridor-plan JSON used to derive exact cumulative checkpoints.",
    )
    parser.add_argument("--fixed-rejection-profile-rank", type=int, default=1)
    parser.add_argument(
        "--fixed-rejection-profile-checkpoint-interval",
        type=int,
        default=0,
        help="Bind cumulative gaps every N draws while leaving within-window gaps free.",
    )
    parser.add_argument("--max-missing-positions", type=int, default=12)
    parser.add_argument(
        "--max-full-shuffle-rounds",
        type=int,
        default=0,
        help="0 means unlimited; later eligible rounds fall back to tuple tries.",
    )
    parser.add_argument(
        "--max-linear-domain-rounds",
        type=int,
        default=0,
        help=(
            "Bind up to N additional large-missing rounds with a full shuffle "
            "network and the exact linear delete-to-observed domain automaton."
        ),
    )
    parser.add_argument(
        "--shuffle-encoding",
        choices=("network", "singleton-trace", "none"),
        default="network",
        help=(
            "Use the exact full permutation network or a sound smaller trace of "
            "observed singleton UID domains."
        ),
    )
    parser.add_argument(
        "--max-singleton-traces",
        type=int,
        default=0,
        help="Maximum evenly spaced singleton traces per round; 0 keeps all.",
    )
    parser.add_argument(
        "--combine-prefix-tuples-with-traces",
        action="store_true",
        help=(
            "When using singleton traces, also bind the exact correlated "
            "Fisher--Yates prefix tuple table for the same round."
        ),
    )
    parser.add_argument(
        "--full-shuffle-task",
        action="append",
        default=[],
        metavar="TASK_ID_OR_PREFIX",
        help=(
            "Bind the full public shuffle only for the selected Discovery task(s); "
            "repeatable. With no selector, retain the normal first-eligible policy."
        ),
    )
    parser.add_argument("--time-limit", type=float, default=300.0)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--state-shard-bits", type=int, default=0)
    parser.add_argument("--state-shard-index", type=int, default=0)
    parser.add_argument(
        "--fixed-accepted-draw",
        action="append",
        default=[],
        metavar="DRAW:VALUE",
        help="Fix one zero-based accepted bounded draw; repeat for exact shards.",
    )
    parser.add_argument(
        "--assumption-checkpoint-draw",
        type=int,
        help=(
            "Instead of one broad solve, scan exact cumulative-rejection states "
            "at this bounded-draw checkpoint in mean-first order."
        ),
    )
    parser.add_argument(
        "--assumption-secondary-checkpoint-draw",
        type=int,
        help=(
            "Optionally pair a second exact checkpoint with every primary "
            "assumption shard, ordered by joint statistical likelihood."
        ),
    )
    parser.add_argument(
        "--assumption-extra-checkpoint-draw",
        type=int,
        action="append",
        default=[],
        help=(
            "Add another exact cumulative-rejection checkpoint to each "
            "assumption profile; repeatable."
        ),
    )
    parser.add_argument(
        "--assumption-time-limit",
        type=float,
        default=15.0,
        help="Per-state solve limit for --assumption-checkpoint-draw.",
    )
    parser.add_argument(
        "--assumption-max-values",
        type=int,
        default=0,
        help="Optional cap on checkpoint states attempted; 0 scans all or stops at SAT.",
    )
    parser.add_argument(
        "--assumption-shard-count",
        type=int,
        default=1,
        help=(
            "Split the mean-first checkpoint assumption order into this many "
            "disjoint strided worker shards."
        ),
    )
    parser.add_argument(
        "--assumption-shard-index",
        type=int,
        default=0,
        help="Zero-based worker shard selected by --assumption-shard-count.",
    )
    parser.add_argument(
        "--mt-encoding",
        choices=("dense", "sparse"),
        default="dense",
        help="Encode MT outputs as dense initial-state XORs or sparse local gates.",
    )
    parser.add_argument(
        "--validate-next-discovery",
        action="store_true",
        help="Replay the SAT witness against the next excluded Discovery row.",
    )
    parser.add_argument(
        "--forecast-postsolve-replay",
        action="store_true",
        help=(
            "Fit only the selected rows, then concretely replay the next row "
            "from the SAT witness. This avoids adding unconstrained forecast "
            "alignment variables to the solve."
        ),
    )
    parser.add_argument(
        "--forecast-seed-probe",
        help="Condition the excluded forecast row on an arbitrary comma-separated triplet.",
    )
    parser.add_argument(
        "--bind-forecast-shuffle",
        action="store_true",
        help="Bind the excluded row's public shuffle while keeping its seed label hidden.",
    )
    parser.add_argument("--prelude", default="654,347,964")
    parser.add_argument(
        "--seed-method",
        choices=("integers-unique", "choice-without-replacement"),
        default="integers-unique",
        help="Candidate NumPy API used to generate each three-seed triplet.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/research/preseed_mt_xorsat_joint.json"),
    )
    args = parser.parse_args()
    fixed_accepted_draws = parse_fixed_accepted_draws(args.fixed_accepted_draw)
    if int(args.assumption_shard_count) < 1:
        parser.error("--assumption-shard-count must be at least one")
    if not 0 <= int(args.assumption_shard_index) < int(args.assumption_shard_count):
        parser.error("--assumption-shard-index must be within assumption shard count")
    if int(args.assumption_shard_count) > 1 and args.assumption_checkpoint_draw is None:
        parser.error("assumption sharding requires --assumption-checkpoint-draw")
    if args.assumption_extra_checkpoint_draw and args.assumption_checkpoint_draw is None:
        parser.error("extra assumption checkpoints require a primary checkpoint")
    if args.forecast_postsolve_replay and not args.validate_next_discovery:
        parser.error("--forecast-postsolve-replay requires --validate-next-discovery")
    if args.forecast_postsolve_replay and args.bind_forecast_shuffle:
        parser.error("postsolve replay cannot bind the forecast shuffle")
    if args.forecast_postsolve_replay and args.forecast_seed_probe:
        parser.error("postsolve replay cannot condition a forecast seed probe")
    try:
        from pycryptosat import Solver
    except ModuleNotFoundError as error:
        raise RuntimeError("run through uv with --with pycryptosat") from error

    payload = json.loads(args.constraints.read_text(encoding="utf-8"))
    tuple_payload = (
        json.loads(args.tuple_report.read_text(encoding="utf-8"))
        if args.tuple_report
        else {}
    )
    tuple_tables = {
        str(row["task_id"]): [
            [int(value) for value in values]
            for values in row.get("choice_tuples") or []
        ]
        for row in tuple_payload.get("rounds") or []
    }
    all_constraint_rows = [
        row
        for row in payload.get("rounds") or []
        if int(row.get("shuffle_size") or 0) > 1
    ]
    discovery_rows = [
        row
        for row in all_constraint_rows
        if row.get("seed_label_partition") == "discovery"
        and len(row.get("discovery_seed_label") or []) == 3
    ]
    rows = (
        all_constraint_rows[: max(0, args.rounds)]
        if args.include_sealed_shuffles
        else discovery_rows[: max(0, args.rounds)]
    )
    if args.include_sealed_shuffles and args.validate_next_discovery:
        raise ValueError(
            "--include-sealed-shuffles cannot be combined with "
            "--validate-next-discovery"
        )
    modelled_discovery_count = sum(
        len(row.get("discovery_seed_label") or []) == 3 for row in rows
    )
    excluded = (
        discovery_rows[modelled_discovery_count]
        if modelled_discovery_count < len(discovery_rows)
        else None
    )
    alignment_rows = list(rows)
    forecast_probe = None
    if args.forecast_seed_probe:
        forecast_probe = [int(value) for value in args.forecast_seed_probe.split(",")]
        if len(forecast_probe) != 3 or any(value < 100 or value > 999 for value in forecast_probe):
            raise ValueError("--forecast-seed-probe requires three values in 100..999")
    if (
        args.validate_next_discovery
        and excluded is not None
        and not args.forecast_postsolve_replay
    ):
        forecast_row = dict(excluded)
        forecast_row["discovery_seed_label"] = forecast_probe or [None, None, None]
        alignment_rows.append(forecast_row)
    prelude = [int(value) for value in args.prelude.split(",") if value.strip()]
    if args.seed_method == "choice-without-replacement" and args.validate_next_discovery:
        raise ValueError("forecast mode for choice-without-replacement is not implemented")
    maxima, expected, round_slices, seed_slices = build_draw_layout(
        alignment_rows, prelude, args.seed_method
    )
    for draw, value in fixed_accepted_draws.items():
        if draw >= len(maxima):
            raise ValueError("fixed accepted draw is outside the model")
        if value > int(maxima[draw]):
            raise ValueError("fixed accepted draw value exceeds its bound")
    rejection_checkpoints: dict[int, tuple[int, int]] = {}
    for encoded in args.rejection_checkpoint:
        draw, low, high = (int(value) for value in encoded.split(":"))
        if draw < 0 or low < 0 or high < low:
            raise ValueError(f"invalid rejection checkpoint: {encoded}")
        rejection_checkpoints[draw] = (low, high)
    if args.auto_statistical_checkpoints:
        automatic = statistical_rejection_checkpoints(
            maxima,
            interval=max(1, args.checkpoint_interval),
            sigma=max(0.1, args.checkpoint_sigma),
        )
        automatic.update(rejection_checkpoints)
        rejection_checkpoints = automatic
    if args.auto_mode91_checkpoints:
        if args.seed_method != "integers-unique":
            raise ValueError("--auto-mode91-checkpoints applies only to integers-unique")
        rejection_checkpoints[3] = (0, 0)
        relative = {
            32: (0, 5),
            64: (4, 14),
            96: (16, 30),
            128: (40, 54),
            160: (44, 59),
            192: (63, 78),
            224: (74, 87),
            258: (91, 91),
        }
        for round_index in range(len(alignment_rows)):
            draw_base = 3 + round_index * 258
            rejection_base = round_index * 91
            for relative_draw, (low, high) in relative.items():
                rejection_checkpoints[draw_base + relative_draw] = (
                    rejection_base + low,
                    rejection_base + high,
                )
    fixed_profile_checkpoints: dict[int, tuple[int, int]] = {}
    if args.fixed_rejection_profile:
        if args.fixed_rejection_profile_checkpoint_interval <= 0:
            raise ValueError(
                "--fixed-rejection-profile-checkpoint-interval must be positive"
            )
        profile_payload = json.loads(
            args.fixed_rejection_profile.read_text(encoding="utf-8")
        )
        profile = next(
            (
                item
                for item in profile_payload.get("corridors") or []
                if int(item.get("rank") or 0)
                == int(args.fixed_rejection_profile_rank)
            ),
            None,
        )
        if profile is None:
            raise ValueError("fixed rejection profile rank is absent from the plan")
        fixed_profile_checkpoints = profile_rejection_checkpoints(
            [int(value) for value in profile.get("rejection_gaps") or []],
            len(maxima),
            args.fixed_rejection_profile_checkpoint_interval,
        )
        rejection_checkpoints.update(fixed_profile_checkpoints)
    started = time.monotonic()
    effective_rejection_budget = max(0, args.rejection_budget)
    if args.auto_statistical_checkpoints and rejection_checkpoints:
        effective_rejection_budget = max(
            effective_rejection_budget,
            rejection_checkpoints[len(maxima)][1],
        )
    if fixed_profile_checkpoints:
        effective_rejection_budget = max(
            effective_rejection_budget,
            max(high for _low, high in fixed_profile_checkpoints.values()),
        )
    effective_raw_cap = max(1, args.raw_cap)
    if args.auto_statistical_checkpoints or fixed_profile_checkpoints:
        effective_raw_cap = max(
            effective_raw_cap, len(maxima) + effective_rejection_budget
        )
    solver = Solver(
        verbose=0,
        time_limit=max(1.0, args.time_limit),
        threads=max(1, args.threads),
    )
    cnf = Cnf(solver)
    state_shard = bind_state_shard(
        cnf, args.state_shard_bits, args.state_shard_index
    )
    raw_bits = (
        add_sparse_mt_raw_bits(cnf, effective_raw_cap)
        if args.mt_encoding == "sparse"
        else add_symbolic_raw_bits(cnf, effective_raw_cap)
    )
    if args.alignment_encoding == "lattice":
        forbidden_values = (
            seed_duplicate_forbidden_values(seed_slices, expected)
            if args.seed_method == "integers-unique"
            else {}
        )
        accepted, final_alignment_states, alignment_layers = add_alignment_lattice(
            cnf,
            raw_bits,
            maxima,
            expected,
            rejection_budget=effective_rejection_budget,
            rejection_checkpoints=rejection_checkpoints,
            minimum_total_rejections=max(0, args.min_total_rejections),
            forbidden_values=forbidden_values,
        )
    else:
        accepted, final_alignment_states, alignment_layers = add_alignment(
            cnf,
            raw_bits,
            maxima,
            expected,
            max_rejections=max(0, args.max_rejections),
            rejection_budget=effective_rejection_budget,
            enforce_rejected_values=args.enforce_rejected_values,
            rejection_checkpoints=rejection_checkpoints,
            minimum_total_rejections=max(0, args.min_total_rejections),
        )
    allowed_final_alignment_states = [
        state
        for rejected, state in final_alignment_states.items()
        if rejected >= max(0, args.min_total_rejections)
    ]
    cnf.add(allowed_final_alignment_states)
    for draw, value in fixed_accepted_draws.items():
        for offset, bit in enumerate(accepted[draw]):
            cnf.add([bit if (value >> offset) & 1 else -bit])
    if args.seed_method == "choice-without-replacement":
        # ``choice(np.arange(100, 1000), ...)`` shuffles zero-based array
        # positions and adds the domain offset only when values are observed.
        # Apply that offset to the restart prelude as well as task labels.
        seed_labels: list[list[int] | None] = [choice_domain_targets(prelude)] + [
            (
                choice_domain_targets(row["discovery_seed_label"])
                if len(row.get("discovery_seed_label") or []) == 3
                else None
            )
            for row in rows
        ]
        for (seed_start, seed_stop), labels in zip(seed_slices, seed_labels):
            if labels is not None:
                add_permutation_prefix(cnf, accepted[seed_start:seed_stop], labels)
    missing_total = 0
    full_shuffle_rounds = 0
    standard_full_shuffle_rounds = 0
    linear_domain_rounds = 0
    linear_domain_states = 0
    linear_domain_edges = 0
    full_group_positions = 0
    ambiguous_shuffle_rounds_skipped = 0
    prefix_tuple_rounds = 0
    prefix_tuple_rows = 0
    prefix_tuple_trie_nodes = 0
    full_shuffle_task_ids: list[str] = []
    singleton_trace_rounds = 0
    singleton_traces_bound = 0
    constraint_rows = (
        alignment_rows
        if args.validate_next_discovery and args.bind_forecast_shuffle
        else rows
    )
    for row, (start, stop) in zip(constraint_rows, round_slices):
        initial, observed, _exact = uid_aware_sequences(row)
        omitted_count = len(missing_tokens(initial, observed))
        explicitly_selected = matches_task_selector(
            str(row["task_id"]), args.full_shuffle_task
        )
        full_limit_reached = (
            args.max_full_shuffle_rounds > 0
            and standard_full_shuffle_rounds >= args.max_full_shuffle_rounds
        )
        if args.shuffle_encoding == "none":
            ambiguous_shuffle_rounds_skipped += 1
            continue
        if args.shuffle_encoding == "singleton-trace" and explicitly_selected:
            traced = add_observed_singleton_traces(
                cnf,
                accepted[start:stop],
                row,
                max_traces=max(0, args.max_singleton_traces),
            )
            if traced:
                singleton_trace_rounds += 1
                singleton_traces_bound += traced
                if args.combine_prefix_tuples_with_traces:
                    tuples = tuple_tables.get(str(row["task_id"])) or []
                    depth, nodes = add_choice_tuple_trie(
                        cnf, accepted[start:stop], tuples
                    )
                    if depth:
                        prefix_tuple_rounds += 1
                        prefix_tuple_rows += len(tuples)
                        prefix_tuple_trie_nodes += nodes
                continue
        if (
            omitted_count > max(0, args.max_missing_positions)
            or full_limit_reached
            or not explicitly_selected
        ):
            use_linear_domain = (
                explicitly_selected
                and args.shuffle_encoding == "network"
                and args.max_linear_domain_rounds > linear_domain_rounds
            )
            if use_linear_domain:
                output = add_shuffle_network(cnf, accepted[start:stop], initial)
                states, edges = add_observed_domain_subsequence(
                    cnf, output, initial, observed
                )
                missing_total += omitted_count
                full_shuffle_rounds += 1
                linear_domain_rounds += 1
                linear_domain_states += states
                linear_domain_edges += edges
                full_group_positions += len(observed)
                full_shuffle_task_ids.append(str(row["task_id"]))
                continue
            ambiguous_shuffle_rounds_skipped += 1
            tuples = tuple_tables.get(str(row["task_id"])) or []
            depth, nodes = add_choice_tuple_trie(cnf, accepted[start:stop], tuples)
            if depth:
                prefix_tuple_rounds += 1
                prefix_tuple_rows += len(tuples)
                prefix_tuple_trie_nodes += nodes
            continue
        output = add_shuffle_network(cnf, accepted[start:stop], initial)
        missing_total += add_observed_subsequence(cnf, output, initial, observed)
        full_shuffle_rounds += 1
        standard_full_shuffle_rounds += 1
        full_group_positions += len(observed)
        full_shuffle_task_ids.append(str(row["task_id"]))
    build_seconds = time.monotonic() - started
    assumption_scan: dict[str, Any] = {
        "enabled": args.assumption_checkpoint_draw is not None,
        "draw": args.assumption_checkpoint_draw,
        "per_state_time_limit": (
            args.assumption_time_limit
            if args.assumption_checkpoint_draw is not None
            else None
        ),
        "max_values": (
            args.assumption_max_values
            if args.assumption_checkpoint_draw is not None
            else None
        ),
        "complete": False,
        "shard_complete": False,
        "shard_count": int(args.assumption_shard_count),
        "shard_index": int(args.assumption_shard_index),
        "states_available": 0,
        "states_selected": 0,
        "states_attempted": 0,
        "sat_state": None,
        "sat_profile": None,
        "checkpoint_draws": [],
        "results": [],
    }
    solve_started = time.monotonic()
    if args.assumption_checkpoint_draw is not None:
        checkpoint_draw = int(args.assumption_checkpoint_draw)
        if checkpoint_draw < 0 or checkpoint_draw >= len(alignment_layers):
            raise ValueError(
                "--assumption-checkpoint-draw must be between 0 and "
                f"{len(alignment_layers) - 1}"
            )
        checkpoint_states = alignment_layers[checkpoint_draw]
        extra_draws = [
            int(value) for value in args.assumption_extra_checkpoint_draw
        ]
        secondary_draw = args.assumption_secondary_checkpoint_draw
        profile_draws = [checkpoint_draw]
        if secondary_draw is not None:
            profile_draws.append(int(secondary_draw))
        profile_draws.extend(extra_draws)
        if len(set(profile_draws)) != len(profile_draws):
            raise ValueError("assumption checkpoint draws must be distinct")
        for draw in profile_draws:
            if draw < 0 or draw >= len(alignment_layers):
                raise ValueError(
                    "assumption checkpoint draw must be between 0 and "
                    f"{len(alignment_layers) - 1}"
                )
        profile_mode = len(profile_draws) > 1
        total_profiles = None
        ranked_profiles = None
        if profile_mode:
            scan_layers = {
                draw: alignment_layers[draw] for draw in profile_draws
            }
            total_profiles = checkpoint_profile_count(scan_layers)
            ranking_limit = (
                int(args.assumption_max_values)
                * int(args.assumption_shard_count)
                if int(args.assumption_max_values) > 0
                else 0
            )
            ordered_profiles = checkpoint_profile_order(
                maxima, scan_layers, limit=ranking_limit
            )
            ranked_profiles = len(ordered_profiles)
            selected_profiles = shard_assumption_order(
                ordered_profiles,
                int(args.assumption_shard_count),
                int(args.assumption_shard_index),
            )
            satisfiable, model, scan_records, scan_complete = solve_checkpoint_profiles(
                solver,
                scan_layers,
                selected_profiles,
                time_limit=max(0.01, args.assumption_time_limit),
                max_profiles=max(0, args.assumption_max_values),
            )
        else:
            ordered_values = checkpoint_value_order(
                maxima, checkpoint_draw, list(checkpoint_states)
            )
            selected_values = shard_assumption_order(
                ordered_values,
                int(args.assumption_shard_count),
                int(args.assumption_shard_index),
            )
            satisfiable, model, scan_records, scan_complete = solve_checkpoint_assumptions(
                solver,
                checkpoint_states,
                selected_values,
                time_limit=max(0.01, args.assumption_time_limit),
                max_values=max(0, args.assumption_max_values),
            )
        selected_order = (
            selected_profiles if profile_mode else selected_values
        )
        selected_limit = (
            min(len(selected_order), int(args.assumption_max_values))
            if int(args.assumption_max_values) > 0
            else len(selected_order)
        )
        shard_complete = (
            len(scan_records) == selected_limit
            and all(record["status"] != "unknown" for record in scan_records)
        )
        # Preserve a SAT witness from any worker. A local UNSAT only excludes
        # that worker's disjoint subset and is never a global family result.
        if int(args.assumption_shard_count) > 1 and satisfiable is False:
            satisfiable, model = None, None
        scan_complete = bool(
            int(args.assumption_shard_count) == 1
            and scan_complete
            and (not profile_mode or ranked_profiles == total_profiles)
        )
        solved = satisfiable, model
        sat_record = next(
            (record for record in scan_records if record["status"] == "sat"), None
        )
        assumption_scan.update(
            {
                "complete": scan_complete,
                "shard_complete": shard_complete,
                "checkpoint_draws": sorted(profile_draws),
                "states_available": len(checkpoint_states),
                "states_selected": len(selected_order),
                "states_attempted": len(scan_records),
                "sat_state": (
                    sat_record.get("cumulative_rejections") if sat_record else None
                ),
                "secondary_draw": (
                    int(secondary_draw) if secondary_draw is not None else None
                ),
                "extra_draws": extra_draws,
                "profiles_available": (
                    total_profiles if profile_mode else None
                ),
                "profiles_ranked": (
                    ranked_profiles if profile_mode else None
                ),
                "profiles_selected": (
                    len(selected_profiles) if profile_mode else None
                ),
                "sat_profile": (
                    sat_record.get("checkpoint_rejections") if sat_record else None
                ),
                "results": scan_records,
            }
        )
    else:
        solved = solver.solve()
    solve_seconds = time.monotonic() - solve_started
    satisfiable, model = solved
    status = "sat" if satisfiable is True else "unsat" if satisfiable is False else "unknown"
    final_rejections = None
    witness_checkpoint_rejections: dict[str, int] = {}
    if satisfiable is True and model:
        for rejected, state in final_alignment_states.items():
            if state < len(model) and model[state]:
                final_rejections = rejected
                break
        for draw in sorted(rejection_checkpoints):
            if draw >= len(alignment_layers):
                continue
            for rejected, state in alignment_layers[draw].items():
                if state < len(model) and model[state]:
                    witness_checkpoint_rejections[str(draw)] = rejected
                    break
    excluded_validation: dict[str, Any] = {
        "attempted": False,
        "complete": False,
        "seed_ordered_exact": False,
        "shuffle_subsequence_exact": False,
        "seed_prediction_unique": None,
    }
    if (
        args.validate_next_discovery
        and satisfiable is True
        and model
        and final_rejections is not None
        and excluded is not None
    ):
        seed_bits: list[list[int]] = []
        if args.forecast_postsolve_replay:
            raw_values = [model_uint(model, bits) for bits in raw_bits]
            forecast = replay_integer_unique_round(
                raw_values,
                len(maxima) + int(final_rejections),
                int(excluded["shuffle_size"]),
            )
            if forecast is None:
                predicted_choices = []
                predicted_seeds = []
            else:
                predicted_choices, predicted_seeds, forecast_raw_stop, forecast_rejections = forecast
                excluded_validation["forecast_raw_start"] = len(maxima) + int(final_rejections)
                excluded_validation["forecast_raw_stop"] = int(forecast_raw_stop)
                excluded_validation["forecast_rejections"] = int(forecast_rejections)
        else:
            forecast_start, forecast_stop = round_slices[len(rows)]
            seed_bits = accepted[forecast_stop : forecast_stop + 3]
            predicted_choices = [
                model_uint(model, bits)
                for bits in accepted[forecast_start:forecast_stop]
            ]
            predicted_seeds = [model_uint(model, bits) + 100 for bits in seed_bits]
        initial, observed, _exact = uid_aware_sequences(excluded)
        predicted_order = (
            replay_shuffle(initial, predicted_choices)
            if len(predicted_choices) == max(0, len(initial) - 1)
            else []
        )
        excluded_validation["attempted"] = True
        excluded_validation["task_id"] = str(excluded["task_id"])
        excluded_validation.update(
            {
                "complete": True,
                "predicted_seed_triplet": predicted_seeds,
                "seed_ordered_exact": predicted_seeds
                == [int(value) for value in excluded["discovery_seed_label"]],
                "shuffle_subsequence_exact": is_exact_observed_subsequence(
                    predicted_order, observed
                ),
            }
        )
        if args.forecast_postsolve_replay:
            # One witness is a prospective hit/miss test, not a uniqueness
            # proof. Keep promotion closed until alternative fitted states are
            # sampled or the forecast bits are proved unique symbolically.
            excluded_validation["seed_prediction_unique"] = None
        elif forecast_probe is not None:
            excluded_validation["conditioned_seed_triplet"] = forecast_probe
        else:
            block_prediction = []
            for bits in seed_bits:
                for variable in bits:
                    block_prediction.append(-variable if bool(model[variable]) else variable)
            cnf.add(block_prediction)
            diversity_started = time.monotonic()
            alternate_satisfiable, alternate_model = solver.solve()
            excluded_validation["diversity_solve_seconds"] = round(
                time.monotonic() - diversity_started, 6
            )
            if alternate_satisfiable is True and alternate_model:
                excluded_validation["seed_prediction_unique"] = False
                excluded_validation["alternate_seed_triplet"] = [
                    model_uint(alternate_model, bits) + 100 for bits in seed_bits
                ]
            elif alternate_satisfiable is False:
                excluded_validation["seed_prediction_unique"] = True
    report = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "numpy-mt19937-full-shuffle-native-xorsat",
        "summary": {
            "status": status,
            "rounds_modelled": len(rows),
            "seed_method": args.seed_method,
            "mt_encoding": args.mt_encoding,
            "state_shard_bits": len(state_shard),
            "state_shard_index": int(args.state_shard_index),
            "state_shard_variables": state_shard,
            "fixed_accepted_draws": [
                {"draw": draw, "value": value}
                for draw, value in sorted(fixed_accepted_draws.items())
            ],
            "alignment_encoding": args.alignment_encoding,
            "seed_permutation_prefix_positions_bound": (
                3 * len(seed_slices)
                if args.seed_method == "choice-without-replacement"
                else 0
            ),
            "discovery_seed_labels_bound": sum(
                len(row.get("discovery_seed_label") or []) for row in rows
            ),
            "sealed_shuffle_rounds_modelled": sum(
                row.get("seed_label_partition") != "discovery" for row in rows
            ),
            "sealed_seed_labels_opened": 0,
            "full_group_positions_bound": full_group_positions,
            "full_shuffle_rounds_bound": full_shuffle_rounds,
            "standard_full_shuffle_rounds_bound": standard_full_shuffle_rounds,
            "linear_domain_rounds_bound": linear_domain_rounds,
            "linear_domain_automaton_states": linear_domain_states,
            "linear_domain_automaton_edges": linear_domain_edges,
            "full_shuffle_task_selectors": list(args.full_shuffle_task),
            "full_shuffle_task_ids_bound": full_shuffle_task_ids,
            "shuffle_encoding": args.shuffle_encoding,
            "singleton_trace_rounds_bound": singleton_trace_rounds,
            "singleton_uid_traces_bound": singleton_traces_bound,
            "prefix_tuples_combined_with_traces": bool(
                args.shuffle_encoding == "singleton-trace"
                and args.combine_prefix_tuples_with_traces
            ),
            "ambiguous_shuffle_rounds_skipped": ambiguous_shuffle_rounds_skipped,
            "prefix_tuple_rounds_bound": prefix_tuple_rounds,
            "prefix_tuple_rows_bound": prefix_tuple_rows,
            "prefix_tuple_trie_nodes": prefix_tuple_trie_nodes,
            "missing_positions_modelled": missing_total,
            "bounded_draws_modelled": len(maxima),
            "forecast_round_in_alignment": bool(
                args.validate_next_discovery
                and excluded is not None
                and not args.forecast_postsolve_replay
            ),
            "forecast_postsolve_replay": bool(args.forecast_postsolve_replay),
            "forecast_seed_probe_conditioned": forecast_probe is not None,
            "forecast_shuffle_bound": bool(
                args.validate_next_discovery
                and args.bind_forecast_shuffle
                and excluded is not None
            ),
            "raw_cap": effective_raw_cap,
            "max_rejections_per_draw": args.max_rejections,
            "global_rejection_budget": effective_rejection_budget,
            "minimum_total_rejections": args.min_total_rejections,
            "rejection_checkpoints": {
                str(draw): [low, high]
                for draw, (low, high) in sorted(rejection_checkpoints.items())
            },
            "fixed_rejection_profile_checkpoint_interval": (
                int(args.fixed_rejection_profile_checkpoint_interval)
                if args.fixed_rejection_profile
                else None
            ),
            "fixed_rejection_profile_checkpoints": len(
                fixed_profile_checkpoints
            ),
            "witness_total_skipped_words": final_rejections,
            "witness_checkpoint_rejections": witness_checkpoint_rejections,
            "assumption_checkpoint_scan": assumption_scan,
            "variables": cnf.next_variable - 1,
            "cnf_clauses": cnf.clauses,
            "xor_clauses": cnf.xor_clauses,
            "build_seconds": round(build_seconds, 6),
            "solve_seconds": round(solve_seconds, 6),
            "rejected_word_inequalities_enforced": bool(
                args.alignment_encoding == "lattice" or args.enforce_rejected_values
            ),
            "alignment_inequalities_exact": bool(
                args.alignment_encoding == "lattice" or args.enforce_rejected_values
            ),
            "seed_duplicate_retries_exact": bool(
                args.alignment_encoding == "lattice"
                and args.seed_method == "integers-unique"
            ),
            "statistical_checkpoint_sigma": (
                args.checkpoint_sigma if args.auto_statistical_checkpoints else None
            ),
            "mt19937_state_recovered": False,
            "excluded_discovery_validation": excluded_validation,
            "excluded_discovery_predicted": bool(
                excluded_validation["seed_ordered_exact"]
                and excluded_validation["shuffle_subsequence_exact"]
                and excluded_validation["seed_prediction_unique"] is True
            ),
        },
        "interpretation": (
            "UNSAT rejects this bounded common-stream branch. SAT is only a compatibility "
            "witness until exact rejected words are enforced and an excluded Discovery row "
            "is predicted without fitting it."
        ),
        "safety": {
            "holdout_opened": False,
            "state_persisted": False,
            "uid_permutation_persisted": False,
            "network_reads": False,
            "submission_writes": False,
        },
    }
    atomic_json(args.output.resolve(), report)
    print(json.dumps({"output": str(args.output.resolve()), **report["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
