import json

import numpy as np

from tools.preseed_modern_fixed_gap_search import (
    candidate_rows,
    choice_triplet,
    fixed_choice_gap_hits,
    fixed_float_gap_hits,
    fixed_gap_hits,
    float_triplet,
    unique_triplet,
)


def test_fixed_gap_search_recovers_synthetic_gap():
    rng = np.random.Generator(np.random.PCG64(42))
    targets = [unique_triplet(rng)]
    for _ in range(3):
        rng.integers(100, 1000, size=7, dtype=np.int64)
        targets.append(unique_triplet(rng))
    assert fixed_gap_hits("numpy-pcg64", 42, targets, 20, 5) == [7]


def test_candidate_rows_keeps_only_integer_reports(tmp_path):
    integer = tmp_path / "integer.json"
    choice = tmp_path / "choice.json"
    integer.write_text(
        json.dumps(
            {
                "search": {
                    "engine": "numpy-sfc64",
                    "method": "integers-unique",
                    "first_triplet_candidates": [1, 2],
                }
            }
        ),
        encoding="utf-8",
    )
    choice.write_text(
        json.dumps(
            {
                "search": {
                    "engine": "numpy-sfc64",
                    "method": "choice",
                    "first_triplet_candidates": [3],
                }
            }
        ),
        encoding="utf-8",
    )
    assert [row["initializer"] for row in candidate_rows([integer, choice])] == [1, 2]
    assert [row["initializer"] for row in candidate_rows([integer, choice], "choice")] == [3]


def test_fixed_choice_gap_search_recovers_synthetic_raw32_gap():
    rng = np.random.Generator(np.random.SFC64(37))
    targets = [choice_triplet(rng)]
    for _ in range(3):
        rng.integers(0, 1 << 32, size=11, dtype=np.uint32)
        targets.append(choice_triplet(rng))
    assert fixed_choice_gap_hits("numpy-sfc64", 37, targets, 30, 137) == [11]


def test_fixed_float_gap_search_recovers_synthetic_random_gap():
    rng = np.random.Generator(np.random.Philox(73))
    targets = [float_triplet(rng)]
    for _ in range(3):
        rng.random(size=13)
        targets.append(float_triplet(rng))
    assert fixed_float_gap_hits("numpy-philox", 73, targets, 30, 7) == [13]
