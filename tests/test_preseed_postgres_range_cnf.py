from tools.preseed_postgres_range_cnf import replay_range


def test_range_replay_returns_only_expected_domain():
    values = replay_range(0x123456789ABCDEF0, 0xFEDCBA9876543210, 20)
    assert len(values) == 20
    assert all(100 <= value <= 999 for value in values)
