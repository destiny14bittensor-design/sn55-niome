# Seed-aware submission optimizer architecture

## Decision

After the benchmark seeds are public, submission selection is treated as a
deterministic constrained optimization problem rather than a random search.
The optimizer must maximize the validator's exact final-score proxy while
preserving an all-HDR outcome across every published seed.

For an all-HDR submission, Stage 4 is constant and equal to `1.0`. The live
objective therefore reduces to:

```text
objective = sum(stage2_weighted_score) * distribution_fidelity
```

`distribution_fidelity` is reproduced exactly from the validator's Stage 5
implementation. It is the geometric mean of mutation, Cas, strand, joint,
12-mer entropy, and distinct-guide ratios.

## Evidence

Task `992618ca-95a9-4b1f-bf2f-d12add211ab8` produced:

| Metric | UID 80 | Rank 1 | Gap |
|---|---:|---:|---:|
| Final score | 322.6807650894 | 334.2114234594 | 11.5306583700 |
| Stage 2 weighted total | 345.7715370234 | 352.8787070631 | 7.1071700397 |
| Consistency factor | 1.0 | 1.0 | 0 |
| Distribution fidelity | 0.9332195700 | 0.9471 | 0.0138804300 |

The uploaded object reproduced the official score bit-for-bit. All 250 rows
were HDR for all three seeds. The remaining gap is therefore set selection,
not unrecoverable runtime randomness.

## Current architecture limitation

The current `seed-aware` path:

1. assigns one fixed mutation/Cas/strand quota plan;
2. sorts each bucket lexicographically by all-HDR, HDR count, cut count,
   indel length, and Stage 2 score;
3. takes the first rows in each bucket; and
4. never evaluates the final Stage 5 objective during selection.

This guarantees consistency but cannot trade a small Stage 2 change for a
larger diversity gain, cannot discover a better quota plan, and does not
protect against a lexicographic variant frontier with poor 12-mer entropy.

## Target architecture

### 1. Candidate annotation

Every valid candidate is annotated once with:

- Stage 2 weighted score;
- deterministic outcomes for every published seed;
- all-HDR eligibility;
- joint bucket `(mutation, Cas, strand)`;
- guide sequence and its 12-mers; and
- stable design identity used by the validator's duplicate rule.

The simulation cache remains task-local and deterministic.

### 2. Hard safety frontier

All-HDR candidates form the primary frontier. A non-HDR candidate may only be
used if an otherwise required joint bucket cannot be filled. Such a fallback
is explicitly recorded and must never silently replace an all-HDR candidate.

### 3. Quota-plan search

Instead of one hard-coded 76/24 mutation split, the optimizer evaluates a
small deterministic grid around the measured frontier. Each plan specifies:

- minority-mutation share;
- primary-Cas share per mutation; and
- balanced strand allocation.

The grid includes the old plan, so optimization cannot regress merely because
the new search space was introduced.

### 4. Fidelity-aware selection

Within each quota plan, rows are selected from a bounded high-quality pool.
The selector evaluates the exact projected objective, not an arbitrary linear
bonus:

```text
projected_stage2_total * exact_stage5_fidelity(projected_selection)
```

Joint-count fidelity is fixed by the quota plan. Candidate choice then trades
Stage 2 score against exact 12-mer entropy and distinct-guide ratio. Stable
tie-breaking makes repeated runs identical.

### 5. Exact objective gate

The chosen optimized submission and the legacy seed-aware submission are both
scored with the same exact proxy. The optimized submission is accepted only
when:

- it contains the target number of unique valid designs;
- every selected row is HDR for every published seed; and
- its proxy score is not lower than the legacy baseline.

Otherwise the legacy submission is returned. This makes the rollout
monotonic for the known deterministic objective.

### 6. Bounded runtime and degradation

Candidate enumeration remains deadline-aware. Optimization has its own small
time budget and works only on bounded per-bucket pools. Degradation order is:

1. full quota grid plus diversity-aware refinement;
2. reduced quota grid;
3. legacy all-HDR selection; and
4. existing seed-blind safe submission.

The safe upload lane remains independent of this optimizer.

### 7. Diagnostics

Builder diagnostics must record:

- objective version;
- legacy and optimized proxy scores;
- weighted total and every fidelity component;
- evaluated quota-plan count and winning plan;
- all-HDR candidate counts per bucket;
- fallback row count;
- optimizer elapsed time; and
- whether the exact objective gate accepted or rejected the result.

No request URL, signature, or private envelope field is included.

## Implementation boundaries

The scoring proxy and combinatorial selector live in a separate pure module.
Candidate generation remains in `submission_builder.py`, which supplies
annotated candidates to the optimizer. The pure boundary permits synthetic
unit tests and replay tests without network, PM2, or S3.

The initial implementation deliberately avoids multi-process execution. First
it must demonstrate a higher replay score within the existing seed-to-
validation deadline. Parallel candidate annotation is a later optimization if
profiling proves CPU throughput is the limiting factor.

## Acceptance criteria

1. Exact Stage 5 fidelity matches the validator implementation on saved live
   submissions.
2. All selected rows remain all-HDR for every supplied seed.
3. Optimized proxy score never falls below the legacy candidate on the same
   candidate universe.
4. Selection is deterministic.
5. Existing seed-blind behavior and safe-lane deadlines are unchanged.
6. Full tests pass and a saved-task replay reports score, fidelity, runtime,
   and selected quota plan before any live-process restart is considered.

## Implemented replay result

The architecture was implemented and replayed against task
`992618ca-95a9-4b1f-bf2f-d12add211ab8` with seeds `[217, 962, 84]`.

| Metric | Uploaded legacy | New optimizer | Change |
|---|---:|---:|---:|
| Final/proxy score | 322.6807650894 | 336.6003602766 | +13.9195951871 |
| Stage 2 weighted total | 345.7715370234 | 367.4588894215 | +21.6873523981 |
| Consistency factor | 1.0 | 1.0 | 0 |
| Distribution fidelity | 0.9332195700 | 0.9160218189 | -0.0171977511 |
| Full builder elapsed | 80.263 s live | 92.774 s final replay | +12.511 s |

The exact Stage-5 proxy is the validator implementation reproduced in the pure
optimizer and all 250 selected rows remained Stage-1-valid and all-HDR for all
published seeds. The search consumed its bounded 20-second window; the
reported 20.865-second optimizer diagnostic also includes candidate-pool
preparation outside that search window. Relative to the round's official first
place (`334.2114234594`), the replay is ahead by `2.3889368171`.

The final selection used a 205/45 mutation split, near-balanced global Cas
counts (134/116), a 121/129 strand split, 250 distinct guides, and all-HDR
outcomes for every row and seed. Its 12-mer entropy ratio rose to
`0.9846679864`. Cross-bucket exact-objective refinement made 15 swaps, 12 of
them between different joint buckets, proving that symmetric quotas were an
unnecessary restriction.

The implementation now adds five bounded layers:

1. 30,464 focused variants across the strongest four PAM anchors per joint
   bucket, generated from a 1,024-variant reservoir per anchor;
2. exact scoring of 216 structured quota plans;
3. diversity-aware construction for both scalar-score finalists and explicit
   20/22/24/26% mutation-balance anchors at 50% Cas allocation;
4. an optimistic-bound shortlist that does not penalize recoverable k-mer
   concentration before diversity construction; and
5. exact cross-bucket swap refinement over all six Stage-5 components.

The balance anchors confirmed the trade-off directly. Their replay scores were
`335.4564` at 20%, `334.7660` at 22%, `333.0269` at 24%, and `331.5469` at
26%. This shows that adding minority rows monotonically is not the right fix;
the best replay remained near 18%, while the 20% anchor provides a strong
high-fidelity alternative.

## Further improvement frontier

The second-wave implementation addresses the original five opportunities:

1. Adaptive batched focused generation with a fixed hard cap.
2. A 192-row score partition plus a 64-row start/12-mer diversity partition.
3. A bounded two-step beam after exact one-swap convergence.
4. Separate pool-preparation, search, and total optimizer timings.
5. Persistent 20/22/24/26% balance anchors rather than a fixed 18% policy.

## Second-wave architecture

The next implementation keeps the exact objective and safety gate unchanged,
but separates candidate sufficiency, pool construction, and local search.

### Adaptive all-HDR reservoir

Focused variants are generated in deterministic batches distributed across
the strongest PAM anchors. After every complete anchor round, the builder
counts candidates that are all-HDR for every public seed and are within a
small weighted-score tolerance of the best all-HDR row in that joint bucket.
Generation stops when the bucket has enough near-best all-HDR rows to fill its
largest normal seed-aware quota plus a 10% reserve. Alignment-start and k-mer
coverage are enforced independently by the diversity half of the bounded pool,
so an intrinsically single-start high-score frontier does not force wasteful
generation to the hard cap.

A hard per-anchor cap remains. Thus adaptive generation can only reduce work
relative to the proven 1,024-variant reservoir; it cannot run without bound.

### Score and diversity pool union

The bounded optimizer pool is no longer a raw weighted-score prefix. It is the
stable union of:

1. score leaders, which preserve the Stage-2 ceiling; and
2. a diversity reservoir ranked by new alignment starts, unseen 12-mers, rare
   12-mer mass, and then weighted score.

The score partition is always larger than the maximum expected joint quota.
The final pool size remains bounded, so search complexity does not grow with
the full candidate universe.

### Bounded two-step escape

Exact one-swap refinement remains the primary local optimizer. If it converges
and time remains, a small beam retains the best deterministic near-neutral
first moves, including a bounded number that are individually non-improving.
Each beam state receives one exact improving follow-up move. Only a two-move
result that beats the original exact proxy is accepted. The non-regression
gate still compares the final result with the legacy submission.

### Deadline accounting

Diagnostics report candidate-pool preparation, exact search, and total
optimizer time separately. The exact-search deadline begins only after the
pool is ready, while an optional enclosing builder deadline can shorten that
budget. Adaptive generation checks the enclosing deadline between batches.
No partially generated selection bypasses the legacy fallback.

### Second-wave acceptance criteria

1. Adaptive and fixed-max generation produce deterministic candidates.
2. Every selected optimized row remains all-HDR for every public seed.
3. Each optimizer pool preserves the configured score-leader count and has no
   duplicate design identity.
4. Two-step escape is accepted only on exact objective improvement.
5. Seed-blind output is byte-for-byte unaffected by focused generation.
6. Candidate preparation and search timings are independently observable.
7. Saved-task replay must not regress below `336.6003602766` before rollout.

## Second-wave replay result

The conservative adaptive policy requires at least two full 256-variant rounds
per PAM anchor before applying its sufficiency test. On the saved task it
reduced focused candidates from `30,464` to `20,385` (33.1%) while reproducing
the full-reservoir result:

| Metric | First-wave full reservoir | Second-wave adaptive |
|---|---:|---:|
| Final proxy | 336.6003602766 | **336.6024578003** |
| Stage-2 weighted total | 367.4588894215 | 366.5241368512 |
| Distribution fidelity | 0.9160218189 | **0.9183636873** |
| Focused candidates | 30,464 | **20,385** |
| Full builder elapsed | 92.774 s | **91.565 s** |
| Pool preparation | not separated | 2.137 s |
| Exact search | deadline-limited | 17.728 s |

The score/diversity union preserved 192 score leaders and added 64 diversity
leaders in every joint bucket. Exact refinement completed 19 one-row swaps.
The two-step beam evaluated 768 first moves and four beam states, correctly
rejecting all of them because none improved the exact proxy. The result remains
`2.3910343408` above the saved round's official first-place score.

An initially more aggressive one-batch stop reduced focused candidates to
16,419 but scored only `336.5561386264`; it was rejected by the replay gate.
This negative result is why the implementation retains the two-batch minimum.

The live PM2 processes were not restarted during implementation or replay, so
the running round continued using the previously loaded builder. Deployment is
intentionally separate from code validation.

## Third-wave local-search frontier audit

The second-wave pool contains 192 score leaders and 64 diversity leaders per
joint bucket, but the original one-swap implementation consumed only the first
16 unselected rows. Because score leaders are stored first, the diversity
partition was effectively invisible to local refinement.

A direct replacement experiment reserved four of those 16 slots for
structure-ranked candidates. It was deterministic and valid, but changed the
hill-climb path and reduced the saved-task replay from `336.6024578003` to
`336.5796412337`. That design was rejected.

The accepted architecture preserves the original 16 score-frontier slots and
runs a separate, deadline-bounded diversity polish only after the established
one-swap result. The polish ranks candidates against the current selection by
new alignment start, unseen 12-mers, rare 12-mer mass, and weighted score. Its
result is accepted only on exact proxy improvement, so timeout, no-op, and
inferior paths retain the established selection. The two-step beam uses the
same mixed-frontier principle under its own exact acceptance gate.

Saved-task replay of the accepted architecture reproduced `336.6024578003`
exactly with 250/250 valid rows, `0.9183636873` distribution fidelity, and
`366.5241368512` Stage-2 weighted total. The diversity polish found no exact
improvement on that task and was correctly rejected. Search took `17.785 s`;
total optimizer time was `19.971 s`, within the 20-second budget.

An independent replay on task `2b703bb2-8b35-442d-9cbc-0b978d54e027` with
seeds `[128, 43, 659]` scored `249.7236306757`, compared with the live old-code
submission's local score of `241.4919942095` (`+8.2316364662`). All 250 rows
were valid and fidelity increased from `0.9343742075` to `0.9422638006`. The
winning minority share was 22%, rather than the saved task's 18%, confirming
that the quota grid adapts to task-specific score/fidelity geometry instead of
encoding a single global balance point.
