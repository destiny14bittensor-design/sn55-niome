import random

from tools.preseed_stateful_prng_search import (
    _python_stream,
    search_named_material,
    search_numpy_integer_seeds,
    search_python_integer_seeds,
)


def test_fixed_seed_search_recovers_full_persistent_python_stream():
    targets = _python_stream(37, "sample", 5)
    result = search_python_integer_seeds(targets, 100, workers=1)
    assert any(
        hit["seed"] == 37 and hit["method"] == "sample"
        for hit in result["full_stream_hits"]
    )


def test_fixed_seed_search_rejects_first_only_collision():
    rng = random.Random(37)
    targets = [rng.sample(range(100, 1000), 3), [101, 202, 303]]
    result = search_python_integer_seeds(targets, 100, workers=1)
    first = next(hit for hit in result["first_triplet_hits"] if hit["seed"] == 37)
    assert first["matched_tasks"] == 1
    assert not any(hit["seed"] == 37 for hit in result["full_stream_hits"])


def test_named_material_requires_the_entire_sequence():
    targets = _python_stream("niome", "sample", 4)
    result = search_named_material(targets, ["niome", "other"])
    assert result["full_stream_hits"]
    assert result["best_match"] == 4


def test_numpy_fixed_seed_search_recovers_persistent_random_state():
    rng = __import__("numpy").random.RandomState(23)
    targets = [
        [int(value) for value in rng.choice(range(100, 1000), 3, replace=False)]
        for _ in range(4)
    ]
    result = search_numpy_integer_seeds(targets, 50, workers=1)
    assert any(
        hit["seed"] == 23
        and hit["engine"] == "random-state"
        and hit["method"] == "choice"
        for hit in result["full_stream_hits"]
    )
