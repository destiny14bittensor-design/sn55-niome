# Rank-30 fast-screen results

## Scorer accuracy versus seed uncertainty

The local Stage 1-5 clone was checked against task
`2058e6c3-cf85-4143-9c88-4a77af07f793` after its seed became public. With
the exact public seed triplet (`337,990,570`), both local scores and every
breakdown component matched the official API exactly for won1 and won2. The
final-score error was `0.0` for both lanes.

The remaining pre-publication error is therefore seed-sampling uncertainty,
not an implementation mismatch. For the surviving `champion-reservoir005-v3`
policy on three historical tasks, the three-stress-seed estimate minus the
public-seed replay was `+0.2141`, `+0.7370`, and `-0.7752` points. This is
accurate enough for coarse candidate rejection, but it does not reveal the
unobserved seed.

A local score computed from an assumed seed is not an observation of the
validator's seed. The mapping from a three-seed tuple to one aggregate score is
many-to-one, and adjacent integer seeds do not produce adjacent outcomes
because experiment RNG states are hash-derived. Consequently, local estimates
alone cannot identify an authoritative seed or guarantee consistency in a
`0.5-0.8` band. No pre-publication seed-inference or same-round overwrite path
is enabled by this work.

The incremental calibration model now retains evidence from migrated artifact
roots and adds new published won rounds without duplication. Evidence grew
from 34 to 36 records; chronological holdout MAE improved from `0.5800` to
`0.3995`, and the global P10-P90 residual interval narrowed to approximately
`[-1.5152, +1.8529]`.

## Successive screen

Eight initial policies were replayed on the latest scored task. The exact
public-seed scores were:

| Policy | Score | Estimated rank | Consistency | Weighted | Fidelity |
| --- | ---: | ---: | ---: | ---: | ---: |
| champion-reservoir005-v3 | 28.5782 | 89 | 0.08611 | 364.71 | 0.91003 |
| rank30-reservoir008-cas65-v4 | 28.2439 | 101 | 0.08541 | 364.82 | 0.90645 |
| champion-reservoir003-cas65-v3 | 28.1598 | 108 | 0.08531 | 365.31 | 0.90354 |
| rank30-reservoir004-cas65-v4 | 27.2034 | 149 | 0.08228 | 365.32 | 0.90504 |
| champion-hybrid20-cas65-v3 | 26.9033 | 159 | 0.08633 | 342.63 | 0.90956 |
| rank30-reservoir002-cas65-v4 | 26.8606 | 159 | 0.08126 | 365.32 | 0.90488 |
| rank30-hybrid20-cas65-g96-v4 | 26.7072 | 165 | 0.08616 | 340.51 | 0.91030 |
| rank30-reservoir003-cas65-g96-v4 | 25.8254 | 183 | 0.07814 | 365.32 | 0.90464 |

The top two were replayed on three scored tasks. Neither reached rank 30;
`champion-reservoir005-v3` won all three pairwise comparisons and had ranks
193, 155, and 89. The `reservoir008` candidate had ranks 199, 198, and 101.

Two high-energy candidates slightly raised consistency to `0.08886` and
`0.08781`, but collapsed weighted score to `258.96` and `260.30`, yielding
only `20.74` and `20.48` final points. They were rejected.

No candidate passed the canary gate, so neither live miner policy was changed.
won1 remains the control and won2 remains on `champion-reservoir005-v3`.

## Frozen live-payload audit

The actual latest won1 and won2 payloads were then evaluated on three paired,
independent synthetic seed triples without using the task's published score or
seed. won1's score range was `25.3266-29.9992` with median `26.2209`; won2's
range was `25.1985-28.6606` with median `26.5528`. won1 won two ensembles and
won2 won one. Payload Jaccard was `0.4409`, but score correlation was `0.9765`.

The lanes therefore contain materially different experiment identities but
still react similarly to task/seed difficulty. There is no evidence from this
audit that changing the live policies would improve the rank-30 probability.
