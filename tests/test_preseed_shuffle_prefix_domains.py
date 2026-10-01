from tools.preseed_fisher_yates_model import replay_shuffle
from tools.preseed_shuffle_prefix_domains import exact_prefix_domains, uid_aware_sequences


def test_exact_permutation_yields_singleton_prefix_choices():
    initial = [f"u{index}" for index in range(8)]
    choices = [3, 1, 4, 0, 2, 1, 0]
    final = replay_shuffle(initial, choices)
    result = exact_prefix_domains(initial, final, depth=4)
    assert result["status"] == "sat"
    assert [row["choice_domain"] for row in result["prefix"]] == [
        [value] for value in choices[:4]
    ]


def test_missing_token_is_enumerated_as_possible_suffix_insertion():
    initial = ["a", "b", "c", "d"]
    result = exact_prefix_domains(initial, ["a", "b", "c"], depth=1)
    assert result["status"] == "sat"
    assert result["omitted_group_tokens"] == 1
    assert result["prefix"][0]["choice_domain"] == [2, 3]


def test_uid_aware_sequences_preserves_singleton_uid_domains():
    row = {
        "initial_uids": [10, 11, 12],
        "initial_domain_sequence": ["g0", "g1", "g1"],
        "ordered_uid_domains": ["g1", "g0", "u12"],
        "ordered_group_domains": ["g1", "g0", "g1"],
        "uid_domains": {"g0": [10], "g1": [11, 12], "u12": [12]},
    }
    initial, observed, exact = uid_aware_sequences(row)
    assert initial == ["u10", "g1", "u12"]
    assert observed == ["g1", "u10", "u12"]
    assert exact == 2
