import hashlib

from tools.preseed_composite_prng_search import (
    composite_material,
    digest_integer,
    seeded_triplet,
)


RECORD = {
    "task_id": "db80ab60-fce1-4c99-b5e8-af1eae84f50b",
    "created_at": "2026-09-26T06:06:32.517388",
    "contract_material": '{"active_mutations":["A"]}',
    "block_context": {"created": {"number": 9150020}},
}


def test_composite_material_respects_order_and_delimiter():
    block_hash = "0x" + "01" * 32
    forward = composite_material(
        RECORD, block_hash, 9150020, "raw", "task-id", "block-field", b":"
    )
    reverse = composite_material(
        RECORD, block_hash, 9150020, "raw", "task-id", "field-block", b":"
    )
    assert forward == bytes.fromhex("01" * 32) + b":" + RECORD["task_id"].encode()
    assert reverse == RECORD["task_id"].encode() + b":" + bytes.fromhex("01" * 32)


def test_digest_integer_and_seeded_triplet_match_reference():
    material = b"public-block:task"
    expected = int.from_bytes(hashlib.sha256(material).digest()[:4], "big")
    assert digest_integer(material, "sha256", "first4-big") == expected
    assert seeded_triplet(expected, "python", "sample") == seeded_triplet(
        expected, "python", "sample"
    )
