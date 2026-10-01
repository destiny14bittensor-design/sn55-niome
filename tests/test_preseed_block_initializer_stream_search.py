import random

from tools.preseed_block_initializer_stream_search import (
    public_initializers,
    python_triplet,
    search_block_candidates,
)


def test_public_initializers_are_deduplicated():
    rows = public_initializers(123, "0x" + "11" * 32)
    keys = [(type(value), value) for _label, value in rows]
    assert len(keys) == len(set(keys))
    assert any(label == "raw-bytes" for label, _value in rows)
    assert any(label == "height-int" for label, _value in rows)


def test_search_recovers_python_bytes_persistent_stream():
    block_hash = "0x" + "42" * 32
    raw = bytes.fromhex(block_hash[2:])
    rng = random.Random(raw)
    prelude = python_triplet(rng, "sample")
    targets = [python_triplet(rng, "sample") for _ in range(3)]
    result = search_block_candidates(456, block_hash, prelude, targets, [8, 8, 8])
    assert any(
        row["initializer"] == "raw-bytes"
        and row["engine"] == "python-random"
        and row["method"] == "sample"
        and row["matched_triplets_including_prelude"] == 4
        for row in result["exact"]
    )
