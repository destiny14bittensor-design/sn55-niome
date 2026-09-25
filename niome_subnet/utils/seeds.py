"""Derive NIOME benchmark seeds from the round's finalized block hashes."""

from __future__ import annotations

import hashlib
import logging
import time

import niome_subnet.utils.settings as config
from niome_subnet.utils.misc import FINALITY_LAG


logger = logging.getLogger(__name__)


def seed_blocks(block: int) -> list[int]:
    round_start = block - (block - config.BASE_BLOCK_NUMBER) % config.INTERVAL_BLOCKS
    start = round_start + config.SEED_BLOCK
    return [start + offset for offset in range(config.SEED_COUNT)]


def _block_hash(subtensor, block: int) -> str:
    delay = 5.0
    for attempt in range(1, config.SEED_READ_ATTEMPTS + 1):
        try:
            info = subtensor.block_info(block)
            if info is None or not info.hash:
                raise RuntimeError(f"node served no header for block {block}")
            return info.hash
        except Exception as error:
            if attempt == config.SEED_READ_ATTEMPTS:
                raise
            logger.warning(
                "Could not read seed block %d (%d/%d): %s; retrying in %.1fs",
                block,
                attempt,
                config.SEED_READ_ATTEMPTS,
                error,
                delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, 60.0)


def _hash_bytes(block_hash: str) -> bytes:
    raw = bytes.fromhex(str(block_hash).removeprefix("0x"))
    if len(raw) != config.BLOCK_HASH_BYTES:
        raise ValueError(f"expected a {config.BLOCK_HASH_BYTES}-byte block hash")
    return raw


def seed_from_block_hash(
    block_hash: str,
    exclude=(),
    seed_range=config.SEED_RANGE,
) -> int:
    raw = _hash_bytes(block_hash)
    low, high = seed_range
    span = high - low + 1
    counter = 0
    while True:
        digest = hashlib.sha256(raw + counter.to_bytes(4, "big")).digest()
        seed = low + int.from_bytes(digest, "big") % span
        counter += 1
        if seed not in exclude:
            return seed


def seeds_from_block_hashes(
    block_hashes,
    seed_range=config.SEED_RANGE,
) -> list[int]:
    seeds: list[int] = []
    for block_hash in block_hashes:
        seeds.append(
            seed_from_block_hash(block_hash, exclude=seeds, seed_range=seed_range)
        )
    return seeds


def generate_seeds(current_block: int, subtensor) -> list[int]:
    blocks = seed_blocks(current_block)
    target = blocks[-1] + FINALITY_LAG
    if current_block < target:
        subtensor.wait_for_block(target, timeout=config.SEED_FINALITY_TIMEOUT)
    hashes = [_block_hash(subtensor, block) for block in blocks]
    seeds = seeds_from_block_hashes(hashes)
    logger.info("Derived benchmark seeds %s from blocks %s", seeds, blocks)
    return seeds


def generate_seed_prefix(
    current_block: int,
    subtensor,
    count: int,
) -> list[int]:
    """Read the first ``count`` seed blocks once those hashes exist.

    This is used only when a late request's upload URL cannot survive until
    finality.  Even one known benchmark seed materially improves the averaged
    three-seed result; callers explicitly log that these hashes are provisional.
    """
    blocks = seed_blocks(current_block)[:count]
    if not 1 <= count <= len(seed_blocks(current_block)):
        raise ValueError(f"invalid seed prefix count {count}")
    hashes = [_block_hash(subtensor, block) for block in blocks]
    seeds = seeds_from_block_hashes(hashes)
    logger.info("Derived provisional seed prefix %s from blocks %s", seeds, blocks)
    return seeds


def seeds_available_block(block: int) -> int:
    return seed_blocks(block)[-1] + FINALITY_LAG
