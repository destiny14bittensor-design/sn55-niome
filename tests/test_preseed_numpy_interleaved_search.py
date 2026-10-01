from tools.preseed_numpy_interleaved_search import (
    generate_interleaved,
    search_ranges,
)


def test_interleaved_search_replays_shuffle_before_each_triplet():
    sizes = [8, 9, 10, 11]
    targets = generate_interleaved(31, "choice", sizes)
    result = search_ranges(targets, sizes, [(0, 50)], workers=1)
    assert any(
        hit["seed"] == 31
        and hit["method"] == "choice"
        and hit["matched_tasks"] == 4
        for hit in result["full_stream_hits"]
    )


def test_wrong_noninterleaved_target_is_not_promoted():
    sizes = [8, 9]
    targets = [[101, 202, 303], [404, 505, 606]]
    result = search_ranges(targets, sizes, [(0, 20)], workers=1)
    assert result["full_stream_hits"] == []
