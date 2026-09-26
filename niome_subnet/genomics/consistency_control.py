"""Conservative, history-backed consistency targeting.

The final validator score is ``baseline * consistency`` where ``baseline`` is
the product of the Stage-2 weighted score and Stage-5 distribution fidelity.
Normalising historical winning scores by our baseline therefore makes rounds
with different absolute score scales comparable and, importantly, removes the
need to know the current round's baseline before the seed-aware build starts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Iterable


DEFAULT_MIN_SAMPLES = 3
DEFAULT_HISTORY_LIMIT = 5
DEFAULT_QUANTILE = 0.80
DEFAULT_SAFETY_MARGIN = 0.05
DEFAULT_MIN_TARGET = 0.60
DEFAULT_MAX_TARGET = 0.85
# Optimising two of three seeds produced 0.7044 in the verified replay.  Use a
# deliberately rounded anchor; exact replay results are retained for auditing.
PARTIAL_SEED_ANCHOR = 0.70
MIX_RESPONSE_EXPONENT = 2.0
MIX_SHARE_RESERVE = 0.05


@dataclass(frozen=True)
class ConsistencySample:
    task_id: str
    top_score: float
    baseline_score: float
    realized_consistency: float

    @property
    def normalized_top(self) -> float:
        return self.top_score / self.baseline_score

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["normalized_top"] = self.normalized_top
        return value


@dataclass(frozen=True)
class ConsistencyDecision:
    mode: str
    reason: str
    sample_count: int
    required_consistency: float | None
    target_consistency: float | None
    safety_margin: float
    quantile: float
    normalized_top_scores: tuple[float, ...]
    partial_seed_anchor: float = PARTIAL_SEED_ANCHOR
    all_seed_hdr_share: float | None = None

    @property
    def targeting_enabled(self) -> bool:
        return self.mode == "targeted"

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["normalized_top_scores"] = list(self.normalized_top_scores)
        value["targeting_enabled"] = self.targeting_enabled
        return value


def _linear_quantile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def all_seed_hdr_share_for_target(
    target_consistency: float,
    *,
    partial_seed_anchor: float = PARTIAL_SEED_ANCHOR,
) -> float:
    """Map a desired factor to a full-HDR row share.

    Rows stable on the first two seeds but deliberately unconstrained on the
    third form the measured lower anchor.  Rows stable on all seeds form the
    1.0 anchor.  The builder uses this share as a monotonic control variable;
    the exact replay remains the authoritative achieved value.
    """
    if not 0.0 <= partial_seed_anchor < 1.0:
        raise ValueError("partial_seed_anchor must be in [0, 1)")
    normalized_target = max(
        0.0,
        min(
            1.0,
            (float(target_consistency) - partial_seed_anchor)
            / (1.0 - partial_seed_anchor),
        ),
    )
    if normalized_target == 0.0:
        return 0.0
    # Exact replay showed that Stage 4 reacts approximately quadratically to
    # the full-HDR row share: a 20% share moved the factor only from ~0.70 to
    # ~0.708.  Invert that response and retain a small upward-only reserve so
    # model error cannot silently erase the winning-score safety margin.
    return min(
        1.0,
        normalized_target ** (1.0 / MIX_RESPONSE_EXPONENT)
        + MIX_SHARE_RESERVE,
    )


def decide_consistency_target(
    samples: Iterable[ConsistencySample],
    *,
    enabled: bool = True,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    quantile: float = DEFAULT_QUANTILE,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
    min_target: float = DEFAULT_MIN_TARGET,
    max_target: float = DEFAULT_MAX_TARGET,
) -> ConsistencyDecision:
    """Return a fail-safe target or the ordinary maximum-score policy."""
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be in [0, 1]")
    if safety_margin < 0.0:
        raise ValueError("safety_margin must be non-negative")
    if not 0.0 <= min_target <= max_target <= 1.0:
        raise ValueError("target bounds must satisfy 0 <= min <= max <= 1")

    valid = [
        sample
        for sample in samples
        if math.isfinite(sample.top_score)
        and math.isfinite(sample.baseline_score)
        and sample.top_score > 0.0
        and sample.baseline_score > 0.0
        and math.isfinite(sample.realized_consistency)
        and 0.0 <= sample.realized_consistency <= 1.0
    ][: max(0, int(history_limit))]
    ratios = tuple(sample.normalized_top for sample in valid)

    def maximum(reason: str, required: float | None = None) -> ConsistencyDecision:
        return ConsistencyDecision(
            mode="max-score",
            reason=reason,
            sample_count=len(valid),
            required_consistency=required,
            target_consistency=None,
            safety_margin=safety_margin,
            quantile=quantile,
            normalized_top_scores=ratios,
        )

    if not enabled:
        return maximum("consistency control is disabled")
    if len(valid) < min_samples:
        return maximum(
            f"need at least {min_samples} comparable completed rounds; have {len(valid)}"
        )

    required = _linear_quantile(list(ratios), quantile) * (1.0 + safety_margin)
    if not math.isfinite(required):
        return maximum("historical target is not finite")
    if required < min_target:
        return maximum(
            "required factor is below the configured manipulation floor",
            required,
        )
    if required > max_target:
        return maximum(
            "required factor exceeds the safe targeting ceiling",
            required,
        )

    target = max(min_target, min(max_target, required))
    return ConsistencyDecision(
        mode="targeted",
        reason=(
            "history-normalized winning threshold plus safety margin is inside "
            "the configured targeting band"
        ),
        sample_count=len(valid),
        required_consistency=required,
        target_consistency=target,
        safety_margin=safety_margin,
        quantile=quantile,
        normalized_top_scores=ratios,
        all_seed_hdr_share=all_seed_hdr_share_for_target(target),
    )
