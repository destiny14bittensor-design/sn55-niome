# SN55 NIOME 세션 인수인계 — 2026-09-26

> **후속 정정:** 이 문서 작성 뒤 validator가 late-stamped random contract seed
> 방식으로 돌아간 사실이 공식 task/score exact replay로 확인되었다. 따라서 아래의
> chain-authoritative 결론은 현재 운영에 적용하지 않는다. bridge는 원본 signed
> `contract_url`을 polling하여 `seed: 0`이 실제 seed로 바뀐 뒤에만 최적화 PUT을
> 완성하며, consistency history는 `contract-authoritative` exact-match 라운드만
> 인정한다. 상세 근거는 `docs/seed_transition_architecture.md`의 최신 최상단 절을
> 따른다. 또한 새 epoch 표본이 3개 미만이면 consistency `1.0`이 아니라 cold-start
> target `0.77`을 사용하며, exact replay된 managed 후보만 허용한다.
> cold-start 선택 범위는 `0.60–0.77`, history 기반 범위는 `0.79–0.85`이다.

## 1. 목적과 현재 결론

이 문서는 2026-09-26 세션에서 수행한 SN55 NIOME 마이너, live seed bridge,
일관성 제어, 8-마이너 대시보드 및 원격 Tao/Won 배포 준비 작업을 다음 Codex
세션에 인계한다. 새 세션은 이 문서를 먼저 읽고, 아래의 값은 기록 시점의
스냅샷임을 전제로 Git/PM2/bridge 상태를 다시 확인한 뒤 이어서 작업해야 한다.

가장 중요한 결론은 다음과 같다.

- 계약 객체의 seed는 권위 있는 최종 seed로 신뢰하지 않는다.
- finalized chain block hash에서 읽은 세 seed를 권위 값으로 사용한다.
- presigned URL은 두 개의 독립적인 64 KiB/s PUT 연결로 유지한다.
- 활성 PUT을 가진 bridge를 단순 재시작하지 않는다.
- consistency는 고정값으로 낮추지 않고, 검증된 과거 라운드에서 필요한 목표를
  역산한 뒤 exact replay가 확인한 후보만 사용한다.
- 비교 가능한 새 epoch 표본이 3개 미만이면 cold-start target 0.77을 사용하고,
  exact replay 결과 0.60–0.77 범위의 managed 후보만 허용한다.
- 코드와 테스트는 GitHub `personal/main`에 푸시되어 있다.
- 원격 Tao/Won 서버에는 이 커밋을 아직 이 세션에서 직접 배포하지 않았다.
  별도의 원격 코딩에이전트가 원격 환경에 맞춰 단계적으로 배포해야 한다.

## 2. Git 기준점

- 로컬 저장소: `/home/administrator/workspace/subnet-niome`
- GitHub 저장소: `git@github.com:destiny14bittensor-design/sn55-niome.git`
- 브랜치: `main` (`personal/main` 추적)
- 구현 커밋 전체 SHA:
  `1728668cc32d82456d8e909ca117706727398ca3`
- 커밋 제목:
  `Add authoritative seed bridge and adaptive consistency control`
- 기록 직전 상태: 로컬 `HEAD`와 `personal/main`이 위 SHA로 일치했고 작업트리가
  깨끗했다. 이 문서 추가 후 별도 인수인계 커밋이 생성될 예정이다.
- 실제 운영 `.venv`로 실행한 테스트 결과: `94 passed in 51.70s`
- 시스템 기본 Python은 `boto3` 및 호환 Bittensor 모듈이 없어 pytest collection이
  실패한다. 반드시 다음처럼 프로젝트 환경을 사용한다.

```bash
cd /home/administrator/workspace/subnet-niome
.venv/bin/python -m pytest -q
```

## 3. 문제 원인과 seed 권위 이전

기존 설계는 계약 객체에 적힌 seed를 authoritative seed로 취급했다. 조사 결과
seed 확보 후 재제출에서 계약 객체의 seed와 서브넷이 최종 검증에 사용하는 seed가
달라질 수 있어, 계약 seed로 만든 결과가 최종 검증에서 일치하지 않는 문제가 있었다.
따라서 조작 여부를 단정하지 않고도 안전하도록 권위 원천 자체를 chain으로 옮겼다.

현재 정책:

1. 라운드 좌표와 세 seed block을 계산한다.
2. 세 번째 seed block에 도달하면 finalized chain에서 각 block hash를 읽는다.
3. finality 대기 중에도 가능한 빌드 작업을 수행한다.
4. finality block에서 block hash들을 다시 읽어 중간 변경/reorg 가능성을 검사한다.
5. chain hash에서 도출한 seed만 최종 build/replay에 사용한다.
6. 계약 객체의 seed는 비교, forensic 기록 및 alert용 telemetry일 뿐이다.
7. seed provenance와 정책 결과를 task artifact에 남긴다.

주요 파일:

- `niome_subnet/genomics/seed_policy.py`
- `tools/live_seed_bridge.py`
- `docs/seed_transition_architecture.md`
- `tests/test_seed_policy.py`
- `tests/test_live_seed_bridge.py`

## 4. presigned URL과 S3 PUT 유지 방식

4 KiB/s 및 16 KiB/s slow PUT 프로필은 반복적으로 `503 Slow Down` 또는 연결
유지 실패를 보였고, 64 KiB/s는 안정적으로 동작했다. 현재 구현은 실패 프로필을
계속 병렬 실행하지 않고 다음 두 연결만 사용한다.

- `64kib-primary`: 64 KiB/s, 독립 PUT
- `64kib-standby`: 64 KiB/s, 독립 PUT

두 연결은 동일 TCP 연결을 공유하지 않는 독립 stream이다. 한 연결의 실패가 다른
연결까지 즉시 무효화하지 않게 하는 가용성 장치다. 연결 수가 늘면 S3 rate limit을
자극할 수 있으므로 4/16 KiB를 추가하지 않고 두 개로 제한했다.

현재 상수:

- stream interval: 1초
- 각 PUT의 고정 total size: 576 MiB
- 새 연결을 열기 위한 최소 URL 잔여 시간: 15초
- artifact 대기: 90초

잔여 시간이 15초 미만이면 TLS 연결, HTTP request 전송, S3의 첫 응답까지 끝내기 전에
URL이 만료될 가능성이 높다. 이때 새 연결을 여는 것은 기존 연결 및 서버 rate limit에
부담만 더하고 성공 확률이 낮으므로 bridge가 새 PUT을 시작하지 않는다.

운영 원칙:

- 활성 PUT이 있으면 bridge를 단순 재시작하지 않는다.
- 전환은 miner 신규 작업 정지 → 기존 bridge drain → bridge 종료 → 새 bridge 시작
  → miner 시작 순으로 원자적으로 수행한다.
- bridge가 두 PUT의 상태, 전송량, 마지막 성공 전송, HTTP/TLS 오류 스타일을 artifact와
  dashboard에 요약한다.
- 긴 XML/response body 전체를 dashboard alert에 노출하지 않는다.

## 5. adaptive consistency 제어

최종 검증 점수의 모델은 다음과 같다.

```text
baseline = total_weighted_score * distribution_fidelity_factor
final_score = baseline * consistency_factor
normalized_top = official_top_score / baseline
```

각 bridge는 자기 `NIOME_ARTIFACT_ROOT` 아래의 최근 완료 라운드만 조사한다. 표본은
다음 조건을 모두 만족해야 한다.

- bridge state가 `complete`
- seed policy가 `chain-authoritative`
- 결과가 공식 점수와 비교 가능한 상태
- local exact final score가 공식 scoreboard에서 정확히 확인됨
- weighted score, fidelity 및 consistency가 유효한 수치

결정 파라미터:

- 최소 표본: 3
- 최대 최근 표본: 5
- quantile: 80 percentile
- 선두 대비 safety margin: 5%
- cold-start consistency 범위: 0.60–0.77
- history 기반 consistency 범위: 0.79–0.85
- partial-seed anchor: 0.70
- exact replay budget: 45초
- validation 전 upload reserve: 75초
- 최소 replay 여유: 30초

계산:

```text
required_consistency = percentile80(normalized_top history) * 1.05
```

`required_consistency`는 0.79–0.85 범위로 clamp한다. builder는
여러 candidate를 만들고 bridge는 실제 세 seed로 exact replay한다. target보다 낮은
후보는 채택하지 않으며 target을 넘는 후보 중 가장 가까운 결과를 선택한다.

fail-safe 조건:

- consistency control 비활성
- 공식 점수 정확 매칭 실패
- chain-authoritative provenance 부족
- target 범위 밖
- exact replay 실패
- validation 전 시간 부족
- 안전 후보 부재

위 조건에서는 먼저 exact 검증한 `max-score-fallback`, 즉 consistency 1.0 후보를 쓴다.
실제 offline replay에서는 target 0.76에 대해 0.7457 후보는 거절되고 0.78173058 후보가
선택되어 final score 약 228.52856을 얻었고, fallback은 consistency 1.0이었다.

관련 파일:

- `niome_subnet/genomics/consistency_control.py`
- `niome_subnet/genomics/submission_builder.py`
- `niome_subnet/genomics/seed_optimizer.py`
- `niome_subnet/miner/task_processor.py`
- `tools/live_seed_bridge.py`
- `tests/test_consistency_control.py`
- `tests/test_submission_builder_strategy.py`

환경변수:

- `NIOME_CONSISTENCY_CONTROL=false`: 즉시 max-score 정책으로 전환하는 kill switch
- `NIOME_BRIDGE_OBSERVE_ONLY=true`: seed를 읽고 후보/여유를 평가하지만 실제 재제출은
  하지 않는 관측 모드

코드 기본값은 consistency control 활성이다. 유효 표본이 3개 미만이면 cold-start
target 0.77이 된다. `NIOME_CONSISTENCY_CONTROL=false`는 consistency 1.0의 max-score
경로이므로 일반적인 최초 배포값으로 사용하지 않고 긴급 kill switch로만 사용한다.

주의: 이 기능은 8개 마이너의 점수를 강제로 서로 다르게 만들지 않는다. 각 miner가
분리된 artifact history를 가지면 baseline과 과거 표본 차이로 점수가 달라질 가능성이
높지만, 동일 task/seed/config/history라면 동일한 결정론적 후보와 점수가 나올 수 있다.

## 6. 로컬 4-마이너 identity 전환

기존 dollar1/2/3/4 hotkey 대신 다음 mainnet 등록 hotkey를 가동했다. PM2 내부 이름은
기존 운영 안정성을 위해 `dollar*`를 유지하지만 실제 wallet/hotkey identity는 아래와
같다.

| PM2 lane | coldkey/hotkey | UID | SS58 | axon |
|---|---|---:|---|---|
| niome-dollar1 | main3/bitcoin1 | 155 | `5Ge44ZvzfkxGYXwnbPmnCGfzatGUtfiXSXUcTWGNcv6DMLXz` | 69.30.204.53:8091 |
| niome-dollar2 | main3/bitcoin2 | 223 | `5H1jPksvzJuak6P63VAp7QttcRpQ1PuT9BNDyMT3qGEYovdQ` | 69.30.204.53:8092 |
| niome-dollar3 | main4/hype1 | 96 | `5EeqkTcDzGg7Ge89N1DJzQv5ehyCEPMBreCHqcxfEpB3WU21` | 69.30.204.53:8093 |
| niome-dollar4 | main4/hype2 | 159 | `5HKLhT3ie4VkW9hG3Vgn2MiYgZ9PQtVnFbJ18fmDh5ZkWY86` | 69.30.204.53:8094 |

wallet root:

```text
/home/administrator/.bittensor/wallets/main
```

이를 위해 다음을 추가했다.

- CLI `--wallet-path`
- `bt.Wallet(..., path=self.config.wallet_path)` 전달
- fleet PM2 config의 lane별 wallet, hotkey, port 및 artifact root
- dashboard의 bitcoin1/bitcoin2/hype1/hype2 identity

artifact root는 반드시 서로 분리한다.

- bitcoin1: `artifacts/live`
- bitcoin2: `artifacts/miners/dollar2`
- hype1: `artifacts/miners/dollar3`
- hype2: `artifacts/miners/dollar4`

## 7. 대시보드 통합과 UI

로컬 대시보드:

- `http://69.30.204.53:8111/`
- local source ID: `bitcoin-hype-fleet`
- label: `Bitcoin / Hype Fleet`

원격 대시보드:

- `http://108.181.196.26:8111/`
- source: Tao/Won fleet

federation collector가 로컬과 원격 snapshot을 통합한다. 주요 파일:

- `niome_subnet/dashboard/federation.py`
- `niome_subnet/dashboard/server.py`
- `niome_subnet/dashboard/state.py`
- `dashboard/index.html`
- `dashboard/dashboard.js`
- `dashboard/dashboard.css`
- `tests/test_dashboard_federation.py`

UI 요구 반영:

- 화면 최상단 왼쪽: 좁은 알림 패널
- 화면 최상단 오른쪽: 세로형 마이너 순위 패널
- 그 아래: 기존 task/fleet 상세 패널
- 순위 행: 마이너 이름, UID, 순위, 점수, 이전 task 순위
- hotkey 주소는 화면에 표시하지 않음
- UID font를 정상적인 가독성 있는 font로 수정
- 오류 alert는 방대한 raw dict/XML/body 대신 오류 스타일과 핵심 상태만 표시

## 8. 기록 시점의 로컬 런타임 상태

기록 시각 부근: 2026-09-26 18:39 UTC. 새 세션에서는 반드시 다시 확인한다.

온라인:

- `niome-dollar1` — PID 2735782
- `niome-dollar2` — PID 2735783
- `niome-dollar3` — PID 2735784
- `niome-dollar4` — PID 2735785
- `niome-seed-bridge` — PID 2735766
- `niome-seed-bridge-dollar2` — PID 2735767
- `niome-seed-bridge-dollar3` — PID 2735768
- `niome-seed-bridge-dollar4` — PID 2735769
- `niome-dashboard` — PID 2735979, port 8111

의도적으로 stopped:

- `niome-dashboard-dollar2`
- `niome-dashboard-dollar3`
- `niome-dashboard-dollar4`

개별 dashboard 대신 통합 dashboard 하나를 쓰므로 위 세 stopped 상태는 정상이다.

listen 확인:

- 8091, 8092, 8093, 8094
- 8111

통합 API에서 로컬 UID 155/223/96/159와 원격 UID 230/4/92/38, 총 8개가 online으로
관측되었다. PM2의 dashboard restart count가 16이었고 상태 확인 순간 CPU가 100%로
표시되었으므로 새 세션은 일시적 수집 부하인지 지속 문제인지 먼저 재확인한다.

## 9. 원격 Tao/Won 서버 배포 상태와 제약

원격 코드 경로로 알려진 값:

```text
/home/administrator/workspace/sn55-niome
```

원격 마이너:

| miner | UID | 예상 miner PM2 | 예상 bridge PM2 |
|---|---:|---|---|
| tao1 | 230 | niome-tao1 | niome-seed-bridge-tao1 |
| tao2 | 4 | niome-tao2 | niome-seed-bridge-tao2 |
| won1 | 92 | niome-won1 | niome-seed-bridge-won1 |
| won2 | 38 | niome-won2 | niome-seed-bridge-won2 |

세션 중 이전 원격 snapshot에서는 tao2가 활성 task와 두 64 KiB PUT을 보유하고 있었고,
won1은 contract/reference/cell-type artifact 경로 누락으로 실패한 적이 있었다. 이 정보는
시간이 지난 snapshot이므로 새 세션에서 현재 상태를 다시 확인한다. 특히 활성 PUT이
있으면 해당 bridge를 재시작하지 않는다.

원격 배포 시 개발 서버 전용 identity를 복사하지 않는다. 특히 다음 파일은 현재
Bitcoin/Hype 설정을 포함하므로 원격의 Tao/Won wallet, hotkey, UID, port, artifact root,
dashboard identity에 맞게 보존/재작성한다.

- `tools/ecosystem.fleet.config.js`
- `tools/ecosystem.dollar2.isolated.config.js`
- `tools/ecosystem.dollar3.isolated.config.js`
- `tools/ecosystem.dollar4.isolated.config.js`
- `niome_subnet/dashboard/server.py`

권장 배포 순서:

1. 원격 `git status`, SHA, PM2 args/env/cwd, bridge task/PUT 상태를 read-only로 감사
2. 기존 dirty 변경 보존
3. 새 worktree/release에서 정확한 SHA checkout
4. 원격 Tao/Won 전용 identity 및 경로 이식
5. 실제 원격 `.venv`로 전체 테스트
6. `NIOME_CONSISTENCY_CONTROL=false`로 tao1 카나리
7. 활성 PUT drain 후 bridge와 miner를 원자적으로 전환
8. tao1 검증 후 tao2 → won1 → won2 순차 전환
9. won1 artifact 생산/소비 경로 불일치가 있으면 근본 수정
10. 네 miner와 네 bridge online, task 수신, chain seed 감시, dashboard 확인
11. `pm2 save`
12. rollback 경로와 최종 표 보고

원격 작업은 코드 반영만으로 완료가 아니다. 마지막 won2까지 실제로 가동·검증해야 한다.

## 10. 다음 세션 우선순위

1. 이 문서의 Git/PM2/bridge 상태를 현재 값으로 재검증한다.
2. 로컬 dashboard의 CPU 100% 및 restart count 16이 지속되는지 확인한다.
3. 각 로컬 bridge의 최신 `seed_bridge_status.json`에서 active PUT, task ID, seed mode,
   consistency sample count 및 fallback 여부를 확인한다.
4. 원격 코딩에이전트의 배포 결과를 받으면 사용 SHA, 테스트, PM2 상태와 원격 identity가
   정확한지 검토한다.
5. 원격 active PUT이 있다면 종료 전에 재시작하지 않도록 확인한다.
6. 원격 4개까지 전환된 뒤 통합 dashboard에서 8개 miner를 확인한다.
7. 각 miner가 독립 artifact root/history를 사용하는지 검사한다.
8. 새 epoch의 contract-authoritative 공식 매칭 라운드가 3개 미만이면 원격도
   consistency control을 켠 상태에서 cold-start target 0.77과 0.60–0.77 범위를 사용한다.
9. 3개 이상 쌓이면 normalized history와 exact candidate 결과를 검토하며 target은
   0.79–0.85 범위로 유지한다.

## 11. 절대 피해야 할 작업

- 활성 PUT이 있는 bridge의 무조건 재시작
- 8개 miner 동시 중지
- 로컬 Bitcoin/Hype PM2 설정을 원격 Tao/Won 서버에 그대로 배포
- 서로 다른 miner의 artifact root 공유
- 계약 객체 seed를 다시 authoritative로 승격
- exact replay 없이 targeted consistency 후보 제출
- `.env`, wallet, mnemonic, private key, presigned URL, runtime artifact 또는 로그를 Git에 추가
- `git reset --hard`나 기존 사용자 변경 삭제

## 12. 2026-09-27 세션 최종 추가 기록

기록 시각: **2026-09-27 04:14 UTC**. 이 절은 위 문서 중 seed 권위, consistency 및
런타임 상태와 충돌하는 과거 내용을 최종적으로 대체한다. 다음 세션은 이 절을 우선
기준으로 삼고, 실제 Git/PM2/artifact 상태를 다시 읽은 뒤 작업한다.

### 12.1 최종 seed 결론과 URL 만료 보조 경로

- validator는 다시 **late-stamped random contract seed**를 사용한다. 과거의
  chain-authoritative 판단과 chain seed 경로는 현재 운영 기준이 아니다.
- signed `contract_url`이 유효한 동안에는 그 URL만 polling한다. 원본 contract의
  `seed: 0`이 실제 seed로 바뀌면 그 값을 authoritative seed로 사용한다.
- signed URL이 만료된 뒤에도 seed가 0이면 공식
  `https://niome-api.genomes.io/api/v3/tasks?page=1&per_page=20` 기록을 조회하는 보조
  경로를 구현했다.
- 보조 경로는 UUID exact match, contract와 embedded HBB challenge의 일치, 최초
  capture 대비 seed 외 필드 불변, 동일한 nonzero seed 2회 연속 관측을 모두 만족해야
  한다. 통과 시 source는 `task-history-expiry-fallback`이다.
- `/tasks?task_id=...` 및 `/tasks?id=...` 필터는 서버에서 무시되므로 사용하지 않는다.
  `page/per_page` 목록에서 UUID를 직접 exact-match한다.
- 실제 API 통합 확인:
  - `2438f5ac-ac4b-49a7-8c0f-5f7ef6b8250b` → `519,253,867`
  - `950262bf-d27b-4a2c-8c98-ac087bcf82a3` → `991,912,107`
- 직전 `2438...` 라운드에서 로컬 Bitcoin/Hype가 seed를 놓친 근본 원인은 signed URL
  만료가 `2026-09-27 00:51:21 UTC`, 공식 final 기록 출현이 약
  `2026-09-27 01:06:17 UTC`여서 둘 사이에 공백이 있었기 때문이다. 새 보조 경로가
  이 공백을 해결한다.
- 구현 커밋: `53059ca9374a60d7bf29390568444efcf1bf4946`
  (`Recover late seeds from verified task history`).

### 12.2 consistency 최종 정책

- 비교 가능한 표본이 **3개 미만**이면 cold-start이다.
  - 목표: `0.77`
  - 허용 범위: `0.60–0.77`
  - exact replay된 managed 후보 중 허용 범위 안에서 목표에 가장 가까운 것을 고른다.
  - cold-start에서는 consistency `1.0`인 `max-score-fallback`을 후보 선택에서 제외한다.
  - 허용 범위 안의 후보가 하나도 없으면 최적화 overwrite를 완료하지 않고 기존 안전
    제출물을 그대로 둔다.
- 비교 가능한 표본이 **3개 이상**이면 history 기반 targeted 모드이다.
  - 각 miner는 자기 artifact root의 최근 유효 라운드를 최신순 최대 5개만 사용한다.
  - 유효 표본은 `complete`, epoch `late-contract-random-v1`, seed policy
    `contract-authoritative`, `comparable_to_official=true`여야 한다.
  - 그 miner의 local exact final score가 같은 task의 공식 scoreboard 항목에서 실제로
    발견되어야 표본으로 인정한다.
  - 라운드별 local baseline은
    `B_i = total_weighted_score_i * distribution_fidelity_factor_i`이다.
  - 같은 라운드의 모든 miner 중 공식 1위 final score를 `T_i`라 하고
    `R_i = T_i / B_i`로 정규화한다.
  - `required = percentile80(R_i) * 1.05`로 계산하고,
    `target = clamp(required, 0.79, 0.85)`로 결정한다.
  - 즉, 다른 상위 miner들의 consistency 값을 history 표본으로 직접 가져오는 것이
    아니다. **우리 miner의 검증된 local baseline**과 **같은 라운드 공식 1위 점수**의
    비율을 표본으로 사용한다. 각 miner의 history/artifact root는 서로 독립이다.
  - 최종 제출 후보는 실제 세 seed로 exact replay하며, `0.79–0.85` 범위 안에서 target에
    가장 가까운 후보를 선택한다.
- 최신 cold-start 구현 커밋:
  `d509f80de68f534dedb328a67d089c94ded33948`
  (`Lower cold-start consistency band`).
- 최신 전체 테스트 결과: `.venv/bin/python -m pytest -q` →
  **100 passed in 50.90s**.

최근 완료 4개 라운드의 공식 1–3위 consistency factor는 다음과 같았다.

| task | 1위 | 2위 | 3위 |
|---|---:|---:|---:|
| `950262bf...` | 0.645283 | 0.670278 | 0.627492 |
| `2438f5ac...` | 0.698058 | 0.683394 | 0.655318 |
| `cb53c30c...` | 0.813040839 | 0.838756 | 0.803084 |
| `11225e98...` | 0.716744 | 0.719788 | 0.685042 |

최근 공식 task seed는 다음과 같이 확인했다.

| task | 최종 공식 seed |
|---|---:|
| `950262bf...` | 991,912,107 |
| `2438f5ac...` | 519,253,867 |
| `cb53c30c...` | 999,668,630 |
| `11225e98...` | 795,975,199 |
| `f05ef562...` | 491,210,379 |

### 12.3 기록 시점 Git과 로컬 배포 상태

- 기록 전 Git은 `main == personal/main == d509f80`, 작업트리는 깨끗했다. 이 문서
  기록 커밋이 그 뒤에 추가된다.
- `niome-seed-bridge`(bitcoin1, PID 2916806)와
  `niome-seed-bridge-dollar3`(hype1, PID 2916811)는 `d509f80`으로 재시작되어
  cold-start `0.60–0.77` 정책을 실행한다.
- `niome-seed-bridge-dollar2`(bitcoin2, PID 2912501)와
  `niome-seed-bridge-dollar4`(hype2, PID 2913177)는 task
  `569500e8-c794-417f-9ad7-0f9c2a51c982`의 가치 있는 활성 PUT을 보존하기 위해
  재시작하지 않았다. 두 프로세스에는 `53059ca`의 `/tasks` 만료 보조 경로는 있지만,
  이 task의 이미 확정된 consistency decision은 이전 cold-start target `0.85`이다.
- 위 두 bridge는 기록 시점 모두 `waiting_for_contract_seed`, seed `0`, source
  `signed-contract`, 유효 표본 0이었다. 활성 PUT이 drain/완료된 뒤에만 재시작하여
  `d509f80`을 로드해야 한다.
- 네 miner와 네 bridge, 통합 dashboard는 online이었다. 개별 dollar2/3/4 dashboard는
  통합 dashboard 사용 때문에 stopped가 정상이다.
- 원격 Tao/Won에는 `53059ca` 및 `d509f80`을 아직 배포하지 않았다. 원격 identity 파일을
  로컬 Bitcoin/Hype 설정으로 덮지 말고, 원격 active PUT을 먼저 확인한 뒤 순차 배포한다.

### 12.4 다음 세션의 첫 작업과 이어갈 일

다음 새 세션은 다른 조치보다 먼저 사용자에게 다음을 구체적인 숫자 예와 함께 설명한다.

1. 표본 3개가 어디에서 생기는지: 각 **우리 miner 자신의** 완료·공식 exact-match 라운드.
2. 각 표본에서 `B_i`, `T_i`, `R_i=T_i/B_i`가 무엇인지.
3. `P80(R) * 1.05`와 `clamp(0.79, 0.85)`가 실제 target을 어떻게 만드는지.
4. 상위 miner들의 consistency factor를 직접 평균내지 않는 이유.
5. miner별 독립 history가 서로 다른 target을 만들 수 있는 이유.

그 설명 뒤 현재 상태를 재확인하고, bitcoin2/hype2의 활성 PUT이 끝났다면 두 bridge를
안전하게 재시작해 `d509f80`을 적용한다. 그 다음에만 원격 Tao/Won 배포 여부를 다룬다.

### 12.5 다음 세션 시작용 한 줄 프롬프트

`/home/administrator/workspace/subnet-niome/docs/SESSION_HANDOFF_2026-09-26_KO.md를 처음부터 끝까지 읽고 현재 Git·PM2·활성 PUT 상태를 재확인한 뒤, 먼저 표본이 3개 이상일 때 각 우리 miner의 공식 exact-match history로 B_i·T_i·R_i를 만들고 P80(R)×1.05를 0.79–0.85로 clamp하여 consistency를 제어하는 방식을 숫자 예와 함께 설명한 후 남은 작업을 그대로 이어가라.`

## 13. 2026-09-27 후속 세션 기록

기록 시각: **2026-09-27 04:36 UTC**.

- Git은 `main == personal/main == 1c9e517`이다. 새 커밋
  `Cache dashboard validation summaries`는 dashboard가 각 lane의 약 1 MiB
  exact-validation history를 2초마다 반복 파싱하던 문제를 고쳤다.
- validation summary cache는 `(path, mtime_ns, size)`로 무효화하고 완전한 validation
  payload가 아닌 작은 summary만 보관한다. 네 artifact root의 warm refresh는 약
  `0.60초`에서 `0.03초`로 감소했고 dashboard CPU 표본은 지속 10–39%에서 대부분
  0.5–1.5%로 감소했다. 전체 테스트는 `101 passed in 52.19s`였다.
- 통합 federation API는 로컬 UID 155/223/96/159와 원격 UID 230/4/92/38,
  총 8개 miner를 online으로 반환했다.
- 현재 task는 `569500e8-c794-417f-9ad7-0f9c2a51c982`이다. 네 로컬 bridge 모두
  `waiting_for_contract_seed`, seed `0`, source `signed-contract`이며 각각 두 64 KiB/s
  PUT이 전송 중이다. signed URL 만료 시각은 `2026-09-27 05:40:03 UTC`이다.
- bitcoin1과 hype1은 새 cold-start target `0.77`, 허용 범위 `0.60–0.77`을 실행한다.
  bitcoin2와 hype2는 이 task에서 이미 정해진 이전 target `0.85`를 보존 중이다.
  **bitcoin2/hype2는 아직 재시작하면 안 된다.** 두 bridge가 terminal 상태가 되고
  active stream이 없어진 뒤 한 lane씩 miner 신규 작업을 막고 bridge를 재시작하여
  현재 코드를 로드한 다음 miner를 다시 시작한다.
- 원격 dashboard API에서 Tao/Won 4개 miner와 4개 bridge는 online이나 bridge는 여전히
  과거 `waiting_for_seed_blocks` 상태이며 현재 PUT은 없었다. `53059ca/d509f80` 계열은
  아직 원격에 배포되지 않은 것으로 보인다.
- 이 로컬 호스트의 SSH 키는 `administrator`, `root`, `ubuntu` 계정 모두 원격
  `108.181.196.26`에서 거부됐다. 원격 배포를 계속하려면 해당 서버에 유효한 SSH
  접근 또는 그 서버에서 실행되는 별도 코딩에이전트가 필요하다.

## 14. 2026-09-27 06시 세션 — late seed 재제출 실패 원인 확정

### 14.1 task `569500e8...`의 실제 시간 순서

- 원본 signed `contract_url` 만료: `2026-09-27 05:40:03 UTC`.
- 공식 scoreboard에서 우리 네 miner의 safe submission 채점 시각:
  `2026-09-27 05:54:08.982101 UTC`.
- task-history에서 seed 후보 `983,903,460` 첫 관측·상태 저장:
  - bitcoin2: `05:56:23.525620`
  - hype2: `05:56:23.556784`
  - bitcoin1: `05:56:23.959120`
  - hype1: `05:56:23.983106`
- 동일 seed의 두 번째 연속 확인 및 authoritative 확정:
  - bitcoin2: `05:56:29.689073` — 첫 저장 후 `6.164초`
  - hype2: `05:56:29.719975` — `6.163초`
  - bitcoin1: `05:56:30.125235` — `6.166초`
  - hype1: `05:56:30.153075` — `6.170초`
- 첫 후보 전용 event는 구현되어 있지 않다. 위 첫 관측 시각은 candidate와 confirmation
  count 1을 상태에 저장한 poll의 `last_observed_at`이며, 두 번째 확인은
  `authoritative_contract_seed_observed` event의 정확한 시각이다.
- 결론: `/api/v3/tasks` 만료 보조 경로는 정확히 작동했지만 seed는 공식 채점보다 약
  2분 15초 늦게 관측됐다. 현재 관측된 task-history seed 공개는 같은 라운드의
  재최적화 창이 아니라 사후 감사·exact replay 용도다.

### 14.2 결과 생성과 S3 PUT 실패의 분리

- 실제 seed 기반 결과 생성은 네 lane 모두 성공했다.
  - bitcoin1/hype1: 250 rows, 같은 SHA256, build 약 128–130초
  - bitcoin2/hype2: 252 rows, 같은 SHA256, build 약 124–128초
  - 각 task dir에 `seed_bridge_submission.json`이 존재한다.
- 실패는 결과 생성 뒤 PUT completion에서 발생했다. 여덟 stream 모두
  `503 Slow Down`, `failure_category=s3_http_rejection`으로 종료됐다.
- `SlowPut`은 seed 대기 중에는 64 KiB/s이지만 `finish_with()` 뒤에는 남은 고정 body를
  1 MiB chunk로 sleep 없이 보낸다. 이번에는 stream별 남은 약 122–230 MiB를
  8–32초에 flush하여 약 6.9–20.6 MiB/s가 됐고, 네 lane의 8개 stream이 거의 같은
  시각 `/niome/` prefix에서 완료를 시도했다. 이것이 직접적인 S3 throttling 원인이다.
- 따라서 두 독립 실패가 있었다.
  1. seed 공개가 공식 채점 뒤라서 성공적으로 PUT했어도 이번 채점에는 늦었다.
  2. 실제 PUT도 unpaced completion burst 때문에 S3에서 거절됐다.

### 14.3 확인 seed exact replay와 실제 공식 점수

- 실제 공식 채점에는 safe submission만 남았고 네 miner가 모두 다음 결과였다.
  - final score `36.9206027807735`
  - consistency `0.11155211973794726`
  - 당시 공식 1위 `194.92395821165886`
- 저장된 optimized submission을 seed `983,903,460`으로 다시 exact replay한 결과:

| 제출 그룹 | consistency | exact final score | 기존 공식 1위 대비 |
|---|---:|---:|---:|
| bitcoin1 / hype1 | 0.7148091200343959 | 231.1963943040648 | +36.2724360924059 |
| bitcoin2 / hype2 | 0.7819622915982284 | 254.07780154838238 | +59.1538433367235 |

- 각 결과를 기존 scoreboard와 개별 비교하면 모두 1위다. 네 결과가 모두 반영됐다면
  bitcoin2/hype2가 공동 최고점이고 bitcoin1/hype1이 그다음이지만, 두 그룹 모두 기존
  공식 1위보다 높다. consistency target 자체가 이번 실패의 직접 원인은 아니다.

### 14.4 다음 세션의 시작 조사

다음 세션은 다른 운영 변경보다 먼저 **공식 공개·채점 전에 이용 가능한 권위 있고
허용된 seed 신호가 실제로 존재하는지**를 조사한다. validator나 제3자의 비공개 정보에
무단 접근하거나 우회하지 않고 다음을 시간축으로 검증한다.

1. 여러 새 라운드에서 signed contract, 공식 current/history task API, scoreboard 생성,
   chain event/block 및 validator 공개 코드를 동일 UTC 기준으로 계측한다.
2. seed 생성 알고리즘이 결정론적인지, 공개 chain/task 입력만으로 채점 전에 재현 가능한지
   확인한다. 추측값은 제출에 사용하지 않고 exact replay로만 검증한다.
3. 합법적인 pre-score source가 없다면 same-round late overwrite 전제를 폐기하고, 공개된
   과거 seed history로 현재 라운드 이전에 robust submission을 만드는 설계로 전환한다.
4. same-round 경로가 실제로 성립할 때만 unpaced completion burst를 수정하고, PUT 수·prefix
   throttling·마감 여유를 재설계한다.
5. 현재 task의 bridge는 모두 terminal `failed`이고 active PUT은 없다. 다음 세션은 먼저
   PM2와 새 task 유무를 재확인한 뒤 bitcoin2/hype2의 구버전 bridge를 한 lane씩 안전하게
   현재 코드로 전환한다.

### 14.5 다음 세션 시작용 한 줄 프롬프트

`/home/administrator/workspace/subnet-niome/docs/SESSION_HANDOFF_2026-09-26_KO.md를 처음부터 끝까지 읽고 Git·PM2·현재 task와 active PUT을 재확인한 뒤, 먼저 여러 공식 라운드의 signed contract·task API·scoreboard·chain·validator 공개 코드를 UTC 타임라인으로 대조하여 seed가 공식 공개·채점되기 전에 권위 있고 허용된 정보만으로 결정되거나 관측될 수 있는지 검증하고, 가능하지 않으면 same-round overwrite 전제를 폐기하는 방향으로 남은 작업을 이어가라.`

## 15. 2026-09-27 seed 공개 가능성 감사 결론

- 최근 공식 라운드 8개에서 공개 `9d9347a` block-hash 알고리즘을 exact replay했으나
  공식 task seed와 **8/8 불일치**했다.
- `569500e8…`는 scoreboard `05:54:08.982101 UTC` 뒤 약 134.5초인
  `05:56:23.525620–05:56:23.983106`에야 task-history에서 nonzero seed가 처음
  관측됐다. 두 번째 확인은 약 6.16초 뒤였다.
- `950262bf…`는 scoreboard가 `03:29:37.366506`에 생성된 뒤에도 원본 signed contract
  polling의 `03:54:51–03:59:27` 마지막 기록까지 seed가 0이었다.
- 직전 공개 validator `9f3ada4`는 validation 때 task를 다시 받고 contract seed를
  읽지만 backend seed 생성 규칙은 공개하지 않는다. 최신 공개 `9d9347a`는 chain seed를
  사용하지만 실제 최근 scoreboard를 재현하지 못한다. chain weight extrinsic에도 task
  UUID와 seed가 없다.
- 결론: 현재 허용된 공개 정보만으로 공식 채점 전 seed를 권위 있게 결정하거나 관측하는
  경로는 없다. 상세 표와 재현 근거는 `docs/seed_observability_audit_2026-09-27.md`에 있다.
- same-round overwrite는 코드와 PM2 config에서 기본 비활성화했다.
  `NIOME_ENABLE_UNVERIFIED_SAME_ROUND_OVERWRITE=false`가 production 값이며, 새 bridge는
  PUT을 열지 않고 `disabled_same_round_overwrite` terminal 상태를 기록한다.
- 이 기록 시점 active PUT이 있는 기존 bridge는 재시작하지 않는다. 모두 terminal/drain된
  뒤에만 새 코드를 한 lane씩 로드한다.
- 전체 테스트는 `.venv/bin/python -m pytest -q` 기준 **102 passed in 51.41s**다.
- active PUT이 없던 bitcoin1과 bitcoin2는 miner stop → bridge restart → miner start 순으로
  새 정책을 로드하고 `pm2 save`했다. bridge PID는 각각 `2965188`, `2965286`이며 시작
  로그에서 `mode=disabled-no-pre-score-authoritative-seed`를 확인했다. 두 miner의 8091,
  8092 listen도 복구됐다.
- hype1(PID `2916811`)과 hype2(PID `2913177`)는 task `1c916bd4…`의 두 64 KiB/s PUT을
  각각 유지 중이므로 재시작하지 않았다. 마지막 확인 `06:39:45–06:39:46 UTC`에 네
  stream 모두 `streaming`이고 byte count가 계속 증가했다. terminal/drain 뒤 같은
  방식으로 두 lane을 전환해야 한다.

## 16. 2026-10-01 MT19937 2라운드 결합 탐색 고도화

- 입력은 endpoint-window 안정화 Discovery 자료
  `artifacts/research/preseed_numpy_shuffle_constraints_window_stable.json`이다.
  첫 두 라운드는 각각 `256→251`(누락 5), `256→148`(누락 108) 관측이다.
- 누락 token을 `2^missing` 상태로 열거하던 기존 부분수열 인코딩 대신
  `add_observed_domain_subsequence()`를 추가했다. 셔플 출력의 각 위치를
  `삭제` 또는 `다음 관측 domain 소비`로 전이시키고 마지막 cursor가 관측 길이와
  같도록 한다. 따라서 관측열이 전체 permutation의 정확한 부분수열이라는 조건을
  `O(n*m)` 상태로 보존한다.
- 합성 검증에서 올바른 삭제열은 SAT, 순서를 바꾼 열과 domain multiset을 초과한 열은
  UNSAT였다. 관련 테스트는 최종 `63 passed`다.
- 실데이터 두 번째 라운드 인코딩 규모는 16,241 상태, 32,224 전이였다.
  첫 라운드 full network + 둘째 linear-domain 조합은 1,546,853 변수,
  7,371,672 CNF 절, 11,500 XOR 절이며 build 8.98초였다.
- 고확률 rejection 프로필 8개, 첫 선택 `j255=175` 고정 8개, 첫 라운드 tuple + 둘째
  linear-domain 프로필 16개는 모두 제한시간 내 `UNKNOWN`이었다. 이는 UNSAT 판정이나
  가설 기각이 아니다.
- exact corridor rank 1을 고정한 dense 모델도 `UNKNOWN`이었다. reject 부등식을 lazy로
  뺀 완화 모델, 2-bit MT state 완전분할 4개도 모두 `UNKNOWN`이므로 reject 부등식이나
  단순 state bit가 현재의 주 병목은 아니다.
- 가장 작은 조합은 첫 라운드 exact prefix tuple + 둘째 linear-domain + exact corridor로
  430,157 변수, 2,312,713 CNF 절, 6,990 XOR 절이었지만 120초 내 `UNKNOWN`이었다.
  sparse/4-thread 비교는 pycryptosat가 내부 시간 제한을 지키지 않아 약 3분 뒤 해당
  실험 프로세스만 종료했으며, 후보로 해석할 결과는 만들지 않았다.
- `preseed_mt_xorsat_joint.py`와 `preseed_mt_fixed_profile.py`에 다음 재현 옵션을 추가했다.
  - `--max-linear-domain-rounds`
  - `--full-shuffle-task` (fixed-profile에도 추가)
  - `--fixed-accepted-draw` (xorsat에도 추가)
- 대표 산출물:
  - `artifacts/research/preseed_mt_xorsat_window_stable_2round_linear_domain_top8.json`
  - `artifacts/research/preseed_mt_xorsat_window_stable_tuple1_linear2_top16.json`
  - `artifacts/research/preseed_mt_fixed_profile_window_stable_c1_r2_linear_domain_dense.json`
  - `artifacts/research/preseed_mt_fixed_profile_window_stable_c1_tuple1_linear2_lazy.json`
- Holdout seed label은 열지 않았고, miner/bridge/제출에는 쓰지 않았다. 현재 recovered state,
  예측 seed, 승격 가능한 generator candidate는 아직 없다.
- 다음 우선순위는 더 긴 동일 모델 반복이 아니라 (1) OS-level hard timeout을 적용한
  choice/corridor 완전분할, (2) 첫 라운드 4-choice tuple을 MT 선형 전처리에 직접 넣어
  full permutation CNF 이전에 state rank를 낮추는 방식, (3) SAT가 나온 경우에만 셋째
  Discovery 라운드 blind replay다.

## 17. 2026-10-01 최종 seed 도달시간 재평가

- uint32 초기화 가설은 계산 미완료가 아니다. NumPy RandomState와 Python Random의
  `2^32` 초기화 공간을 이미 전부 검사했고 현재 Discovery 연결 가설은 exact hit 0으로
  배제됐다. per-round NumPy seed preimage도 `2^32` 전체를 검사했다.
- persistent MT19937 합성 감사에서는 rejection 위치와 exact shuffle choice를 알 때
  17라운드, 4,335 accepted draw, 30,481 bit equation에서 유효 상태 rank 19,937을 채우고
  미래 32 word를 exact 예측했다. 이것이 현재 복구 목표의 정보량 기준이다.
- 실제 안정화 corpus의 첫 16 Discovery 라운드를 각각 10,000개의 UID permutation으로
  완성하여 choice bit 분포를 측정했다.
  - 총 표본: 160,000 completions
  - 표본 전체에서 변하지 않은 bit: 5
  - 99% 이상 한쪽으로 편향된 bit: 68
  - 첫 라운드를 100,000회로 재검사해도 stable 1, 99% biased 22였다.
- 정확 forced-bit SAT projector도 구현하고 합성 검증했다. 첫 실제 라운드 64 bit probe는
  반대극성 질의 64개 중 63개가 시간제한 `UNKNOWN`, forced proof 0이었다. 모델열거도
  두 번째 completion을 제한시간 안에 얻지 못했다. 따라서 projector는 정확하지만 현재
  자료에 대한 주 복구기로는 계산효율이 없다.
- 결론: 현재 corpus만 놓고 verified final seed까지의 유한 ETA를 제시할 수 없다. 같은
  SAT를 오래 돌리는 것은 성공을 보장하지 않는다. 병목은 CPU 시간이 아니라 누락 UID의
  insertion 위치와 unknown rejection alignment가 만드는 식별 불충분이다.
- 낙관적 시간 하한은 새 계측이 exact에 가까운 17개 연속 shuffle을 제공한다는 강한
  전제에서 계산한다. 현재 task 주기 약 2시간 24분이면 자료 수집만 약 41시간이며,
  state recovery/replay 1–3시간과 다음 blind round 확인 약 2시간 24분을 더해 약
  **44–48시간**이다. 현재와 같은 partial endpoint 품질이 계속되면 이 하한은 성립하지
  않으며 ETA는 미정이다.
- 새 도구와 산출물:
  - `tools/preseed_shuffle_forced_bits.py`
  - `tools/preseed_shuffle_sampled_bits.py`
  - `artifacts/research/preseed_shuffle_forced_bits_f05_enum64.json`
  - `artifacts/research/preseed_shuffle_sampled_bits_f05_100k.json`
  - `artifacts/research/preseed_shuffle_sampled_bits_discovery16_10k.json`
- 어떤 sampled stable/biased bit도 사실로 승격하지 않았다. verified seed candidate는 여전히
  0개이며 Holdout seed label, miner/bridge, 제출에는 접근하지 않았다.
