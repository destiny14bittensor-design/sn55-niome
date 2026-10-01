from tools.preseed_runtime_identity_search import (
    derived_seeds,
    numpy_stream,
    public_materials,
    python_stream,
)


def test_runtime_material_labels_are_unique_and_values_not_empty() -> None:
    rows = public_materials("2026-09-26T14:57:51.192497Z")
    assert len({label for label, _value in rows}) == len(rows)
    assert all(value for _label, value in rows)


def test_derived_streams_are_deterministic() -> None:
    seeds = list(derived_seeds("material"))
    assert seeds == list(derived_seeds("material"))
    integer = next(value for _name, value in seeds if isinstance(value, int))
    assert python_stream(integer, "sample", 3) == python_stream(integer, "sample", 3)
    assert numpy_stream(integer, "random-state", "choice", 3) == numpy_stream(
        integer, "random-state", "choice", 3
    )
