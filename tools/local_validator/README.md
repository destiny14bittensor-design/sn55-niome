# SN55 Local Validator Clone

이 도구는 public validator commit의 Stage 1~5와 score-to-weight 계산을 로컬에서 재현한다.
S3, NIOME backend, Bittensor chain에는 write하지 않는다.

## Evaluate a submission

```bash
uv run python -m tools.local_validator evaluate \
  --submission data/submission.json \
  --contract data/contract.json \
  --hbb-reference data/hbb_reference.json \
  --chromosome-11 data/chr11.fa \
  --cell-types data/cell_types.json \
  --uid 12 \
  --output local-score.json
```

결과에는 최종 점수, validator-compatible breakdown, retained/dropped row 정보,
Stage 1/2 결과, seed별 Stage 3/4/5 trace, artifact hash와 runtime 버전이 포함된다.

## Simulate top-10 weights

`scores.json`은 UID 순서의 list 또는 `{ "uid": score }` object다.

```bash
uv run python -m tools.local_validator weights \
  --scores scores.json \
  --owner-uid 0 \
  --min-allowed-weights 1 \
  --max-weight-limit 65535 \
  --output local-weights.json
```

실제 emission은 여러 validator의 stake/weights와 chain consensus가 필요하므로 이 도구의
범위 밖이다. `--max-weight-limit`에는 평가하려는 block에서 사용한 SDK 표현값을 넣어야 한다.
