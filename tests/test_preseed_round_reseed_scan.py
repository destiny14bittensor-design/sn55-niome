import pytest

from tools.preseed_round_reseed_scan import discovery_targets


def test_discovery_targets_requires_fixed_twenty_rows() -> None:
    document = {"records": [{"seeds": [100, 101, 102]} for _ in range(20)]}
    assert len(discovery_targets(document)) == 20


def test_discovery_targets_rejects_duplicate_seed() -> None:
    document = {"records": [{"seeds": [100, 101, 102]} for _ in range(20)]}
    document["records"][3]["seeds"] = [100, 100, 102]
    with pytest.raises(ValueError, match="distinct"):
        discovery_targets(document)
