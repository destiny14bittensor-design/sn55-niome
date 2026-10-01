from pathlib import Path
import json

from tools.preseed_mt_shift_shard_supervisor import (
    build_shard_command,
    shard_paths,
    shift_values,
    summarize_output,
)


def test_shift_values_is_inclusive_and_rejects_invalid_ranges():
    assert shift_values("0:5") == [0, 1, 2, 3, 4, 5]
    for value in ("1", "-1:2", "3:2"):
        try:
            shift_values(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid shift range accepted: {value}")


def test_shard_command_fixes_exact_requested_shift(tmp_path: Path):
    output, checkpoint = shard_paths(tmp_path, "run", 3)
    command = build_shard_command(
        constraints=Path("constraints.json"),
        tuple_report=Path("tuples.json"),
        corridor_plan=Path("corridors.json"),
        resume=Path("resume.json"),
        output=output,
        checkpoint=checkpoint,
        corridor_rank=1,
        rounds=4,
        target_round=1,
        target_position=67,
        shift=3,
        time_limit=10,
        threads=2,
    )
    index = command.index("--fixed-trace-shift")
    assert command[index + 1] == "1:67:3"
    assert command[command.index("--cegar-resume") + 1] == "resume.json"


def test_missing_shard_is_never_reported_as_rejected(tmp_path: Path):
    result = summarize_output(tmp_path / "missing.json", 2, 124)
    assert result["terminal"] is False
    assert result["status"] == "missing"
    assert result["candidate_promoted"] is False


def test_old_unprebound_artifact_is_not_exact_shift_evidence(tmp_path: Path):
    path = tmp_path / "old.json"
    path.write_text(
        json.dumps(
            {
                "summary": {
                    "status": "unsat",
                    "fixed_trace_shifts": [
                        {"round": 1, "position": 67, "shift": 0}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    result = summarize_output(
        path, 0, 0, target_round=1, target_position=67
    )
    assert result["status"] == "unsat"
    assert result["exact_shift_certified"] is False


def test_prebound_artifact_is_certified_for_only_its_requested_shift(tmp_path: Path):
    path = tmp_path / "new.json"
    path.write_text(
        json.dumps(
            {
                "summary": {
                    "status": "unsat",
                    "fixed_trace_positions_prebound": 1,
                    "fixed_trace_shifts": [
                        {"round": 1, "position": 67, "shift": 2}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    assert summarize_output(
        path, 2, 0, target_round=1, target_position=67
    )["exact_shift_certified"] is True
    assert summarize_output(
        path, 1, 0, target_round=1, target_position=67
    )["exact_shift_certified"] is False
