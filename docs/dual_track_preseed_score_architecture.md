# 채점 전 seed 생성기·조기 개별 점수 이중 연구 아키텍처

## 목표와 결합 조건

연구는 서로 독립적인 두 증명 트랙으로 운영한다.

1. **Track A — 채점 전 seed 생성기**: score나 사후 seed를 입력으로 사용하지 않고,
   task 생성 시점에 이미 공개된 정보만으로 세 seed를 예측한다.
2. **Track B — 조기 개별 점수**: task 전체 batch 공개보다 앞서 특정 마이너의 정밀
   score를 허가된 관측면에서 얻고, 그때도 다른 소유 제출 경로가 쓰기 가능하며 충분한
   build/upload 시간이 남는지 측정한다.

두 트랙 중 하나만 성공한 상태는 운영 가능 상태가 아니다. 결합 가능 판정은 아래 조건을
모두 충족해야 한다.

- Track A: 미래 task에서 score 공개 전에 기록한 seed가 5회 연속 exact다.
- Track B: 개별 score가 batch보다 먼저 관측된 미래 task가 3회 이상이며, 각 task에서
  독립적으로 확인한 소유 채널 쓰기 가능성과 양의 deadline budget이 함께 존재한다.
- 공통: task UUID, miner hotkey, submission SHA가 동일한 관측끼리만 결합한다.
- 안전: 자동 PUT, 재제출, validator 비공개 상태 접근은 별도 운영 승인 전까지 없다.

## 2026-09-30 공개 W&B `Scores` 조기 개별점수 후보

공개 W&B run `genomes/niome/non2mca3`의 console에는 validator가 backend 결과 API를
호출하기 직전에 `Scores: [MinerScore(...)]`를 기록한다. 인증 없는 공개 GraphQL
`logLines`만 사용해 task별 `Scores` 시각을 이후의 정확한 task 결과 제출 로그와 결합하고,
공개 score API의 `created_at`과 41라운드를 대조했다.

- 41/41에서 W&B `Scores` source timestamp가 score API보다 빨랐다.
- 선행시간은 최소 `0.395572초`, 중앙값 `0.511770초`, 최대 `81.412755초`였다.
- W&B 한 줄은 4,095--4,096자로 잘려 라운드당 앞쪽 12--13개 UID만 보인다.
- 표시된 523개 행 중 520개가 이후 공식 점수와 exact 일치했다. 불일치 3개는 UID
  `246`, `7`, `181`이며, 차이는 각각 약 `0.0000593`, `0.0000357`, `0.421517`이었다.
- 관리 중인 UID는 3라운드에서만 잘린 앞부분에 들어왔고, 관측된 관리 UID 값은 이후
  공식값과 exact 일치했다.

이는 **일부 UID 점수의 공개 조기 후보**지만, 역사적 source timestamp만으로 해당 행이
그 순간 GraphQL에서 실제 조회 가능했다고 증명할 수는 없다. W&B가 나중에 과거 시각의
로그를 반영했을 가능성을 배제하려면 전향 `first_seen` 기록이 필요하다.

`tools/early_score_monitor.py`는 다음 라운드부터 task의 공개 `Fetched task <uuid>`를 먼저
확인해 source를 arm한 뒤, 인증 없이 W&B tail을 polling한다. 다른 task fetch가 보이면
자동 disarm하며, `Scores`가 보일 때 원문·점수값·endpoint를 저장하지 않고 UID와
`first_seen`/`source_at`만 기록한다. 이후 공개 batch가 실제로 증가해야
`confirmed_before_batch_completion`으로 판정한다. 2026-09-30 `b5126688…` task가 이
전향 검증에 arm된 첫 라운드다. 이 monitor는 제출·PUT·자격 증명 사용 기능이 없다.

## 2026-09-29 연구방향 전환: 변경점 기반 현행 epoch

Track A는 더 이상 과거 전체를 하나의 생성기 stream으로 취급하지 않는다. 현행 연구
epoch는 아래 값으로 명시적으로 고정한다.

- epoch ID: `post-score-random-triple-100-999-v1`
- 시작: `2026-09-25T22:54:35.523942Z`
- 출력 계약: 서로 다른 seed 3개, 각 `100..999`
- 공개 순서 가설: score가 public seed보다 먼저 보임

### 고정 Discovery 전수 탐색 현황

Discovery 20개와 Holdout 5개 ID는 `data/preseed_discovery_manifest.json`에 고정한다.
새 task가 들어와도 이 분할을 밀어내지 않는다. Holdout은 Discovery 20/20 ordered-exact
후보가 생기기 전에는 열지 않는다.

2026-09-29 기준으로 다음 bounded family를 완료했다.

- 기본 public chain/task digest 640개와 격리한 과거 direct-map 363개
- 라운드 720개 전체 block offset의 hash·block-number·PRNG·task composite 158,423개
- Bittensor Drand pulse 57,762개를 이용한 533,732개
- Substrate RandomnessCollectiveFlip을 정확히 재현한 483,308개
- W&B validator process 재시작 경계 이후 연속 16 task를 대상으로 한 상태형
  Python/NumPy 고정·시간 seed 25,676,992개
- 동일 NumPy 전역 상태에서 매 라운드 `shuffle(256 UID) → seed 3개`를 재현한
  interleaved 고정·프로세스시각 seed 2,345,602개
- 재시작 직후 직전 task seed를 먼저 뽑는 실제 호출순서
  `prelude seed → shuffle → seed`를 반영한 NumPy 2,985,606개
- 같은 prelude를 포함하되 shuffle과 독립된 Python/NumPy 상태형 stream 25,676,936개
- 공개 W&B의 실제 validation 시작시각을 기준으로 초·밀리초·마이크로초 고정
  offset을 적용한 PRNG/digest 4,012,030개
- W&B seed 생성 로그 시각과 최초 공개 score 시각을 각각 기준으로 같은 고정 offset을
  적용한 PRNG/digest 8,024,060개
- 이전 공개 seed와 다음 task/contract/공개 block 문맥을 결합한 digest·HMAC·
  Python/NumPy 재시드 recurrence 22,580개
- `Generated seeds` 로그 시각을 라운드별로 독립 허용한 초·밀리초·마이크로초 및
  Python float-clock 재시드 32,327,808개

기존 campaign 69,975,747개와 위 신규 32,350,388개 가운데 Discovery 전체를
ordered-exact로 재현한 후보는 0개다.
상태형 검색은 Python 고정 seed `0..9,999,999`, 프로세스 시작시각 주변의 초·밀리초·
마이크로초 seed, NumPy 고정 seed `0..999,999`의 `RandomState`/`default_rng`, 공개 실행
식별자 유도값을 포함한다.
이 결과는 OS entropy/CSPRNG, 더 큰 비공개 seed, 비공개 재시드, 관측하지 못한 interleaved
draw를 배제하지 않는다. 따라서 “랜덤해 보임”을 생성기 발견으로 승격하지 않는다.

### NumPy shuffle 상태누출 트랙

공개 validator 코드의 broadcast 경로는 `np.random.shuffle(miner_uids)`를 한 라운드에
한 번 호출한다. 같은 W&B console에는 그 직후 `/forward` 요청 약 245개의 순서가
남는다. endpoint 문자열 원문을 저장하지 않고 중복을 고려한 multiset permutation
정보량만 계산한 결과, 전체 W&B 로그 29라운드의 상한은 36,707.29비트다. 이는 MT19937 상태
19,937비트를 넘는다.

이 값은 곧바로 복구 가능한 raw bit 수가 아니다. 동일 endpoint를 공유하는 UID의
순서가 소실되고, 역사적 metagraph와 정확히 정렬해야 하며, NumPy bounded integer의
rejection draw도 모델링해야 한다. 가장 중요한 미확정 조건은 benchmark seed가 이
shuffle과 **같은** NumPy 전역 RNG를 소비하는지 여부다. 따라서 현재 상태는
`feasible research lead`이며 성공 후보가 아니다. 이후 단계는 역사적 metagraph에서
고유 endpoint UID를 고정하고, Fisher–Yates 제약을 MT19937 symbolic state에 연결한 뒤,
학습에 쓰지 않은 미래 라운드 seed로 검증하는 것이다.

W&B GraphQL의 legacy `last` resolver가 10,000줄에서 조용히 잘리는 문제를 발견해
`useImprovedPagination` cursor로 실행 시작점까지 복구했다. 역사적 SN55 metagraph를 각
task 생성 block에 고정해 라운드를 다시 정렬한 결과, 6,850개 group-order 관측과 3,795개
unique-UID 관측을 solver 입력으로 만들었다. 초기 28라운드의 6,831개 위치는 당시 endpoint
multiset과 일치했고 그중 3,681개는 endpoint가
유일하여 UID를 하나로 고정할 수 있었다. 일부 endpoint는 task 생성 직후 broadcast
사이에 변경되어 모든 라운드의 counter가 완전히 같지는 않았지만, symbolic solver
입력 준비 기준(16라운드 및 1,800개 unique UID 위치)은 통과했다. 원시 endpoint와 IP는
어떤 연구 artifact에도 저장하지 않는다.

실패 로그의 `Error querying miner UID`도 순서에 병합해 endpoint 추정이 아닌 exact UID
142개를 추가했다. 현재 29라운드에서 로그에 남지 않은 위치는 574개다. 최상의 라운드는
256개 중 251개가 보이며 누락은 5개다.
이 group sequence가 실제 Fisher–Yates shuffle의 부분수열이 되는 swap witness는 선택한
8라운드 모두 구성·로컬 replay에 성공했다. 다만 이것은 가능한 permutation witness일 뿐
MT19937 상태복원이 아니다. 다음 gate는 swap index를 `random_interval`의 rejection draw와
단일 MT19937 symbolic state에 묶고, solve에서 제외한 Discovery seed를 예측하는 것이다.

그 gate의 정보량 가능성을 실제 NumPy 알고리즘과 동일한 합성 스트림에서 따로 검증했다.
`rk_interval`의 bit mask와 범위 밖 재추출을 그대로 재현하고, accepted raw word의 알려진
하위 비트만 GF(2) MT19937 전이에 넣었을 때 17번째 256-UID 셔플에서 유효 상태
19,937비트의 rank가 19,937에 도달했다. 자유인 31개 저장비트를 0으로 둔 동치 상태는 그 뒤
32개 raw word를 모두 정확히 예측했다. 이는 정확한 permutation과 raw-draw 정렬을 안다는
합성 조건에서의 식별 가능성 증명일 뿐 실 validator 상태복원은 아니다. 실제 단계에는
항상 누락되는 axon 없는 UID 5개 이상의 삽입 위치, 공유 endpoint, rejection draw 위치,
그리고 seed 생성과 shuffle이 같은 NumPy 상태를 쓰는지 여부가 아직 남아 있다.

로그 순서의 한 라운드 offset도 별도 감사했다. 비교 가능한 Discovery 16개에서 validation
뒤에 기록된 `Generated seeds`는 16개 모두 같은 fetched task의 최종 label과 일치했고,
다음 task label과 일치한 사례는 0개였다. 따라서 상태 결합 모델은
`shuffle(task T) → seed(task T)`를 사용하며 `shuffle(T) → seed(T+1)` 가설은 기각한다.

초기 prelude 정렬 감사에서 기존 두 도구가 잘못된 값을 사용한 결함도 발견했다. 새 W&B
run에서 실제 첫 `Generated seeds`는 `654,347,964`인데, 도구는 process start 이전 task의
공개 label `164,957,553`을 사용했다. NumPy prelude, 독립 Python/NumPy stream, runtime
identity 도구는 explicit public W&B prelude를 인자로 받도록 수정했다. 수정 후 독립
stream 25,676,936개, NumPy shuffle-interleaved initializer 2,985,606개, runtime identity
6,027개 모두에서 첫 triplet 일치가 0개였다. 따라서 이제 이 음성 결과들은 올바른
restart 경계를 사용한다.

백엔드/패치가 JavaScript 계열일 가능성도 별도 상태복원으로 검사했다. 프로세스 restart
직후 공개된 prelude 3개와 이후 Discovery 16라운드의 48개 값을 합친 51개 출력으로 구형
V8 `state0 >> 12` xorshift128 계열을 GF(2)로 풀었다. 128비트 full rank에 도달한 뒤 제약이
모순되어 이 계열은 기각됐다. 현행 V8 xorshift128+의 `state0 + state1`/53비트 계열은 처음
Z3와 Boolector에서 timeout이 났으나, xorshift와 64비트 carry adder 및 정확한 900개 bucket
경계를 CNF/XOR로 비트블라스트한 독립 CryptoMiniSat 모델을 추가했다. 이 모델은 합성 stream의
숨긴 suffix를 정확히 복원했고 실제 stream은 14번째 출력에서 UNSAT가 나와 연속 호출 가설을
기각했다. 실제 W&B 실행체는 CPython validator이고 forward가 이미 NumPy를 사용하므로
Python `random.sample` 및 NumPy 전역 `choice`/`randint` 결합이 더 높은 우선순위다.

단일 정수로 축약하지 않고 UUID/block hash를 32비트 word 배열로 NumPy
`SeedSequence`에 전달하는 구현도 추가했다. PCG64, PCG64DXSM, MT19937, Philox,
SFC64와 `choice`/unique-`integers`를 block offset 0..719에서 조합한 72,020개를
Discovery 20개에 전수검사했으며 exact 후보는 0개였다. 최고 후보도 우연 수준인 전체
60개 위치 중 3개 일치에 그쳤다.

공개 runtime identity 고정 seed도 restart prelude를 포함해 확장했다. validator UID/netuid,
wallet 이름, W&B run id, git commit, process-start 문자열, 공개 hotkey/coldkey 및 curated
결합을 SHA-256/SHA-512/BLAKE2b·CRC32·Adler32로 파생하고 Python/NumPy 연속 stream 6,027개를
검사했다. 첫 task triplet조차 일치한 후보가 0개여서 이 계열도 기각했다. 원시 공개 주소는
artifact에 저장하지 않고 material label만 남겼다.

이전 task의 공개 seed를 다음 상태로 직접 쓰는 구현도 별도 검사했다. seed 5가지 직렬화와
현재 task UUID·contract·created time·4개 block role의 hash/header를 결합해 SHA-256,
SHA-512, BLAKE2b, HMAC, Python `Random`, NumPy `RandomState/default_rng` 22,580개를
19개 Discovery 전이에 적용했다. 정확 triplet 전이는 단 한 번도 없었고 최고 후보도 57개
위치 중 3개 우연 일치뿐이었다. 단순 previous-seed recurrence는 기각한다.

단순 clock 재시드는 validation 시작시각뿐 아니라 W&B `Generated seeds` source timestamp와
해당 task의 최초 공개 score-row source timestamp로 확장했다. seed-log는 Discovery 16개,
score는 20개 전부에서 초 ±600, 밀리초·마이크로초 ±100,000 범위를 Python/NumPy/digest
10종으로 검사했으며 첫 triplet 일치조차 0개였다. 따라서 이 세 공개 시계에 한 개의 고정
offset을 더해 매 라운드 재시드하는 구현은 기각하지만, OS entropy나 가변 private latency,
장기 state는 배제하지 않는다.

작은 내부 상태를 직접 복구할 수 있는 비-Python 계열도 초기 seed brute force가 아니라
31~48비트 state 자체를 제약해 검사했다. Java `Random.nextInt(900)`, ANSI/MSVC high-bit
LCG, ANSI/Numerical Recipes full-word modulo, Park--Miller 총 6종은 restart prelude와 이후
Discovery 16개 연속 stream에 모두 UNSAT였다. Java bound-900 rejection 확률은 draw당
`4e-7` 미만이므로 이 검사는 연속 호출 모델을 대상으로 하며 숨은 소비자는 별도 가설이다.

새 task의 sequential broadcast가 진행되는 동안 부분 query 순서를 완성된 permutation으로
오인하지 않도록 최소 200개 관측 gate를 추가했다. 2026-09-29 11:48 UTC 기준 29라운드가
최소 관측 gate를 통과했고 진행 중 라운드는 0개다. `niome-preseed-shuffle` 감독 프로세스는
120초마다 공개 W&B를 확인하고 완료
라운드 수가 증가했을 때만 역사적 metagraph 제약과 witness를 원자적으로 갱신한다.

전체 256단계 array-SMT는 첫 라운드에서도 120초 timeout이 났으므로, 누락 token 삽입과
공유 endpoint 선택을 Fisher--Yates의 가장 높은 index부터 정확히 열거하는 prefix-domain
단계를 추가했다. 현재 29라운드에서 완전히 계산된 choice domain은 111개, 합계
306.456833 domain-reduction bits이며 최대 완성 깊이는 8이다. singleton은 아직 0개다.
제약 corpus의 one-member UID domain도 exact UID로 보존해 라운드당 exact token을 최대
150개까지 사용한다. 다만 처음부터 로그에 없는 UID 5개 이상의 suffix 삽입이 marginal
domain을 지배하므로 exact UID 증가만으로 첫 선택집합은 줄지 않았다.

Marginal domain이 버리는 선택 간 상관관계도 별도 exact tuple로 보존했다. 깊이 4,
라운드당 50,000 state cap에서 29라운드의 완성/부분 prefix가 총 304,607개 허용 tuple을
만들었고 10라운드는 요청 깊이 4를 전부 완성했다. 이 tuple을 restart prelude, 모든
Fisher--Yates draw, bounded-integer rejection 및 Discovery seed와 한 NumPy MT19937 stream에
결합하는 경량 CP-SAT 모델을 추가했다. 1라운드는 rejection cap 1에서 SAT였고, rejection을
0으로 고정한 진단 모델은 6라운드/두 번의 MT twist까지 SAT, 7라운드부터 120초 내
UNKNOWN이었다. 현실의 256-way shuffle은 라운드당 약 94.74 rejected raw word를 기대하므로
rejection=0 결과는 인코딩/연속-state 가능성 검사일 뿐 생성기 후보가 아니다. 전체 symbolic
pool 결합 모델도 1라운드 300초에 UNKNOWN이어서 현재 gate는 더 깊은 상관제약을 더 작은
인코딩으로 옮기고 실제 rejection 정렬을 푸는 것이다.

현실적인 rejection 총량도 별도로 강제했다. 첫 라운드에서 draw당 rejection cap 5와
라운드 총량 `59..129`(기대값 약 94의 3σ 범위)를 함께 적용한 모델은 147초에 SAT였고,
raw word 320개를 소비하는 witness를 만들었다. 따라서 첫 라운드 적합은 비현실적인
무-rejection 경로에만 의존하지 않는다. 같은 제약으로 첫 두 Discovery 라운드와 첫 MT
twist를 묶은 모델은 600초에 UNKNOWN이었으므로 이 가설은 기각되지도, 상태가 복구되지도
않았다. 공개 shuffle만 사용하고 label을 열지 않은 sealed window는 3라운드와 6라운드에서
각각 SAT였다. 이는 Holdout sealing을 유지한 연속성 진단이지 seed 예측이 아니다.

전체 group 순서를 `Select/Store` array로 압축한 독립 Z3 모델도 추가했으나, 한 라운드와
rejection 0 진단조차 120초에 UNKNOWN이었다. 따라서 array-SMT는 정확성 교차검증용으로
보존하고 주 탐색기로 승격하지 않는다. exact prefix를 선택적으로 깊게 확장한 결과,
`f05ef562…`는 깊이 7에서 1,398,882개 상태가 되었고 깊이 8에서 상한을 넘었다.
관측 구조가 가장 유리한 `cd4998dc…`도 깊이 10에서 980,352개 상태, 깊이 11에서 상한을
넘었다. 단순 tuple 열거의 추가 확장은 실용적이지 않으며 라운드별 정보/모호성 선별과
압축된 결정도 또는 전용 XOR-SAT가 필요하다.

이에 따라 MT19937의 선형 출력은 native XOR clause로, bounded draw 정렬·Fisher--Yates
swap·누락 token 순서는 CNF로 결합한 CryptoMiniSat 경로를 추가했다. 실제 rejected raw
word에 `masked > maximum`을 강제하고 라운드별 총 rejection을 91개로 고정한 한 확률 shard에서
첫 6개 Discovery 라운드(그중 2개는 전체 공개 group 순서, 나머지는 exact prefix tuple)가
SAT였다. 허용 bounded value를 selector로 전부 열거하던 CNF를 금지값 직접 clause로 바꾸자
기존 6라운드 UNKNOWN이 47.7초 SAT로 개선됐다. 그러나 91 고정은 가능한 정렬 하나일 뿐이고
상태는 아직 유일하지 않다. 첫 5개를 학습하고 6번째 shuffle prefix만 결합한 뒤 실제 label과
무관한 `[101,100,100]`, `[999,999,999]`를 각각 조건화했을 때 둘 다 SAT였다. 따라서 이
제약 깊이에서는 다음 seed가 식별되지 않는다. 6개를 학습한 7번째 probe는 300초 내
UNKNOWN이므로 비식별/기각 어느 쪽으로도 세지 않는다. 6라운드에서 전체 셔플을 3개 또는
4개로 늘린 모델도 각각 600초 UNKNOWN이었다. 현재 계산 경계는 전체 셔플 2개 SAT와
3개 UNKNOWN 사이이다. 상태와 정확한 UID permutation은 artifact에 저장하지 않는다.

전체 셔플 선택 편향을 제거하기 위해 full-shuffle task를 명시하는 옵션도 추가했다. 공개
UID가 가장 많이 남은 1·4·5번 라운드(251/249/250개)를 강하게 묶은 full3 모델도 600초
상한에서 UNKNOWN이었다. 이는 계열 기각이 아니라 solver 계산 경계다.

새 validator patch가 seed를 `np.random.choice(100..999, 3, replace=False)`로 만들었을
가능성을 위해 900개 배열 전체를 SAT에 펼치지 않고, 알려진 최종 세 위치를 Fisher--Yates
swap을 역방향으로 추적하는 O(3n log n) 제약을 구현했다. 최초 진단은 공개 seed의
`100..999` 값을 permutation index `0..899`로 바꿀 때 Discovery label에는 100을
뺐지만 restart prelude에는 빼지 않은 결함 때문에 `964`를 범위 밖 index로 묶어 거짓
UNSAT를 냈다. 변환을 통일하고 회귀 테스트를 추가한 뒤, prelude와 첫 Discovery 및
shuffle-prefix tuple을 결합한 zero-rejection shard는 180초 이내 `UNKNOWN`이었다.
따라서 현재 choice 계열이나 zero-rejection 정렬을 기각한 증거는 없다. 실제 900
permutation은 평균 약 382개의 masked rejection을 소비하므로 다음 단계는 rejection
checkpoint shard와 제외 Discovery 예측이다.

이후 accept 사이의 가능한 gap마다 간선을 만드는 기존 정렬기를 raw word별
`accept/reject` 두 간선만 갖는 격자 회로로 다시 작성했다. comparator는 masked value가
현재 maximum 이하인지 양방향으로 강제하므로 rejected-word 부등식도 정확하다. 첫
Discovery와 prelude를 누적 rejection 평균의 1σ checkpoint corridor 및 공개 shuffle
prefix tuple에 묶은 모델은 변수 2,808,598개, CNF 11,698,872개였고 300 CPU초 내
`UNKNOWN`이었다. 3.5σ 단일 모델은 약 15 GiB까지 증가해 운영 프로세스를 보호하기 위해
중단했다. 따라서 넓은 corridor는 겹치는 bounded shard로 분할해야 한다.

repeated-unique 정수 방식은 범위 밖 masked value뿐 아니라 이미 선택한 seed와 같은 정상
범위 값도 다시 뽑는다. 이 duplicate retry를 격자의 `valid` 조건에 추가한 뒤 합성 stream
회귀 테스트를 통과시켰다. 개선된 정수 방식 1라운드 5σ corridor는 변수 754,098개,
CNF 3,552,013개로 26.4초에 SAT였다. 이는 계열과 관측이 양립한다는 뜻일 뿐이며 witness가
하한 34 skipped-word 경로를 택했고 MT 상태나 다음 seed는 유일하지 않으므로 생성기
복구로 세지 않는다. 2라운드 이상은 동일 정확성을 유지하되 prefix tuple shard와 메모리
상한을 함께 사용해야 한다.

그 방식으로 첫 라운드 전체 공개 permutation과 두 번째 라운드의 7,334개 상관 prefix
tuple을 2라운드 3.5σ exact lattice에 함께 넣었다. 변수 1,192,189개, CNF
5,313,436개였고 168.1초 뒤 `UNKNOWN`이었다. 최대 RSS는 약 7.7 GiB로 설정한 12 GiB
연구 프로세스 상한 안에 머물렀다. 이는 UNSAT도 SAT도 아니므로 계열 기각이나 후보
승격에 사용하지 않으며, 다음 solver 시도는 cumulative-rejection corridor를 겹치는
좁은 shard로 나눠야 한다.

MT 출력 비트를 최초 상태의 긴 XOR 식으로 전개하던 dense 방식 대신, in-place twist와
temper를 작은 지역 XOR gate로 직접 연결하는 sparse 회로도 구현했다. twist 경계를 넘는
합성 출력이 dense 모델과 비트 단위로 같음을 검증했다. 동일한 2라운드 3.5σ 문제에서
최대 RSS는 약 7.7 GiB에서 5.3 GiB로 줄었지만 solver는 563.8초 후에도 `UNKNOWN`이었다.
따라서 sparse는 더 깊은 라운드의 메모리 기반을 제공하지만 현재 형태로는 solve-time
개선이 아니며, 다음 단계는 dense/sparse hybrid와 rejection corridor shard 병렬화다.

exact lattice에는 CNF를 한 번만 만든 뒤 CryptoMiniSat assumption으로 누적 rejection
상태를 분할하는 실행기도 추가했다. 2라운드에서 최종 checkpoint의 확률상 상위 15개
상태를 각각 10초씩 검사한 결과는 전부 `UNKNOWN`이었다. 첫 라운드 종료와 둘째 라운드
종료를 동시에 고정한 확률상 상위 30개 profile도 각각 5초 안에는 전부 `UNKNOWN`이었다.
이는 어떤 profile도 기각하지 않으며, endpoint만 고정한 채 11,760개 조합을 맹목적으로
확장하는 것은 타당하지 않다는 계산 증거다. 다음 solver 단계는 shuffle 회로 자체를
축소하거나 더 강한 중간 정렬 정보를 넣어야 한다. 분할 탐색은 모든 reachable state를
끝까지 UNSAT로 판정했을 때만 계열을 기각하고, timeout은 항상 UNKNOWN으로 보존한다.

공개 소스 복구도 upstream, 두 공개 fork, GitHub commit/PR, Sourcegraph를 대상으로
`Generated seeds:` 패치를 검색했으나 공개본은 없었다. W&B metadata가 가리키는 실행
commit도 upstream `9d9347a7ffab85a04eda6c36b9e87c59c8bb4049`이다. 따라서 현재 seed
로그 동작은 별도 공개 revision이 아니라 validator 작업트리의 미커밋 배포 변경이다.

전체 permutation network를 줄이기 위해 공개 로그에서 singleton UID domain만 선택해
각 최종 위치를 Fisher--Yates swap의 역방향으로 추적하는 별도 회로도 구현했다. 누락된
로그 행은 모든 UID가 공유하므로 각 관측 위치의 shift를 독립 선택하지 않고 관측 순서에
따라 단조 증가하도록 묶었다. 합성 incomplete shuffle 회귀 테스트를 포함한 MT 테스트
16개가 통과했다. 4라운드에서 task당 16개 singleton(총 64개)을 exact lattice와 결합한
모델은 약 1.2 GiB로 동작했지만 90초 제한 뒤 `UNKNOWN`이었고, task당 32개(총 128개)는
변수 2,814,441개, CNF 11,963,886개로 210.6초 뒤에도 `UNKNOWN`이었다. rejected-word
부등식을 풀어 준 jump 모델도 `UNKNOWN`이어서 부등식 자체가 주병목은 아니다. 따라서
trace 수를 무작정 늘리는 대신 공통 누락위치와 shuffle token 전이를 더 직접 공유하는
회로 또는 다른 solver 계열이 필요하다.

Discovery label 경계와 공개 입력 경계도 분리했다. 동일 validator 프로세스에서 확보된
shuffle 행은 현재 32개이며, 뒤쪽 행은 seed label을 전혀 읽지 않고도 채점 전 공개된
순서 제약으로 사용할 수 있다. `--include-sealed-shuffles`는 이 행들의 seed draw를 unknown
으로 유지하고 shuffle만 모델링하며 excluded-row 검증과의 혼용을 금지한다. 첫 20-task
실행은 16개 Discovery의 48개 label과 4개 sealed shuffle-only 행을 결합했고 sealed seed
label은 0개 열었다. sparse MT + exact lattice + task당 singleton 4개 모델은 변수
23,838,508개, CNF 94,072,647개, XOR 688,016개였으며 build 126.7초, solve 63.6초 뒤
`UNKNOWN`이었다. 최대 RSS는 약 9.3 GiB였다. 이 결과는 20-task 확장이 작동함을 보이지만
80개 UID trace만으로는 상태복원 정보가 부족하고 unary rejection lattice가 메모리의
주병목임을 보여준다. 다음 구현 우선순위는 누적 rejection 상태의 binary/segmented
counter 압축이다.

기존 correlated Fisher--Yates tuple의 깊이 4 제한도 재검사했다. 깊이 5, task당 상태
상한 100,000으로 32개 공개 shuffle을 모두 다시 열거한 결과 4개 task만 깊이 5를 완전
통과했고, 나머지는 깊이 2~4에서 상한에 도달했다. 총 tuple 수는 1,150,071개, 산출물은
약 81.7 MB였다. 깊이 8·상한 500,000은 첫 행부터 2분 30초 이상 소비해 중단했다. 쉬운
4개 task에서 choice 한 개씩을 더 얻는 대신 tuple 수와 trie가 수배 커지므로, 이 경로도
그대로 확장하는 것은 비효율적이다. 깊이 5 결과는 보존하되 대표 solver 입력은 작은
깊이 4 corpus를 유지한다.

unary rejection lattice를 제거하기 위해 5,163개 bounded draw의 masked-rejection 법칙을
10,000회 모의하고, 41개 checkpoint에서 폭 80인 corridor 4개를 greedy cover로 골랐다.
경험적 union coverage는 98.75%이며 이는 확률 추정이지 완전성 증명이 아니다. 각 corridor는
checkpoint 범위뿐 아니라 선택된 대표 경로의 draw별 rejection gap 5,163개와 SHA-256을
보존한다. 대표 경로를 고정하면 accepted/rejected raw 위치를 선택하는 변수가 사라지고,
모든 범위 밖 값과 repeated-unique 중복 retry를 정확하게 강제할 수 있다.

20-task sparse-event over-approximation은 16개 Discovery의 48개 label과 4개 sealed shuffle을
108개 관측 event로 줄였다. corridor 1에서 변수 947,979개, CNF 2,686,407개,
XOR 482,816개였지만 184.1초 뒤 `UNKNOWN`이었다. prefix를 모두 빼도 `UNKNOWN`이어서
병목은 tuple trie가 아니라 가변 raw-index 선택과 XOR core의 결합이었다. 이에 exact
rejection profile solver와 concrete replay CEGAR를 추가했다. Holdout/sealed label은 열지
않고 상태와 raw stream도 artifact에 저장하지 않는다.

첫 Discovery task에서는 corridor 1의 exact path, seed 3개와 공개 shuffle 251개 위치를
full Fisher--Yates network로 묶은 모델이 6.97초에 `SAT`였고 전체 포함행을 재생했다. 그러나
제외한 두 번째 Discovery seed에서 즉시 mismatch였으므로 후보로 승격하지 않았다.
corridor 2·3·4 대표 경로도 각각 첫 행은 완전 적합했지만 같은 제외행 seed를 예측하지
못했다. 이는 한 행 적합이 rejection 경로의 증거가 아니라 19,937-bit MT 상태의
과소결정임을 확인한다.

거짓 모델을 제거하는 CEGAR는 singleton trace를 45개까지 누적했지만 첫 행 전체 replay를
통과하지 못했다. 관측 위치의 하한보다 작은 reverse swap은 해당 UID를 건드릴 수 없으므로
정확히 생략하고, 여러 반례 중 가장 뒤쪽 위치를 선택하도록 회로를 줄였다. 둘째 행에서는
기존 위치 1 대신 위치 190을 골라 66개 swap만 추적했지만 제한 안에 새 모델을 얻지 못했다.
현재 choice-bit slice 한 조합만 금지하는 보조회로 없는 CEGAR도 같은 재탐색 병목을
보였다. timeout은 모두 `UNKNOWN`이며 corridor를 기각하지 않는다.

셔플 trace보다 먼저 seed label이 상태 자유도를 줄이는지 보기 위해 첫 행 full network의
solver에 후속 라운드의 exact rejection path와 seed 3개만 순차 추가하는 증분 solver도
구현했다. MT CNF stream은 필요한 raw 위치까지만 확장하되, 첫 solve 전에 2라운드까지
prebuild하는 구성이 가장 안정적이었다. corridor 1에서 첫 행은 279.71초 `SAT`, 둘째 행의
seed/rejection 추가는 같은 solver의 학습 절을 유지해 5.05초 `SAT`였다. task seed 6개와
restart prelude 3개를 concrete replay로 확인했고 첫 공개 shuffle은 계속 통과했다.
세 번째 행 추가는 450초 뒤 `UNKNOWN`이므로 현재 확정 경계는 **2라운드 seed 연속성 SAT,
3라운드 UNKNOWN**이다. 이는 생성기 복구나 제외행 예측이 아니며 다음 단계는 이 경계에서
MT state 자유도/rank를 계측하거나 다른 XOR solver로 세 번째 증분을 분할하는 것이다.

또 다른 독립 분기로 초기 상태 전체가 아니라 명시적 32비트 initializer를 사용했을
가능성을 완전탐색한다. C++ 탐색기는 `RandomState(uint32)`의 MT19937 초기화와 NumPy
`rk_interval`을 재현하여 `prelude → shuffle(256) → 첫 Discovery triplet`을 검사한다.
별도 탐색기는 CPython 3.12의 `init_by_array(uint32)`와 top-10 `getrandbits` rejection을
재현하여 연속 Python triplet을 검사한다. 두 구현은 여러 알려진 seed에서 실제 NumPy 및
Python 출력과 일치함을 교차검증했다. 이 검사는 일부 시간창이 아니라 각 엔진의
`0..2^32-1` 전체 initializer 공간을 닫는다.

전수검사 결과 NumPy에는 전주곡만 맞는 initializer 7개, CPython에는 5개가 남았지만
그 직후의 첫 Discovery 호출까지 맞는 값은 각각 0개였다. 전주곡과 첫 타스크 사이에
관측하지 못한 RNG 소비가 있었다는 반론도 닫기 위해 이 12개 initializer 각각에서
0..10,000,000 raw-word 간격을 추가로 검사했다. NumPy는 각 간격 뒤의 첫 공개 shuffle
상관 tuple을 거쳐 실제 첫 seed까지, CPython은 각 간격 뒤의 첫 seed triplet까지 정확히
재생했으며 둘 다 exact 후보가 0개였다. 따라서 명시적 uint32 초기화 후 같은 전역
stream을 썼다는 두 계열은 이 넓은 숨은-consumption 경계 안에서 기각된다.

전주곡이 다른 RNG에서 나왔거나 배포 시점에 별도 재초기화됐을 가능성은 독립 분기로
검사한다. 첫 Discovery 공개 shuffle의 첫 네 Fisher--Yates choice에 허용되는 정확한
상관 tuple 1,426개를 AVX2 사전필터로 사용하고, 통과한 seed만 전체 공개 permutation과
첫 seed triplet으로 검증한다. seed API는 NumPy의 repeated bounded integers와
`choice(..., replace=False)`를 모두 포함한다. CPython은 첫 Discovery triplet부터 직접
시작하여 `sample/randrange`의 top-10 방식과 `random()/choices`의 53-bit float 방식을
한 번의 uint32 전수검사에서 함께 평가한다.

이 재초기화 분기도 전수검사를 마쳤다. NumPy는 첫 네 choice tuple만 맞는 initializer가
1,481개였지만 공개 전체 shuffle 부분순서까지 맞는 값이 0개여서 두 seed API 모두
검사 전에 탈락했다. CPython은 첫 Discovery triplet만 맞는 top-10 initializer 9개와
float initializer 5개가 있었으나 바로 다음 triplet까지 연속 일치한 값은 0개였다.
각 후보에서 다음 triplet 전 숨은 raw-word 간격 `0..10,000,000`도 검사했다. float는
0개였고 top-10에서 `seed=3538549647`, gap `9,923,142` 하나가 우연히 두 번째 task까지
맞았다. 같은 상태를 정확히 전진시켜 세 번째 Discovery triplet 전의 또 다른 천만
간격을 검사하자 일치가 0개였다. 따라서 이 값은 생성기가 아니라 1억4천만 회 규모
gap scan에서 기대 가능한 단일 전이 우연 적중으로 분류하며, 16개 겹침 Discovery 중
2개만 맞은 경로를 campaign exact로 올리지 않는다.

task 생성 블록과 validator의 finality-lag 메타그래프가 달랐을 가능성도 Discovery 20개 중
공개 shuffle과 겹치는 16개 라운드에서 조사했다. 생성 블록 상대 `-8..+2`를 모두 조회한
결과 11개 offset의 다중집합 일치 3,893개와 고유 UID 2,130개가 완전히 같았다. 이 구간에는
metagraph 변화가 없었으므로 block offset은 현재 누락/중복 라벨의 원인이 아니다.

seed가 validator가 아니라 backend/DB에서 생성될 가능성도 별도 상태복원 트랙으로 열었다.
PostgreSQL 공식 `pg_prng.c`의 128비트 xoroshiro128** 전이, `top-52 / 2^52` double 변환,
`random(100,999)`의 top-10 rejection sampler를 그대로 구현했다. Z3 strict continuous
stream은 double-floor가 2개 task까지 SAT 후 3번째 UNKNOWN, integer-range는 첫 task부터
UNKNOWN이었다. 독립 CNF/XOR 회로는 double-floor 실제 stream을 9개 출력까지 SAT로
진행한 뒤 10번째 UNKNOWN, integer-range는 16개 출력에서 UNKNOWN이었다. 합성 회로 테스트는
통과했으나 solver가 합성 상태도 완전히 복구하지 못했으므로 이 결과는 실제 계열의 기각이
아니다. connection pool이 task마다 세션을 바꾸거나 DB PRNG 외 생성기를 쓰는 경우에는
strict stream 가정 자체가 성립하지 않는다.

프로세스 시작 무렵의 공개 block material로 장기 PRNG를 단 한 번 초기화했을 가능성도
별도 계열로 검사했다. 캐시된 18,804개 공개 block 전체에 대해 raw/reversed hash,
hex 문자열, block number, SHA-256/SHA-512/BLAKE2/SHA3 파생값을 Python `random`,
NumPy `RandomState/default_rng`의 seed-only 및 `prelude → shuffle → seed` 지속 스트림에
넣었다. 총 11,846,490개 경로에서 공개 restart prelude `654,347,964` 자체를 맞춘
후보가 0개였다. 따라서 이 범위의 공개 block 단독 process initializer는 기각하지만,
OS entropy나 비공개 material 초기화까지 기각하는 결과는 아니다.

block-only PRNG 검색과 direct digest-to-triplet 검색 사이의 공백도 닫았다. 각 Discovery
task의 생성 block `+0..719`에서 block raw/hex/height와 task UUID/생성시각/계약을 양방향,
4개 구분자로 결합하고 SHA-256/SHA-512/BLAKE2 digest의 전체·앞/뒤 32비트 및 endian을
Python `random`, NumPy `RandomState/default_rng` initializer로 사용했다. 11,819,520개
경로 중 첫 Discovery triplet을 맞춘 후보부터 0개였고 따라서 20/20 후보도 0개다.
이 결과로 흔한 `hash(block || task) → PRNG` 라운드별 재시드 구현은 검사 범위에서
기각됐다. Holdout은 열지 않았다.

고정 rejection corridor 1의 MT19937 모델에는 공개 shuffle 반례를 한 위치씩 넣는
CEGAR도 적용했다. 첫 4개 Discovery의 seed 12개를 먼저 SAT로 만든 뒤 concrete replay가
어긴 첫 UID 위치만 추가하고 같은 solver의 learned clause를 유지했다. 180초 예산은
60개 반례까지 SAT 후 61번째에서 `UNKNOWN`이었고, 900초 예산은 **114개 반례까지 SAT**,
115번째에서 `UNKNOWN`이었다. 이어서 3,600초 deep single-session은 **237개 반례까지
SAT**였고 238번째에서 `UNKNOWN`이었다. 첫 공개 행 전체는 아직 통과하지 못했고 excluded
seed도 예측하지 않았으므로 생성기 후보가 아니다. 다만 `UNKNOWN`은 corridor 모순이
아니라 solver 예산 경계다. deep 실행에서는 위치쌍 반례마다 같은 singleton 위치를 다시
회로화하여 총 326 trace 중 고유 위치가 150개뿐인 중복도 확인했다. 이후 실행은 위치별
trace와 단조 위치쌍 제약을 캐시하여 learned clause를 유지하면서 중복 회로를 제거한다.

캐시 회귀검증 후 같은 4-round 모델에서 단일 반례 방식은 403.70초 동안 81개 고유
trace까지 SAT였고 다음 solve 도중 종료했다. 한 concrete model에서 확인되는 공개 범위
위반을 최대 8개씩 묶는 batch CEGAR도 추가했다. 590.06초 동안 13개 batch, 고유 trace
104개와 단조 위치쌍 91개가 SAT였으며 다음 solve는 미완료였다. 이는 종전 deep 실행의
동일 위치 재회로화 176건을 제거하지만 아직 첫 공개 행 전체 적합은 아니다. 첫 행의
누락 수가 5이므로 마지막 singleton shift `0..5`의 완전 구조 shard도 각각 검사했다.
각 shard는 짧은 60 CPU초 예산에서 모두 초기 solve `UNKNOWN`으로, 어느 shard도 SAT·UNSAT
판정을 내리지 못했다. 따라서 해당 결과로 corridor나 MT 가설을 기각하지 않는다.

첫 batch deep 실행은 18번째 solve에서 `UNKNOWN`이었다. 1,822.73초 동안 고유 trace
144개까지 누적했지만 공개행은 통과하지 못했고, 505,967변수/2,047,113절이었다. 분석 결과
범위 밖 UID 8개를 한 번에 추가할 때 그 위치들 사이의 단조 관계도 조기에 넣어 solve를
불필요하게 어렵게 만든 것을 확인했다. 반례를 `range`와 `monotone`으로 명시적으로
분류하여, range 단계에는 독립 exact trace만 넣고 실제 monotone 반례에서만 위치쌍 관계를
추가하도록 수정했다. 두 제약 모두 공개 Discovery 로그에서 유도되며 Holdout을 사용하지
않는다.

저위치 reverse trace의 비용 증가에는 적응형 batch를 적용했다. 위치 32 이상은 range
반례를 8개씩, 위치 31 이하는 한 개씩 추가했다. 이 실행은 1,914.95초에 고유 singleton
trace 150개와 단조 관계 187개를 모두 SAT로 만들었고, 종전 237-step 실행과 달리 중복
trace는 0개였다. 그 뒤에도 첫 공개행이 불일치했지만 singleton 반례가 더는 나오지 않은
이유는 남은 관측이 7개 다중-UID endpoint group(도메인 크기 4--24)이기 때문이다.

이에 group-domain CEGAR를 추가했다. 관측 위치 `p`의 그룹 토큰은 최종 위치 `p..p+5`
중 하나에서 해당 그룹 UID 집합의 원소로 역추적되어야 하며, 모든 관측 위치의 shift는
비감소해야 한다. 반복 그룹에도 `p+shift`가 엄격히 증가하므로 같은 UID를 두 번 쓰지
않고, 이 조건은 공개 observed sequence가 full shuffle의 subsequence라는 조건과 같다.
합성 반복-group permutation 회귀검증을 통과했다. 기존 66-step 제약을 재구성한 resume는
learned clause를 잃어 190.41초 후 `UNKNOWN`이었으므로, group CEGAR는 동일 solver 세션에서
singleton 단계부터 점진적으로 다시 실행한다.

첫 group-domain 장기 세션은 2시간 외부 제한까지 첫 공개행을 완전히 통과하지 못했다.
그러나 singleton/domain trace **215개**와 단조 관계 189개까지 같은 corridor 모델이 SAT였고,
다음 group 위치 97을 추가한 solve 도중 watchdog이 종료했으므로 UNSAT 증거는 아니다.
구성은 singleton range 38단계, singleton monotone 33단계, domain range 65단계였다. 그룹
trace를 한 개씩 추가하는 후반부가 병목이어서, group 위치 64 초과는 4개씩 묶고 그 이하는
한 개씩 추가하는 적응형 domain batch를 별도 세션으로 병렬화했다. 각 세션은 독립 artifact를
쓰며 Holdout label, MT state, raw stream은 저장하지 않는다.

초기 적응형 domain batch는 위치 64 초과를 4개씩 묶었다. 6-thread 실행은 5,585.75초 동안
고유 trace 210개까지 SAT였고, 다음 저위치 group batch를 추가한 214-trace solve는
`UNKNOWN`이었다. 공개행 통과는 0이고 corridor 반증도 아니다. 병목 batch가
`[118,105,103,99]`처럼 긴 reverse trace 네 개를 동시에 포함했으므로 group 저위치
임계값을 128로 올렸다. 새 실행은 128 초과만 4개 batch로 처리하고 128 이하는 한 개씩
추가하여 learned clause를 유지한다.

구버전 DB 계열도 빠뜨리지 않았다. PostgreSQL `erand48`의 48비트 상태와 MySQL
`RAND()`의 두 30비트 상태를 초기 seed brute force가 아니라 직접 상태 제약으로 풀었다.
두 모델 모두 현행 20개 Discovery의 60개 seed strict stream에서 UNSAT였으므로, 기존
Java/ANSI/MSVC/Park--Miller와 함께 8개 소형 상태 계열이 기각됐다. 이 결론은 DB connection
pool별 독립 상태나 task 사이 숨은 draw까지 기각하지는 않는다.

공개 Swagger는 `/api/v3/data/hbb-mutations` GET을 문서화하지만 실제 route는 별도 서비스
Authorization을 요구한다. W&B key는 그 권한이 아니므로 호출 우회나 credential 재사용은
하지 않았다. 따라서 mutation universe와 반환 순서를 알 수 없는 상태에서 계약 mutation을
임의 인덱스로 바꿔 PRNG 제약에 넣지 않는다.

20개 Discovery seed label 자체의 식별 가능성도 별도 gate로 계산했다. 순서 있는 서로 다른
`100..999` 세 값의 정보 상한은 task당 약 29.4365비트이고 20개 합계는 588.730603비트다.
seed label만으로 Python/NumPy MT19937의 유효 상태 19,937비트를 복구하려면 최소
19,348.269397비트가 더 부족하다. 그러므로 MT 계열은 공개 shuffle 결합 없이는 원리상
유일 예측기가 될 수 없고, OS CSPRNG나 keyed PRF는 sample만으로 복구 대상이 아니다.

프로세스 첫 validation에서는 `/tasks/seed` 갱신이 HTTP 500으로 거절됐다는 W&B 경고와
곧이어 `Generated seeds: 654,347,964` 로그가 함께 남았다. 공개 API 감사에서도 이 route가
실제로 존재하고 `PUT, OPTIONS`만 허용함을 확인했다. OpenAPI의 request body는 validator가
`task_id`와 `seed` 문자열을 함께 보내도록 요구한다. 이후 로그의 순서는 매 라운드
`final submissions upload → seed update → Generated seeds`다. 따라서 현재 값은 backend가
PUT 응답에서 새로 골라 주는 것이 아니라 validator가 먼저 생성해 전달하는 구조다.
W&B run은 공식 commit `9d9347a`를 가리키지만 해당 commit의 block-hash 구현과 실제 로그가
다르므로 배포 working tree에 미공개 patch가 존재한다. W&B run files에는 requirements와
metadata만 있고 source/code artifact는 없어 정확한 `choice`/`randint` 호출은 직접 읽지
못했다.

공식 `9d9347a`의 block-hash 공식(`round_start+430..432`, SHA-256, 범위 0..1000)도
정확히 재실행했다. 이 공식은 2026-09-23~25의 짧은 과거 구간에는 맞지만 현 Discovery
20개의 60개 위치에서는 일치가 0개다. 따라서 현재 epoch의 생성기로 사용할 수 없다.
반면 작은 정수·프로세스 clock 초기화는 위 전수검사에서 모두 탈락했으므로 남은 상태복원
가설은 OS entropy로 초기화된 장기 상태 또는 아직 관측하지 못한 draw 순서다.

이전의 단일 wide seed, 단일 narrow seed, 계약 기반 3중 seed, block-hash rollout 및
`9232,9168,6832` wide triplet은 학습/holdout에서 모두 제외한다. 이 자료는 변경점 분류,
과거 구현 계열 식별, 과거 체제 재등장 탐지에만 사용한다. 기존 363개 cross-history
direct-map 결과도 현행 생성기 기각 증거가 아니라 격리된 역사 baseline으로 취급한다.

시작점 이후 labeled task에서 seed 개수, 범위 또는 distinct 조건이 한 번이라도 달라지면
collector와 laboratory는 fail-closed로 prediction gate를 닫는다. 그 뒤 값이 다시 같은
형태로 돌아오더라도 자동으로 현재 epoch에 이어 붙이지 않는다. 새 변경점을 확인하고 새
epoch ID를 명시적으로 설정해야 discovery/holdout을 처음부터 다시 시작한다. 공개 순서의
변화는 seed-signal 관측기가 별도로 판정하며, 외형이 같은 `3 × 100..999`라고 해서 내부
PRNG 동일성을 가정하지 않는다.

## 신뢰 경계

허용 입력은 공개 task/score API, 공개 finalized chain header, 공개 W&B 자료, 정상 miner가
자기 요청으로 받은 signed contract, 자기 artifact와 자기 프로세스 로그다. API key는
운영자가 명시적으로 제공한 범위의 read-only 호출에만 사용할 수 있으며 report에는 URL,
header, credential, 전체 score row를 저장하지 않는다.

validator 메모리, admin endpoint, 제3자 트래픽, 재사용한 signature, 유출 credential,
접근제어 우회는 두 트랙 모두의 입력으로 인정하지 않는다. 이런 신호는 정확하더라도
`authorized=false`이므로 결합 gate를 열 수 없다.

## Track A 상태기계

```text
COLLECT_PUBLIC_INPUTS
  -> FIT_DISCOVERY
  -> VERIFY_HOLDOUT
  -> RECORD_PROSPECTIVE_PREDICTION
  -> RESOLVE_AFTER_PUBLICATION
  -> 5_CONSECUTIVE_EXACT
```

기존 363개 모델은 UUID, created time, contract 등을 단일 `Random` 또는 digest에 넣는
cross-history stateless direct-map 계열이다. 이 결과는 `quarantined/historical-baseline`으로
동결하고 현행 epoch의 기각 근거로 사용하지 않는다. 현행 epoch만으로 별도 실행하는 공개
chain/task 결합 registry의 결과와 prospective 검증만 Track A 승인에 사용한다.

새 모델군은 다음 순서로 검사한다.

- task 생성·score 시각에 정렬한 공개 block window와 task/contract 결합 digest
- round start/seed/validation/weight block 및 finalized head entropy
- 이전 출력에 의존하는 상태형 수열과 worker 혼합 진단
- mutation/cell 선택과 seed가 같은 RNG stream에서 나온다는 공동 호출순서 가설

2026-09-29에는 공개 `BlockInfo.header`를 과거 현행-epoch task의 정확한 블록 높이로
backfill하여 hash 기반 320개와 header 기반 320개를 모두 평가했다. 640개 모두 Discovery
20개 task에서 입력 누락 없이 실행됐지만 ordered/unordered exact triplet은 한 task에서도
나오지 않았다. 따라서 이 640개 공개 block/task 직접 digest 계열은 현행 epoch에서
기각하며, 다음 탐색은 persistent process state와 task metadata 공동 RNG 호출순서 계열로
이동한다.

모델 이름, 입력 가용시각, 직렬화, hash/PRNG, 출력 변환을 registry에 명시한다. 생성 후
알게 된 seed를 입력에 포함한 모델은 즉시 data leakage로 기각한다. 모델 수를 늘릴 때마다
미래 prospective set을 새로 시작하여 다중검정으로 생긴 우연한 적중을 승인하지 않는다.

### RNG 출처·결합 판정 수정

공개 `forward.py`에서 확인되는 셔플 생산자는 validator 프로세스의
`np.random.shuffle(miner_uids)`다. 현행 seed는 같은 W&B validator run의
`Generated seeds` 로그와 `/tasks/seed` PUT에서 관측되므로 비공개 validator patch가
생성·기록한다는 증거가 강하다. 그러나 이 두 사실만으로 patch가 NumPy 전역
`RandomState`를 재사용했다고 결론낼 수는 없다. patch가 Python `random.sample`,
`secrets`, 별도 `default_rng` 또는 라운드별 재시드를 썼다면 공개 셔플로 seed 상태를
복구할 수 없다. 대시보드는 이를 다음처럼 분리한다.

- same validator process evidence: `true`
- exact seed RNG API: `unobserved`
- same RNG state coupling: `unverified`

따라서 shuffle→seed MT 복구는 폐기하지 않되 결합을 미래 label로 입증하기 전까지
후보 생성기로 승격하지 않는다. 특히 rejection corridor의 empirical coverage는
누적 rejection 수가 band 안에 들 확률이지 solver가 고른 **단일 exact rejection path**가
실제 경로일 확률이 아니다. 대표 path에서 SAT가 나와도 임의의 과소결정 MT 상태가 존재한다는
뜻일 뿐이다. 이 경로의 2라운드 SAT·3라운드 UNKNOWN을 생성기 적합도나 반증으로 세지 않는다.

### 라운드별 uint32 재시드 역상 트랙

장기 stream 검색과 별개로 비공개 patch가 매 라운드 `random.seed(x)`를 호출하는 경우를
검사한다. AVX2 scanner는 고정 Discovery 20개 각각에 대해 CPython 3.12의 첫
`sample(range(100,1000),3)` 및 float-triplet을 만드는 모든 uint32 initializer를
`0..2^32-1` 한 번의 scan으로 동시에 수집한다. 결과 분석기는 각 라운드의 작은 candidate
set을 task 생성시각, validation 시작시각, seed log 시각, 생성 block 및 round index와
결합하여 다음 두 관계를 전 라운드에서 검사한다.

```text
initializer = public_source + constant (mod 2^32)
initializer = public_source XOR constant
```

같은 상수가 모든 관측 가능한 Discovery 라운드에서 후보 하나를 선택할 때만 생성기 후보로
승격한다. 일부 라운드 적중이나 라운드별로 다른 상수는 다중검정 우연으로 취급한다. 이
완전탐색은 uint32 initializer와 첫 출력만 닫으며, 32비트보다 큰 정수·문자열·bytes seed,
OS entropy 및 triplet 전 숨은 draw는 기각하지 않는다. Holdout 5개 label은 이 단계에서
열지 않는다.

관계식 입력에는 task/validation/seed-log 시각의 초·밀리초·마이크로초, 생성 block,
round index, UUID word/XOR/sum/CRC/Adler/SHA 조각 및 생성 block hash 앞뒤 32비트를 넣는다.
고정 offset과 XOR뿐 아니라 `A*x+C mod 2^32`도 첫 두 라운드 역상 후보에서 열거한 뒤 나머지
모든 라운드로 검증한다. 별도로 공개 W&B metadata의 host, writer ID, 실행경로, 시작시각과
그 조합에서 만든 Python/NumPy persistent initializer 4,576개를 검사했지만 restart prelude
첫 triplet부터 맞춘 후보가 0개였다. metadata 원문과 process args는 artifact에 저장하지
않았다.

CPython 3.12 uint32 전체 공간의 라운드별 역상 결과는 sample/randrange-unique 101개와
float-triplet 124개였고 두 방식 모두 20개 task에 적어도 하나의 역상이 있었다. 그러나
위 public source들의 고정 offset/XOR/affine 관계는 20/20 후보가 0개였으며, 고정 상수의
최대 지지도도 1개 task뿐이었다. 별도 validation-start clock search는 W&B에서 시각을
확정할 수 있는 16개 task에 초 ±120, 밀리초 ±5,000, 마이크로초 및 float-second
±250,000µs를 각각 sample/randrange로 검사한 32,327,808개 초기화에서 적중 task가 0개였다.
따라서 공개 source에서 단순하게 만든 라운드별 Python uint32 seed와 validation clock
재시드 계열은 기각한다.

NumPy `RandomState(uint32)`의 first `randint` repeated-unique도 `0..2^32-1` 전체를
검사했다. 20개 task의 exact 역상은 총 141개(task당 4..14개)였지만 같은 public source
offset/XOR/affine 관계는 0개였고 최대 고정관계 지지도는 1개 task였다. validation
nanosecond를 uint32로 절단한 ±5ms 창도 0/16이었다. 따라서 legacy NumPy의 단순 라운드별
uint32 재시드 계열도 기각한다. `choice(..., replace=False)`와 OS-entropy initial state는
이 전수검사의 범위가 아니다.

별도 `Generator` 계열도 NumPy 2.5.1의 공개 `SeedSequence`, PCG64/PCG64DXSM,
SFC64 및 Philox 초기화,
32-bit buffer, Lemire bounded mapping을 C++로 동일 구현하여 회귀 검증했다. validator
재시작 직후 공개된 prelude `654,347,964`를 첫 출력으로 두고 이후 16개 Discovery
triplet을 한 stream에서 연속 재생하는 조건으로 각 엔진의 `integers` repeated-unique,
`choice(900, 3, replace=False)`, `random()` float→range repeated-unique를 각각 모든
uint32 initializer에 대해 검사했다. integer/choice의 첫
triplet 역상은 PCG64 integers 3개, PCG64 choice 6개, PCG64DXSM integers 7개,
PCG64DXSM choice 4개, SFC64 integers 5개, SFC64 choice 7개, Philox integers 6개,
Philox choice 2개였다. float 방식의 첫 역상은 각각 PCG64 9개, PCG64DXSM 6개,
SFC64 4개, Philox 7개였다. 그러나 다음 Discovery까지 이어진 후보는 12개 계열 모두
0개였다. 따라서 숨은 draw가 없는 persistent `Generator(uint32)` 12개 계열은 기각한다.
이 결과는
OS에서 얻은 128-bit entropy, uint32보다 넓은 initializer 또는 라운드 사이 숨은 draw를
기각하지 않는다. 12개 scan은 합계 51,539,607,552 initializer를 완전검사했고 Holdout
label은 열지 않았다.

숨은 draw 한계를 별도로 넓히기 위해 첫 prelude 역상을 통과한 integers initializer
21개, choice initializer 19개, float initializer 26개만 남긴 뒤, 모든 라운드 사이에
같은 수의 hidden draw가 끼어드는 가설을 `0..100,000,000`에서 검사했다. integers는
bounded call 수, choice는 NumPy `next_uint32` raw word 수, float는 `random()` call 수를
gap 단위로 삼았고, choice 쪽은 Floyd 세 draw와 후속 shuffle 두 draw의 Lemire rejection까지
정확히 재생했다. 합성 gap 7/11/13 회귀 테스트를 각각 통과했지만 실제 integers
2,100,000,021경로, choice 1,900,000,019경로, float 2,600,000,026경로 모두 Discovery
exact 후보가 0개였다. 따라서 이 uint32 초기화 후보들의 고정 호출간격 변형도 1억 범위
안에서 기각한다.

shared NumPy 상태의 셔플 제약을 더 깊게 만들기 위해 correlated prefix path에서 현재
sparse permutation과 남은 suffix/multiset이 같은 상태를 DAG로 병합하는 방법도 시험했다.
누락이 5개뿐인 최상 라운드에서 깊이별 고유 상태가 `6, 31, 157, 1,426, 18,876`으로
늘었고 6번째 accepted choice에서 100,000개 상한을 넘었다. transition 수와 상태 수가
같아 실질적인 path 병합도 없었다. 따라서 full-path DAG는 tuple 폭발을 줄이지 못하며,
향후 shared-state solver는 개별 UID trace를 MT choice 변수에 공유하는 factorized encoding을
사용해야 한다.

singleton UID 역추적 비용도 별도로 줄였다. 관측 위치 `p`의 UID를 Fisher--Yates 이전
위치로 되감으려면 swap `p..255`만 보면 되므로, 균등 표본 대신 각 라운드의 가장 늦은
singleton들을 선택하는 exact encoding을 추가했다. 합성 permutation에서 기존 spread
방식과 같은 정답을 허용하면서 변수·절 수가 더 작음을 회귀검증했다. 실제 20라운드
고정 corridor 1에서 라운드당 8개(총 160개) late trace는 649,182변수/1,048,460절,
라운드당 1개(총 20개)는 562,080변수/538,012절로 줄었다. 각각 180 CPU초와 720
CPU초에서 모두 `UNKNOWN`이었으므로 MT 공통상태 가설이나 해당 rejection profile을
기각하지 않는다. 이는 단순 wall-time 확대보다 rejection-path/trace 분할과 더 나은
factorized encoding이 필요하다는 계산 병목 증거다.

fixed-profile 회로의 accepted unknown word에는 이미 `validity(raw_index)`가 참이라는 compact
comparator가 있었는데, 모든 invalid 값을 다시 열거하는 중복 제약도 붙어 있었다. 이를
제거해 20라운드 labels-only corridor 1의 CNF 절을 487,990개에서 270,602개로 줄였고
합성 경계 회귀검증을 통과했다. 한 번에 푸는 8라운드 이상은 여전히 `UNKNOWN`이었지만,
초기 4라운드를 함께 바인딩한 뒤 같은 solver에 한 라운드씩 추가하면 4→5→6→7이 모두
SAT였다. solve 시간은 각각 31.04초, 3.84초, 18.94초, 633.53초였고 마지막 checkpoint는
Discovery seed triplet 21개를 바인딩했다. 이 결과는 rejection corridor 1과 seed label의
호환성만 보이며 공개 shuffle replay는 0라운드이므로 생성기 후보로 승격하지 않는다.

별도로 첫 라운드 공개 shuffle의 correlated prefix tuple 1,426개만 exact trie로 묶은
경량 경로는 첫 라운드 0.48초, 두 번째 seed 라운드 추가 234.23초에 SAT였다. 세 번째
라운드는 외부 600초 watchdog 안에 종결되지 않았다. Holdout seed label은 두 경로 모두
열지 않았고, solver state/raw stream도 artifact에 저장하지 않았다.

fixed rejection path의 모든 rejected-word 부등식을 처음부터 넣는 방식과 독립적으로,
accepted word와 seed/shuffle 제약을 먼저 풀고 concrete MT replay에서 실제로 accept되어
버리는 rejected word만 clause로 추가하는 lazy rejection CEGAR를 구현했다. 이 모드는
현재 모델이 선택한 exact corridor의 모든 rejected word를 concrete oracle로 재검사해
위반이 0개가 된 경우에만 `rejection_profile_certified=true`로 표시한다. 따라서 단순
relaxation과 달리 인증이 끝난 모델은 기존 eager encoding과 같은 exact rejection 조건을
만족하며, 인증 전 `UNKNOWN`이나 SAT witness는 후보로 승격할 수 없다. 실제 첫 Discovery
라운드 smoke test에서는 97개 위반을 한 batch로 추가한 뒤 0.61초에 SAT 및 exact-path
인증을 마쳤다. 이는 생성기 적중이 아니라 다음 독립 solver 경로가 정상 작동한다는
검증이며, Holdout label·MT state·raw stream은 저장하지 않았다.

저위치 group trace에서 한 관측의 최종 위치 `p..p+5`를 한 번에 선택하는 SAT 병목을
분할하기 위해 repeatable `--fixed-trace-shift ROUND:POSITION:SHIFT` shard도 추가했다.
각 shift는 disjoint하고 가능한 shift 전부의 합집합이 원래 탐색공간과 같으므로 누락 없이
순차 실행할 수 있다. checkpoint resume 시에도 이미 추가한 trace와 고정 shift를 함께
복원하며, 범위 밖 라운드·위치·shift 및 같은 위치의 상충 지정은 fail-closed 처리한다.
`preseed_mt_shift_shard_supervisor.py`는 같은 checkpoint에서 각 shift를 독립적으로
순차 실행하고 shard별 terminal/SAT/UNSAT/UNKNOWN 및 promotion gate를 원자적 aggregate에
기록한다. artifact가 없거나 timeout인 shard는 절대로 기각으로 세지 않으며, 모든 shard가
`UNSAT`인 경우에만 해당 exact profile의 완전 기각으로 표시한다. 모든 artifact가 단순히
terminal인 것과 모든 shard가 conclusive `UNSAT`인 것은 별도 지표로 유지한다.

초기 구현에서는 resume checkpoint에 아직 없던 fixed position이 첫 solve 뒤 CEGAR에서만
추가될 수 있어, 첫 solve가 `UNKNOWN`이면 해당 shift를 실제로 검사하지 않은 artifact가
남는 결함이 있었다. `pos67_shift0` 구 산출물이 이 경우이므로 증거에서 폐기했다. 현재는
모든 fixed trace를 첫 solver 호출 전에 강제로 회로에 넣고, 이미 결합된 관측 trace들과
shift 비감소 제약도 함께 복원한다. supervisor 역시 요청한 round/position/shift mapping과
`fixed_trace_positions_prebound >= 1`을 모두 확인한 shard만 `exact_shift_certified`로 세며,
모든 shard가 이 인증을 받고 `UNSAT`일 때만 exact profile을 기각한다.
수정 뒤 position 67의 shift 0과 1은 각각 fixed position 1개와 기존 trace 사이 단조관계
125개를 첫 solve 전에 결합했지만 502.98초와 434.64초 뒤 모두 `UNKNOWN`이었다. 두 shift는
살아 있으며, 나머지 네 shift는 같은 저정보 representative exact path에 시간을 반복하지
않고 choice seed-event CEGAR로 우선순위를 옮기기 위해 중단했다.

private validator patch가 NumPy 전역 상태에서 `choice(range(100,1000), 3,
replace=False)`를 썼을 가능성도 fixed-profile 상태복원에 추가했다. 이 API는 공개 seed
하나당 단순 bounded draw 세 번이 아니라 900개 배열의 전체 Fisher--Yates permutation을
소비하므로, 별도의 `choice-without-replacement` draw layout과 concrete seed-prefix replay를
사용한다. 4개 Discovery용 5,515 bounded draw corridor 4개를 2,000 Monte Carlo path에서
생성했고 checkpoint-band union coverage는 96.5%였다. 단, 첫 실제 1라운드 smoke는 기존
주 solver와 병행한 120초 외부 상한 안에 artifact를 만들지 못했다. 이는 가설 기각이
아니며, 주 실행 종료 뒤 seed-event prefix를 단계적으로 바인딩하는 독립 실행 대상으로
남긴다.

choice fixed-profile 계산은 restart prelude seed-prefix를 최초 회로에 반드시 결합하고,
후속 Discovery seed 이벤트는 concrete replay가 처음 실패한 이벤트부터 하나씩 추가하는
CEGAR 모드도 갖는다. prelude조차 없는 임의 MT 모델의 rejection path를 먼저 인증하는
낭비를 피하면서도 최종 후보는 모든 학습 seed 이벤트를 정확히 만족해야 한다. rejection
부등식 역시 현재 모델에서 위반한 raw word를 반복 추가하여 0개가 된 뒤에만 exact path로
인증한다.
또한 lazy mode에서 후속 Discovery draw를 미리 회로화하는 비용을 피하도록 `rounds=0`을
허용했다. 이 단계는 restart prelude의 899개 bounded draw와 세 prefix 값만 fit하고, 첫
Discovery의 shuffle+seed 전체를 excluded prediction으로 남긴다. 여기서 얻은 상태는 매우
과소결정이므로 excluded row를 우연히 통과하지 않는 한 후보가 아니지만, prelude 회로 자체와
후속 draw 회로의 계산 병목을 분리할 수 있다.

W&B run의 공개 `requirements.txt`와 metadata에서 실제 프로세스가 NumPy 2.5.1 및
CPython 3.12.3을 사용했음을 확인했다. 동일 NumPy 2.5.1을 격리 실행해 전역
`np.random.choice(np.arange(100,1000), 3, replace=False)`와
`np.random.permutation(... )[:3]`의 결과뿐 아니라 호출 직후 16개 raw uint32 상태 진행도
완전히 같음을 확인했다. 따라서 choice branch가 가정한 899개 Fisher--Yates draw 소비는
production dependency에 맞는 호출 의미다.
추가로 같은 버전에서 `range(100,1000)`, 해당 list, `np.arange(100,1000)`, 그리고
정수 domain `900` 뒤 `+100`을 각각 같은 RandomState에서 실행했다. 세 seed 값과 호출 직후
16개 raw uint32가 네 경우 모두 동일했다. 따라서 private patch의 domain 표현 차이는 현재
choice 모델의 출력이나 상태 진행을 바꾸지 않는다.

choice branch의 exact corridor 1 restart prelude를 2-bit MT state 완전분할로 검사한 결과,
shard 1은 152.31초에 strict SAT 및 rejection-path 인증까지 도달했다. 나머지 shard 0, 2,
3은 `UNKNOWN`이므로 살아 있다. 이 SAT 상태는 0개 Discovery row만 맞춘 과소결정 witness이며,
제외해 둔 첫 Discovery seed는 맞히지 못했으므로 후보가 아니다. 같은 shard 1에 첫 Discovery
seed triplet 전체를 추가한 실행, 다시 이를 4-bit state의 네 하위공간으로 완전분할한 실행,
마지막 두 Fisher--Yates draw의 여섯 값 조합으로 완전분할한 실행은 모두 `UNKNOWN`이었다.
첫 seed 위치 하나만 결합한 실행도 289.30초에 `UNKNOWN`이었고, 그 문제를 4-bit state의 네
하위공간으로 완전분할한 결과 역시 각각 266.30, 275.35, 282.45, 268.31초에 모두
`UNKNOWN`이었다. 어느 결과도 UNSAT가 아니므로 가설이나 분할공간을 기각하지 않는다.

위 결과에 따라 임의 state-bit 분할을 더 늘리는 대신, 동일 solver에서 첫 Discovery seed의
최종 순열 위치를 한 자리씩 결합하는 progressive 경로를 구현했다. target별 reverse/forward
Fisher--Yates 회로를 분리하여 position 0을 매 단계 중복 회로화하지 않고, 위치 1→2→3마다
즉시 solve한 뒤 learned clause를 다음 깊이에 그대로 유지한다. 각 깊이의 status와 시간은
checkpoint에 별도 기록한다. 이 실행도 strict exact rejection path의 계산 진단일 뿐이며,
세 위치가 모두 SAT이고 공개 row replay 및 제외 Discovery 예측 gate를 통과하기 전에는
생성기 후보로 승격하지 않는다.

주의할 점은 choice corridor 네 개의 checkpoint-band Monte Carlo 합집합 coverage 96.5%가
네 개의 **정확한 rejection gap 열** 자체의 확률 질량을 뜻하지 않는다는 것이다. 현재
fixed-profile 실행은 각 corridor의 대표 exact path 하나만 고정하므로 실제 생성 경로
전체에서 차지하는 비중은 극히 작다. 따라서 exact-path UNSAT도 해당 한 경로만 기각하며,
choice 생성 방식 전체를 기각하려면 rejection path를 포괄하는 별도 factorized encoding이
필요하다.

progressive 회로의 실제 첫 실행에서는 같은 state shard의 restart prelude가 먼저 SAT에
도달했지만, 첫 Discovery seed의 position 0 값 491을 추가한 solve가 164.83초 뒤
`UNKNOWN`이었다. 1-thread 재현도 같은 위치에서 `UNKNOWN`으로 끝났으므로 position 1, 2는
추가되지 않았다. 이는 첫 위치 제약이 이미 주 계산 병목임을 보여주지만, UNSAT가 아니므로
해당 exact path나 choice 가설을 기각하지 않는다.

대표 exact rejection path의 낮은 확률질량 문제를 피하기 위해 broad lattice 솔버도
`rounds=0`을 지원하도록 바꿨다. restart prelude 하나의 899 bounded draw만 대상으로,
64-draw 간격 1σ 누적 rejection band 안의 모든 상태와 각 raw word의 accept/reject를 exact
lattice로 결합했다. 이 식은 raw word 1,307개, 변수 1,066,676개, CNF 절 4,276,601개였고
187.13초 뒤 `UNKNOWN`이었다. 따라서 이 band는 살아 있으나 아직 witness는 없다. 후속
탐색은 마지막 draw의 누적 rejection 357..408을 mean-first assumption shard로 완전 순회하며,
모든 52개 상태가 결정된 UNSAT일 때만 이 1σ band를 기각한다.

1σ 마지막 누적 rejection 357..408의 52개 assumption shard는 전부 실제로 순회했지만
모두 `UNKNOWN`이었다. 따라서 `complete=true`는 스캔 완결만 뜻하며 band 기각은 아니다.
더구나 corridor 1의 알려진 prelude SAT path는 draw 448에서 누적 174인데 1σ 하한은 175라,
그 SAT path 자체가 1σ 식에 포함되지 않았다. 2σ로 넓히고 draw 448=174, draw 899=371만
고정한 식도 221.48초 뒤 `UNKNOWN`이었다.

exact corridor gap 열에서 매 N draw의 누적 rejection만 자동 고정하고 구간 내부 gap 배치는
자유로 두는 재현 가능한 checkpoint-profile 옵션을 추가했다. fixed profile의 최대 누적값에
맞춰 rejection budget과 raw cap을 자동 확장한다. 초기 구현에서 이 확장이 빠져 N=32 식이
기본 budget 160에 막혀 즉시 UNSAT가 된 artifact는 `invalid_budget`으로 격리했고 증거에서
폐기했다. 수정 뒤 corridor 1 prelude를 N=64,32,16,8,4,2,1로 좁힌 결과는 모두
`UNKNOWN`이었으며 solve 시간은 각각 184.92, 152.29, 121.85, 91.83, 61.42, 58.16,
60.72초였다. N=1은 매 draw gap이 완전히 정해져 fixed-profile과 논리적으로 같은 path다.
fixed-profile에서 SAT였던 2-bit state shard 1까지 lattice에 동일하게 추가한 N=1 대조도
242.58초 뒤 `UNKNOWN`이었다. 이는 UNSAT나 encoding 불일치가 아니라, 같은 논리 경로도
lattice 회로에서는 현재 솔버가 훨씬 어렵게 탐색한다는 성능 경계다. 다음 solver 방향은
fixed-profile의 작은 빠른 회로를 유지하면서 구간별 rejection 선택만 factorize하는
하이브리드 encoding이다.

그 하이브리드 encoding도 구현했다. 선택한 half-open draw window마다 corridor의 누적
rejection 총량은 보존하되, 그 총량의 모든 ordered weak composition을 one-hot selector로
허용한다. 창 전후 raw index는 그대로 유지되고, 창 안 accepted bits만 selector-conditioned
mux로 seed permutation 회로에 연결된다. 겹치는 창, 범위 밖 창, 대안 cap 초과 및 lazy/omit
rejection과의 혼용은 fail-closed 처리한다. SAT model의 concrete replay에서 실제 선택된
창별 gap 배열도 report에 남기되 MT state/raw stream은 저장하지 않는다.

corridor 1 restart prelude의 2-bit state shard 1에서 `4:6` 두-draw 창의 2개 대안을 허용한
식은 195.17초에 SAT였다. `4:6`과 `10:12`를 함께 푼 4경로 식도 152.76초에 SAT였고,
첫 창은 baseline `[1,0]`이 아닌 `[0,1]`을 선택했다. 첫 16 draw를 단일 창으로 묶은
136대안 식은 362.77초 뒤 `UNKNOWN`이었지만, 이를 `0:8`, `8:16`의 8×8=64경로로
factorize한 식은 199.20초에 SAT였다. 다시 `40:48`, `48:56`을 더한 8⁴=4,096경로 식도
198.68초에 SAT였으며 네 창 모두 baseline과 다른 rejection 위치를 선택했다. 즉 작은
독립 창의 선택공간 곱은 공유회로로 감당할 수 있지만, 한 창의 큰 selector 폭은 현재
병목이다. 다음 단계는 이 4창 SAT solver에 첫 Discovery seed 위치를 progressive하게
추가하는 것이다.

공개 W&B 로그의 UID 순서 누락을 다른 문구로 보완할 수 있는지도 다시 감사했다. 현재
20,279개 log line에서 miner/query 관련 본문을 endpoint·숫자 제거 후 정규화하면 12개
형식뿐이며, 실제 query 순서를 주는 것은 `/forward` HTTP request와
`Error querying miner <UID>` 두 계열뿐이었다. 성공 request는 endpoint만 남기고 UID를
기록하지 않으며, connection/status error만 정확 UID를 남긴다. 첫 고품질 Discovery
라운드의 로그 없는 UID 5개는 공개 historical metagraph로 `28,109,156,160,168`임을
확인했지만 이들이 최종 permutation의 어느 다섯 위치에 삽입됐는지는 로그에 없다. 따라서
기존 `shift 0..5` 불확실성은 parser 누락이 아니라 공개 console의 실제 관측 한계다.

### UID 순서 코퍼스의 권위 교정

기존 `endpoint-snapshot` 코퍼스는 각 task 생성 block의 metagraph endpoint→UID 대응을
그 task의 전체 validator broadcast에 적용했다. 그러나 실제 broadcast는 약 한 시간 동안
이어지고 validator는 그 동안 변할 수 있는 `self.metagraph`를 참조한다. 실제 16개
Discovery 라운드의 시작/종료 snapshot을 비교하자 14개에서 2~19개 endpoint domain이
달라졌다. 따라서 단일 시작 snapshot 전체를 exact로 쓰는 artifact는 비교용으로만
보존한다.

첫 교정으로 만든 `event-index` 코퍼스는 공개 `/forward` 요청의 순번과 개수만 보존하고,
validator error 또는 소유 request envelope로 직접 증명됐다고 본 위치만 UID singleton으로
뒀다. 그러나 소유 캡처 결합기가 endpoint를 확인하지 않고 캡처 뒤 첫 event를 골랐다는
추가 오류를 발견했다. 실제 task에서 UID 45와 243 캡처가 그 UID의 chain endpoint가 아닌
다른 순번에 결합됐다. 따라서 과거 `owned_capture_matches=84`는 전부 권위 증거에서
제외한다. 수정된 결합기는 공개 metagraph endpoint와 시간창이 동시에 맞을 때만 exact로
승격하고, endpoint가 없거나 다르면 fail-closed한다.

권위 코퍼스는 `endpoint-window-stable` 방식으로 다시 만들었다. 각 라운드에서 validator의
finality lag 4블록을 반영한 시작·¼·½·¾·종료의 역사적 metagraph 5개를 조회하고, 다섯
snapshot에서 UID domain이 동일한 endpoint만 채택한다. 변한 endpoint의 event는 익명
누락으로 처리하고 `Error querying miner <UID>` 209개만 직접 exact로 유지한다. endpoint,
IP, credential은 artifact에 저장하지 않는다. 42라운드에서 owned exact는 0개이며,
stable singleton 순서 5,597개, error singleton을 포함한 exact singleton 5,806개,
stable group 순서 9,128개를 보존했다. prefix 검증은 42개 모두 UNSAT 없이 통과했고,
depth 4에서 9개는 완전 열거, 33개는 상태 상한 전까지의 완전 prefix를 산출했다. Holdout과
prospective seed label은 열지 않았다.

다섯 snapshot 사이에서 endpoint가 바뀌었다가 원상복귀하는 극단적 transient까지 chain
storage로 전 블록 증명한 것은 아니므로 `window-stable`은 보수적인 sampled-stability
가정이다. 반면 관측 multiset이 초기 multiset을 넘는 라운드는 없고, 공개 error UID도
동일 endpoint domain과 양립하며, 모든 라운드의 prefix 열거가 SAT/부분 SAT였다는 세 가지
일관성 검사를 통과했다.

진단용 `event-index` 식에서는 `integers-unique`, corridor 1 exact rejection path, 첫 4개
Discovery seed label 12개와 당시 exact trace 34개를 결합한 식이 81.10초 SAT였고 concrete
replay를 통과했다. 다만 여기에 잘못 결합된 owned 위치가 포함됐고 순서 정보량도 부족하므로
생성기 증거로 사용하지 않는다. 5번째 seed-only 증분과 full 증분은 각각 590.28초,
554.28초 뒤 `UNKNOWN`이었으며 기각도 아니다. lazy rejection 방식도 초기 4라운드에서
위반 부등식 256개를 추가한 뒤 479.93초 `UNKNOWN`으로 끝났다.

stable-window 코퍼스의 multiset-order 정보 상한은 48,122.85비트이고, 20개 Discovery seed
label을 모두 쓴다고 넉넉하게 잡은 588.73비트까지 합치면 48,711.58비트다. 이는 MT19937
유효 상태 19,937비트를 넘으므로 전체 상태 복구를 시도할 정보학적 근거가 다시 생겼다.
단, domain ambiguity와 부분 subsequence 때문에 실제 유효 정보는 더 작으며 상한 초과가
복구를 보장하지 않는다.

교정 코퍼스의 첫 strict 실행은 corridor 1, seed 3개, late exact trace 16개, 완전 prefix
tuple 1,426개에서 0.93초 SAT였다. concrete replay counterexample만 추가한 CEGAR는 trace
142개까지 연속 SAT였고, 위치 8과 6을 더한 144개 단계에서 누적 698.61초 뒤 `UNKNOWN`에
도달했다. exact rejection path는 계속 인증돼 있다. 이는 첫 라운드 full replay나 미래
예측이 아니므로 후보 승격 조건을 충족하지 않으며, 다음 단계는 미관측 5개 삽입의 해당
shift 조합을 단조·완전분할하여 이 UNKNOWN을 작은 shard로 푸는 것이다.

이후 같은 첫 라운드를 독립 trace 묶음 대신 하나의 full Fisher--Yates network와 exact
subsequence로 표현하자 251개 공개 순서 전체와 seed 3개, strict rejection path가 5.31초
SAT였고 concrete replay도 통과했다. 제외한 두 번째 Discovery seed는 맞히지 못했으므로
후보는 아니다. 동일 live solver에 두 번째 seed 3개만 추가한 단계는 247.94초 뒤
`UNKNOWN`이었다. 이는 데이터 모순이 아니라 후속 seed 연결에서의 계산 경계이며,
incremental state-bit assumption의 완전분할 대상으로 넘겼다.

### 2026-09-30 긴급 MT 연속상태 캠페인

교정된 `endpoint-window-stable` 코퍼스에서 두 번째 Discovery 연결을 빠르게
판정하기 위해 cumulative-rejection assumption을 상호배타적 worker shard로 나누는
기능을 추가했다. 여러 worker가 같은 mean-first 상태를 중복 검사하지 않으며, 한 worker의
국소 UNSAT는 전체 assumption universe가 모두 결정되기 전에는 전역 UNSAT로 승격되지
않는다. 별도 campaign 집계기도 이 불변식을 검사한다.

첫·둘째 라운드 경계만 사용한 3σ 실험은 다음 결과를 냈다.

- 첫 경계 78개: 범위 밖 2개 UNSAT, 나머지 76개 `UNKNOWN`
- 결합확률 상위 경계쌍 512개: 전부 `UNKNOWN`
- 세 번째 라운드를 SAT 안에 넣지 않고 concrete postsolve replay하도록 축소한 동일 512개:
  전부 `UNKNOWN`; 변수 1,002,878개, CNF 4,516,806개
- 두 번째 라운드의 11,773개 correlated prefix tuple을 추가한 상위 32개: 전부 `UNKNOWN`
- draw 128·261·389·519 네 경계를 동시에 고정한 상위 64개: 전부 `UNKNOWN`

네 경계의 전체 monotone profile 수는 35,669,200개다. 이를 전부 materialize하지 않고
독립 rejection 증가량의 결합확률에 따른 정확한 top-K를 동적계획으로 생성하도록
solver를 바꿨다. 위 64개 결과는 전체 계열 기각이 아니라 확률 상위 profile의 계산
경계다.

fixed-profile 경로에서는 corridor 1·2에 첫 라운드 full network와 두 번째 라운드 tuple을
결합했으나 각각 297.50초와 315.56초 뒤 `UNKNOWN`이었다. 정수 seed 세 값을 한꺼번에
추가하는 대신 첫 값부터 점진적으로 추가하는 경로도 구현했다. corridor 1에서 두 번째
seed의 첫 공개 값 `795` 하나를 추가한 단계부터 `UNKNOWN`이었고, dense MT 인코딩은 같은
3비트 완전분할을 35.93초에 끝내 sparse의 142.71초보다 약 4배 빨랐다. dense 6비트
64-shard 얕은 scan은 전부 `UNKNOWN`이었으며, dense 3비트 8-shard에 각각 약 30초의
벽시계를 준 심층 scan도 8/8 `UNKNOWN`이었다.

따라서 같은 corridor에서 state-bit 수나 정적 timeout만 늘리는 작업은 중단한다. 다음
우선순위는 다음과 같다.

1. 두 번째 라운드의 얕은 prefix tuple 대신 stable endpoint 순서를 더 직접 압축하는
   결정도/네트워크를 만들어 교차라운드 제약량을 높인다.
2. 연속 MT 가설은 새 압축 모델에서 최소 두 개 제외 Discovery를 연속 정확 예측할 때만
   유지한다. 한 라운드 SAT나 fitted-row replay는 후보가 아니다.
3. `integers-unique`와 별개로 공개 patch 가능성이 있는
   `choice(100..999, 3, replace=False)`를 동일한 Discovery/Holdout gate로 평가한다.
4. 어느 경로도 Discovery 제외 예측을 통과하기 전에는 Holdout 5개를 열지 않는다.

이번 캠페인에서는 Holdout label, MT 상태, raw stream, presigned URL을 artifact에 저장하지
않았고 제출·bridge·마이너 프로세스를 변경하지 않았다.

## Track B 상태기계

```text
CAPTURE_OWN_TASK
  -> POLL_AUTHORIZED_SIGNALS
  -> FIRST_INDIVIDUAL_SCORE?
  -> CLASSIFY_BATCH_OR_STAGGER
  -> CHECK_OWN_CHANNEL_WRITABLE
  -> COMPUTE_BUILD_UPLOAD_BUDGET
  -> ACTIONABLE_EVIDENCE_ONLY
```

score row는 정확한 task UUID와 miner hotkey를 모두 만족해야 한다. 같은 task score들의
`created_at` 분산과 최초 관측 시각을 기록하여 `batch`, `staggered`, `insufficient`로
분류한다. task 전체 batch의 첫 row는 개별 조기 score로 세지 않는다.

실제 API 점검 결과 `miner_hotkey` query parameter는 서버측 필터로 동작하지 않았으므로
개별 전용 endpoint로 간주하지 않는다. 전체 task cohort를 페이지별로 한 번 수집하고 그
동일 snapshot 안에서 소유 hotkey를 찾는다. 소유 행이 처음 보인 뒤 cohort revision이 실제로
증가하고, 이후 3초 이상 안정화되어 batch 완료가 확인된 경우에만 `batch 이전`으로 판정한다.
완성된 cohort에서 처음 함께 발견된 소유 행은 대기시간과 무관하게 `same_cohort_snapshot`이며
조기 신호가 아니다.

`actionable=true`는 다음을 모두 만족할 때만 가능하다.

- 특정 소유 miner score가 batch 완료보다 먼저 관측됨
- score 정밀도가 probe inversion에 충분함
- 동일 task의 다른 소유 제출 채널이 그 시점에 독립적으로 writable로 확인됨
- `deadline - now - build_estimate - upload_reserve > 0`
- 해당 제출물이 아직 validator에 snapshot/채점되지 않았다는 허가된 증거가 있음

단순히 score가 task-history seed보다 90초 빠른 것은 `seed-publication lead`일 뿐
`individual-before-batch lead`가 아니므로 actionable gate를 열지 않는다.

## 병렬 실행과 artifact 계약

두 worker는 서로를 기다리지 않는다.

- Track A report: `artifacts/research/preseed_generator_state.json`
- Track A prediction ledger: `artifacts/research/preseed_predictions.json`
- Track B task timeline: 소유 task artifact 아래 append-only JSONL
- Track B aggregate: `artifacts/research/early_score_state.json`
- Track B channel evidence: presigned URL 자체를 저장하지 않고 만료시각, bridge 정책,
  build/upload 추정치만 `submission_channel_status.json`에 위생 처리해 기록한다.
- 통합 snapshot: `artifacts/research/seed_research_state.json`

모든 JSON snapshot은 임시 파일을 쓴 뒤 rename한다. JSONL은 credential을 제거한 작은
event만 append한다. 통합 오케스트레이터는 worker가 죽어도 마지막 정상 snapshot을
보존하고 age를 표시하며, stale worker를 성공으로 해석하지 않는다.

## 대시보드 판독

상단 연구 패널은 두 트랙을 나란히 표시한다. Track A의 post-score probe 5/5는 label
acquisition 능력이며 pre-score prediction으로 표시하지 않는다. Track B의 task batch
score는 조기 개별 score로 표시하지 않는다. 결합 gate는 두 카드가 각자 검증된 뒤에만
열리며 기본값은 항상 false다.

## 장애와 오판 방지

- epoch 변경 시 기존 모델 승인을 폐기하고 새 discovery를 시작한다.
- block hash가 task 생성 뒤에만 존재했다면 pre-score 입력으로 사용할 수 없다.
- 중단된 worker는 완료 목록에 넣지 않고 체크포인트에서 재개한다.
- 한 task의 여러 score row가 같은 timestamp이면 batch 증거로 기록한다.
- score endpoint의 task 필터를 신뢰하지 않고 응답 row의 task UUID를 다시 확인한다.
- 모델 개발에 본 seed는 prospective 검증에 재사용하지 않는다.
- 어느 트랙도 관측 결과만으로 제출 프로세스를 자동 변경하지 않는다.
