from tools.preseed_wandb_runtime_identity_search import safe_materials


def test_safe_materials_never_reads_args_or_environment() -> None:
    rows = safe_materials(
        {
            "host": "public-host",
            "writerId": "writer",
            "startedAt": "2026-01-01T00:00:00Z",
            "args": ["--api-key", "secret"],
            "environment": {"TOKEN": "secret"},
        },
        "run",
    )
    labels = [label for label, _value in rows]
    values = [value for _label, value in rows]
    assert all("args" not in label and "environment" not in label for label in labels)
    assert all("secret" not in value for value in values)
    assert "host" in labels
