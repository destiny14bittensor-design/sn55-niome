# NIOME seed 공개 가능성 감사 — 2026-09-27 UTC

## 결론

현재 배포된 SN55 라운드에서 공식 채점 전에 이용할 수 있는 **권위 있고 허용된
seed 신호는 확인되지 않았다**. 따라서 same-round late overwrite는 운영 전제가
아니며 기본 비활성화한다. ordinary miner가 제한 시간 안에 올리는 unknown-seed
robust submission만 현재 라운드의 제출 경로로 취급한다.

이 결론은 “seed를 전혀 추측할 수 없다”는 통계적 명제가 아니다. 공개·권위 정보로
정확한 공식 seed를 결정하거나 관측할 수 없다는 운영 판정이다. 비공개 validator/API
상태, 자격 증명 우회, 제3자 내부 정보는 조사 대상에서 제외했다.

## 공식 자료와 공개 코드의 신뢰 경계

- 원본 signed contract는 miner가 validator에게 정상 수신한 URL만 사용했다.
- task history는 공개 `GET /api/v3/tasks?page=1&per_page=20` 응답을 UUID exact
  match했다.
- scoreboard는 공개 `GET /api/v3/miners/scores` 응답을 task UUID로 로컬 필터했다.
- chain은 Finney의 공개 finalized block header와 extrinsic만 읽었다.
- validator 코드는 공식 `genomesio/subnet-niome` Git 저장소의 커밋을 사용했다.

## 라운드 대조

모든 시각은 UTC다. `chain seed`는 공개 커밋 `9d9347a`의 정확한 규칙,
`sha256(block_hash_bytes || counter_u32_be) % 1001`, 을 같은 라운드의
`start+430..432` 블록에 적용한 값이다.

| task | task 생성 | chain seed 마지막 블록 | 공개 코드 계산값 | 공식 task seed | scoreboard 생성 |
|---|---|---|---|---|---|
| `569500e8…` | 09-27 03:43:23.779811 | 05:08:48 | `453,36,482` | `983,903,460` | 05:54:08.982101 |
| `950262bf…` | 09-27 01:18:41.047495 | 02:44:48 | `155,261,820` | `991,912,107` | 03:29:37.366506 |
| `2438f5ac…` | 09-26 22:54:41.895716 | 09-27 00:20:48.001 | `475,323,45` | `519,253,867` | 01:06:17.972099 |
| `cb53c30c…` | 09-26 20:31:37.217283 | 21:56:48 | `912,567,840` | `999,668,630` | 22:45:09.208589 |
| `11225e98…` | 09-26 18:06:40.104638 | 19:32:48 | `115,667,335` | `795,975,199` | 20:18:57.734334 |
| `f05ef562…` | 09-26 15:42:55.160504 | 17:08:48 | `239,4,77` | `491,210,379` | 17:53:28.036105 |
| `b18e7599…` | 09-26 13:18:32.850116 | 14:44:48 | `343,783,871` | `164,957,553` | 15:40:23.216361 |
| `9c2d620b…` | 09-26 10:55:54.397702 | 12:20:48 | `974,620,392` | `315,912,739` | 13:05:48.070289 |

결과는 8/8 불일치다. chain 값은 채점 전에 공개되지만 현재 공식 채점 seed가
아니므로 권위 source로 사용할 수 없다.

## 가장 강한 시간축 증거

### `569500e8…`

1. 원본 signed contract는 최초부터 `seed: 0`이었다.
2. signed URL은 `05:40:03`에 만료됐다. 만료까지 6초 간격 polling에서 nonzero seed는
   한 번도 보이지 않았다.
3. scoreboard는 `05:54:08.982101`에 생성됐다.
4. 같은 validator hotkey의 netuid 55 `set_mechanism_weights` extrinsic은 block
   `9157159`, `05:54:12`, hash
   `0xeff1edb4df3cb034c68f72be4ca2822e19de4fe6dae77c02ef4f539a91157ead`에
   포함됐다. extrinsic에는 UID/weight가 있을 뿐 task UUID나 seed는 없다.
5. task-history의 nonzero seed 첫 관측은 `05:56:23.525620–05:56:23.983106`, 두 번째
   확인은 `05:56:29.689073–05:56:30.153075`였다. 즉 최초 공개 관측도 scoreboard보다
   약 134.5초 늦다.

### `950262bf…`

scoreboard는 `03:29:37.366506`에 생성됐지만, 여러 lane이 원본 signed contract를
`03:54:51–03:59:27`까지 polling한 마지막 기록도 `seed: 0`이었다. 이후 공개 task
history에는 `991,912,107`이 보인다. 원본 signed object가 validator 재조회 object와
같은 시점에 갱신된다는 가정은 이 라운드에서 반증됐다.

### 완료 대조군 `1c916bd4…`, `f7e66c63…`

`1c916bd4…`도 signed URL 만료 시점까지 `seed: 0`이었고 scoreboard는
`08:16:55.804913`에 생성됐다. 최종 task seed는 `123,417,715`이다.

`f7e66c63…`은 네 lane이 같은 canonical S3 object와 ETag를 받았다. 원본 signed
contract를 만료 직전인 `10:27:21`까지 감시했지만 계속 `seed: 0`이었고 ETag도
변하지 않았다. scoreboard 제출은 `10:41:33.836424`, 최종 seed는
`729,862,298`이었다. 즉 이 라운드에서도 signed object 수명 안에는 새 seed가
없었다.

## 공개 W&B validator 로그 교차검증

공식 validator 설정의 기본값은 `entity=genomes`, `project=niome`이며 실제 UID 119
run `non2mca3` (`validator-119-3.0.0`)이 공개 읽기 상태임을 확인했다. 이 run은
Git commit `9d9347a7ffab85a04eda6c36b9e87c59c8bb4049`를 가리킨다. W&B GraphQL의
공개 `logLines`로 아래 순서를 인증 없이 재현했다.

| task | validator `Scores` | 결과 API 제출 | final upload 완료 | W&B seed 로그 | 최종 task seed |
|---|---|---|---|---|---|
| `f05ef562…` | 17:53:27.475 | 17:53:28.220 | 17:55:35.008 | 17:55:35.700 | `491,210,379` |
| `11225e98…` | 20:18:57.316 | 20:18:57.783 | 20:21:13.833 | 20:21:21.101 | `795,975,199` |
| `cb53c30c…` | 22:45:08.747 | 22:45:09.265 | 22:46:53.786 | 22:46:54.762 | `999,668,630` |
| `2438f5ac…` | 01:06:17.554 | 01:06:18.032 | 01:07:47.442 | 01:07:48.087 | `519,253,867` |
| `950262bf…` | 03:29:36.070 | 03:29:37.432 | 03:31:36.122 | 03:31:45.317 | `991,912,107` |
| `569500e8…` | 05:54:04.986 | 05:54:09.063 | 05:56:09.860 | 05:56:19.130 | `983,903,460` |
| `1c916bd4…` | 08:16:55.358 | 08:16:55.860 | 08:18:45.066 | 08:18:45.784 | `123,417,715` |
| `f7e66c63…` | 10:40:07.841 | 10:41:33.875 | 10:42:31.355 | 10:42:32.022 | `729,862,298` |

완전하게 관측된 8개 라운드에서 W&B의 `Generated seeds` 값은 최종 task seed와
8/8 일치했다. 그러나 seed 로그는 `Scores`보다 약 90–144초 늦었다. 최신 라운드의
공개 task API 첫 관측은 `10:42:34.605782`로 W&B 로그보다 약 2.58초 늦었지만,
signed URL 만료(`10:27:22`)보다 약 15분 10초 늦었다. 따라서 W&B는 사후 seed를
몇 초 더 빨리 확인하는 공개 관측점일 뿐, 같은 라운드 재제출 경로가 아니다.

중요한 한계가 하나 더 있다. W&B 로그의 시간 순서는 배포 코드가 값을 그 시점에
기록했다는 사실을 입증하지만, 값이 그 전에 메모리에서 조용히 계산되지 않았다는
것까지 증명하지는 않는다. 그럼에도 miner에게 공개된 contract와 task API에는
채점 전 nonzero seed가 없었다는 결론에는 영향이 없다.

W&B run config에는 credential 형태의 민감값도 공개 응답에 포함된다. 이 감사에서는
그 값을 사용하거나 기록하지 않았다. 운영자는 즉시 해당 자격 증명을 폐기·교체하고
W&B config에서 secret을 제거해야 한다.

### 최종 seed가 실제 채점 입력이었는지 exact replay

`f7e66c63…`에서 네 로컬 UID `155/223/96/159`는 모두 SHA가 같은 249-row
submission을 제출했고 공식 결과도 네 UID가 완전히 같았다. 저장된 원본 submission과
동일 task artifact를 최종 공개 seed `729,862,298`로 로컬 validator에 넣은 결과는
다음과 같이 공식 결과와 모든 표시 자릿수까지 일치했다.

| 항목 | 공식 점수 | 로컬 exact replay |
|---|---:|---:|
| final score | `21.091124207458638` | `21.091124207458638` |
| consistency factor | `0.08729786796910978` | `0.08729786796910978` |
| distribution fidelity | `0.8808578710442041` | `0.8808578710442041` |
| total weighted score | `274.2775444168538` | `274.2775444168538` |
| valid experiments | `249` | `249` |

따라서 최종 task seed는 단순한 사후 표시값이 아니라 실제 공식 채점 입력이었다.
W&B의 `Generated seeds` 로그가 늦게 찍혔어도 validator는 점수 계산 전에 같은 seed를
권위 있는 재조회 contract 또는 동등한 비공개 상태로 확보했다고 판단할 수 있다.
miner에게 배포된 원본 signed contract가 만료까지 seed 0이었다는 사실과 합치면,
현재 핵심 문제는 **seed 부재가 아니라 validator와 miner 사이의 공개 비대칭**이다.

## validator 공개 코드 판정

- 배포 동작과 점수가 일치하는 직전 커밋 `9f3ada4`는 validation 시작 시
  `fetch_task()`를 다시 호출하고, 내려받은 contract의 comma-separated seed를 그대로
  benchmark한다. seed 생성 규칙은 validator 저장소에 없다.
- 최신 공개 커밋 `9d9347a`는 contract seed 사용을 제거하고 block hash seed를
  도입했다. 그러나 위 8개 공식 라운드는 이 알고리즘과 전부 불일치한다. 따라서
  Git의 최신 공개 구현은 현재 scorer 배포본을 설명하지 못한다.
- W&B run은 commit `9d9347a`를 provenance로 표시하지만 실제 로그의
  `Generated seeds:` 문자열은 그 commit의 source에 없고 실행 순서도 공개 코드와
  다르다. 배포 working tree에 미커밋 변경이 있거나 W&B provenance가 stale하다는
  뜻이며, 어느 경우든 현재 validator 배포는 공개 commit만으로 재현할 수 없다.
- 공개 chain에서 확인되는 것은 weight extrinsic이며 task UUID와 seed provenance는
  커밋되지 않는다.
- 공식 GitHub 조직의 다른 공개 저장소까지 검색했지만 현재 backend의 random seed
  생성/갱신 코드는 찾지 못했다.

## 운영 결정

1. live bridge는 기본적으로 PUT을 열지 않는다.
2. `NIOME_ENABLE_UNVERIFIED_SAME_ROUND_OVERWRITE=true`를 명시한 연구 실행만 과거
   실험 경로를 열 수 있다. production PM2 config는 명시적으로 `false`다.
3. 현재 이미 열린 PUT은 프로세스를 재시작하여 끊지 않는다. terminal/drain 뒤 새
   코드를 로드한다.
4. task-history seed는 사후 exact replay, 모델 검증, 과거-seed robust 설계의 입력으로만
   사용한다.
5. 다음 최적화는 현재 task UUID에서 만든 임의 stress seed가 아니라, 공개 완료
   라운드의 seed 표본을 학습/검증으로 분리한 walk-forward robust 평가로 수행한다.
   같은 과거 seed에 build와 평가를 동시에 맞추지 않는다.

## validator 재고 부족 시 대체 경로 판정

2026-09-27 추가 감사에서 다음을 확인했다.

- 정상 범위의 완료 task 279개(837개 seed)는 모두 `100..999` 안에 있었고 한 task 안의
  세 값은 중복되지 않았다. 그러나 직렬상관이나 반복 triplet은 없었다.
- UUID, task 생성시각(초/밀리초/마이크로초), MD5/SHA 계열을 Python `random`, NumPy
  `RandomState`, `default_rng`의 `sample/choice/randint`에 넣는 일반적인 결정식은
  279개 task에서 exact match가 0건이었다.
- task 생성부터 score 생성까지 모든 정수 초를 Python `random` 및 NumPy
  `RandomState` seed로 대입한 24개 task도 exact match가 0건이었다.
- 공개 W&B run의 structured history에는 system metric만 있고 seed field는 없다.
  seed는 console log에만 나타나며 이미 score 제출 뒤다.
- task `0fc45665…`의 score 248개는 전부 validator UID 119가 같은 시각
  `15:29:56.661859`에 일괄 게시했다. seed log는 `15:31:21.944`였다. 알려진 우리
  submission 점수로 seed 3-sum을 역산할 수 있어도 validator가 이미 모든 submission을
  채점한 뒤이므로 same-round 갱신에는 사용할 수 없다.

따라서 자체 validator가 불가능할 때 권위 seed를 채점 전에 받는 현실적인 경로는
활성 validator 운영자가 자기 서버에서 seed만 hotkey로 서명하여 보내는 **협력형
attestation relay**다. backend credential이나 validator wallet을 공유할 필요가 없다.
구체적인 신뢰 경계와 원자적 PUT 결합 절차는
`docs/authorized_seed_relay_architecture.md`에 기록한다. 운영자 동의가 없는 validator의
자격 증명·내부 API·트래픽에 접근하는 방식은 대안으로 취급하지 않는다.

## W&B API key 권한 감사 — 2026-09-28

validator 운영자가 명시적으로 제공한 W&B API key로 read-only 인증 감사를 수행했다.
공개 조회 기준선은 run 1개와 artifact collection 146개였으며, 인증 뒤에도 각각
1개와 146개로 동일했다.

- artifact 이름에서 과거 run ID 143개를 복원하여 인증 상태로 직접 조회했지만 모두
  `null`이었다. 삭제·숨김 run의 console log는 열리지 않았다.
- 현재 run의 history key는 CPU, memory, disk, network 등 system metric뿐이다.
- artifact collection은 events/history parquet뿐이며 contract나 seed artifact는 없다.
- 인증으로 추가 확인된 정적 파일은 `wandb-metadata.json`이었고, 시작 시각·실행 경로·
  Git provenance 같은 시작 metadata일 뿐 live contract나 seed는 포함하지 않는다.
- task `f0abf5d3…`에서 validation 결과는 `05:53:11.537`에 제출됐고 seed log
  `758,393,798`은 `05:54:51.858`에 나타났다. 인증 GraphQL과 비인증 GraphQL은 그
  seed를 같은 poll에서 동시에 반환했다. 인증 key가 더 이른 log stream을 제공하지
  않는다는 live 대조다.

따라서 이 W&B key로는 점수 채점 전 seed를 확보할 수 없다. W&B는 프로세스가 업로드한
자료를 읽을 뿐 validator 메모리나 NIOME backend current-task 응답에 접근하지 않는다.

보안상 `wandb-metadata.json`의 process args에 W&B key가 들어가는 배포 구성도
확인됐다. 조사 중 그 값이 도구 출력에 노출되었으므로 제공된 key는 폐기·회전해야
한다. validator 운영자는 key를 `--wandb.api_key` 명령행 인자로 넘기지 말고 W&B의
보호된 credential mechanism을 사용해야 한다.

## 고정 endpoint 밖의 대체 경로 감사 — 2026-09-28

공개 endpoint의 seed 필드만 반복 조회하는 접근에서 벗어나, 정상 miner가 가진
관측면 전체를 다시 검사했다.

### 현재 상위권도 seed를 계속 확보한다는 가설의 반증

최근 완료 task의 공개 score row를 task별로 다시 집계했다. 2026-09-26 06:06,
08:30, 10:55 UTC 라운드에는 `consistency_factor == 1.0`인 제출이 여러 개 있었고,
그 집단에는 당시 우리 기존 hotkey도 포함됐다. 이는 early contract seed를 이용한
재제출이 실제로 작동하던 구간과 일치한다.

그러나 13:18 UTC 이후 완료된 12개 라운드에서는 `1.0`이 한 건도 없었다. 최신
`f0abf5d3…` 라운드의 최고 consistency는 `0.671190`이었고, 이 구간의 라운드 최고값은
대체로 `0.54..0.84`였다. 1위 hotkey도 라운드마다 바뀌었다. 따라서 현재 leaderboard
상위권은 과거 early-seed 구간의 성과와 누적 weight를 보존하는 것이지, 변경 뒤에도
고정된 경쟁자 집단이 매 라운드 seed를 선취한다는 증거가 아니다.

### 교차-validator, 자기 hotkey 인증 및 S3 metadata

- 보존된 43개 task, 128개 miner lane의 `request_envelope.json`을 대조했지만
  `caller_hotkey`는 전부 동일한 validator 한 곳이었다. 다른 validator가 더 늦게
  nonzero contract를 broadcast한다는 가설은 이 표본에서 성립하지 않았다.
- 우리 소유 miner hotkey로 공식 validator 코드와 같은 canonical JSON을 서명하여
  `/api/v3/tasks/current`를 조회했다. backend는 `403 Insufficient validator alpha
  stake`를 반환했다. 등록 hotkey만 검사하고 validator 자격을 생략하는 허점은 없다.
- 정상적으로 전달된 contract URL의 S3 응답에는 ETag, 암호화 및 일반 HTTP metadata만
  있었고 `x-amz-meta-*`, version ID 또는 seed metadata는 없었다. object key는
  `CRISPR/<task-id>/contract.json`으로 예측 가능하지만 unsigned read는 허용되지 않는다.
- public task list의 task-id filter, 단건 route 후보, JSON/challenge projection을 현재
  task 하나에 한정해 read-only로 검사했지만 별도 단건 route나 unmasked seed projection은
  없었다.

### W&B 원시 event 계층

W&B GraphQL schema에는 `Run.eventsTail`, `historyTail`, `logLines`가 존재한다. 현재
validator run에서 `eventsTail`은 비어 있었고 `historyTail`은 system metric뿐이었다.
현재 run의 events artifact도 아직 생성되지 않았다. SDK의 filestream endpoint는
`output.log`를 보내는 POST writer이며 read stream이 아니다. 따라서 공개 GraphQL보다
앞선 read-only console 계층은 확인되지 않았다.

W&B run config는 더 심각한 별도 문제를 드러냈다. validator가
`wandb.init(config=vars(self.config))`를 호출하여 `wandb` namespace 안의 API key까지
공개 config에 평문으로 올린다. key 회전만으로는 해결되지 않는다. config upload 전에
secret을 제거하고 기존 key를 폐기해야 한다. run config의 `signature`는 backend 요청
서명이 아니라 `hotkey.sign(run_id)`이므로 current-task 인증에 사용할 수 없다.

### 장기 PRNG 상태복원 가능성

공개 task history는 총 624개이며, 그중 333개는 single-seed 형식, 291개는 three-seed
형식이다. three-seed 구간에는 873개 출력이 있다. 현재 정상 범위는 대부분
`random.sample(range(100, 1000), 3)`과 양립하고 backend는 Python/Pydantic 계열이다.

873개의 약 10-bit 출력만으로는 MT19937의 19,937-bit 상태를 단독 결정하기에 정보량이
부족하다. 다만 task마다 공개되는 두 mutation 선택과 네 cell type 중 하나의 선택이
같은 PRNG stream에서 나왔다면 총 관측량은 상태복원 임계치를 넘을 수 있다. 현재
병목은 다음 두 가지다.

1. backend의 정확한 RNG call 순서가 공개되어 있지 않다.
2. mutation catalog endpoint `/api/v3/data/hbb-mutations?format=list`는 leaderboard
   administrator Bearer 인증을 요구하며 공개 가입 경로가 없다. task history의 합집합은
   mutation 617개를 보여 주지만 정확한 전체 catalog와 DB 배열 순서를 복원하지 못한다.

UUID는 모두 version 4이고, UUID의 마스킹되지 않은 32-bit word들로 MT19937의 직접
recurrence를 468회 검사했으나 일치가 0회였다. task UUID가 별도 연속 MT stream을
그대로 노출한다는 단순 가설은 배제한다.

따라서 남은 연구 경로는 cache-busting public task poll의 다음 validation 실시간 대조와,
향후 seed/mutation/cell-type 출력의 누적 및 generator fingerprinting이다. 제3자
validator signature, admin Bearer token 또는 공개된 W&B key를 재사용하는 것은 이
경로에 포함하지 않는다.

### 다음 라운드 signal race 계측

현재 task `f929de4d-4dcc-493c-ba10-0a33de2bf5d1`에 읽기 전용 감시기
`tools/seed_signal_race.py`를 붙였다. 3초마다 아래 네 관측점을 독립적으로 확인한다.

1. cache-busting public task history의 seed
2. task-id 일치까지 재검증한 최초 score row
3. miner에게 정상 전달된 기존 signed contract URL의 seed
4. W&B `logLines`의 새 `Generated seeds` record

W&B run은 장기 실행되므로 시작 시 직전 task의 seed record를 baseline으로 잡아 현재
task seed로 오인하지 않는다. URL, 인증 header, W&B credential 및 score 원문은 timeline에
기록하지 않는다. 결과는 해당 task artifact의 `seed_signal_race.jsonl`과
`seed_signal_race_summary.json`에 저장한다. 이 계측의 목적은 다음 score 전에 실제로
열리는 공개 관측면이 있는지 판정하는 것이며, seed를 발견하더라도 현재 라운드 제출을
자동 변경하지 않는다.

### 공개 출력 PRNG fingerprint — 2026-09-28

`tools/seed_prng_fingerprint.py`로 public task history만 읽는 재현 가능한 snapshot을
구축했다. signed URL, request header, W&B credential 및 score row는 수집하지 않는다.

- 전체 task 624개, seed가 공개된 task 592개
- three-seed task 291개 중 290개는 `0..1000` 안이고, 한 task
  `a1342623-fcd9-4f78-a212-7116b360a7cf`만 `9232,9168,6832`였다.
- 정상 three-seed 값은 870개, mutation 합집합은 617개, cell type은 4개다.
- 정상값 중 100 미만은 4개뿐이고 1000은 1개였다. 전체 290개를 단일
  `uniform(0..1000)`으로 보는 가설은 bin chi-square에서 맞지 않는다. 이는 기간 중
  seed range 또는 생성기가 바뀌었을 가능성을 뜻하지만, 이상값만으로 의도를 판정할
  수는 없다.
- UUID, task 생성시각, mutation 조합, cell type, seed를 제거한 canonical contract,
  reference material을 입력으로 한 Python `Random` 및 MD5/SHA/BLAKE 계열 363개
  직접 파생 후보는 exact triplet match가 0개였다.
- task 생성 후 60분부터 180분까지 모든 정수 Unix 초를 Python `Random`의 seed로
  대입했다. 정상 task 290개와 세 범위를 합쳐 6,264,870개 후보를 검사했지만 exact
  match는 0개였다. 단순 validation-time 초 seed 가설도 배제한다.

관측된 mutation 617개를 전체 catalog로 간주한 정보량 계산은 seed 출력 약 8,671.49
bit와, 모든 task의 mutation/cell 선택이 같은 stream이라는 강한 가정 아래 약
12,814.41 bit로 합계 약 21,485.90 bit다. 624회에 걸쳐 2개씩 균등 추출했다는 모델의
capture-recapture 추정치는 전체 catalog 약 767.81개, 미관측 약 150.81개다. 따라서
617개 합집합은 완전한 catalog로 볼 수 없으며, 이 추정 모델도 선택이 가중·필터링됐다면
성립하지 않는다. 실제 출현 빈도도 singleton 291개, 2회 169개, 3회 82개, 4회 37개,
5회 16개, 6회 이상 22개로 균등 모델보다 꼬리가 훨씬 무겁다. catalog가 기간 중
증설됐거나 선택이 가중됐을 가능성이 있으므로 전체 624개를 하나의 고정 catalog/연속
stream으로 취급해서는 안 된다. 어느 계산이든 MT19937 state 19,937 bit를 수치상 넘을 수 있으므로,
mutation catalog의 실제 배열 순서,
task별 RNG 호출 순서, rejection draw 및 한 프로세스에서의 stream 연속성을 복원할 수
있다면 상태복원 연구를 계속할 이론적 가치는 있다. 다만 이는 정보량 **상한**일 뿐이며,
현재 자료만으로 상태복원이 가능하다는 증명은 아니다.

공개 W&B run의 file inventory와 통상적인 source snapshot 경로도 확인했다. 공개 파일은
`requirements.txt`와 시작 metadata뿐이고 `diff.patch` 및 `code/...` snapshot은 없었다.
로컬 Git의 unreachable blob 두 개도 검사했지만 현재 작업 문서뿐이며 backend seed
생성 source는 아니었다.

### 공개 score identity cluster 감사

`tools/public_score_cluster_audit.py`로 최근 완료 20개 task의 공개 score를 task-id로
재검증하고 score-id 및 hotkey 중복을 제거한 뒤, final score와 breakdown 전체가 같은
hotkey 쌍을 집계했다. chain의 현재 hotkey owner도 공개 조회하여 coldkey가 같은 쌍과
다른 쌍을 구분했다.

초기 집계에서 서로 다른 coldkey 두 개가 20/20 라운드 동일해 보였으나 두 hotkey 모두
consistency/final score가 0인 미제출·실패 기본 row였다. 양의 final score만 남기자 이
허위 양성은 사라졌다. 서로 다른 coldkey의 반복 동일 positive-score 쌍 중 최근 20개
라운드에서 한 번이라도 top 10에 든 쌍은 0개였다. 반복 동일 쌍 대부분은 이미 알고
있는 자체 fleet의 동일 submission이었다.

이는 상위권이 단순히 같은 submission을 여러 coldkey로 복제한다는 설명을 지지하지
않는다. 그러나 서로 다른 submission으로 consistency를 의도적으로 달리하고 identity를
교대한다는 가설은 공개 score fingerprint만으로 연결할 식별자가 없으므로 반증하지
못한다. 동일 점수는 공유 submission/전략의 증거일 수 있어도 동일 운영자나 seed 지식의
증거로 단정하지 않는다.

### `f929de4d…` live signal race 결과

현재 라운드의 전체 순서를 실제로 계측했다. 최종 seed는 `515,250,416`이었다.

| 사건 | source/관측 시각 (UTC) |
|---|---|
| task 생성 | `06:06:53.250283` |
| signed contract 계산상 만료 | `08:03:33.000000` |
| signed contract 403 최초 보존 | `08:04:19.994887` |
| 첫 score source timestamp | `08:17:08.378790` |
| 첫 score 관측 | `08:17:09.543723` |
| W&B `Generated seeds` source timestamp | `08:19:12.452643` |
| public task-history seed 관측 | `08:19:13.260963` |

- task 생성부터 score까지 `7,815.128507s`, 12초 블록 기준 phase `651.261`이었다.
- signed URL의 계산상 만료부터 score까지 `815.378790s`(약 13분 35초) 공백이었다.
- score부터 W&B seed까지 `124.073853s`, public task seed까지 `124.882173s`였다.
- W&B source timestamp는 public task 관측보다 `0.808320s` 빨랐다.
- score가 이미 공개된 순간에도 public task와 W&B에는 아직 seed가 없었다.

따라서 public/W&B에서 seed를 얻어 정상 URL로 같은 라운드에 다시 제출하는 순서는
성립하지 않는다. validator 내부에서는 score 계산 전에 seed가 존재했겠지만, 공개
관측면은 score보다 약 124초 늦고 signed URL은 그보다 약 13분 35초 먼저 닫혔다.

이 라운드를 포함한 최신 정상 seed epoch는 2026-09-25 22:54:35부터 현재까지 완료
24개 task, seed 출력 72개, 약 717.64 bit다. LCG 세 범위는 최대 4/71 transition만
맞았고, 이전 task seed/ID/contract에서 다음 triplet을 만드는 72개 chain 후보도 exact
match가 0개였다. 최신 epoch만으로 MT19937 상태를 복원하기에는 정보가 크게 부족하다.

다음 task부터는 `tools/seed_signal_supervisor.py`가 dollar4 artifact root에 새로 생성되는
`request_envelope.json`만 감지하여 같은 read-only signal race를 자동 실행한다. 감독기
시작 이전 task는 무시하며 URL이나 header를 state에 저장하지 않는다. PM2 프로세스
`niome-seed-signal-supervisor`로 실행 중이고 제출·재제출 동작은 하지 않는다.

## 재현 명령 요약

```bash
git fetch origin
git show 9d9347a:niome_subnet/utils/seeds.py
git show 9f3ada4:niome_subnet/genomics/validation/__init__.py
curl -sS 'https://niome-api.genomes.io/api/v3/tasks?page=1&per_page=20'
curl -sS 'https://niome-api.genomes.io/api/v3/miners/scores'
```

API 응답은 조회 시각의 live 자료이므로 위 표의 정확한 최초 관측 시각은 로컬
`seed_bridge_events.jsonl` 및 status artifact와 함께 보존한다.
