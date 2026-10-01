import numpy as np

from tools.preseed_pcg64_seed32_scan import compile_scanner, scan


def unique_triplet(rng):
    values = []
    while len(values) < 3:
        value = int(rng.integers(100, 1000))
        if value not in values:
            values.append(value)
    return values


def float_triplet(rng):
    values = []
    while len(values) < 3:
        value = 100 + int(float(rng.random()) * 900)
        if value not in values:
            values.append(value)
    return values


def test_scanner_matches_numpy_2_5_pcg64_stream(tmp_path):
    rng = np.random.default_rng(42)
    targets = [unique_triplet(rng), unique_triplet(rng)]
    binary = tmp_path / "scanner"
    compile_scanner(binary)
    result = scan(binary, targets, 0, 100, 2)
    assert 42 in result["first_triplet_candidates"]
    assert result["full_stream_candidates"] == [42]
    assert result["tested"] == 100


def test_scanner_rejects_wrong_second_triplet(tmp_path):
    rng = np.random.default_rng(42)
    first = unique_triplet(rng)
    binary = tmp_path / "scanner"
    compile_scanner(binary)
    result = scan(binary, [first, [100, 101, 102]], 42, 43, 1)
    assert result["first_triplet_candidates"] == [42]
    assert result["full_stream_candidates"] == []


def test_choice_scanner_matches_numpy_choice_without_replacement(tmp_path):
    rng = np.random.default_rng(73)
    domain = np.arange(100, 1000)
    targets = [rng.choice(domain, 3, replace=False).tolist() for _ in range(2)]
    binary = tmp_path / "scanner"
    compile_scanner(binary)
    result = scan(binary, targets, 0, 100, 2, "choice")
    assert 73 in result["first_triplet_candidates"]
    assert result["full_stream_candidates"] == [73]


def test_dxsm_scanner_matches_numpy_integer_stream(tmp_path):
    rng = np.random.Generator(np.random.PCG64DXSM(91))
    targets = [unique_triplet(rng), unique_triplet(rng)]
    binary = tmp_path / "scanner"
    compile_scanner(binary)
    result = scan(binary, targets, 0, 100, 2, "integers-unique", "pcg64dxsm")
    assert 91 in result["first_triplet_candidates"]
    assert result["full_stream_candidates"] == [91]


def test_dxsm_scanner_matches_numpy_choice_stream(tmp_path):
    rng = np.random.Generator(np.random.PCG64DXSM(19))
    domain = np.arange(100, 1000)
    targets = [rng.choice(domain, 3, replace=False).tolist() for _ in range(2)]
    binary = tmp_path / "scanner"
    compile_scanner(binary)
    result = scan(binary, targets, 0, 100, 2, "choice", "pcg64dxsm")
    assert 19 in result["first_triplet_candidates"]
    assert result["full_stream_candidates"] == [19]


def test_sfc64_scanner_matches_numpy_integer_stream(tmp_path):
    rng = np.random.Generator(np.random.SFC64(61))
    targets = [unique_triplet(rng), unique_triplet(rng)]
    binary = tmp_path / "scanner"
    compile_scanner(binary)
    result = scan(binary, targets, 0, 100, 2, "integers-unique", "sfc64")
    assert 61 in result["first_triplet_candidates"]
    assert result["full_stream_candidates"] == [61]


def test_sfc64_scanner_matches_numpy_choice_stream(tmp_path):
    rng = np.random.Generator(np.random.SFC64(37))
    domain = np.arange(100, 1000)
    targets = [rng.choice(domain, 3, replace=False).tolist() for _ in range(2)]
    binary = tmp_path / "scanner"
    compile_scanner(binary)
    result = scan(binary, targets, 0, 100, 2, "choice", "sfc64")
    assert 37 in result["first_triplet_candidates"]
    assert result["full_stream_candidates"] == [37]


def test_philox_scanner_matches_numpy_integer_stream(tmp_path):
    rng = np.random.Generator(np.random.Philox(47))
    targets = [unique_triplet(rng), unique_triplet(rng)]
    binary = tmp_path / "scanner"
    compile_scanner(binary)
    result = scan(binary, targets, 0, 100, 2, "integers-unique", "philox")
    assert 47 in result["first_triplet_candidates"]
    assert result["full_stream_candidates"] == [47]


def test_philox_scanner_matches_numpy_choice_stream(tmp_path):
    rng = np.random.Generator(np.random.Philox(29))
    domain = np.arange(100, 1000)
    targets = [rng.choice(domain, 3, replace=False).tolist() for _ in range(2)]
    binary = tmp_path / "scanner"
    compile_scanner(binary)
    result = scan(binary, targets, 0, 100, 2, "choice", "philox")
    assert 29 in result["first_triplet_candidates"]
    assert result["full_stream_candidates"] == [29]


def test_float_scanner_matches_all_modern_numpy_engines(tmp_path):
    binary = tmp_path / "scanner"
    compile_scanner(binary)
    for name, constructor, seed in (
        ("pcg64", np.random.PCG64, 17),
        ("pcg64dxsm", np.random.PCG64DXSM, 23),
        ("sfc64", np.random.SFC64, 31),
        ("philox", np.random.Philox, 43),
    ):
        rng = np.random.Generator(constructor(seed))
        targets = [float_triplet(rng), float_triplet(rng)]
        result = scan(binary, targets, 0, 64, 2, "float-unique", name)
        assert seed in result["first_triplet_candidates"]
        assert result["full_stream_candidates"] == [seed]
