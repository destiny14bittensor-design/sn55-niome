"""Local simulation of the public score-to-weight path."""

from __future__ import annotations

from typing import Any

import numpy as np


U16_MAX = 65_535
DEFAULT_DISTRIBUTION = np.asarray(
    [0.30, 0.20, 0.20, 0.15, 0.05, 0.03, 0.025, 0.02, 0.015, 0.01]
)


def process_scores_top(
    scores: np.ndarray,
    *,
    top_count: int = 10,
    distribution: np.ndarray = DEFAULT_DISTRIBUTION,
) -> np.ndarray:
    positive_count = int(np.sum(scores > 0))
    if positive_count == 0:
        return np.zeros_like(scores)
    sorted_indices = np.argsort(-scores)
    weights = np.zeros_like(scores, dtype=np.float32)
    count = min(top_count, positive_count)
    ratios = distribution[:count]
    ratio_sum = np.sum(ratios)
    normalized = ratios / ratio_sum if ratio_sum > 0 else np.ones(count) / count
    for rank in range(count):
        weights[sorted_indices[rank]] = normalized[rank]
    return weights


def normalize_max_weight(values: np.ndarray, limit: float) -> np.ndarray:
    epsilon = 1e-7
    weights = values.copy()
    sorted_values = np.sort(weights)
    if values.sum() == 0 or len(values) * limit <= 1:
        return np.ones_like(values) / values.size
    estimation = sorted_values / sorted_values.sum()
    if estimation.max() <= limit:
        return weights / weights.sum()
    cumulative = np.cumsum(estimation, 0)
    estimation_sum = np.asarray(
        [(len(sorted_values) - index - 1) * estimation[index] for index in range(len(sorted_values))]
    )
    n_values = (estimation / (estimation_sum + cumulative + epsilon) < limit).sum()
    cutoff_scale = (limit * cumulative[n_values - 1] - epsilon) / (
        1 - limit * (len(estimation) - n_values)
    )
    cutoff = cutoff_scale * sorted_values.sum()
    weights[weights > cutoff] = cutoff
    return weights / weights.sum()


def process_chain_constraints(
    uids: np.ndarray,
    weights: np.ndarray,
    *,
    owner_uid: int,
    min_allowed_weights: int,
    max_weight_limit: float,
    metagraph_size: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    if weights.dtype != np.float32:
        weights = weights.astype(np.float32)
    nonzero_index = np.atleast_1d(np.argwhere(weights > 0).squeeze())
    nonzero_uids = uids[nonzero_index]
    nonzero_weights = weights[nonzero_index]
    count = metagraph_size if metagraph_size is not None else len(uids)
    if nonzero_weights.size == 0:
        all_weights = np.zeros(count, dtype=np.float32)
        owner_indices = np.where(uids == owner_uid)[0]
        if len(owner_indices) == 0:
            uniform = np.ones(count) / count
            return np.arange(count), uniform
        all_weights[owner_indices[0]] = 1.0
        normalized = normalize_max_weight(all_weights, max_weight_limit)
        final_nonzero = np.where(normalized > 0)[0]
        return uids[final_nonzero], normalized[final_nonzero]
    if count < min_allowed_weights:
        return np.arange(count), np.ones(count) / count
    if nonzero_weights.size < min_allowed_weights:
        expanded = np.ones(count) * 1e-5
        expanded[nonzero_index] += nonzero_weights
        return np.arange(count), normalize_max_weight(expanded, max_weight_limit)
    return nonzero_uids, normalize_max_weight(nonzero_weights, max_weight_limit)


def convert_for_emit(uids: np.ndarray, weights: np.ndarray) -> tuple[list[int], list[int]]:
    if np.min(weights) < 0 or np.min(uids) < 0:
        raise ValueError("UIDs and weights must be non-negative")
    if len(uids) != len(weights):
        raise ValueError("UID and weight lengths differ")
    if np.sum(weights) == 0:
        return [], []
    output_uids, output_weights = [], []
    for uid, weight in zip(uids, weights):
        encoded = round(float(weight) * U16_MAX)
        if encoded != 0:
            output_uids.append(int(uid))
            output_weights.append(encoded)
    return output_uids, output_weights


def simulate_weights(
    scores: list[float],
    *,
    owner_uid: int,
    burning_rate: float = 0.02,
    min_allowed_weights: int = 1,
    max_weight_limit: float = 65_535,
) -> dict[str, Any]:
    score_array = np.nan_to_num(np.asarray(scores, dtype=np.float32))
    uids = np.arange(len(score_array))
    ranked = process_scores_top(score_array)
    processed_uids, processed = process_chain_constraints(
        uids,
        ranked,
        owner_uid=owner_uid,
        min_allowed_weights=min_allowed_weights,
        max_weight_limit=max_weight_limit,
    )
    full_miner = np.zeros(len(score_array), dtype=np.float32)
    if len(processed_uids) > 0:
        full_miner[np.asarray(processed_uids)] = processed
    if np.sum(full_miner) > 0:
        full_miner /= np.sum(full_miner)
    final = full_miner * (1 - burning_rate)
    if 0 <= owner_uid < len(final):
        final[owner_uid] += burning_rate
    final /= np.sum(final) or 1.0
    nonzero = np.where(final > 1e-8)[0]
    emit_uids, emit_weights = convert_for_emit(nonzero, final[nonzero])
    return {
        "sanitized_scores": score_array.tolist(),
        "rank_weights": ranked.tolist(),
        "final_float_weights": final.tolist(),
        "emit_uids": emit_uids,
        "emit_uint16_weights": emit_weights,
    }
