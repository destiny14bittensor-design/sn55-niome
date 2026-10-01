from datetime import datetime, timezone

from tools.preseed_collective_flip_search import (
    _compact_u32,
    _triplet_mix,
    collective_flip_output,
)
from tools.preseed_drand_search import (
    DRAND_GENESIS_TIME,
    DRAND_PERIOD_SECONDS,
    task_base_round,
)


def test_scale_compact_lengths_used_by_collective_flip():
    assert _compact_u32(0) == b"\x00"
    assert _compact_u32(63) == b"\xfc"
    assert _compact_u32(64) == b"\x01\x01"


def test_triplet_mix_is_bitwise_majority():
    zeros = bytes(32)
    ones = bytes([0xFF]) * 32
    assert _triplet_mix([zeros, ones, ones]) == ones
    assert _triplet_mix([zeros, zeros, ones]) == zeros


def test_collective_flip_needs_exact_81_parent_hashes_and_subject_changes_output():
    block = 1_000
    hashes = {
        str(height): "0x" + height.to_bytes(32, "big").hex()
        for height in range(block - 81, block)
    }
    empty = collective_flip_output(block, b"", hashes)
    named = collective_flip_output(block, b"seed", hashes)
    assert empty is not None and len(empty) == 32
    assert named is not None and named != empty
    hashes.pop(str(block - 40))
    assert collective_flip_output(block, b"", hashes) is None


def test_drand_round_mapping_uses_public_beacon_period():
    timestamp = DRAND_GENESIS_TIME + 100 * DRAND_PERIOD_SECONDS
    record = {
        "created_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    }
    assert task_base_round(record) == 101
