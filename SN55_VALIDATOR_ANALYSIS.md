# SN55 / NIOME Validator Scoring Pipeline Reverse Engineering

> 분석 기준: 2026-09-22 UTC, `main` @ `9f3ada447b8fbec8d5b200694507f134ea8b9a6b`
> 분석 원칙: README가 아니라 위 커밋의 실행 코드가 기준이다. 확인할 수 없는 값은 `UNKNOWN`으로 표시한다.
> 범위: 분석과 로컬 복제 설계까지만 포함한다. production miner 코드는 수정하지 않았다.

## 1. Executive Summary

SN55의 현재 validator는 miner가 HTTP 응답 본문으로 결과를 돌려주는 일반적인 request/response 구조가 아니다. validator는 miner axon의 `/forward`에 `Task`와 약 5분짜리 S3 presigned PUT URL을 보내고, HTTP 상태 코드만 확인한다. miner는 결과 JSON을 그 URL에 별도로 업로드해야 한다. 이후 validator는 S3의 `niome/{uid}.json`을 내려받아 로컬에서 5단계 평가를 수행한다.

핵심 결론은 다음과 같다.

1. **최종 점수는 Stage 1~5의 가중합이 아니다.** Stage 1은 hard validation/filter, Stage 2는 각 valid row의 biological base score, Stage 3은 seed별 synthetic outcome, Stage 4는 그 synthetic outcome을 Random Forest가 얼마나 일관되게 예측하는지 측정, Stage 5는 coverage/diversity factor를 곱한다.
2. seed 하나의 최종식은 대략 다음 구조이며, 실제 정의는 §12에 정확히 적었다.

   ```text
   seed_score
     = sum(Stage2 weighted_score)
       × Stage4 consistency_factor
       × Stage5 distribution_fidelity

   biological_score = mean(seed_score for every contract seed)
   ```

3. **Stage 3은 외부 CRISPR 예측 모델이 아니다.** Azimuth, Rule Set 3, inDelphi, FORECasT checkpoint를 불러오지 않는다. 코드에 직접 적힌 확률식과 seed 고정 Python PRNG를 사용한다.
4. **Stage 4의 ML은 pretrained inference가 아니다.** 제출물마다 `RandomForestRegressor` 세 개를 새로 cross-validation하며 synthetic labels를 회귀한다. 모델 파일이나 checkpoint는 없다.
5. **Stage 5는 real/reference dataset과 비교하지 않는다.** 제출물 내부의 mutation/Cas/strand/joint/12-mer/guide 다양성만 계산한다. README의 설명과 실행 코드가 충돌한다.
6. Stage 1의 정상적인 규칙 위반은 row 하나를 invalid로 만든다. 그러나 required key 누락, 잘못된 top-level type, 일부 type error 등은 예외를 발생시키며 validator의 UID 단위 broad `except`까지 전파된다. 이 경우 해당 UID는 그 validation run의 score 목록에서 사라지고 결과적으로 weight 입력은 0이다.
7. executable submission schema에는 field whitelist가 없다. README가 금지한다고 쓰는 `efficiency`, repair outcome, off-target score 등의 derived field도 실제 코드는 거부하지 않고 무시한다. 따라서 “정책상 금지”와 “현재 validator가 실행 시 강제”를 구분해야 한다.
8. score가 emission에 비례 배분되는 것도 아니다. positive-score miner 중 상위 10 UID만 고정 비율 `[30%, 20%, 20%, 15%, 5%, 3%, 2.5%, 2%, 1.5%, 1%]`을 받고, 부족하면 해당 prefix를 재정규화한다. 그 뒤 2% burn/owner weight를 혼합한다. 즉 biological score는 주로 **순위와 top-10 진입**을 결정한다.
9. EMA, historical score smoothing, score moving average는 현재 weight 경로에 없다. 설정에 남은 `moving_average_alpha`나 `self.scores`는 이 경로에서 사용되지 않는다.
10. 정확 복제의 가장 큰 장벽은 알고리즘보다 **평가 시점의 artifact**다. contract, HBB reference, cell-type table, chr11 파일, seed 목록, dependency 버전을 함께 보존하지 않으면 동일 점수를 재현할 수 없다.
11. 추가로 중요한 구현상 경계가 있다. 현재 Stage 4는 5-fold R²를 사용하므로 valid merged row가 2~9개이면 적어도 한 test fold가 1개가 되어 R²가 `NaN`이고 consistency factor가 0이 된다. 0 또는 1개도 코드상 0이다. 따라서 정상적인 sklearn 동작 기준으로 **최소 10 valid rows 전에는 최종 score가 0**이다. 이는 명시적 business rule이 아니라 구현 결과다.
12. S3 object key에는 task ID가 없고 freshness 검증도 없다. validation은 현재 등록 miner 전원의 같은 UID key를 읽는다. 그러므로 stale object가 평가될 수 있으며, broadcast task와 validation task의 비동기 경계 및 task 재-fetch 때문에 “보낸 task”와 “평가한 contract”가 다를 가능성도 코드상 존재한다.

복제 가능성은 Stage 1/2/3/5와 최종 산술은 artifact 확보 후 A, Stage 4와 rank/chain transform은 B, 실제 네트워크 emission은 validator stake와 Yuma consensus 때문에 D다.

## 2. Repository / Version

| 항목 | 확인 결과 |
|---|---|
| 공식 repository | `https://github.com/genomesio/subnet-niome.git` |
| 분석 checkout | `/home/administrator/workspace/subnet-niome` |
| branch | `main` |
| HEAD | `9f3ada447b8fbec8d5b200694507f134ea8b9a6b` |
| HEAD 제목/날짜 | `upload all miners submissions`, 2026-09-11 |
| runtime `niome_subnet.__version__` | `3.0.0` |
| `pyproject.toml` project metadata version | `0.1.0` — runtime version과 불일치 |
| spec version | `3000` |
| 분석 시점 | 2026-09-22 UTC |
| local vs upstream | 분석 시점 `git fetch origin main` 후 local `main`과 `origin/main`이 동일. source diff 없음 |
| production 배포가 이 HEAD와 동일한가 | `UNKNOWN` — 공개 repository만으로 실행 중 validator binary/container를 attest할 수 없음 |

최근 scoring/validator 관련 history:

| 날짜 | 변경 |
|---|---|
| 2026-08-10 | 최대 row 수 기준 truncation 도입 |
| 2026-08-11 | `experiment_id` deduplication, 당시 top-5 archive |
| 2026-08-13 | top-10 emission distribution 도입 |
| 2026-08-14 | v3, interval 720, 임시 IP/coldkey filter |
| 2026-08-26 | multiple seeds, IP/coldkey filter 제거 |
| 2026-08-27 | validation block 변경 |
| 2026-09-11 | positive-score 제출물 전부 archive, validation block 500 |

패키지 선언은 Python `>=3.12`, Bittensor `11.0.1`, scikit-learn `>=1.9.0` 등을 요구한다. 코드가 직접 import하는 `pandas`, `numpy`, `requests` 중 일부는 `pyproject.toml`의 direct dependency로 고정되어 있지 않다. 따라서 lockfile/container 없이 환경을 재생성하면 특히 Stage 4와 tie ordering의 bit-exact 결과가 달라질 수 있다.

이 분석 셸의 관찰 환경은 Bittensor 10.5.0, NumPy 2.4.4, pandas 3.0.3, scikit-learn 1.9.0이며 BioPython/boto3가 설치되어 있지 않았다. 이것은 **repository source 차이**가 아니라 **현재 audit shell과 project가 기대하는 runtime의 차이**다. production validator의 실제 installed versions는 `UNKNOWN`이다.

2026-09-22 block 9,124,318에서 이 audit 환경으로 읽은 netuid 55 값은 다음과 같다.

```text
min_allowed_weights = 1
max_weight_limit    = 65535
weights_version     = 1002
weights_rate_limit  = 100
tempo               = 360
immunity_period     = 7200
activity_cutoff     = 5000
registration_allowed = true
subnet_active         = true
```

주의: audit 환경은 Bittensor 10.5.0이고 source requirement는 11.0.1이다. 특히 `max_weight_limit` 표현 단위와 `weights_version` 의미를 package spec version `3000`과 단순 동일시하면 안 된다. 위 값은 관찰 기록이지 deployed validator가 같은 Python API 표현을 받는다는 보장은 아니다.

## 3. Validator Entry Point

실행 entry point는 `neurons/validator.py`의 `Validator`다.

```text
neurons/validator.py::__main__
  └─ with Validator()                       # BaseValidatorNeuron 초기화/state load/W&B
       └─ BaseValidatorNeuron.__enter__
            └─ run_in_background_thread
                 └─ BaseValidatorNeuron.run
                      ├─ sync()
                      ├─ concurrent_forward()
                      │    └─ Validator.forward()
                      │         └─ niome_subnet.validator.forward.forward(self)
                      └─ sync()
```

`Validator.forward()`는 얇은 wrapper이고 실제 phase scheduler는 `niome_subnet/validator/forward.py::forward`다. 기본 `num_concurrent_forwards`는 1이다.

중요한 비직관적 사실:

- base run loop의 `should_set_weights()` branch는 현재 `pass`다.
- 실제 `set_weights()`는 `run_validation()` 마지막에서 즉시 호출된다.
- `WEIGHT_SET_BLOCK = 700`은 phase 700~719에 별도의 weight-setting 동작을 만들지 않는다.
- commit/reveal weight 경로는 사용하지 않고 `bt.set_weights(...)`를 직접 호출한다.

## 4. End-to-End Call Graph

요청에 제시된 일반 call graph를 실제 코드에 맞게 고치면 다음과 같다.

```text
Validator process starts
  ↓
BaseValidatorNeuron.run() / periodic sync
  ↓
forward(): chain block phase = (block - 8,843,300) % 720
  ├─ phase 0..499: broadcast_task()를 asyncio task로 시작
  │    ↓
  │  backend GET current task
  │    ├─ Task(id, contract_url, hbb_ref_url)
  │    ├─ contract.json 다운로드
  │    └─ hbb_reference.json 다운로드
  │    ↓
  │  eligible miner UID 목록 shuffle
  │    ↓
  │  UID별 S3 key `niome/{uid}.json` presigned PUT URL 생성
  │    ↓
  │  POST http://{axon}/forward
  │    body = GenomicsTaskSynapse(task, presigned_url, timeout=20)
  │    auth = bt.http_auth(wallet)
  │    ↓
  │  HTTP status만 확인; response body는 parse하지 않음
  │    ↓
  │  miner가 별도 S3 PUT으로 JSON 업로드
  │
  ├─ phase 500..699: run_validation()을 asyncio task로 시작
  │    ↓
  │  backend task를 다시 fetch하고 local contract/HBB를 덮어씀
  │    ↓
  │  public cell-type map fetch
  │    ↓
  │  현재 miner UID별 `niome/{uid}.json` 다운로드
  │    ↓
  │  benchmark_submission()
  │    ├─ json.load(raw submission)
  │    ├─ parse contract seed list
  │    ├─ run_stage12() once
  │    │    ├─ truncate_submission()
  │    │    ├─ Stage 1 hard row validation
  │    │    └─ Stage 2 feature/base/weighted score
  │    ├─ each seed:
  │    │    ├─ Stage 3 synthetic simulation
  │    │    ├─ Stage 4 per-submission RF cross-validation
  │    │    └─ Stage 5 coverage/diversity × consistency × weighted sum
  │    └─ mean(seed final scores) + averaged breakdown
  │    ↓
  │  backend score POST, truncated submission S3 overwrite
  │    ↓
  │  all positive submissions archive POST
  │    ↓
  │  Validator.set_weights(score DTOs)
  │    ├─ UID score vector
  │    ├─ NaN/Inf sanitize
  │    ├─ positive top-10 fixed rank distribution
  │    ├─ chain process_weights_for_netuid
  │    ├─ 98% miner + 2% owner/burn
  │    ├─ uint16 conversion
  │    └─ bt.set_weights(netuid=55, version_key=3000)
  │
  └─ phase 700..719: scheduler상 아무 작업 없음
```

### 4.1 단계별 주요 argument와 반환값

| 단계 | 함수 | 주요 입력 | 반환/side effect | 다음 데이터 |
|---|---|---|---|---|
| task fetch | `fetch_task(wallet)` | backend URL, signed headers | `Task`; contract/HBB 파일 저장 | synapse + local artifacts |
| miner selection | `get_miner_uids(self)` | metagraph | UID list | shuffled query order |
| query | `query_miner(...)` | axon, `GenomicsTaskSynapse` | 성공 boolean 성격; body 미사용 | miner가 S3 PUT |
| download | boto3 `download_file` | bucket, `niome/{uid}.json` | shared `submission.json` | `benchmark_submission` |
| parse/cap | `truncate_submission` | raw list, max rows | retained list | Stage 1 |
| S1/S2 | `run_stage12` | submission, contract, HBB, chr11, cell types | valid/invalid artifacts, Stage2 rows | S3/S4/S5 |
| S3 | `run_stage3` | valid rows, seed | simulated outcome rows + summary | S4 |
| S4 | `run_stage4` | Stage2 + Stage3, seed | consistency metrics/factor, total weighted | S5 |
| S5 | `run_stage5` | contract, Stage1/2 rows, S4 | final seed score + distribution details | aggregate |
| aggregate | `benchmark_submission` | per-seed Stage5 | seed mean `final_score`, averaged breakdown | score DTO |
| rank | `process_scores_top` | full UID score vector | sparse top-k ratios | chain processing |
| chain set | `bt.set_weights` | uids, uint16 weights, version 3000 | extrinsic result | subnet consensus |

### 4.2 lifecycle/race/freshness 관찰

- `broadcast_task()`와 `run_validation()`은 `asyncio.create_task`로 fire-and-forget된다. boolean flags가 같은 task 중복을 줄이지만 phase 경계에서 broadcast가 끝났음을 await하고 validation을 시작하지는 않는다.
- broadcast는 miner에게 query하기 **전** UID를 `collected_uids`에 넣는다. query 실패도 동일 task ID 동안 재시도되지 않는다.
- `collected_uids`는 task ID가 바뀔 때만 clear된다. backend가 같은 task ID를 여러 720-block cycle 유지하면 다음 cycle broadcast는 기존 UID를 skip하지만 validation은 S3 object를 다시 읽을 수 있다.
- S3 key에는 task ID, nonce, upload timestamp가 없고 validation에서 object freshness를 검사하지 않는다. stale/current submission 구분은 불가능하다.
- validation은 task를 다시 fetch하여 shared contract/HBB 파일을 덮어쓰지만 broadcast의 `self.task_id`와 다시 일치하는지 확인하지 않는다. score DTO에는 이전 `self.task_id`가 들어간다. backend task가 그 사이 바뀌는 경우 mismatch 가능성이 있다.
- UID별 validation 전체가 broad `except Exception: continue` 안에 있고 예외 로그도 없다. malformed submission, missing S3 object, dependency 오류 모두 같은 방식으로 UID를 조용히 누락시킨다.

## 5. Task Schema

### 5.1 miner에게 실제 전달되는 envelope

Pydantic model:

```python
class Task(BaseModel):
    id: str
    contract_url: str
    hbb_ref_url: str

class GenomicsTaskSynapse(bt.Synapse):
    task: Optional[Task] = None
    presigned_url: str = ""
    timeout: Optional[float] = None
```

실제 validator는 세 synapse field를 모두 채운다.

| 필드 | required at runtime | 의미/제약 |
|---|---:|---|
| `task.id` | Yes | task identity. 형식/range 추가 검증 없음 |
| `task.contract_url` | Yes | validator와 miner가 contract JSON을 받을 URL |
| `task.hbb_ref_url` | Yes | HBB mutation/reference JSON URL |
| `presigned_url` | Yes | miner가 결과 JSON을 PUT할 S3 URL; validator 생성 만료 300초 |
| `timeout` | Yes in sender | 값 20초. miner upload deadline 자체를 enforcement하는 field인지 miner 구현에 의존 |

miner가 HTTP POST에 반환해야 하는 scoring payload schema는 없다. HTTP 응답 본문은 무시된다. 제출물은 `presigned_url`에 upload하는 top-level JSON list다.

### 5.2 contract schema — formal model이 아니라 code access로 역추론

contract용 Pydantic/dataclass는 없다. 아래는 실행 코드가 접근하는 key다.

| Contract field | 실행상 필요성 | 사용 |
|---|---:|---|
| `seed` | Required | comma-separated integer seed 문자열; seed별 S3~S5 평가 |
| `active_mutations` | Required | S1 whitelist, S5 support |
| `rules` | Required | validation/scoring config |
| `rules.max_mismatches` | Required | S1 guide-target Hamming limit |
| `rules.base_padding` | Required | proximity, distance decay; 0이면 Stage2 divide-by-zero 가능 |
| `rules.proximity_gate` | Optional, default false | mutation 주변 hard gate |
| `rules.max_experiments` | Optional, default `None` | retained unique-ID row cap |
| `rules.cas_systems` | Optional | S5 support; default `['Cas9','Cas12a']` |
| `cell_type` | Optional/nullable | non-null이면 submission row exact match 요구 |
| `mutation_weights` | Optional | Stage2 row multiplier; missing mutation은 1.0 |
| `mutation_regions` | Optional | Stage3 energy offset; mapping key는 mutation |

HBB reference JSON은 최소한 다음 구조를 필요로 한다.

```text
mutation_map[mutation] -> chromosome coordinate
gene_region.start
gene_region.end
```

### 5.3 공개/변동/비공개 구분

| 항목 | miner에게 공개되는가 | challenge마다 변동 가능 | 현재 값 |
|---|---:|---:|---|
| task ID | Yes | Yes | `UNKNOWN` |
| full contract | Yes, URL 제공 | Yes | `UNKNOWN` |
| HBB reference | Yes, URL 제공 | Yes | `UNKNOWN` |
| seed list | contract 안에 있음 | Yes | `UNKNOWN` |
| active mutations/weights/regions | contract 안에 있음 | Yes | `UNKNOWN` |
| max experiments/mismatches/padding | contract 안에 있음 | Yes | `UNKNOWN` |
| required cell type | contract 안에 있음 | Yes | `UNKNOWN` |
| cell-type accessibility table | synapse에는 없음; public backend endpoint | backend에서 변동 가능 | audit 시 §15 값 |
| chr11 source | code/script 공개 | release/code 변경 시 | Ensembl GRCh38 release 116 |
| validator AWS credential/bucket internals | No | Yes | private |
| 다른 miner submission/score | No | Yes | private |
| 다른 validator weights/stake/state | chain 일부만 공개, live-dependent | Yes | scoring 시점 상태 `UNKNOWN` |

현재 task endpoint는 validator signed headers 없이는 `400 Missing required headers`를 반환하므로 audit 시점의 active contract, dataset cap, active mutation 목록과 seed 수는 `UNKNOWN`이다. 이는 miner가 실제 query를 받았을 때 URL을 통해 확보할 수 없는 정보라는 뜻은 아니다.

Randomization은 task creation backend 내부가 공개되지 않아 `UNKNOWN`이다. repository의 validator는 task를 생성하지 않고 backend에서 가져오기만 한다.

## 6. Miner Response Schema

### 6.1 실제 top-level format

validator는 S3 object를 다음처럼 읽는다.

```python
submission = json.load(file)
```

그 뒤 list type인지 schema validation하지 않고 바로 iteration한다. 따라서 정상 형식은 **JSON array of experiment objects**다. top-level object/dict는 key 문자열을 순회하다 `.get()`에서 예외가 나 UID 전체가 누락될 수 있다.

### 6.2 row fields

| Field | 기대 type | Required | Allowed values/range | 실제 validation | Used by stage |
|---|---|---:|---|---|---|
| `experiment_id` | string | Yes | non-empty, submission 내 unique | `.get`; missing/blank/non-string/duplicate ID row는 truncation에서 drop | pre/S3/S4 merge |
| `guideRNA` | string | Yes | length exactly 20 or 23; 명시적 alphabet check 없음 | direct `[]`, `len`; reference와 Hamming; uppercase `G/C`만 GC로 count | S1,S2,S3,S4,S5 |
| `target_alignment_start` | integer-like | Yes | `0 <= start`, target/PAM slice 가능 | arithmetic/comparison; formal type/range schema 없음 | S1,S2,S3,S4 |
| `target_alignment_end` | integer-like | Yes in valid row | exactly `start + len(guide)` | `.get`; mismatch/missing은 row invalid | S1 |
| `strand` | string | Yes in valid row | `+` or `-` | PAM 함수에서 invalid strand row fail | S1,S3,S5; seed/labels 경유 S4 |
| `mutation` | string | Yes | member of `contract.active_mutations`, HBB map 필요 | direct access + whitelist | S1,S2,S3,S4,S5 |
| `cas_system` | string | Yes | 실행상 `Cas9` or `Cas12a` | other value는 PAM 검사 `invalid_cas` | S1,S2,S3,S5 |
| `cell_type` | string | Conditional | contract `cell_type`과 exact equality | contract value가 non-null일 때만 required/equal | S1; contract accessibility가 S2~S4 |
| extra/derived fields | any | No | whitelist 없음 | **거부하지 않음; scoring에서 무시** | none |

`efficiency`, predicted repair outcome, off-target score 등 miner-supplied derived values를 금지하는 executable check는 없다. 그 값은 계산에 사용되지 않지만 원본 experiment object에 남아 valid artifacts/archive에 포함될 수 있다. README의 “strictly prohibited”는 현재 parser가 강제하는 rule이 아니다.

### 6.3 missing/malformed 처리의 정확한 차이

| 문제 | 실제 결과 |
|---|---|
| missing/blank/duplicate `experiment_id` | 그 row를 cap 전처리에서 drop |
| missing/wrong `target_alignment_end` | 정상적으로 Stage1 invalid row |
| missing/invalid `strand` | 정상적으로 Stage1 invalid row |
| contract cell type이 있는데 row missing/mismatch | 정상적으로 Stage1 invalid row |
| missing `guideRNA`, `mutation`, `cas_system`, `target_alignment_start` | direct access/type 연산 예외 가능; 전체 UID validation skip |
| top-level dict/string/null | type에 따라 iteration 또는 `.get` 예외; 전체 UID skip |
| invalid JSON/missing S3 object | 전체 UID skip |
| duplicate biological design `(cas,start,strand,guide)` | 첫 valid design 유지, 뒤 row Stage1 invalid |
| cap 초과 | unique valid-ID 기준 앞쪽 retained rows까지만 평가; 뒤 rows drop |
| extra field | 무시; reject 없음 |

“전체 UID skip”은 명시적으로 score=0 DTO를 POST한다는 뜻이 아니다. exception 때문에 score list에 UID가 추가되지 않고, 이후 full UID vector의 기본 0이 그대로 남는다는 뜻이다.

## 7. Stage 1 Analysis

Stage 1은 `run_stage12()` 내부에서 truncation 후 row별 `stage1()`을 호출한다. 정상 rule failure는 `(0.0, reason)`으로 반환되어 invalid artifact에 `{stage1_pass: false, reason: ...}`로 기록되고 그 row만 Stage 2에서 제외된다.

### 7.1 실행 순서와 rule matrix

| Rule | Condition | Pass | Fail / penalty | Hard reject scope | 위치 |
|---|---|---|---|---|---|
| experiment ID prefilter | cap 전 모든 row | nonblank string, 처음 나온 ID | row drop; invalid artifact에도 안 들어감 | row | `truncate_submission` |
| dataset cap | `max_experiments` non-null | 앞에서부터 cap개 retained | 이후 row drop | row suffix | `truncate_submission` |
| mutation whitelist | every retained row | `mutation in active_mutations` | `mutation_not_allowed` | row | `stage1` |
| cell type | contract cell type non-null | row exact equality | `cell_type_mismatch` | row | same |
| guide length | every row | 20 or 23 | `invalid_length` | row | same |
| alignment end | every row | `end == start + guide length` | `invalid_alignment_end` | row | same |
| chromosome bounds | every row | `start >= 0` and `start + L < len(chr11)` | `out_of_bounds` | row | same/check_pam |
| Cas9 PAM | `cas_system == Cas9` | plus: downstream `NGG`; minus: RC(upstream 3bp) `NGG` | wrong motif reason은 구현상 `pam_ok`; boundary는 `pam_out_of_bounds` | row | `check_pam` |
| Cas12a PAM | `cas_system == Cas12a` | plus: upstream `TTTN`; minus: RC(downstream 4bp) `TTTN` | wrong motif reason은 구현상 `pam_ok`; boundary는 `pam_out_of_bounds` | row | `check_pam` |
| Cas system | every row | Cas9/Cas12a path | `pam_invalid_cas` | row | `check_pam` |
| strand | every row | `+` or `-` | `pam_invalid_strand` | row | `check_pam` |
| target sequence | every row | chromosome slice exists | bound/PAM fail | row | same |
| guide-target mismatch | plus/minus | Hamming ≤ `max_mismatches` | `too_many_mismatches` | row | `stage1` |
| proximity gate | `proximity_gate=true` | `abs(start-mut_pos) <= base_padding` | `mutation_too_far` | row | same |
| biological duplicate | only after earlier S1 pass | tuple not seen | later row `duplicate_experiment` | row | `run_stage12` |

### 7.2 PAM/strand와 alignment의 exact semantics

Let `L = len(guideRNA)` and `S = target_alignment_start`.

```text
Cas9, strand +:
    PAM = chr11[S+L : S+L+3]
    pass iff len(PAM)=3 and PAM[1:] == "GG"     # NGG

Cas9, strand -:
    require S >= 3
    PAM = reverse_complement(chr11[S-3 : S])
    pass iff len(PAM)=3 and PAM[1:] == "GG"

Cas12a, strand +:
    require S >= 4
    PAM = chr11[S-4 : S]
    pass iff len(PAM)=4 and PAM[:3] == "TTT"    # TTTN

Cas12a, strand -:
    PAM = reverse_complement(chr11[S+L : S+L+4])
    pass iff len(PAM)=4 and PAM[:3] == "TTT"
```

plus strand mismatch는 `hamming(guide, chromosome target)`이고 minus strand mismatch는 `hamming(reverse_complement(guide), chromosome target)`이다.

PAM helper는 motif가 틀린 경우에도 status 문자열 자체는 `"ok"`를 반환하고 boolean만 false로 준다. caller가 `f"pam_{pam_status}"`를 만들기 때문에 invalid motif의 persisted reason이 역설적으로 **`pam_ok`**다. clone에서 reason까지 비교한다면 이 버그성 문자열도 그대로 복제해야 한다.

Boundary check가 `S + L >= len(sequence)`를 reject하므로 guide가 염색체 마지막 base에서 정확히 끝나는 경우도 reject된다. 이는 Python slice 가능 여부보다 엄격한 off-by-one 동작이며 clone에서 그대로 보존해야 한다.

### 7.3 명시적으로 존재하지 않는 validation

- DNA alphabet `[ACGT]` check가 없다.
- lowercase normalization이 없다. lowercase guide는 reference mismatch로 세고 GC numerator에도 포함되지 않는다.
- integer/coercion schema가 없다.
- field whitelist가 없다.
- submission 전체 최소 row 수 rule이 Stage 1에는 없다.
- Stage 1 자체의 scalar score/감점은 없다. valid/invalid filter일 뿐이다.
- duplicate `experiment_id`와 duplicate biological design은 서로 다른 rule이다.

비 DNA 문자가 실제로 pass할 수 있는지는 contract의 mismatch limit과 reference 일치에 달렸다. 그러나 “alphabet 때문에” reject하는 코드는 확실히 없다.

### 7.4 duplicate/order/cap semantics

1. raw row 순서대로 `experiment_id` prefilter를 한다.
2. 유효 ID를 가진 첫 row가 ID를 선점한다. 동일 ID 후속 row는 내용이 더 좋아도 drop된다.
3. retained row가 `max_experiments`에 도달하면 즉시 stop한다. 이후 Stage1에서 invalid로 판명될 row도 cap을 소비한다.
4. Stage1 pass 후 `(cas_system, target_alignment_start, strand, guideRNA)` tuple을 다시 dedup한다. 이때도 먼저 나온 valid row가 이긴다.

이 biological duplicate key에는 `mutation`, `cell_type`, `experiment_id`가 없다. 따라서 mutation만 다른 두 row라도 Cas/start/strand/guide가 같으면 뒤 row가 duplicate다. ID 비교는 원문 문자열 기준이라 대소문자와 바깥 공백을 canonicalize하지 않는다. 공백뿐인 ID는 drop하지만 `"x"`와 `" x "`는 서로 다른 ID다.

따라서 row order는 평가 대상 집합 자체를 바꿀 수 있다.

## 8. Stage 2 Analysis

Stage 2는 “statistical plausibility” hard threshold가 아니라 각 valid row에 feature와 score를 붙인다.

### 8.1 feature와 공식

Guide length를 `L`, uppercase GC 개수를 `nGC`, mutation coordinate를 `M`, target start를 `S`, `P = base_padding`이라 두면:

```text
gc = nGC / L
distance = abs(S - M)

gc_score   = max(0, 1 - 2 * abs(gc - 0.5))
dist_score = exp(-distance / P)
consistency = 1.0 if distance < P else 0.3

base_score = 0.625 * gc_score + 0.375 * dist_score
```

`consistency`는 Stage 2의 `base_score`에는 직접 곱하지 않는다. Stage 4 feature로 들어간다.

### 8.2 off-target uniqueness

validator는 HBB gene region에 좌우 50,000bp를 더한 chr11 slice에서 k-mer index를 만든다.

```text
Cas9 seed    = guideRNA[-12:]
Cas12a seed  = guideRNA[:12]
hits         = len(kmer_index.get(seed, []))

offtarget_factor =
    1.0   if hits == 0
    0.7   if hits <= 5
    0.4   if hits <= 20
    0.1   otherwise
```

index loop는 `range(len(seq)-k)`라 마지막 가능한 k-mer 시작점을 포함하지 않는다. exact clone은 이 off-by-one도 복제해야 한다.

### 8.3 row score

```text
structural_score = base_score * offtarget_factor
mutation_weight  = contract.mutation_weights.get(mutation, 1.0)
weighted_score   = structural_score * mutation_weight
accessibility    = cell_type_map.get(contract.cell_type, 1.0)
region_offset    = contract.mutation_regions[mutation] 기반 고정 mapping/default
```

region offset literal table:

```text
5UTR_or_upstream  +0.05
exon1/exon2/exon3 +0.03
intron1/intron2   -0.03
3UTR              +0.02
missing/other      0.00
```

정확한 region-to-offset mapping은 §22의 Stage2 함수에 있으며 clone에서는 literal table을 옮겨야 한다. `accessibility`, `consistency`, `mutation_weight`, `region_offset`은 이후 Stage 3/4에 feature 또는 outcome 생성 input으로 사용된다. 최종 base mass는 모든 row의 `weighted_score` 합이다.

### 8.4 threshold와 failure

- Stage 2에는 row reject threshold가 없다.
- score clipping은 `gc_score`의 lower bound와 off-target bucket 외에는 없다.
- 정상적인 contract라면 structural score는 `[0,1]`이다.
- mutation weight의 schema/range check가 없으므로 contract가 음수/NaN을 주면 downstream 결과가 비정상일 수 있다. 실제 contract 값은 `UNKNOWN`이다.
- `base_padding == 0`이면 distance decay에서 divide-by-zero가 발생할 수 있고 UID 전체가 skip될 수 있다.
- cell-type accessibility는 submission field가 아니라 **contract의 cell type**으로 조회한다. row cell type은 Stage 1 equality gate를 통과시키는 역할만 한다.

## 9. Stage 3 Analysis

Stage 3은 valid experiment마다 각 contract seed에서 cut/repair/indel synthetic outcome을 하나 생성한다. 외부 모델, checkpoint, GPU, NumPy RNG를 쓰지 않는다. Python `random.Random`의 private instance를 row마다 만든다.

### 9.1 row seed

```text
payload = "|".join([
    round_seed,
    mutation,
    cas_system,
    guideRNA,
    target_alignment_start,
    strand,
])

experiment_seed = int(SHA256(payload).hexdigest(), 16) mod 2^32
rng = random.Random(experiment_seed)
```

따라서 같은 contract seed와 동일 row fields이면 다른 submission 안에서도 그 row의 simulation은 같다. `experiment_id`, row index, cell type은 seed payload에 없다. 단 cell type은 아래 energy를 통해 확률에 간접 영향한다.

### 9.2 energy와 microhomology

Stage 2의 `gc`, `distance`, `accessibility`, mutation region offset을 사용한다.

```text
energy = clamp(
    accessibility * (
        1.8 * gc
        + 0.6 * exp(-distance / 1500)
        + region_offset
    ),
    0,
    1,
)

microhomology_probability = min(0.6, gc * (1-gc) * 2.2)
has_microhomology = rng.random() < microhomology_probability
```

### 9.3 cut probability

```text
base_cut = 0.86 if Cas9 else 0.78
cut_probability = clamp(base_cut + 0.18 * energy, 0.4, 0.99)
draw = rng.random()
is_cut = not (draw > cut_probability)  # 즉 draw <= probability
```

cut이 없으면 repair outcome은 `no_cut`, indel length는 0이다.

### 9.4 repair와 indel

cut인 경우:

```text
HDR weight      = (0.32 if Cas9 else 0.24) + 0.35 * energy
MH_NHEJ weight  = 0.30 if has_microhomology else 0.12
BLUNT_NHEJ      = 0.35

u = rng.random() * (HDR + MH_NHEJ + BLUNT_NHEJ)
```

누적 구간으로 `HDR`, `MH_NHEJ`, `BLUNT_NHEJ` 중 하나를 선택한다.

```text
HDR indel          = 0
MH_NHEJ indel      = max(1, int(rng.gammavariate(2.2, 2.8)))
BLUNT_NHEJ indel   = max(1, int(rng.expovariate(0.6)))
```

Stage 3 summary의 cut rate, HDR rate, mean indel은 진단 출력이다. 최종식에 summary scalar가 직접 들어가지 않고 per-row labels가 Stage 4로 들어간다.

### 9.5 모델/외부 의존성 판정

| 질문 | 실제 답 |
|---|---|
| library/model | Python standard-library `random`, `math`, handwritten simulator |
| architecture/name | 없음 |
| pretrained weights/checkpoint | 없음 |
| model version | repository commit으로만 버전 관리 |
| device | CPU; GPU 사용 없음 |
| random seed | SHA-256 derived 32-bit seed, 완전 제어 |
| external data | Stage2에서 이미 계산된 contract/reference/accessibility-derived features |
| Monte Carlo 반복 | seed당 row당 outcome 1개; contract seed 수만큼 반복 |

README가 실제 biological predictor 이름을 언급하더라도 이 실행 경로에는 그 모델 import/load/inference가 없다.

## 10. Stage 4 Analysis

Stage 4의 목적은 Stage 2 feature로 Stage 3 synthetic labels를 cross-validated Random Forest가 얼마나 일관되게 예측할 수 있는지 측정하는 것이다. pretrained model quality를 평가하는 단계가 아니다.

### 10.1 merge와 입력

Stage 2와 Stage 3 결과를 `experiment_id`로 inner merge한다. truncation에서 ID는 unique여야 하므로 정상 입력은 1:1이다.

Feature matrix:

```text
X = [
    gc,
    distance,
    gc_score,
    dist_score,
    consistency,
    energy,
    has_microhomology,
]
```

Regression targets 세 개:

```text
y_cut   = is_cut
y_hdr   = is_hdr
y_indel = indel_length
```

Sample weight는 Stage 2의 `mutation_weight`다.

### 10.2 model과 CV

각 target에 별도 모델을 매 fold 새로 fit한다.

```text
RandomForestRegressor(
    n_estimators=200,
    random_state=42,
    max_depth=12,
    # 나머지는 sklearn defaults
)

k = min(5, number_of_rows)
KFold(n_splits=max(k, 2), shuffle=True, random_state=round_seed)
```

명시적 `n_jobs`와 device 설정은 없다. 모델 weights/checkpoint도 없다.

### 10.3 target별 metric

out-of-fold true/predicted 값으로 target마다:

```text
r2 = r2_score(y_true, y_pred)
mae = mean_absolute_error(y_true, y_pred)

normalized_mae =
    mae / std(y_true), if std(y_true) >= 1e-9
    mae,               otherwise
```

세 target 평균:

```text
avg_r2   = mean(r2_cut, r2_hdr, r2_indel)
avg_nmae = mean(nmae_cut, nmae_hdr, nmae_indel)

consistency_score = (
    0.7 * max(avg_r2, 0)
    + 0.3 * (1 - avg_nmae)
) * 100

consistency_factor = clamp(consistency_score / 100, 0, 1)
```

Stage 4는 함께 `total_weighted_score = sum(Stage2 weighted_score)`를 계산하고 `final_reward = total_weighted_score * consistency_factor`도 반환한다. Stage 5는 이 값을 그대로 읽기보다 동등한 구성요소로 최종값을 다시 계산한다.

### 10.4 row-count cliff와 NaN

- merged row 0 또는 1이면 guard가 동작하여 consistency를 0으로 반환한다.
- 이 guard는 1 valid row의 실제 Stage2 합도 breakdown에서 보존하지 않고 `total_weighted_score=0`으로 반환한다.
- 2~5 rows이면 `n_splits=n`, 모든 test fold 크기가 1이다.
- 6~9 rows이면 5-fold 중 적어도 하나의 test fold 크기가 1이다.
- sklearn의 R²는 sample 1개에서 `NaN`이다. 그 NaN이 평균에 전파되고 Python `max(NaN, 0)`도 NaN이다.
- 최종 `consistency_factor`는 NaN이면 0으로 처리된다.
- 따라서 현재 구현과 통상 sklearn 1.9 동작에서는 **merged valid rows < 10이면 consistency factor가 0**이다.

10 rows부터 각 5-fold test set이 최소 2개다. 다만 label 분포와 예측 성능에 따라 finite consistency가 0으로 clamp될 수 있으므로 “10개면 양수 보장”은 아니다.

### 10.5 환경 의존성

RF와 KFold 모두 seed가 고정되어 논리적으로 deterministic하다. 그러나 exact floating result, tree split tie, sklearn defaults/implementation은 Python/scikit-learn/NumPy 버전에 영향을 받을 수 있다. source는 sklearn `>=1.9.0`일 뿐 exact patch와 NumPy/pandas를 pin하지 않는다. 따라서 Stage 4 replication은 B다.

## 11. Stage 5 Analysis

Stage 5는 제출물의 coverage/diversity를 측정한다. 이름이나 README와 달리 synthetic dataset과 별도의 real/reference dataset 사이의 distance를 계산하지 않는다.

### 11.1 support와 count

```text
mutation support = contract.active_mutations
Cas support      = contract.rules.cas_systems
                   or [Cas9, Cas12a]
strand support   = [+, -]
joint support    = Cartesian product(mutation, Cas, strand)
```

Stage1-valid rows에서 각 support category count를 만든다.

### 11.2 normalized entropy ratios

support `C`에 대해:

```text
H(C) = -sum(p_i * log2(p_i)) for nonzero support counts
ratio(C) = H(C) / log2(|C|), when |C| > 1
ratio(C) = 1,                when |C| <= 1
```

다음 네 ratio를 계산한다.

- mutation entropy ratio
- Cas entropy ratio
- strand entropy ratio
- mutation×Cas×strand joint entropy ratio

### 11.3 sequence diversity

각 guide의 모든 overlapping 12-mer를 모은다.

```text
kmer_entropy_ratio = H(observed 12-mer counts) / log2(total 12-mer occurrences)
distinct_guide_ratio = number of unique guide strings / number of valid rows
```

분모가 성립하지 않는 edge case는 helper의 fallback을 따른다.

### 11.4 distribution fidelity

여섯 component의 geometric mean이다.

```text
components = [
    mutation_entropy_ratio,
    cas_entropy_ratio,
    strand_entropy_ratio,
    joint_entropy_ratio,
    kmer_entropy_ratio,
    distinct_guide_ratio,
]

distribution_fidelity = exp(
    mean(log(max(component, 1e-9)))
)
distribution_factor = clamp(distribution_fidelity, 0, 1)
```

한 component가 0이어도 전체 factor가 정확히 0이 되지는 않는다. 계산 전에 `1e-9`로 올리기 때문이다. 다만 sixth-root 규모로 크게 감소한다.

### 11.5 CAS shift diagnostics

Cas별 valid row가 각각 5개 이상이면 Cas9/Cas12a 사이의 일부 feature에 Jensen-Shannon divergence와 Wasserstein distance를 계산한다. 이 값은 diagnostics이며 `distribution_fidelity` 또는 final score에 들어가지 않는다.

### 11.6 reference dataset, threshold, randomness

| 항목 | 실제 코드 |
|---|---|
| real/reference dataset | 없음 |
| statistical distance to real data | 없음 |
| sample size hard threshold | 없음; 0 row는 distribution 0 |
| random sampling | 없음 |
| seed dependence | fidelity 자체는 없음; seed loop마다 같은 값을 재계산 |
| hard fail | 없음; multiplicative factor로만 작동 |

## 12. Final Score Formula

### 12.1 정확한 구조

Stage 1을 통과한 row 집합을 `V`, contract seed 목록을 `R`이라고 하자.

```text
base_mass = Σ[e in V] Stage2.weighted_score(e)

for seed r in R:
    outcomes_r = Stage3(V, r)
    consistency_r = Stage4(V, outcomes_r, r).consistency_factor
    diversity = Stage5_distribution(V, contract)  # seed와 무관

    score_r = base_mass * consistency_r * diversity

final_biological_score = mean(score_r for r in R)
```

동등하게:

```text
final = base_mass × diversity × mean(consistency_factor_r)
```

현재 code path에는 `w1*S1 + ... + w5*S5` 형태의 additive stage weights가 없다.

### 12.2 benchmark pseudocode

```python
def benchmark_submission(contract, submission, hbb, chr11, celltypes):
    seeds = [int(x.strip()) for x in contract["seed"].split(",")]

    valid_rows, stage2_rows, invalid_rows = run_stage12(
        submission, contract, hbb, chr11, celltypes
    )

    per_seed = []
    for seed in seeds:
        stage3 = simulate(valid_rows, stage2_rows, seed)
        stage4 = fit_cv_and_measure(stage2_rows, stage3, seed)
        stage5 = score_distribution_and_final(
            contract, valid_rows, stage2_rows, stage4
        )
        per_seed.append(stage5.final_score)

    return mean(per_seed), mean_each_breakdown_field(per_seed)
```

### 12.3 clipping, threshold, invalid, NaN

| 항목 | 처리 |
|---|---|
| Stage1 invalid row | final 합에서 제거 |
| all invalid/no valid rows | base 0, downstream 0 |
| fewer than 10 merged rows | 구현상 Stage4 factor 0 가능성이 아니라 정상 sklearn에서 0으로 귀결 |
| Stage4 consistency | factor `[0,1]` clamp; NaN이면 0 |
| Stage5 fidelity | 각 component floor `1e-9`, 최종 `[0,1]` clamp |
| Stage2 total | 명시적 upper clamp 없음; row 수와 mutation weights에 따라 증가 |
| final biological score | 명시적 global floor/ceiling 없음 |
| empty seed list | mean에서 divide-by-zero; UID 전체 exception skip |
| per-UID exception | score DTO 미생성, weight vector default 0 |
| weight-vector NaN/Inf | `np.nan_to_num`; NaN→0, infinities→dtype finite extrema |
| rank eligibility | score strictly `> 0`만 positive |

Stage2가 row 합이므로 dataset cap 안에서는 valid row 수 증가가 base mass를 선형으로 키울 수 있지만, Stage4/5 factor와 contract cap이 함께 작용한다.

## 13. Score → Reward → Weight Pipeline

### 13.1 score vector

`Validator.set_weights(miner_scores)`는 metagraph 크기의 float32 zero vector를 만들고 성공적으로 benchmark된 DTO의 UID 위치에 biological score를 쓴다.

```text
scores = zeros(n_uids, float32)
scores[result.uid] = result.score
scores = nan_to_num(scores)
```

실패/timeout/missing submission/exception UID는 결과적으로 0이다. explicit blacklist는 이 경로에 없다. query할 miner 목록은 metagraph eligibility helper가 결정한다.

### 13.2 top-10 rank transform

`process_scores_top`:

```text
nominal ratios = [
    0.30, 0.20, 0.20, 0.15, 0.05,
    0.03, 0.025, 0.02, 0.015, 0.01
]

positive_count = count(scores > 0)
k = min(10, positive_count)
ranked = argsort(scores descending)[:k]
allocated_ratios = nominal[:k] / sum(nominal[:k])
all other UID weights = 0
```

상위 두 등수는 20%로 동률이다. positive miner가 10명 미만이면 prefix만 100%로 재정규화한다. biological score 간 절대 간격은 배정 비율에 반영되지 않는다. `1.0001`이 `1.0`보다 한 등수 위면 고정 rank share 차이를 얻는다.

동점 tie-break는 `np.argsort` default sort에 의존하며 stable sort를 요청하지 않는다. exact tie에서 UID-order 안정성은 보장되지 않는다.

### 13.3 chain constraints와 owner/burn

top weights는 Bittensor `process_weights_for_netuid`에 들어가 current chain constraints에 맞게 정규화/clip된다. 이 함수는 runtime의 `min_allowed_weights`, `max_weight_limit`와 metagraph를 읽는다.

그 뒤:

```text
miner_part = processed_weights * 0.98
owner_part[OWNER_HOTKEY_UID] += 0.02
final = normalize(miner_part + owner_part)
uint16 = round(final * 65535), zeros removed
bt.set_weights(netuid=55, uids=..., weights=..., version_key=3000)
```

owner UID가 이미 miner processed weight를 가진 경우 2%가 추가된다. positive score가 하나도 없으면 code는 owner one-hot fallback을 만들므로 최종도 사실상 owner 100%가 된다.

`BURNING_RATE`라는 변수명에도 불구하고 실행상 지정 owner hotkey UID로 2% weight를 준다. 이 weight가 protocol/owner 쪽에서 어떻게 경제적으로 burn되는지는 이 repository만으로는 `UNKNOWN`이다.

### 13.4 없는 것

- score EMA 없음
- historical score smoothing 없음
- 이전 round score carry-over 없음
- proportional score normalization 없음
- commit/reveal weights 없음
- Stage별 reward pool 없음
- miner별 latency bonus 없음

persist되는 것은 emitted UID/weight, collected UID/task ID 같은 validator state이며 biological score history를 다음 round에 혼합하지 않는다.

### 13.5 biological score와 실제 emission의 차이

이 validator 관점에서 biological score는 positive 여부, top-10 진입, rank를 결정한다. 그러나 실제 subnet emission은 여러 validator의 weight, validator stake, chain consensus/Yuma 처리로 결정된다. 공개 repository 하나로 다음은 계산할 수 없다.

- 모든 live validator가 같은 commit/artifact로 평가했는지
- 각 validator의 task timing/S3 object가 같았는지
- validator별 stake와 weight consensus
- chain-side 최종 incentive/dividend

따라서 local biological score를 정확히 복제해도 실제 emission 금액의 exact 예측 등급은 D다.

## 14. Determinism / Randomness

| Component | Deterministic? | Random/time/external source | Seed controlled? | Local reproducibility |
|---|---|---|---:|---|
| task fetch | No across time | backend current task, network | No | C; captured response면 A |
| miner query order | No | `np.random.shuffle` without seed | No | query timing만 영향; stale/deadline 경유 score에 간접 영향 |
| S3 object selected | No guarantee | UID-only key, upload timing/staleness | No | C/D unless object captured |
| JSON/truncation | Yes | row order is input | N/A | A |
| Stage 1 | Yes | contract/HBB/chr11 artifacts | N/A | A after capture |
| Stage 2 | Yes | contract, public celltype endpoint, chr11 | N/A | A after capture |
| Stage 3 | Yes | Python PRNG | Yes, per-row SHA seed | A with Python behavior pinned |
| Stage 4 | Logically yes | KFold seed + RF seed; library numerics | Yes | B |
| Stage 5 | Yes | no RNG | N/A | A |
| seed aggregation | Yes | contract seed list/order | Yes | A |
| top-10 ordering | Mostly | float32 cast, exact ties, NumPy sort | No tie key | B |
| chain weight processing | Time-dependent | hyperparameters/metagraph | No | B/C with block snapshot |
| actual emission | No locally | all validators/stake/Yuma/chain | No | D |

동일 submission을 두 번 평가해 값이 달라질 수 있는 원인:

1. 두 평가 사이 contract/HBB/celltype endpoint가 바뀜.
2. validation 전에 S3 UID key가 다른 upload로 overwrite되거나 stale object가 선택됨.
3. broadcast와 validation의 비동기 timing, presigned URL 만료, query order.
4. dependency version 차이로 Stage4 RF/numeric 결과 또는 rank tie order가 바뀜.
5. chain block의 metagraph/hyperparameter가 바뀜.

같은 bytes의 artifacts, submission, dependency image를 고정하면 Stage1~5 biological score에는 wall-clock time, GPU nondeterminism, unseeded NumPy random, external API inference가 없다.

## 15. External Models and Data

| Resource | Source | Scoring role | Version/freshness |
|---|---|---|---|
| GRCh38 chromosome 11 | Ensembl FTP | PAM, target, mismatch, local k-mer index | setup script가 release 116 URL 사용 |
| contract JSON | NIOME backend task URL | 거의 모든 rule/seed/weights | current content `UNKNOWN` |
| HBB reference JSON | NIOME backend task URL | mutation coordinate, gene region | current content `UNKNOWN` |
| cell-type table | backend `/celltypes` | Stage2 accessibility → Stage3 energy | live external data |
| miner JSON | AWS S3 presigned upload | evaluated submission | UID key, task freshness 검증 없음 |
| score/archive API | NIOME backend | result 기록/positive submission archive | scoring 산술에는 feedback 없음 |
| scikit-learn | local package | Stage4 RF/CV/metrics | declared `>=1.9.0`, exact prod version `UNKNOWN` |
| W&B | optional validator telemetry | scoring에 사용 안 함 | irrelevant to score |

2026-09-22 public cell-type endpoint에서 관찰한 값:

| cell type | accessibility |
|---|---:|
| `CD34+_HSPC` | 0.87 |
| `HEK293` | 0.35 |
| `HUDEP-2` | 0.82 |
| `K562` | 0.77 |

이 값은 분석 시점 snapshot일 뿐 future challenge에 고정된 상수가 아니다. clone artifact bundle에 응답 bytes와 hash를 저장해야 한다.

외부 biological ML model/checkpoint는 **없다**. repository 밖에서 다운로드하는 모델 weight도 없다.

## 16. Local Replication Matrix

등급: A=거의 exact, B=공개이나 환경에 따른 소차 가능, C=일부 artifact/정보 필요, D=private/live state 때문에 직접 복제 불가.

| Component | Grade | 이유/조건 |
|---|:---:|---|
| synapse/task envelope | A | Pydantic fields와 sender 코드 공개 |
| 현재 active task 사전 취득 | C | signed backend 또는 miner query가 필요; 받은 뒤 bytes capture하면 A |
| submission parsing/truncation | A | pure local code; quirks 포함 복제 가능 |
| Stage 1 | A | contract/HBB/chr11 bytes를 확보하면 deterministic |
| Stage 2 | A | 위 artifacts와 celltype snapshot 필요 |
| Stage 3 | A | formula와 seed derivation 전부 공개 |
| Stage 4 | B | 전부 공개지만 exact library/runtime pin이 필요 |
| Stage 5 | A | pure deterministic code |
| multi-seed final aggregation | A | seed list와 intermediate를 확보하면 exact |
| validator top-10 transform | B | float32/tie/NumPy 및 live UID universe 영향 |
| chain constraint processing | B/C | exact Bittensor version + block snapshot 필요 |
| network-wide reward/emission | D | 다른 validator weight/stake/consensus state 필요 |
| production deployment equivalence | D | public HEAD와 live process 동일성 attestation 없음 |

## 17. Miner-Controlled Variables

표의 `D`는 direct, `I`는 indirect, `—`는 실행상 영향 없음이다.

| Miner-controlled variable | S1 | S2 | S3 | S4 | S5 | Final score sensitivity |
|---|:---:|:---:|:---:|:---:|:---:|---|
| `experiment_id` | D | — | — | D | — | High: dedup/merge 성공 여부; Stage3 seed에는 없음 |
| `guideRNA` | D | D | D | I | D | Very high: validity, GC, off-target, RNG seed, labels, sequence diversity |
| `target_alignment_start` | D | D | D | I | — | Very high: PAM/target/proximity, distance, RNG seed/energy |
| `target_alignment_end` | D | — | — | — | — | Binary: exact equality gate |
| `strand` | D | — | D | I | D | High: PAM/alignment, RNG seed, coverage |
| `mutation` | D | D | D | D/I | D | Very high: whitelist, distance/weight/region, sample weight, coverage |
| `cas_system` | D | D | D | I | D | Very high: PAM, off-target seed orientation, cut/repair, coverage |
| `cell_type` | D | — | — | — | — | Binary match only; accessibility는 contract 값을 사용 |
| row order | D | I | I | D | I | High near cap/duplicates; CV split mapping도 바꿈 |
| number of rows | D | D | D | D | D | Very high: sum scale, <10 cliff, entropy/diversity |
| dataset composition | I | D | D | D | D | Very high across mass, predictability, diversity |
| extra/derived fields | — | — | — | — | — | None in current scoring code |
| upload timing/content at UID key | — | — | — | — | — | Operationally binary: 어떤 object가 평가되는지 결정 |

`cell_type` row field가 S2 이후 `—`인 것은 contract 값과 같아야 S1을 통과하고, 실제 accessibility lookup은 row 값이 아니라 contract 값을 사용하기 때문이다.

## 18. Optimization Surface

이 절은 exploit이나 production miner 변경안이 아니라, 현재 함수가 무엇에 민감한지 정리한 것이다.

### 18.1 목적함수의 세 축

현재 점수는 다음 세 축의 곱이다.

```text
quantity/quality mass        predictability                   coverage/diversity
Σ weighted structural   ×   mean seed consistency factor  ×  geometric fidelity
```

한 축이 매우 낮으면 다른 두 축의 개선 효과가 곱셈으로 억제된다. 따라서 단일 guide의 structural score만 최대화하는 문제도, 무조건 row 수만 채우는 문제도 아니다.

### 18.2 가장 큰 구조적 민감도

1. **Validity와 crash safety**: 정상 invalid row는 제외될 뿐이지만 schema/type exception은 UID 전체를 0 input으로 만들 수 있다. 가장 먼저 제거해야 하는 변동원이다.
2. **10 valid-row cliff**: Stage4 구현 때문에 10개 미만의 merged valid experiment는 consistency 0으로 귀결된다.
3. **Cap/order**: cap은 Stage1 전에 소비된다. cap 앞쪽의 invalid row와 duplicate ID가 valid capacity를 낭비할 수 있다.
4. **Base mass**: valid row의 structural × mutation weight 합이므로 row별 quality와 valid count가 직접 중요하다.
5. **Predictability**: Stage3 label은 guide/start/mutation/Cas/strand로 결정되는 seed RNG와 energy의 함수이고, Stage4는 제출물 내부 feature→label learnability를 본다. 단순 평균 biological plausibility가 아니다.
6. **Coverage**: active mutation, Cas, strand, joint support를 균등하게 덮고 guide/12-mer가 다양할수록 fidelity가 상승하는 구조다.
7. **Rank objective**: downstream이 fixed top-10 distribution이므로 충분히 positive인 뒤에는 절대 score 개선의 경제적 가치는 경쟁자의 score와 rank boundary에 의해 불연속적으로 결정된다.

### 18.3 상충 관계

| 선택 | 좋아질 수 있는 항목 | 나빠질 수 있는 항목 |
|---|---|---|
| 동일한 높은-score guide/design 반복 | base mass | duplicate design reject, distinct guide, k-mer entropy |
| 특정 고중량 mutation 집중 | weighted mass | mutation/joint entropy |
| 한 Cas 집중 | Stage2/3의 유리한 일부 수치 | Cas/joint entropy |
| 많은 row 추가 | base mass, CV sample size | cap 소비, 낮은-quality row, coverage 구성, compute time |
| feature/outcome을 획일화 | 일부 예측 안정성 | target variance/normalized MAE와 diversity; CV 결과는 별도 확인 필요 |
| seed 하나에 맞춘 구성 | 해당 seed consistency | 최종은 모든 contract seed 평균 |

이 trade-off의 최적점은 current contract, reference, seed list, cap을 확보하지 않고 숫자로 답할 수 없다. 해당 값은 audit 시 `UNKNOWN`이다.

### 18.4 README와 executable code의 주요 불일치

| README/명명에서 유도될 수 있는 해석 | 실제 실행 코드 |
|---|---|
| derived values는 제출하면 strict reject | field whitelist/reject 없음; 무시됨 |
| DNA alphabet이 명시적으로 검사됨 | alphabet check 없음 |
| Stage3가 Azimuth/Rule3/inDelphi/FORECasT 계열 모델 | handwritten probability simulator |
| Stage4가 pretrained biological ML inference | submission마다 RF를 새로 CV fit |
| Stage5가 synthetic와 real CRISPR dataset을 비교 | 제출물 내부 entropy/diversity만 측정 |
| miner response를 받아 parse | HTTP body 미사용; 별도 S3 object를 나중에 parse |
| weight-set phase에서 weight setting | validation task 마지막에 즉시 set; phase 700 branch는 없음 |
| 같은 challenge가 평가됨 | 재-fetch는 있으나 task ID consistency/freshness validation 없음 |

## 19. Unknown / Private Components

아래는 코드로 확인할 수 없어 의도적으로 추정하지 않았다.

### 19.1 `UNKNOWN`

- 2026-09-22 active task ID와 contract/HBB bytes
- current `active_mutations`, mutation weights/regions, max experiments, max mismatches, padding, proximity gate, required cell type, seed 목록/개수
- backend의 task 생성 알고리즘, task rotation cadence, randomization 방식
- production validators가 실제로 public HEAD와 동일한 commit/container를 실행하는지
- production Python/Bittensor/NumPy/pandas/sklearn/BioPython 정확 버전
- production chr11 file의 실제 byte hash와 k-mer cache provenance
- S3에서 각 validator가 validation 시점에 읽은 object의 freshness/content
- backend가 score/archive 결과를 추가로 어떻게 사용하거나 공개하는지
- owner 2% weight의 경제적 burn/redistribution 의미
- 모든 live validator의 stake, 평가 결과, chain consensus와 최종 emission
- current chain SDK 11 환경에서 `max_weight_limit`가 validator 함수에 전달되는 exact representation
- miner가 HTTP ack 이외 어떤 response body를 보내야 한다는 별도 backend 정책; validator 코드는 body를 보지 않음

### 19.2 private/live state 때문에 clone 밖에 남는 것

- AWS credentials와 bucket state
- validator wallet/hotkey 서명
- 다른 miner submissions
- backend validation authorization/state
- validation block의 live metagraph
- 다른 validators의 set weights와 stake

### 19.3 확인 가능한 공개 부분과 혼동하지 말 것

contract/HBB URL은 task를 받은 miner에게 공개된다. 그러므로 “영원히 private한 scoring secret”은 아니며, 평가 artifact를 즉시 snapshot하지 않았을 때 audit이 잃는 live 정보다. 반면 network-wide consensus는 단일 miner가 task를 capture해도 재현할 수 없는 D 영역이다.

## 20. Recommended Local Clone Architecture

production miner와 분리된 read-only standalone package로 만든다. validator source 파일의 global `data/*.json`을 직접 공유하지 않는 것이 중요하다.

```text
tools/local_validator/
├── artifacts/
│   ├── manifest.py          # URL, fetched_at, SHA-256, commit, block
│   └── snapshot.py          # contract/HBB/celltypes/chr11 immutable capture
├── ingestion.py             # validator-compatible JSON load/truncate semantics
├── stage1.py                # rule-by-rule result + exact reason
├── stage2.py                # k-mer cache + row score
├── stage3.py                # SHA seed + simulation
├── stage4.py                # pinned sklearn RF/CV
├── stage5.py                # entropy/diversity/final
├── aggregate.py             # multi-seed mean
├── weights.py               # optional top-10/chain snapshot simulation
├── trace.py                 # row/stage intermediate provenance
└── cli.py                   # one artifact bundle + one submission → report

tests/local_validator/
├── fixtures/
├── golden/
├── test_ingestion.py
├── test_stage1.py
├── test_stage2.py
├── test_stage3.py
├── test_stage4.py
├── test_stage5.py
└── test_end_to_end.py
```

### 20.1 설계 원칙

- artifact bundle은 immutable content-addressed directory로 저장한다.
- manifest에 repository commit, Python/package versions, chain block, endpoint response hash, fetch timestamp를 기록한다.
- “strict diagnostic schema”와 “validator-compatible execution”을 분리한다. 전자는 문제를 친절히 보고하고, 후자는 현재 validator의 permissive/exception semantics를 그대로 재현한다.
- every row에 Stage1 reason, Stage2 components, Stage3 per-seed RNG-derived outcome, Stage4 fold membership/prediction, Stage5 count를 trace한다.
- float intermediate를 반올림하지 않고 JSON/text display 단계에서만 format한다.
- exact prod version을 알기 전에는 Python 3.12, Bittensor 11.0.1, sklearn 1.9.x를 base로 pin하되 결과 manifest에 실제 resolved versions를 남긴다.
- clone은 S3/backend write를 하지 않는다.

### 20.2 개발 순서

1. artifact snapshot + submission ingestion/truncation
2. exact Stage1 clone
3. exact Stage2 clone와 chr11/k-mer provenance
4. exact Stage3 simulator
5. pinned Stage4 reproduction
6. Stage5/final aggregation
7. top-10 transform와 optional block-snapshot chain processing
8. actual feedback와 differential harness

Stage1부터 바로 시작하지 않고 ingestion을 먼저 만드는 이유는 cap, ID dedup, malformed exception이 downstream 평가 row 집합 전체를 결정하기 때문이다.

## 21. Differential Testing Plan

### 21.1 fixture unit tests

**Ingestion**

- valid list, top-level dict/null/string
- missing/blank/non-string/duplicate `experiment_id`
- max cap 0/1/N/None
- cap 전에 invalid biological row가 자리 소비하는지
- truncation 시 원본 파일 overwrite와 archive 대상 일치

**Stage 1**

- Cas9/Cas12a × plus/minus PAM valid/invalid
- chromosome 양 끝 boundary와 `start+L == len(chr11)`
- exact max mismatch와 max+1
- proximity exact boundary와 boundary+1
- missing key별 row-fail vs UID exception 분류
- duplicate biological key의 first-wins
- lowercase/non-ACGT behavior를 golden으로 고정

**Stage 2**

- GC 0/0.5/1, distance 0/P/beyond P
- off-target hits 0/1/5/6/20/21
- k-mer final-position omission
- mutation weight/accessibility/region default와 known mappings
- base padding 0의 validator-compatible exception

**Stage 3**

- SHA seed golden values
- 모든 contract seed에서 row outcome golden JSON
- no-cut/HDR/MH_NHEJ/BLUNT_NHEJ와 distribution sampling paths
- row reorder가 개별 outcome을 바꾸지 않는지

**Stage 4**

- n=0,1 clean zero
- n=2..9 NaN→factor 0 cliff
- n=10 finite-fold boundary
- constant targets와 mutation sample weights
- exact pinned version에서 fold IDs, predictions, r2/mae golden files

**Stage 5/final**

- one/multiple support entropy
- missing category의 epsilon floor
- identical vs distinct guides/12-mers
- CAS diagnostic 4/5 row boundary와 final에 미사용임을 검증
- single/multiple/empty seed list

**Weight transform**

- no positive, 1..11 positive miners
- exact score ties
- NaN, ±Inf, tiny positive
- owner UID overlap
- chain hyperparameter snapshots

### 21.2 actual validator differential protocol

```text
same submission bytes + artifact bundle ID
               ├─ local clone → stage trace + predicted final/rank
               └─ actual validator → available final/breakdown/on-chain weight
                                      ↓
                               normalized comparison
```

실험 단위마다 반드시 다음을 보존한다.

- submission SHA-256 및 exact row order
- task ID, contract/HBB bytes와 SHA-256
- celltype response bytes/hash/timestamp
- chr11 hash와 source release
- repository commit와 resolved package versions
- chain block/metagraph snapshot
- actual response 수신/업로드/validation timestamps

backend가 raw Stage trace를 miner에게 제공하는지는 `UNKNOWN`이다. final/breakdown만 보이면 stage별 differential은 local ablation과 validator-reported breakdown 범위로 제한된다.

### 21.3 accuracy metrics

| Metric | 목적 |
|---|---|
| absolute final-score error | 한 submission의 exact numeric 차이 |
| MAE/RMSE | 후보 집합 전체 numeric calibration |
| pass/fail agreement | Stage1 row validity와 UID exception 일치 |
| stage component error | base mass, consistency, fidelity 중 원인 격리 |
| Spearman rho | candidate ordering 일치 |
| Kendall tau | pairwise rank consistency |
| top-k overlap/Jaccard | 실제 top-10 선택 일치 |
| rank-boundary confusion | 10위/11위 및 동점 주변 오류 |

top-10 fixed-share 시스템에서는 absolute score error보다 Spearman/Kendall, top-10 overlap, 특히 10/11 boundary 일치가 경제적으로 더 중요하다.

### 21.4 통과 기준 제안

- Stage1 golden: row status/reason 100% 일치
- Stage2/3/5: exact 또는 machine-precision tolerance
- Stage4: 같은 locked image에서 golden exact/tight tolerance, 다른 environment는 명시 tolerance
- end-to-end: score tolerance뿐 아니라 candidate rank 100% 또는 사전 정의된 tie policy 내 일치
- chain transform: 동일 block snapshot에서 nonzero UID set과 uint16 emitted weights 일치

## 22. Exact File / Function / Line References

라인은 분석 HEAD `9f3ada4` 기준이다.

### 22.1 version, entry, scheduler

| 근거 | 위치 |
|---|---|
| package/spec version | `niome_subnet/__init__.py:20-26` |
| validator class/init/forward wrapper | `neurons/validator.py:33-62` |
| process main entry | `neurons/validator.py:106-110` |
| run loop/concurrent forward | `niome_subnet/base/validator.py:115-162` |
| background context manager | `niome_subnet/base/validator.py:169-204` |
| scheduler block phases/create_task | `niome_subnet/validator/forward.py:166-202` |
| timing/top distribution/backend paths | `niome_subnet/utils/settings.py:21-72` |

### 22.2 task/query/collection

| 근거 | 위치 |
|---|---|
| miner HTTP POST/body ignored | `niome_subnet/validator/forward.py:41-58` |
| task fetch, UID shuffle/state | `niome_subnet/validator/forward.py:60-99` |
| UID-only S3 URL/synapse/query | `niome_subnet/validator/forward.py:101-114` |
| validation re-fetch/download/broad except | `niome_subnet/validator/forward.py:116-164` |
| signed backend GET/POST | `niome_subnet/api/__init__.py:15-94` |
| Task model validation/artifact download | `niome_subnet/api/__init__.py:96-115` |
| score DTO POST | `niome_subnet/api/__init__.py:117-122` |
| positive submission archive | `niome_subnet/api/__init__.py:124-166` |
| Task and score Pydantic models | `niome_subnet/genomics/model.py:5-9,43-58` |
| synapse model | `niome_subnet/protocol.py:12-17` |

### 22.3 Stage 1/2

| 근거 | 위치 |
|---|---|
| k-mer index and cache | `niome_subnet/genomics/validation/stage12.py:21-46` |
| region offsets | `niome_subnet/genomics/validation/stage12.py:49-57` |
| chr11/GC/Hamming/RC helpers | `niome_subnet/genomics/validation/stage12.py:60-75` |
| exact PAM/boundary logic | `niome_subnet/genomics/validation/stage12.py:78-108` |
| Stage1 rules | `niome_subnet/genomics/validation/stage12.py:111-154` |
| off-target buckets | `niome_subnet/genomics/validation/stage12.py:157-167` |
| Stage2 formula/features | `niome_subnet/genomics/validation/stage12.py:170-211` |
| cap/ID dedup/file rewrite | `niome_subnet/genomics/validation/stage12.py:214-249` |
| Stage12 orchestration/duplicate design/artifacts | `niome_subnet/genomics/validation/stage12.py:252-321` |

### 22.4 Stage 3/4/5/final

| 근거 | 위치 |
|---|---|
| experiment SHA seed | `niome_subnet/genomics/validation/stage3.py:26-36` |
| energy/microhomology/cut/repair/indel | `niome_subnet/genomics/validation/stage3.py:39-101` |
| per-row simulation | `niome_subnet/genomics/validation/stage3.py:134-169` |
| Stage3 loop/summary | `niome_subnet/genomics/validation/stage3.py:172-212` |
| Stage4 flatten/features/targets | `niome_subnet/genomics/validation/stage4.py:22-78` |
| KFold/RF/metrics | `niome_subnet/genomics/validation/stage4.py:81-115` |
| guards/merge/consistency/final_reward | `niome_subnet/genomics/validation/stage4.py:118-200` |
| entropy/k-mer/geomean helpers | `niome_subnet/genomics/validation/stage5.py:19-74` |
| diagnostic distances | `niome_subnet/genomics/validation/stage5.py:77-112` |
| fidelity components | `niome_subnet/genomics/validation/stage5.py:115-185` |
| multiplicative final score | `niome_subnet/genomics/validation/stage5.py:188-210` |
| multi-seed orchestration/average | `niome_subnet/genomics/validation/__init__.py:11-44` |

### 22.5 weight path

| 근거 | 위치 |
|---|---|
| score array/sanitize/rank selection | `niome_subnet/base/validator.py:214-242` |
| chain minimum/maximum/fallback processing | `niome_subnet/utils/weight_utils.py:110-206` |
| 98/2 owner mix/normalization | `niome_subnet/base/validator.py:244-260` |
| backend DTO and weight field | `niome_subnet/base/validator.py:264-280` |
| uint16 conversion/set_weights | `niome_subnet/base/validator.py:282-298` |
| persisted state fields | `niome_subnet/base/validator.py:330-359` |
| top-10 argsort/prefix normalization | `niome_subnet/utils/weight_utils.py:241-268` |
| top count/distribution/system/burn/owner | `niome_subnet/utils/settings.py:24-26,63-70` |

### 22.6 reference acquisition/dependencies

| 근거 | 위치 |
|---|---|
| chr11 Ensembl release 116 download | `scripts/run_validator.sh:93-107` |
| Python/dependency declarations | `pyproject.toml:6-16` |

## 우리가 지금 당장 구현해야 할 첫 번째 코드가 무엇인가?

**첫 구현 단위는 하나만 선택한다: `validator-compatible submission ingestion + truncation module`이다.**

구체적으로는 raw JSON bytes와 captured contract를 입력받아 현재 `json.load` 및 `truncate_submission()`과 동일하게:

- top-level/malformed behavior를 분류하고,
- `experiment_id` missing/blank/type/duplicate를 같은 순서로 처리하며,
- `max_experiments`를 같은 시점에 적용하고,
- retained row list와 ordered drop trace, input/output SHA-256을 반환하는

side-effect-free 모듈과 golden unit tests를 먼저 구현해야 한다.

이것을 첫 단위로 고른 이유는 Stage 1보다 앞에서 **어떤 row가 모든 후속 Stage에 존재하는지**를 결정하고, cap/ID/order/exception semantics를 틀리면 아무리 Stage1~5 공식을 정확히 옮겨도 validator와 절대 일치할 수 없기 때문이다. production miner 수정이나 최적화는 이 단계에 포함하지 않는다.
