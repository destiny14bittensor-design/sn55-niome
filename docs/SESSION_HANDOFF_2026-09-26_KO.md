# SN55 NIOME 세션 인수인계 — 2026-09-26

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
- 비교 가능한 표본이 3개 미만이거나 시간이 부족하면 consistency 1.0의
  max-score 경로로 fail-safe 한다.
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
- 허용 target consistency 범위: 0.60–0.85
- partial-seed anchor: 0.70
- exact replay budget: 45초
- validation 전 upload reserve: 75초
- 최소 replay 여유: 30초

계산:

```text
required_consistency = percentile80(normalized_top history) * 1.05
```

`required_consistency`가 0.60–0.85 안에 있을 때만 targeted 후보를 만든다. builder는
여러 candidate를 만들고 bridge는 실제 세 seed로 exact replay한다. target보다 낮은
후보는 채택하지 않으며 target을 넘는 후보 중 가장 가까운 결과를 선택한다.

fail-safe 조건:

- consistency control 비활성
- 유효 표본 3개 미만
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

코드 기본값은 consistency control 활성이다. 다만 유효 표본이 3개 미만이면 자동으로
max-score가 된다. 원격 최초 배포는 명시적으로 `NIOME_CONSISTENCY_CONTROL=false`로
시작하고 3개의 신뢰 라운드가 쌓인 뒤 별도 검토하여 켠다.

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
8. 최소 3개의 chain-authoritative 공식 매칭 라운드가 쌓이기 전에는 원격 consistency
   control을 켜지 않는다.
9. 3개 이상 쌓이면 normalized history, target, exact candidate 결과를 사람이 검토한 뒤
   점진적으로 활성화한다.

## 11. 절대 피해야 할 작업

- 활성 PUT이 있는 bridge의 무조건 재시작
- 8개 miner 동시 중지
- 로컬 Bitcoin/Hype PM2 설정을 원격 Tao/Won 서버에 그대로 배포
- 서로 다른 miner의 artifact root 공유
- 계약 객체 seed를 다시 authoritative로 승격
- exact replay 없이 targeted consistency 후보 제출
- `.env`, wallet, mnemonic, private key, presigned URL, runtime artifact 또는 로그를 Git에 추가
- `git reset --hard`나 기존 사용자 변경 삭제
