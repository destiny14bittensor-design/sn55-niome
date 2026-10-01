from tools.preseed_event_clock_independent import (
    candidate_seed,
    parse_microseconds,
    search_window,
    triplet,
)


def test_parse_microseconds_is_integer_exact():
    assert parse_microseconds("1970-01-01T00:00:01.234567+00:00") == 1_234_567


def test_search_window_finds_independent_millisecond_offset():
    center_us = 1_700_000_000_123_456
    expected = triplet(candidate_seed(center_us, -17, "milliseconds", "python-sample"), "python-sample")
    assert search_window(
        center_us, expected, "milliseconds", "python-sample", 20
    ) == [-17]
