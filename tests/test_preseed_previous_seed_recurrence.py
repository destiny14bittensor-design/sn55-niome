import hashlib

from tools.preseed_previous_seed_recurrence import (
    context_material,
    digest_triplet,
    hmac_triplet,
    previous_material,
)


def test_previous_seed_encodings_are_stable():
    seeds = [101, 202, 999]
    assert previous_material(seeds, "csv") == b"101,202,999"
    assert previous_material(seeds, "json") == b"[101,202,999]"
    assert previous_material(seeds, "concat") == b"101202999"
    assert previous_material(seeds, "u16-big") == b"\x00e\x00\xca\x03\xe7"


def test_context_material_reads_public_block_fields():
    row = {
        "task_id": "task-1",
        "created_at": "2026-01-01T00:00:00",
        "contract_material": "{}",
        "block_context": {"created": {"hash": "0xabc"}},
    }
    assert context_material(row, "task-id") == b"task-1"
    assert context_material(row, "block-created-hash") == b"0xabc"


def test_digest_triplet_counter_matches_direct_calculation():
    material = b"prior:context"
    expected = []
    for counter in range(3):
        digest = hashlib.sha256(material + counter.to_bytes(4, "big")).digest()
        expected.append(100 + int.from_bytes(digest[:8], "big") % 900)
    assert digest_triplet(material, "sha256", "counter-big") == expected


def test_hmac_triplet_counter_matches_direct_calculation():
    import hmac

    key, message = b"491,210,379", b"task-2"
    expected = []
    for counter in range(3):
        digest = hmac.new(
            key, message + counter.to_bytes(4, "big"), "sha256"
        ).digest()
        expected.append(100 + int.from_bytes(digest[:8], "big") % 900)
    assert hmac_triplet(key, message, "sha256", "counter-big") == expected
