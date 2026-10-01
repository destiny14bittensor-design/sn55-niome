import json

import numpy as np

from tools.preseed_numpy_interleaved_search import _seed_triplet
from tools.preseed_numpy_prelude_search import load_layout, search_ranges


def test_layout_uses_last_discovery_before_process_boundary(tmp_path):
    path = tmp_path / "discovery.json"
    path.write_text(
        json.dumps(
            {
                "records": [
                    {"created_at": "2026-01-01T00:00:00Z", "seeds": [1, 2, 3]},
                    {"created_at": "2026-01-01T01:00:00Z", "seeds": [4, 5, 6]},
                    {"created_at": "2026-01-01T02:00:00Z", "seeds": [7, 8, 9]},
                ]
            }
        )
    )
    prelude, segment = load_layout(path, "2026-01-01T01:30:00Z")
    assert prelude == [4, 5, 6]
    assert [row["seeds"] for row in segment] == [[7, 8, 9]]

    observed, segment = load_layout(
        path, "2026-01-01T01:30:00Z", [654, 347, 964]
    )
    assert observed == [654, 347, 964]
    assert [row["seeds"] for row in segment] == [[7, 8, 9]]


def test_prelude_search_recovers_synthetic_interleaved_stream():
    rng = np.random.RandomState(37)
    prelude = _seed_triplet(rng, "integers-unique")
    targets = []
    for size in (8, 9, 10):
        values = np.arange(size)
        rng.shuffle(values)
        targets.append(_seed_triplet(rng, "integers-unique"))
    result = search_ranges(prelude, targets, [8, 9, 10], "fixed", [(0, 100)], 1)
    assert any(
        hit["raw_seed"] == 37 and hit["method"] == "integers-unique"
        for hit in result["full_discovery_hits"]
    )
