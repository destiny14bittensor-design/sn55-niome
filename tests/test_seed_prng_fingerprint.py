from tools.seed_prng_fingerprint import (
    _digest_triplet,
    best_lcg_fit,
    build_seed_epochs,
    build_report,
    estimate_catalog_size,
    parse_seed,
    safe_record,
)


def _task(task_id, created_at, seed, mutations=None, cell_type="K562"):
    return {
        "id": task_id,
        "created_at": created_at,
        "content": {
            "contract": {
                "seed": seed,
                "active_mutations": mutations or ["m1", "m2"],
                "cell_type": cell_type,
                "contract_url": "must-not-survive",
            },
            "hbb_reference": {"url": "must-not-survive"},
        },
    }


def test_parse_seed_handles_unknown_single_and_triple():
    assert parse_seed(0) == []
    assert parse_seed("123") == [123]
    assert parse_seed("123, 456,789") == [123, 456, 789]


def test_short_digest_algorithms_still_produce_three_distinct_values():
    values = _digest_triplet("task", "md5", "big", 0, 1000)
    assert len(values) == 3
    assert len(set(values)) == 3


def test_catalog_estimate_exceeds_observed_union_when_sampling_is_incomplete():
    estimate = estimate_catalog_size(617, 624)
    assert estimate is not None
    assert 760 < estimate < 780


def test_seed_epochs_ignore_unknown_gaps_but_split_generator_regimes():
    records = [
        safe_record(_task("a", "2026-01-01T00:00:00Z", "1,2,3")),
        safe_record(_task("b", "2026-01-01T01:00:00Z", 0)),
        safe_record(_task("c", "2026-01-01T02:00:00Z", "4,5,6")),
        safe_record(_task("d", "2026-01-01T03:00:00Z", "9000,8000,7000")),
        safe_record(_task("e", "2026-01-01T04:00:00Z", "7,8,9")),
    ]
    epochs = build_seed_epochs(records)
    assert [epoch["regime"] for epoch in epochs] == [
        "triple-0-1000",
        "triple-other",
        "triple-0-1000",
    ]
    assert [epoch["task_count"] for epoch in epochs] == [2, 1, 1]


def test_lcg_fit_recovers_an_exact_small_sequence():
    values = [3]
    for _ in range(8):
        values.append((7 * values[-1] + 5) % 101)
    fit = best_lcg_fit(values, 101)
    assert fit["transition_matches"] == len(values) - 1
    assert fit["a"] == 7
    assert fit["c"] == 5


def test_safe_record_has_no_urls_or_unneeded_contract_data():
    record = safe_record(
        _task(
            "00000000-0000-4000-8000-000000000001",
            "2026-09-28T00:00:00Z",
            "123,456,789",
        )
    )
    assert set(record) == {
        "task_id",
        "created_at",
        "seeds",
        "mutations",
        "cell_type",
        "contract_material",
        "reference_material",
    }
    assert "url" not in str(record).lower()


def test_report_counts_public_observations_and_information_bound():
    tasks = [
        _task(
            "00000000-0000-4000-8000-000000000001",
            "2026-09-28T00:00:00Z",
            "123,456,789",
            ["m1", "m2"],
            "K562",
        ),
        _task(
            "00000000-0000-4000-8000-000000000002",
            "2026-09-28T01:00:00Z",
            0,
            ["m2", "m3"],
            "HEK293",
        ),
    ]
    report = build_report(tasks)
    assert report["counts"] == {
        "tasks": 2,
        "seeded_tasks": 1,
        "three_seed_tasks": 1,
        "normal_three_seed_tasks": 1,
        "anomalous_three_seed_tasks": 0,
        "three_seed_values": 3,
        "unique_mutations": 3,
        "unique_cell_types": 2,
    }
    assert report["safety"]["contains_signed_urls"] is False
    assert report["information_upper_bound"]["observed_seed_bits"] > 0
    assert report["candidate_scope"] == "configured-current-epoch-only"
    assert report["research_epoch"]["configured_started_at"].startswith("2026-09-25T22:54:35")
