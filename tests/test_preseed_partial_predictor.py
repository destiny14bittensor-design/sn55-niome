from tools.preseed_generator_lab import ModelSpec, predict_model
from tools.preseed_partial_predictor import (
    SeedDomain,
    predict_partial,
    serialise_prediction,
)


DOMAIN = SeedDomain(10, 19, 3)
MODEL = ModelSpec(
    model_id="synthetic-task-id",
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


def test_target_seed_label_is_never_consumed():
    history = [row(index) for index in range(3)]
    target = row(3)
    first = predict_partial(
        history,
        target,
        registry=[MODEL],
        domain=DOMAIN,
        strategy="registry-weighted",
    )
    target["seeds"] = [19, 18, 17]
    second = predict_partial(
        history,
        target,
        registry=[MODEL],
        domain=DOMAIN,
        strategy="registry-weighted",
    )
    assert first["slot_rankings"] == second["slot_rankings"]
    assert first["target_label_consumed"] is False


def test_perfect_public_model_ranks_each_seed_first():
    history = [row(index) for index in range(3)]
    target = row(3)
    prediction = predict_partial(
        history,
        target,
        registry=[MODEL],
        domain=DOMAIN,
        strategy="registry-weighted",
    )
    assert [ranking[0] for ranking in prediction["slot_rankings"]] == target["seeds"]
    compact = serialise_prediction(prediction, top_k=3)
    assert len(compact["slot_top_k"]) == 3
    assert "slot_probabilities" not in compact


def test_history_must_be_strictly_older_than_target():
    target = row(2)
    try:
        predict_partial(
            [row(3)],
            target,
            registry=[MODEL],
            domain=DOMAIN,
        )
    except ValueError as error:
        assert "strictly older" in str(error)
    else:
        raise AssertionError("future history was accepted")
