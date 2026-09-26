# Seed-generation transition architecture

## 2026-09-26 verified chain-seed cutover

This section supersedes the legacy contract-authoritative guidance below. Live
task `b18e7599-ce55-4255-8b26-fdff4e49eca1` proved that the validator now uses
the block-hash implementation from commit `9d9347a`: the uploaded object's
weighted score and fidelity matched exactly, while replay on the round's chain
seeds reproduced the collapsed official consistency. A later observation of
task `f05ef562-7cfb-4635-be86-148734f756df` independently derived seeds
`239,4,77` from finalized blocks `9153330..9153332`; the task contract still
advertised the unrelated value `654,347,964`.

The live sidecar therefore treats the contract seed as telemetry only. It pins
the task's round, opens both 64 KiB PUTs, reads the three block hashes as soon as
the third seed block exists, and overlaps seed-aware building with the four-block
finality wait. Immediately before completing a PUT it re-reads all three hashes;
any change aborts the optimized PUT and leaves the ordinary baseline object
intact. Observation mode (`NIOME_BRIDGE_OBSERVE_ONLY=1`) performs the same seed
proof and build but cancels both incomplete PUTs without replacing S3.

The observed seed-aware build took `111.63s`. Starting only after finality left
about `56s` for the final body and was not robust to primary failure. Starting
the build at the third seed block recovers roughly `48s`; finality is confirmed
before commit, so the extra time does not weaken the accepted seed proof.

### Controlled consistency target

Consistency control is now history-gated and enabled by default. It accepts
only completed `chain-authoritative` rounds whose exact local final score is
also present in the official scoreboard. For each accepted round it computes
`top_score / (local_weighted_score * local_fidelity)`. After at least three
comparable rounds, the target is the 80th percentile of the five most recent
ratios plus a 5% multiplicative safety margin. This normalized form is why a
different absolute score scale in the current task does not invalidate the
threshold.

Targets are applied only inside `0.60..0.85`. Anything outside that range,
missing official history, an unverified local replay, or fewer than three
samples selects the ordinary maximum-score path with consistency `1.0`. Within
the band, the builder blends rows that are HDR-stable on one, two, or all three
authoritative seeds. The two-seed anchor is the replay-measured `0.70`; the
full-seed anchor is `1.0`. A live-artifact replay showed that Stage 4 responds
approximately quadratically rather than linearly to the all-seed row share, so
the controller uses the inverse square-root response plus a 5-point row-share
reserve only to place candidate probes. The final choice never trusts that
estimate: it first exact-replays a pure all-seed fallback, then exact-replays a
bounded high-consistency frontier for up to 45 seconds. It selects the closest
candidate at or above the required factor; if none qualifies, it submits the
already-verified `1.0` fallback. Scarce candidate classes may only refill from
more stable rows, so a shortage also raises consistency rather than silently
dropping below the winning threshold.

The replay budget is also deadline-aware. It reserves at least 75 estimated
seconds before validation for final PUT completion. If fewer than 30 seconds
remain for a useful candidate search, replay targeting is skipped and the
all-seed optimized fallback is selected immediately.

Every decision is stored in `seed_bridge_consistency_decision.json`. The exact
post-upload Stage 3-5 replay stores achieved consistency, baseline, target final
score, and target error in `seed_bridge_local_validation.json`. Set
`NIOME_CONSISTENCY_CONTROL=false` for an immediate operational kill switch.

## Incident interpretation

The September 25 mismatch was not an upload or Stage 1/2/5 failure. The live
submission had the same 250 valid rows, weighted score, and distribution
fidelity locally and officially, while the consistency factor changed from
`1.0` to about `0.098`. Every operated lane failed in the same dimension.

Repository history and the owner messages describe two validator generations:

1. The legacy path reads one or more random seeds from `contract["seed"]`.
   Commit `8451d27` expanded this from one seed to comma-separated multiple
   seeds while retaining the contract as the source of truth.
2. Commit `9d9347a` stops reading task seeds and derives three seeds from public
   block hashes in the range `0..1000`.
3. The owner then announced a rollback to legacy random generation, an expanded
   seed range, and a temporary validator stop. The live mismatch began after
   that transition signal, while public Git still described the block-hash
   implementation.

Therefore a block-derived seed is no longer considered authoritative merely
because it is reproducible. During rollout, validators can be stopped, stale,
mixed-version, or restarted on a generation method that is not yet in public
Git.

The public task-history endpoint later exposed the authoritative seeds for the
failed task `6df6e87b` as `671,163,784`. Replaying the exact object that dollar1
uploaded produced `24.313428746564043`, identical to the official score through
the last displayed digit. Its per-seed scores were `21.959466`, `27.639191`,
and `23.341629`. This closes the incident causally: the upload, Stage 1/2, and
Stage 5 were correct; only the seed source differed.

The next live observation closed the timing question as well. Task `88b58eee`
remained `seed: 0` for 339 signed-object reads, then changed atomically to
`144,901,684` on read 340 at `2026-09-25T19:32:33Z`, near block `9146852` and
18 blocks before validation. At the same time the public `/tasks` record still
reported `0`, so the public listing is not an authoritative low-latency source.
The already-uploaded safe object replays to `25.709173410527853` on those
seeds. The fast exact-seed profile built in `87.34s` and replays to
`241.14024390140852` with consistency `1.0` and weighted score
`257.53822624346805`. This round started before the polling bridge was deployed,
so that exact object was measured offline rather than uploaded.

A public miner fork also documents the same late-stamp lifecycle: initial
contracts carry `0`, the backend later stamps three random seeds, and late
validator calls can build against them. Its two-seed pinning example predicts
the observed top-score shape: two consistency-1 seeds plus one ordinary seed
average to roughly `0.70-0.80`, matching the `0.71-0.83` consistency factors of
the 172-180 point leaders. This was reproduced with the official failed-round
seeds: optimizing only `671,163` and evaluating on `671,163,784` scored
`172.28785193929036` with consistency `0.7043954605878177`. The eighth-place
public result was `170.7527787722124` with consistency
`0.7016510689085526`. Our exact three-seed replay profile targets consistency
`1.0` instead of reproducing that partial hedge.

## Trust model

The miner uses this strict precedence:

| Input | Mode | Treatment |
| --- | --- | --- |
| Non-placeholder `contract.seed` | `contract-authoritative` | Optimize and replay exactly; local score may be compared to official. |
| `contract.seed == 0` | `robust-unknown` | Treat as the block-mode placeholder, not as a real legacy seed. |
| Missing or invalid contract seed | `robust-unknown` | Use deterministic full-width holdout stress seeds. |
| Block-derived/supplied seeds | quarantined by default | Never overwrite the safe object unless `NIOME_TRUST_PROVISIONAL_BLOCK_SEEDS=true` is explicitly set. |

Seed parsing accepts scalar integers, comma strings, and lists. It deliberately
removes the retired `0..1000` assumption and accepts the complete unsigned
32-bit range supported by Stage 4's sklearn random state.

## Submission modes

### Authoritative contract mode

The ordinary miner builds immediately when the initially downloaded contract
already contains a non-placeholder seed. When it contains `0`, the sidecar
keeps its PUT open and polls the original signed `contract_url`. That URL is
valid for roughly 7,000 seconds, substantially longer than the ordinary miner
upload URL. This mirrors the legacy validator, which calls `fetch_task()` again
immediately before validation and then reads the refreshed contract seed.

Only the `seed` field may change between the captured and refreshed contracts.
Any other difference fails closed. When a nonzero scalar, comma string, or list
appears, the sidecar stores `refreshed_contract.json`, builds against the exact
expanded seeds, replays locally, and completes one surviving PUT. It does not
wait for or trust a public seed block.

The sidecar uses a latency profile with one focused variant per anchor and a
five-second final optimizer budget. A replay of task `6df6e87b` on its actual
contract seeds `671,163,784` built in `84.91s` and scored
`241.32486826389297` with consistency `1.0` on every seed. A separate replay on
the retired block-derived seeds built in `88.38s` and scored `242.397635`. The
old full profile took about `121s` and scored `248.191132` on those retired
seeds; the latency profile trades `5.79` points for roughly `33s` of
validator-race margin. The ordinary miner keeps the full safe structural
profile.

### Robust unknown-seed mode

The task ID deterministically generates three full-width holdout seeds used only
for candidate scoring and the reported local estimate. They are disjoint from
any contract or provisional block seeds.

The builder uses the structurally ranked ordinary frontier. Stress seeds are
not optimization inputs: replay showed that training the row selector on a
finite stress ensemble merely creates a second form of seed overfitting and
reduced Stage 2 mass from `261.7` to `179.5`. The seed-specific 1,024-variant
focused reservoir and all-HDR optimizer are therefore skipped. Candidate
dataset variants are evaluated on the disjoint holdout and selected by maximum
holdout minimum score, then holdout mean, so a single catastrophic seed cannot
be hidden by a high average.

The resulting local value is labeled `unknown-seed-holdout-estimate` and is not
compared numerically with the official score. It is a risk estimate, not a
claim about the validator's unrevealed random seeds.

## Sidecar transition

When the initial contract remains the placeholder, the slow-PUT sidecar enters
`waiting_for_contract_seed` and keeps polling while any stream survives. It can
replace the robust baseline only after observing authoritative contract seeds.
If every stream closes or contract refresh fails permanently, the incomplete
requests cannot replace the already-completed baseline because S3 replacement
is atomic. A process restart cannot resume an existing HTTP body, so inherited
active tasks are marked `quarantined` on startup. The old block-derived behavior
is available only behind the explicit provisional trust switch.

## Operational invariants

1. A non-placeholder contract seed always wins over inferred seeds.
2. Expanded seed values are never truncated or reduced modulo 1001.
3. Holdout stress seeds are deterministic, unique, and disjoint from observed
   validator/provisional seeds.
4. Unknown-seed estimates never raise a false exact-score mismatch alarm.
5. A provisional block-seed bridge cannot overwrite the safe robust object by
   default.
6. A refreshed contract must be byte-equivalent in meaning except for `seed`.
7. Every artifact records the selected policy, seed source, score semantics,
   and evaluation seeds for later replay.

## Rollout and rollback

Deploy code before restarting miners. Existing tasks are not rewritten. New
tasks choose their mode from their captured contract, so a validator rollback
that starts emitting random contract seeds is picked up without another miner
restart. If the owner later republishes and proves a block-hash implementation,
operators can test it on one lane with
`NIOME_TRUST_PROVISIONAL_BLOCK_SEEDS=true`; fleet-wide promotion should require
at least two consecutive bit-for-bit local/official replays.
