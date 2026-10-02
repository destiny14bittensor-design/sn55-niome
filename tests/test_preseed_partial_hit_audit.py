import pytest

from tools.preseed_generator_lab import ModelSpec, predict_model
from tools.preseed_partial_hit_audit import (
    binomial_survival,
    run_walk_forward_audit,
)
from tools.preseed_partial_predictor import SeedDomain


DOMAIN = SeedDomain(10, 19, 3)
MODEL = ModelSpec(
    model_id="synthetic-perfect",
    family="public-digest-triplet-v1",
    components=("task.id",),
    seed_low=DOMAIN.low,
    seed_high=DOMAIN.high,
)


def row(index: int):
    value = {
        "task_id": f"task-{index}",
        "created_at": f"2026-09-29T{index:02d}:00:00+00:00",
        "contract_material": "{}",
        "block_context": {},
        "seeds": [],
    }
    value["seeds"] = predict_model(MODEL, value)
    return value


def test_binomial_survival_exact_edges():
    assert binomial_survival(0, 4, 0.1) == pytest.approx(1.0)
    assert binomial_survival(4, 4, 0.1) == pytest.approx(0.0001)


def test_walk_forward_keeps_final_suffix_separate_and_detects_fixture_signal():
    records = [row(index) for index in range(10)]
    report = run_walk_forward_audit(
        records,
        min_history=3,
        final_holdout_rounds=3,
        strategies=("registry-weighted",),
        ks=(3, 5),
        domain=DOMAIN,
        registry=[MODEL],
    )
    split = report["split_policy"]
    assert split["development_forecasts"] == 4
    assert split["final_holdout_forecasts"] == 3
    final = report["evaluations"]["registry-weighted"]["final_holdout"]
    assert final["position_exact_hits"] == 9
    assert final["rounds_with_all"] == 3
    assert report["verdict"]["code"] == "A"


def test_incomplete_label_is_rejected():
    records = [row(index) for index in range(6)]
    records[-1]["seeds"] = [10]
    with pytest.raises(ValueError, match="complete seed label"):
        run_walk_forward_audit(
            records,
            min_history=2,
            final_holdout_rounds=2,
            strategies=("registry-weighted",),
            ks=(3,),
            domain=DOMAIN,
            registry=[MODEL],
        )
