from tools.preseed_lcg_state_recovery import generate_erand48, generate_mysql


def test_database_generators_are_deterministic_and_in_range():
    assert generate_erand48(123, 5) == generate_erand48(123, 5)
    assert generate_mysql(123, 456, 5) == generate_mysql(123, 456, 5)
    assert all(100 <= value <= 999 for value in generate_erand48(123, 20))
    assert all(100 <= value <= 999 for value in generate_mysql(123, 456, 20))
