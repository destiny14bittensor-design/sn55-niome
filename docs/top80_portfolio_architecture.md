# Top-80 honest submission architecture

## Objective and evidence

The operational objective is **at least one of four miners at rank 80 or
better**, measured only by the official scoreboard for the same task ID.  The
2026-10-02 comparison improved the best rank from 198–240 to 126 while the
three former exploratory lanes moved from 238–240 to 126–143.  Their weighted
score recovered from 103.96–154.57 to about 269 after replacing the old
energy/fidelity/upside builders with the structurally proven champion family.

For task `15e6fbfc-2f6d-47f9-a0c8-96cd0806ff1b`, rank 80 required 23.6854.
The fleet already had sufficient weighted score and fidelity; the remaining
gap was predominantly consistency (roughly 0.083 versus 0.099 at rank 80).

## Design

The fleet keeps one stable structural control and three bounded near-tie
reservoir lanes:

| Lane | Policy | Structural loss bound | Purpose |
|---|---|---:|---|
| tao1 | `champion-v1` | 0 | stable control |
| tao2 | `champion-reservoir001-cas55-v3` | 0.1% per bucket | low-risk independent payload |
| won1 | `champion-reservoir003-cas65-v3` | 0.3% per bucket | balanced independent payload |
| won2 | `champion-reservoir005-v3` | 0.5% per bucket | widest bounded payload spread |

Each reservoir first constructs the same valid candidate universe as the
champion.  Inside every mutation/Cas/strand bucket it retains only candidates
within the declared fraction of the best Stage-2 score, then applies a stable
policy-specific hash ordering.  This changes guide identities—and therefore
unknown-seed experiment hashes—without allowing the broad low-energy selection
that caused the old exploratory lanes' weighted-score collapse.

The design optimizes the best-of-four objective, not the average of four nearly
identical submissions.  A public-seed replay screen reduced pairwise payload
Jaccard from 0.82–0.92 for the v2 champion variants to approximately 0.27–0.57
for the v3 portfolio while retaining valid 250-row submissions.

## Leakage and promotion gates

Historical replay always builds with contract seed `0`.  Candidate payloads are
frozen before the published seed is opened for evaluation.  A policy is not
promoted because it fits one task.  The backtest uses chronological
design/selection/final splits and records:

1. all rows valid;
2. official rank-80 cutoff for the same task;
3. best-of-four target hits in at least three of four final rounds;
4. pairwise payload Jaccard below 0.65;
5. score correlation below 0.75;
6. no unsafe restart before the first upload completes.

Until enough v3 rounds exist, `champion-v1` remains the control and the v3
lanes remain canaries.  Failure of a canary never changes or suppresses the
control submission.

## Feedback loop

The dashboard records the official rank-80 cutoff, each lane's signed gap to
that cutoff, the calibrated unknown-seed score interval, and the exact policy
embedded in the task artifact.  After each official result:

1. rebuild the leakage-safe score calibration artifact;
2. append the task to the chronological portfolio replay;
3. compare weighted score, consistency, fidelity, payload overlap, and rank;
4. promote, retain, or retire one canary only—never all lanes simultaneously;
5. preserve tao1 as the stable reference until a successor passes the final
   holdout gate.
