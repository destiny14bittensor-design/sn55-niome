# Partial seed prediction report

Generated on 2026-10-01 from the 20 public Discovery tasks in the active
`100..999`, three-distinct-seed epoch.

## Question

This audit asks a narrower question than full generator recovery: can public
round material predict at least one exact seed, or rank an exact seed inside a
small Top-K set, even when all three seeds cannot be recovered?

Numeric distance is deliberately ignored.  Stage 3 hashes the integer seed
with experiment material, so a prediction one unit away does not receive
partial credit.  Exact equality is the only Top-1 hit.

## Frozen method

- Tasks are sorted by `created_at`.
- Five initial tasks establish history, followed by 15 expanding-window
  forecasts.
- Every forecast is made from strictly older rows.  The target's `seeds` field
  is removed before any predictor receives it.
- The last four Discovery tasks form the final suffix.  They were not used to
  choose the predictor or its parameters before the first audit run.
- The separate sealed Holdout dataset was not opened.
- The tested primary method is a history-weighted ensemble of the existing 640
  explicit public block/task digest models.  Uniform registry voting, historical
  seed frequency, and previous-round reuse are fixed causal controls.
- K was fixed to `3, 5, 10, 20, 50, 100`.  Reported significance uses a
  Bonferroni correction across strategies, metrics, K values, and the two
  reported time partitions.

The executable audit is `tools/preseed_partial_hit_audit.py`; the reusable
predictor is `tools/preseed_partial_predictor.py`.  The complete machine report
is written to the ignored runtime artifact
`artifacts/research/preseed_partial_hit_audit.json`.

## Walk-forward result

| Strategy | Split | Exact slot hits | Rounds with any Top-1 set hit | Top-20 slot hits | Adjusted Top-20 p |
|---|---|---:|---:|---:|---:|
| registry-weighted | development | 0/33 | 0/11 | 1/33 | 1.0000 |
| registry-weighted | final suffix | 0/12 | 0/4 | 1/12 | 1.0000 |
| registry-weighted | all forecasts | 0/45 | 0/15 | 2/45 | 1.0000 |
| registry-uniform | development | 0/33 | 0/11 | 2/33 | 1.0000 |
| registry-uniform | final suffix | 0/12 | 0/4 | 1/12 | 1.0000 |
| registry-uniform | all forecasts | 0/45 | 0/15 | 3/45 | 1.0000 |
| history-frequency | development | 0/33 | 0/11 | 4/33 | 0.3818 |
| history-frequency | final suffix | 0/12 | 0/4 | 0/12 | 1.0000 |
| history-frequency | all forecasts | 0/45 | 0/15 | 4/45 | 1.0000 |
| previous-round | development | 0/33 | 0/11 | 2/33 | 1.0000 |
| previous-round | final suffix | 0/12 | 0/4 | 0/12 | 1.0000 |
| previous-round | all forecasts | 0/45 | 0/15 | 2/45 | 1.0000 |

The configured domain gives a random slot Top-1 rate of `1/900` and a random
intersection probability of approximately `0.00998` for two unordered
three-value sets.  None of the four methods produced even one exact Top-1
partial hit in 45 evaluated slots.  The primary final-suffix Top-20 result was
`1/12`; its unadjusted binomial p-value was about `0.2364` and its corrected
p-value was `1.0`.  It is not evidence of predictability.

The frozen audit verdict is therefore:

> **C — partial prediction did not exceed the random baseline.**

## Local score value of a genuinely known partial seed

Prediction failure does not mean a partial seed would be valueless.  A separate
offline replay used final-suffix task
`ff481da5-9160-4e1a-b35d-3fbbdc921cc0`, whose public official seeds are
`921,816,217`.  The existing robust payload and newly rebuilt seed-aware
payloads were evaluated by the local public validator.  No network call or
submission was made.

| Scenario | Optimization seeds | Per-seed final scores | Mean final score | Uplift |
|---|---|---|---:|---:|
| existing robust | none | 18.29 / 21.21 / 19.87 | 19.79 | baseline |
| one-seed oracle | 921 | 241.46 / 17.49 / 18.81 | 92.59 | +72.79 (4.68x) |
| two-seed oracle | 921, 816 | 239.86 / 239.86 / 17.35 | 165.69 | +145.90 (8.37x) |
| three-seed oracle | 921, 816, 217 | 236.74 / 236.74 / 236.74 | 236.74 | +216.94 (11.96x) |

This confirms the arithmetic-mean mechanism empirically: one exact seed can
materially improve one third of the evaluation while unknown seeds retain
roughly robust-level performance.  The replay is reproducible with
`tools/preseed_partial_score_replay.py`; its captured result is
`artifacts/research/preseed_partial_score_replay_ff481.json`.

## Decision and next gate

Partial exact knowledge has very high economic value, but the currently
observable public features did not predict it.  The result does not justify
running the stopped round collector or operationalizing Top-K guesses.

The final suffix has now been consumed and must not be reused as an untouched
gate for a newly tuned model.  Any new predictor must be frozen first and then
earn confirmation on at least two genuinely future rounds, with the same
multiple-testing correction.  Until a new independent input or prospective
signal exists, active generator and partial-prediction research remains
rejected.
