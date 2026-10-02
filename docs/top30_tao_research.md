# Tao top-30 research objective

The active objective is for at least one of `tao1` or `tao2` to place at rank
30 or better under an ordinary pre-seed submission. `tao1` remains the stable
`champion-v1` control. `tao2` is the only canary. The `won1` and `won2`
processes are outside this deployment's change scope.

## First measured gap

For task `2058e6c3-cf85-4143-9c88-4a77af07f793`, rank 30 required
`38.20465587731724` points while rank 80 required `28.85257917244887`.
`tao1` scored `27.121355568924784` and `tao2` scored
`26.548013078298396`.

The limiting component was consistency, not Stage-2 weight or distribution
fidelity. Rank 30 had `364.5647` weighted score, `0.8636` fidelity, and
`12.1347` consistency. Tao's weighted score and fidelity were already near or
above the boundary, but consistency was only `8.1697` (`tao1`) and `7.9833`
(`tao2`). At the observed weight and fidelity, reaching the same cutoff would
have required roughly a 40-45% consistency increase.

Across the latest 29 scored public tasks observed on 2026-10-02, the rank-30
cutoff ranged from `17.5710` to `78.9870`, with median `54.2260`, P75
`59.7998`, and P90 `63.7267`. A fixed raw-score target is therefore secondary;
promotion is decided directly by rank-30 success on chronological tasks.

Among 249 hotkeys present in at least 20 of those rounds, only 12 reached rank
30 in at least 25% of their observations. Their successful observations had a
median consistency percentile of `0.9395`, versus `0.4637` when they missed the
target. Weighted-score percentile moved from `0.6452` to `0.7258`, while
fidelity moved only from `0.5927` to `0.6371`. This makes a consistency spike
the strongest observed separator, while confirming that weight and fidelity
floors must still be preserved.

## Research and promotion gate

1. Keep `tao1` unchanged as the paired control.
2. Keep only one `tao2` canary active at a time.
3. Measure every prospective task before adding its result to calibration.
4. Optimize the consistency/weighted-score product, not consistency alone.
5. Reject candidates with invalid rows, timeout growth, fidelity collapse, or
   evidence of post-score/seed leakage.
6. Require at least eight prospective observations for a policy-level model.
7. Promote only after chronological holdout and prospective rounds improve the
   probability of rank 30 without degrading submission reliability.

The next shadow-candidate search should therefore reject the old unrestricted
`maximin-v1` shape: it reduced weighted score to `154.57` without producing a
consistency gain. Candidate construction should instead explore a bounded
energy/feature shell around the champion core, require a task-normalized
weighted-score percentile floor, and rank candidates by consistency upside
subject to that floor. No such candidate replaces the live `tao2` policy until
the current reservoir canary has an official prospective result.

## Current prospective shadow comparison

Task `65ec3d6d-d009-41f4-a1bf-28611054b479` provides the first paired
pre-seed observation against the actual `tao1` control payload. These are
stress-seed estimates made before the official task seeds and scores are
published; they are selection evidence, not official performance claims.

| Policy | Status | Jaccard vs tao1 | Stress score | Weighted score | Consistency | Fidelity |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `champion-reservoir001-cas55-v3` | live tao2 | `0.639344` | `26.403988` | `350.7571` | `8.32642` | `0.904075` |
| `champion-reservoir003-cas65-v3` | leading shadow | `0.453488` | `26.473582` | `351.2410` | `8.36185` | `0.901374` |
| `champion-reservoir005-v3` | diversity-only shadow | `0.488095` | `25.482430` | `350.9350` | `8.02211` | `0.905160` |

The `reservoir003` shadow dominates the live canary on this task's stress
score (`+0.2636%`), weighted score (`+0.1380%`), and consistency (`+0.4255%`),
while reducing control overlap by `0.185856`. Its small fidelity decrease stays
above the research floor. It is therefore the leading next-canary candidate,
but remains undeployed until the frozen live submission receives an official
score and additional prospective evidence passes the promotion gate.

A broader frozen-payload audit then evaluated the exact uploaded `tao1`, exact
uploaded `tao2`, and matching `reservoir003` shadow payload on eight disjoint
synthetic seed triples (24 seeds total). `reservoir003` beat live `tao2` in all
8/8 paired ensembles. Its median score was `26.7453` versus `25.7309`, and its
P25 score was `25.6010` versus `24.5840`; median consistency was `8.4477`
versus `8.1142`. It also beat `tao1` in 7/8 ensembles. The result strengthens
the next-canary case, but does not clear the full promotion gate: correlation
with `tao1` was `0.7807`, slightly above the `0.75` independence ceiling, and
the absolute score uplift remains far smaller than the observed rank-30 gap.
The audit used neither the current official seed nor current official score.

The same eight paired ensembles show a real stability-versus-diversification
trade-off at tolerance `0.005`. `reservoir005` had a lower individual median
(`26.1859`) than `reservoir003` (`26.7453`), and their direct paired result was
4-4 with a median delta of `-0.1485` for `005`. However, `005` correlated only
`0.7062` with `tao1`, below the `0.75` ceiling. The `tao1+reservoir005`
best-of-two portfolio had P25 `25.6670`, minimum `24.6432`, and maximum
`29.3036`, versus `25.6010`, `24.0001`, and `28.2094` for
`tao1+reservoir003`. Thus `003` remains the stable single-canary leader while
`005` is the stronger tail-diversification hypothesis. Neither is promoted
before the current official result and additional prospective observations.

### Deterministic row-order audit

Stage 4 shuffles row indices with the round seed, so deterministic row ordering
was tested as a seed-independent way to decorrelate fold composition without
changing any experiment, weighted score, or fidelity. Three salts were screened
only on ensembles 0-3. `rank30-order-a-v1` advanced because it produced the
highest design-split best-of-two median and maximum while remaining below the
correlation ceiling.

The salt failed the sealed ensembles 4-7. Its individual median was only
slightly higher than unpermuted `reservoir005` (`27.9925` versus `27.9275`),
but the original order won 3/4 paired ensembles. The `tao1+reservoir005`
portfolio also had better P25 (`27.6437` versus `27.4282`), maximum (`29.3036`
versus `29.1592`), and lower correlation with `tao1` (`0.2419` versus
`0.3794`). Deterministic ordering affects finite-fold scores but did not
generalize as an improvement, so no ordering salt is deployed.

Two other shapes were rejected. `champion-hybrid20-cas65-v3` gained
consistency but lost `4.33%` stress score and `8.54%` weighted score versus the
live canary. The experimental `champion-caslearn10-cas55-v4` lost `8.19%`
stress score and `3.80%` consistency, so its temporary implementation was
removed. `reservoir005` is retained only as a low-overlap comparison because
its stress score and consistency both trail the live canary.

`tools/portfolio_backtest.py --reference-submission PATH` now hashes a frozen
control payload and applies the existing Jaccard promotion gate between that
payload and every candidate. This prevents a seemingly diverse set of shadow
policies from passing while all of them still duplicate the control lane. A
single frozen control is task-specific, so this option requires `--tasks 1`.
The report keeps deterministic stress selection separate from published-seed
replay: a placeholder seed of `0` never receives an official-replay label or
an estimated public rank.

Re-evaluate already frozen payloads over several paired unknown-seed ensembles
without rebuilding or uploading them with:

```bash
.venv/bin/python tools/frozen_payload_stress_audit.py \
  --task-root artifacts/miners/tao1/TASK_ID \
  --submission tao1=artifacts/miners/tao1/TASK_ID/submission.json \
  --submission tao2-live=artifacts/miners/tao2/TASK_ID/submission.json \
  --submission reservoir003-shadow=PATH_TO_FROZEN_SHADOW.json \
  --ensembles 8 \
  --output artifacts/research/top30_frozen_payload_stress.json
```

The same tool accepts `--order-variant LABEL=SOURCE:SALT` and
`--ensemble-offset N`. Use disjoint offsets for design and holdout; never choose
a salt after inspecting its holdout results.

Run the public, credential-free audit with:

```bash
.venv/bin/python tools/top_rank_gap_audit.py \
  --tasks 30 \
  --target-rank 30 \
  --miner tao1=5GbhpWKt2SYHaZMHNy2WAsm5pzkGL5sRa7DFnYu9zJY3qYGC \
  --miner tao2=5Ehx52VbhGyvmZVcvaRF2dG8JVJUMHHreRBLRsGMkJ695zih \
  --output artifacts/research/top30_tao_gap.json
```

The dashboard reads `NIOME_TARGET_RANK` and the fleet configuration sets it to
30. No miner policy is changed merely because the displayed objective changes.
