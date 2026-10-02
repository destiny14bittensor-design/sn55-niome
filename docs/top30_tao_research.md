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
