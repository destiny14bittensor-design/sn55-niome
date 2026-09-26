"""Runtime seed policy for validator-generation transitions.

The owner can switch between task-carried random seeds and validator-derived
seeds without changing the task schema.  Treat a non-placeholder contract seed
as authoritative, but never silently promote a miner-derived/block-derived seed
to the same trust level.  Unknown modes use deterministic, full-width stress
seeds so an expanded legacy range does not invalidate the local gate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from typing import Any, Iterable


UINT32_MAX = 2**32 - 1
ROBUST_HOLDOUT_SEED_COUNT = 3


def parse_seed_values(raw: Any) -> list[int]:
    """Parse legacy scalar, comma-string, or list seeds without a 0..1000 cap."""
    if raw is None or isinstance(raw, bool):
        return []
    values: Iterable[Any]
    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple)):
        values = raw
    else:
        values = [raw]

    parsed: list[int] = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError("boolean is not a valid benchmark seed")
        seed = int(value)
        # sklearn's KFold random_state ultimately consumes an unsigned 32-bit
        # seed.  This is a runtime limit, not the retired legacy 0..1000 range.
        if not 0 <= seed <= UINT32_MAX:
            raise ValueError(f"benchmark seed {seed} is outside uint32 range")
        if seed not in parsed:
            parsed.append(seed)
    return parsed


def deterministic_stress_seeds(
    task_id: str,
    *,
    domain: str,
    count: int,
    exclude: Iterable[int] = (),
) -> list[int]:
    """Return reproducible full-width seeds, domain-separated from live seeds."""
    excluded = set(int(seed) for seed in exclude)
    seeds: list[int] = []
    counter = 0
    while len(seeds) < count:
        digest = hashlib.sha256(
            f"niome-seed-policy-v1|{domain}|{task_id}|{counter}".encode()
        ).digest()
        seed = int.from_bytes(digest[:4], "big")
        counter += 1
        if seed in excluded or seed in seeds:
            continue
        seeds.append(seed)
    return seeds


@dataclass(frozen=True)
class SeedPlan:
    mode: str
    source: str
    optimization_seeds: tuple[int, ...]
    evaluation_seeds: tuple[int, ...]
    holdout_seeds: tuple[int, ...]
    comparable_to_official: bool
    contract_seed_raw: Any
    supplied_seeds: tuple[int, ...]
    reason: str

    @property
    def selection_profile(self) -> str:
        # Unknown seeds must not become a new Monte-Carlo training target. The
        # structurally ranked frontier generalizes better; stress seeds are kept
        # strictly out-of-sample for the maximin candidate gate.
        return (
            "seed-aware"
            if self.mode
            in {
                "contract-authoritative",
                "supplied-authoritative",
                "chain-authoritative",
            }
            else "ranked"
        )

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("optimization_seeds", "evaluation_seeds", "holdout_seeds", "supplied_seeds"):
            value[key] = list(value[key])
        return value


def resolve_seed_plan(
    contract: dict[str, Any],
    task_id: str,
    *,
    supplied_seeds: Iterable[int] | None = None,
    trust_supplied_seeds: bool = False,
    prefer_supplied_seeds: bool = False,
    accept_zero_contract_seed: bool = False,
) -> SeedPlan:
    """Choose authoritative legacy seeds or a quarantine-safe stress ensemble."""
    raw = contract.get("seed")
    contract_seed_error: str | None = None
    try:
        contract_seeds = parse_seed_values(raw)
    except (TypeError, ValueError) as error:
        contract_seeds = []
        contract_seed_error = str(error)
    supplied = parse_seed_values(list(supplied_seeds or ()))

    if supplied and trust_supplied_seeds and prefer_supplied_seeds:
        return SeedPlan(
            mode="chain-authoritative",
            source="finalized-block-hashes",
            optimization_seeds=tuple(supplied),
            evaluation_seeds=tuple(supplied),
            holdout_seeds=tuple(),
            comparable_to_official=True,
            contract_seed_raw=raw,
            supplied_seeds=tuple(supplied),
            reason="validator-compatible finalized block-hash seeds override the task seed",
        )

    contract_is_placeholder = contract_seeds == [0] and not accept_zero_contract_seed
    if contract_seeds and not contract_is_placeholder:
        holdout = deterministic_stress_seeds(
            task_id,
            domain="contract-holdout",
            count=ROBUST_HOLDOUT_SEED_COUNT,
            exclude=contract_seeds,
        )
        return SeedPlan(
            mode="contract-authoritative",
            source="contract",
            optimization_seeds=tuple(contract_seeds),
            evaluation_seeds=tuple(contract_seeds),
            holdout_seeds=tuple(holdout),
            comparable_to_official=True,
            contract_seed_raw=raw,
            supplied_seeds=tuple(supplied),
            reason="non-placeholder contract seed follows the legacy validator path",
        )

    if supplied and trust_supplied_seeds:
        return SeedPlan(
            mode="supplied-authoritative",
            source="supplied",
            optimization_seeds=tuple(supplied),
            evaluation_seeds=tuple(supplied),
            holdout_seeds=tuple(),
            comparable_to_official=False,
            contract_seed_raw=raw,
            supplied_seeds=tuple(supplied),
            reason="operator explicitly enabled provisional/block-derived seeds",
        )

    excluded = set(contract_seeds) | set(supplied)
    holdout = deterministic_stress_seeds(
        task_id,
        domain="robust-holdout",
        count=ROBUST_HOLDOUT_SEED_COUNT,
        exclude=excluded,
    )
    if contract_seed_error:
        reason = f"contract seed is unusable ({contract_seed_error})"
    elif contract_is_placeholder:
        reason = "contract seed 0 is treated as a transition placeholder"
    else:
        reason = "contract does not contain usable validator seeds"
    if supplied:
        reason += "; provisional/block seeds are quarantined after live drift"
    return SeedPlan(
        mode="robust-unknown",
        source="deterministic-stress",
        optimization_seeds=tuple(),
        evaluation_seeds=tuple(holdout),
        holdout_seeds=tuple(holdout),
        comparable_to_official=False,
        contract_seed_raw=raw,
        supplied_seeds=tuple(supplied),
        reason=reason,
    )
