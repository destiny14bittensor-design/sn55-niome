"""Pure local clone of NIOME Stage 4 per-submission model evaluation."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import KFold


FEATURE_COLUMNS = [
    "gc",
    "distance",
    "gc_score",
    "dist_score",
    "consistency",
    "energy",
    "mh",
]


def flatten_stage12(data: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for item in data:
        exp, features = item["experiment"], item["features"]
        rows.append(
            {
                "experiment_id": exp["experiment_id"],
                "mutation": exp["mutation"],
                "cas_system": exp["cas_system"],
                "guideRNA": exp["guideRNA"],
                "start": exp["target_alignment_start"],
                "gc": features["gc"],
                "distance": features["distance_to_mutation"],
                "gc_score": features["gc_score"],
                "dist_score": features["dist_score"],
                "consistency": features["consistency"],
                "stage2_score": item["stage2"]["structural_score"],
                "mutation_weight": features.get("mutation_weight", 1.0),
                "weighted_score": item["stage2"].get(
                    "weighted_score", item["stage2"]["structural_score"]
                ),
            }
        )
    return pd.DataFrame(rows)


def flatten_stage3(data: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "experiment_id": item["experiment_id"],
                "mutation": item["mutation"],
                "cas_system": item["cas"],
                "gc": item["features"]["gc"],
                "distance": item["features"]["distance"],
                "gc_score": item["features"]["gc_score"],
                "dist_score": item["features"]["dist_score"],
                "consistency": item["features"]["consistency"],
                "energy": item["energy"],
                "mh": int(item["mh"]),
                "outcome": item["outcome"],
                "indel_length": item["indel_length"],
            }
            for item in data
        ]
    )


def evaluate_target(
    x: pd.DataFrame,
    y: pd.Series,
    *,
    sample_weight: pd.Series | None,
    fold_seed: int,
    n_splits: int = 5,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    count = len(x)
    fold_count = min(n_splits, count) if count > 1 else 1
    kfold = KFold(n_splits=max(fold_count, 2), shuffle=True, random_state=fold_seed)
    r2s, maes, residual_stds = [], [], []
    trace = []
    for fold_index, (train_idx, test_idx) in enumerate(kfold.split(x)):
        x_train, x_test = x.iloc[train_idx], x.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]
        sw_train = sample_weight.iloc[train_idx] if sample_weight is not None else None
        sw_test = sample_weight.iloc[test_idx] if sample_weight is not None else None
        model = RandomForestRegressor(n_estimators=200, random_state=42, max_depth=12)
        model.fit(x_train, y_train, sample_weight=sw_train)
        prediction = model.predict(x_test)
        r2s.append(r2_score(y_test, prediction, sample_weight=sw_test))
        maes.append(mean_absolute_error(y_test, prediction, sample_weight=sw_test))
        residual_stds.append(np.std(y_test - prediction))
        trace.append(
            {
                "fold": fold_index,
                "train_indices": train_idx.tolist(),
                "test_indices": test_idx.tolist(),
                "truth": y_test.tolist(),
                "prediction": prediction.tolist(),
            }
        )
    return {
        "r2_mean": float(np.mean(r2s)),
        "r2_std": float(np.std(r2s)),
        "mae_mean": float(np.mean(maes)),
        "mae_std": float(np.std(maes)),
        "residual_std_mean": float(np.mean(residual_stds)),
        "n_folds": len(r2s),
    }, trace


def normalized_mae(mae_mean: float, y_full: pd.Series) -> float:
    scale = float(np.std(y_full))
    return mae_mean if scale < 1e-9 else mae_mean / scale


def _zero_output() -> dict[str, Any]:
    return {
        "n_valid_experiments": 0,
        "total_weighted_score": 0.0,
        "consistency_score": 0.0,
        "consistency_factor": 0.0,
        "final_reward": 0.0,
        "model_results": {},
        "fold_trace": {},
    }


def run_stage4(
    valid_experiments: list[dict[str, Any]],
    stage3_results: list[dict[str, Any]],
    *,
    seed: int,
    n_folds: int = 5,
) -> dict[str, Any]:
    stage3 = flatten_stage3(stage3_results)
    stage12 = flatten_stage12(valid_experiments)
    if len(stage12) < 2 or len(stage3) < 2:
        return _zero_output()

    stage12_slim = stage12[
        [
            "experiment_id",
            "guideRNA",
            "start",
            "stage2_score",
            "mutation_weight",
            "weighted_score",
        ]
    ]
    merged = stage3.merge(stage12_slim, on="experiment_id", how="inner")
    if len(merged) == 0:
        return _zero_output()
    missing = [column for column in FEATURE_COLUMNS if column not in merged.columns]
    if missing:
        raise ValueError(f"Missing columns in merged dataset: {missing}")
    x = merged[FEATURE_COLUMNS]
    y = pd.DataFrame(
        {
            "is_cut": (merged["outcome"] != "no_cut").astype(int),
            "is_hdr": (merged["outcome"] == "HDR").astype(int),
            "indel_length": merged["indel_length"],
        }
    )
    sample_weight = merged["mutation_weight"]
    results, fold_trace = {}, {}
    for column in y.columns:
        results[column], fold_trace[column] = evaluate_target(
            x,
            y[column],
            sample_weight=sample_weight,
            fold_seed=seed,
            n_splits=n_folds,
        )
    avg_r2 = np.mean([value["r2_mean"] for value in results.values()])
    avg_nmae = np.mean(
        [normalized_mae(value["mae_mean"], y[column]) for column, value in results.items()]
    )
    consistency_score = (0.7 * max(avg_r2, 0) + 0.3 * (1 - avg_nmae)) * 100
    total_weighted_score = float(merged["weighted_score"].sum())
    consistency_factor = (
        0.0
        if np.isnan(consistency_score)
        else max(0.0, min(1.0, consistency_score / 100.0))
    )
    return {
        "n_valid_experiments": len(merged),
        "total_weighted_score": total_weighted_score,
        "consistency_score": (
            float(consistency_score) if not np.isnan(consistency_score) else 0.0
        ),
        "consistency_factor": consistency_factor,
        "final_reward": total_weighted_score * consistency_factor,
        "model_results": results,
        "fold_trace": fold_trace,
    }
