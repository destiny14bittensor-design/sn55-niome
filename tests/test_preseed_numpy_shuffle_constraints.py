from dataclasses import dataclass

from tools.preseed_numpy_shuffle_constraints import (
    apply_owned_capture_observations,
    build_event_index_constraint,
    build_round_constraint,
    is_complete_observation_round,
    metagraph_window_blocks,
    stable_endpoint_uid_map,
)


@dataclass
class Neuron:
    uid: int
    trust: float
    axon: str | None


def test_constraint_builder_keeps_only_unique_endpoint_order_and_seals_label():
    neurons = [
        Neuron(0, 0, "shared"),
        Neuron(1, 0, "unique-a"),
        Neuron(2, 0, "shared"),
        Neuron(3, 0, "unique-b"),
        Neuron(4, 1, "validator"),
    ]
    row = build_round_constraint(
        "task",
        123,
        ["shared", "unique-b", "shared", "unique-a"],
        neurons,
        None,
    )
    assert row["initial_uids"] == [0, 1, 2, 3]
    assert row["unique_uid_subsequence"] == [3, 1]
    assert row["domain_order_constraints"] == 4
    assert row["ambiguous_domain_positions"] == 2
    assert [row["uid_domains"][token] for token in row["ordered_uid_domains"]] == [
        [0, 2],
        [3],
        [0, 2],
        [1],
    ]
    assert row["seed_label_partition"] == "sealed"
    assert "discovery_seed_label" not in row


def test_constraint_builder_includes_only_explicit_discovery_label():
    row = build_round_constraint(
        "task",
        123,
        ["a"],
        [Neuron(0, 0, "a")],
        [101, 202, 303],
    )
    assert row["discovery_seed_label"] == [101, 202, 303]
    assert row["seed_label_partition"] == "discovery"


def test_domain_constraints_drop_endpoint_churn_that_cannot_be_mapped():
    row = build_round_constraint(
        "task",
        123,
        ["a", "a"],
        [Neuron(0, 0, "a"), Neuron(1, 0, None)],
        None,
    )
    assert row["ordered_uid_domains"] == []
    assert row["uid_domains"] == {}


def test_stable_endpoint_map_keeps_only_domains_equal_in_every_snapshot():
    first = [Neuron(0, 0, "a"), Neuron(1, 0, "shared"), Neuron(2, 0, "shared")]
    middle = [Neuron(0, 0, "a"), Neuron(1, 0, "changed"), Neuron(2, 0, "shared")]
    last = [Neuron(0, 0, "a"), Neuron(1, 0, "shared"), Neuron(2, 0, "shared")]
    assert stable_endpoint_uid_map([first, middle, last]) == {"a": [0]}


def test_metagraph_window_blocks_cover_broadcast_with_finality_lag():
    blocks = metagraph_window_blocks(
        1000,
        "2026-09-26T12:00:00+00:00",
        [{"timestamp": "2026-09-26T13:00:00+00:00"}],
        snapshots=5,
        finality_lag=4,
        block_seconds=12,
    )
    assert blocks == [996, 1071, 1146, 1221, 1296]


def test_exact_error_uid_is_kept_as_sanitized_singleton_domain():
    row = build_round_constraint(
        "task",
        123,
        ["a"],
        [Neuron(0, 0, "a"), Neuron(1, 0, "b")],
        None,
        [
            {"kind": "endpoint", "value": "a"},
            {"kind": "uid", "value": 1},
        ],
    )
    assert [row["uid_domains"][token] for token in row["ordered_uid_domains"]] == [
        [0],
        [1],
    ]
    assert row["exact_error_uid_constraints"] == 1
    assert row["initial_domain_sequence"] == ["g0", "g1"]
    assert row["ordered_group_domains"] == ["g0", "g1"]


def test_owned_capture_replaces_first_post_capture_event_with_exact_uid():
    events, stats = apply_owned_capture_observations(
        [
            {
                "kind": "endpoint",
                "value": "before.invalid:1",
                "timestamp": "2026-09-26T12:00:01+00:00",
            },
            {
                "kind": "endpoint",
                "value": "owned.invalid:2",
                "timestamp": "2026-09-26T12:00:06+00:00",
            },
        ],
        [("lane", 42, "2026-09-26T12:00:02+00:00")],
        expected_endpoints={42: "owned.invalid:2"},
        maximum_delay_seconds=10,
    )
    assert stats == {"matched": 1, "rejected": 0}
    assert events[0]["kind"] == "endpoint"
    assert events[1] == {
        "kind": "uid",
        "value": 42,
        "endpoint": "owned.invalid:2",
        "timestamp": "2026-09-26T12:00:06+00:00",
        "source": "owned-request-envelope",
    }


def test_owned_capture_fails_closed_outside_delay_window():
    events, stats = apply_owned_capture_observations(
        [
            {
                "kind": "endpoint",
                "value": "late.invalid:1",
                "timestamp": "2026-09-26T12:01:00+00:00",
            }
        ],
        [("lane", 42, "2026-09-26T12:00:00+00:00")],
        expected_endpoints={42: "late.invalid:1"},
        maximum_delay_seconds=30,
    )
    assert stats == {"matched": 0, "rejected": 1}
    assert events[0]["kind"] == "endpoint"


def test_owned_capture_rejects_a_time_match_on_the_wrong_endpoint():
    events, stats = apply_owned_capture_observations(
        [
            {
                "kind": "endpoint",
                "value": "other.invalid:9",
                "timestamp": "2026-09-26T12:00:03+00:00",
            }
        ],
        [("lane", 42, "2026-09-26T12:00:02+00:00")],
        expected_endpoints={42: "owned.invalid:2"},
        maximum_delay_seconds=10,
    )
    assert stats == {"matched": 0, "rejected": 1}
    assert events[0]["kind"] == "endpoint"


def test_owned_uid_is_counted_separately_from_validator_error_uid():
    row = build_round_constraint(
        "task",
        123,
        ["a", "b"],
        [Neuron(0, 0, "a"), Neuron(1, 0, "b")],
        None,
        [
            {
                "kind": "uid",
                "value": 0,
                "endpoint": "a",
                "source": "owned-request-envelope",
            },
            {
                "kind": "uid",
                "value": 1,
                "endpoint": "b",
                "source": "validator-error",
            },
        ],
    )
    assert row["exact_owned_uid_constraints"] == 1
    assert row["exact_owned_uid_subsequence"] == [0]
    assert row["exact_error_uid_constraints"] == 1
    assert row["exact_error_uid_subsequence"] == [1]


def test_event_index_mode_preserves_positions_without_endpoint_attribution():
    row = build_event_index_constraint(
        "task",
        123,
        [Neuron(0, 0, "stale-a"), Neuron(1, 0, "stale-b"), Neuron(2, 0, None)],
        [101, 202, 303],
        [
            {"kind": "endpoint", "value": "changed.invalid:1"},
            {
                "kind": "uid",
                "value": 1,
                "endpoint": "changed.invalid:2",
                "source": "owned-request-envelope",
            },
        ],
    )
    assert row["identity_mode"] == "event-index-exact-only"
    assert row["initial_domain_sequence"] == ["?", "u1", "?"]
    assert row["ordered_uid_domains"] == ["?", "u1"]
    assert row["uid_domains"]["?"] == [0, 2]
    assert row["uid_domains"]["u1"] == [1]
    assert row["group_sequence_missing_positions"] == 1
    assert row["exact_owned_uid_constraints"] == 1


def test_in_progress_round_is_excluded_until_observation_floor():
    events = [{"kind": "uid", "value": index} for index in range(199)]
    assert is_complete_observation_round(events, 200) is False
    events.append({"kind": "uid", "value": 199})
    assert is_complete_observation_round(events, 200) is True
