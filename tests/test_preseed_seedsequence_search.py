from tools.preseed_seedsequence_search import triplet, words


def test_words_preserve_endian_layout() -> None:
    raw = bytes.fromhex("0000000100000002")
    assert words(raw, "big") == [1, 2]
    assert words(raw, "little") == [0x01000000, 0x02000000]


def test_seedsequence_triplets_are_deterministic_distinct_and_bounded() -> None:
    entropy = [1, 2, 3, 4]
    for engine in ("pcg64", "pcg64dxsm", "mt19937", "philox", "sfc64"):
        for method in ("choice", "integers-unique"):
            first = triplet(entropy, engine, method)
            assert first == triplet(entropy, engine, method)
            assert len(first) == len(set(first)) == 3
            assert all(100 <= value <= 999 for value in first)
