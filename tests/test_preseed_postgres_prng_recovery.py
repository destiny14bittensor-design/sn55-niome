from tools.preseed_postgres_prng_recovery import concrete_step


def test_xoroshiro128ss_matches_reference_vector_shape():
    output, s0, s1 = concrete_step(1, 2)
    assert output == 5760
    assert s0 == 0x0000000001030003
    assert s1 == 0x0000006000000000
