# Rank-30 two-Won research architecture

## Scope and measured gap

This host owns only `won1` and `won2`; `tao1` and `tao2` are owned by the
remote server. Host ownership is selected with `NIOME_FLEET_LANES`, so the
shared repository can serve both machines without deleting either pair.

For task `2058e6c3-cf85-4143-9c88-4a77af07f793`, the official rank-30 cutoff
was `38.2046558773`. `won1` scored `28.1597869400` at rank 108 and `won2`
scored `27.2457164059` at rank 146. The local estimates, `27.9755` and
`27.4090`, were close to the official scores; calibration was not the primary
failure.

The score identity is:

`final = total_weighted_score × consistency_factor × fidelity_factor`

At won1's weighted score (`365.3119`) and fidelity (`0.903542`), reaching the
observed cutoff requires consistency near `0.11575`, versus the observed
`0.08531`: a relative lift of about 35.7%. A 3% safety margin requires roughly
`0.11925`. Small near-tie diversification alone is therefore insufficient.

## Two-lane roles

- `won1` is the control. Preserve the best validated champion-family policy
  until a replacement wins a chronological and prospective gate.
- `won2` is the only local canary. Research may change its policy, but never in
  the same round as won1. Invalid, late, or structurally degraded candidates
  fail closed to the control payload.

The dashboard target is `NIOME_DASH_TARGET_RANK=30`. Seed bridges and legacy
seed-prediction supervisors remain disabled; they are not required for the
honest first-submission path.

## Research sequence

1. **Broader robustness measurement.** Evaluate each frozen submission on a
   predeclared 64-seed holdout and record mean, median, p10, worst case, and
   probability of exceeding the contemporaneous rank-30 cutoff. This is an
   engineering estimate, not an official-score prediction.
2. **Separate design and evaluation seeds.** Any robust candidate selector
   must use a fixed training ensemble and a disjoint holdout ensemble. The
   official seed and same-task official result never enter candidate selection.
3. **Generate full-size alternatives.** Row truncation damages weighted score.
   Candidate research should retain 250 valid rows and vary only bounded
   near-frontier guide identities, bucket allocations, and deterministic
   tie-breaking.
4. **Optimize the multiplicative bottleneck.** Preserve weighted score above
   `360` and fidelity above `0.88`, then seek consistency median above `0.116`
   and p10 above `0.10`. Reject improvements created only by sacrificing one
   of the other factors.
5. **Chronological public-task replay.** Freeze parameters on design rounds,
   choose once on selection rounds, and report rank-30 performance only on the
   final suffix. Do not reuse a consumed suffix as a new holdout.
6. **Prospective canary.** A candidate that passes replay runs only on won2.
   Promotion requires valid on-time submissions and an official improvement
   over won1 on at least three paired future tasks, with no severe regression.

## Promotion gates

A won2 policy is promotable only when all gates pass:

- 250 valid experiments and no upload/deadline regression;
- weighted score `>= 360` and fidelity `>= 0.88` on every final replay;
- holdout consistency median `>= 0.116` and p10 `>= 0.10`;
- payload differs materially from won1 without exceeding the structural-loss
  budget;
- at least three paired prospective official tasks;
- better best-of-two rank outcome than the control in at least two, and no
  catastrophic regression below the control by more than 10%.

The target is measured against the official same-task rank-30 cutoff, never a
fixed score copied from a previous task. `tools/rank_target_analysis.py`
computes the cutoff, factor gaps, and required consistency after each official
publication.
