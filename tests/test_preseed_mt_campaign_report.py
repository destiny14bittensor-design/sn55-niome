from tools.preseed_mt_campaign_report import aggregate_reports


def report(index, results, *, available=4, predicted=False):
    return {
        "summary": {
            "status": "sat" if any(row[1] == "sat" for row in results) else "unknown",
            "excluded_discovery_predicted": predicted,
            "sealed_seed_labels_opened": 0,
            "assumption_checkpoint_scan": {
                "shard_count": 2,
                "shard_index": index,
                "states_available": available,
                "states_selected": len(results),
                "states_attempted": len(results),
                "results": [
                    {"cumulative_rejections": value, "status": status}
                    for value, status in results
                ],
            },
        }
    }


def test_partial_unsat_shards_never_promote_global_unsat():
    combined = aggregate_reports([report(0, [(0, "unsat")])])
    assert combined["summary"]["status"] == "unknown"
    assert combined["summary"]["globally_unsat"] is False


def test_complete_disjoint_unsat_partition_promotes_global_unsat():
    combined = aggregate_reports(
        [report(0, [(0, "unsat"), (2, "unsat")]), report(1, [(1, "unsat"), (3, "unsat")])]
    )
    assert combined["summary"]["status"] == "unsat"
    assert combined["summary"]["complete_coverage"] is True


def test_predictive_candidate_has_priority_over_plain_sat():
    combined = aggregate_reports(
        [report(0, [(0, "sat")], predicted=True), report(1, [(1, "unknown")])]
    )
    assert combined["summary"]["status"] == "predictive-candidate"
