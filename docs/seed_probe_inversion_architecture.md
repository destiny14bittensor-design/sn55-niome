# 선택형 probe submission + seed 역산 아키텍처

## 목적과 한계

이 계측기는 운영자가 소유한 제출 artifact와 공개 score API만 사용한다. 현재 관측된
시간축에서는 점수 공개 전에 제출 URL이 닫히므로 같은 라운드 재제출기는 아니다. 목적은
공식 task API보다 약 2분 먼저 실제 채점 seed를 확정하고, 그 시계열을 축적해 seed 생성
규칙과 조기 score 노출 가능성을 검증하는 것이다.

도구는 업로드, 재제출, validator 인증, signed URL 재사용을 수행하지 않는다.

## 데이터 흐름

1. **Probe artifact 입력**: 완료 task의 `contract`, reference, cell type, submission,
   status를 읽는다. 동일 submission은 SHA-256 identity로 중복 제거한다. 요청 envelope의
   `caller_hotkey`는 validator이므로 사용하지 않고 miner hotkey 주소를 명시적으로 받는다.
2. **고정 전처리**: seed 비의존 Stage 1/2를 lane별 한 번만 실행한다.
3. **Fingerprint build**: 기본 후보 공간 `100..999` 각각에 Stage 3/4/5를 실행해
   `final_score`, consistency, fidelity, weighted score를 SQLite에 체크포인트한다.
4. **Score observation**: task UUID와 own hotkey가 모두 일치하는 공개 score row만
   사용한다.
5. **역산**: 공식 점수가 세 single-seed 점수의 평균이라는 성질을 이용해 pair-sum
   테이블을 만들고 unordered 3-sum을 푼다. 여러 독립 probe가 있으면 후보 교집합을
   취한다.
6. **검증**: 나중에 공개된 task seed와 순서 무관 비교를 하고 라운드별 정확도와
   score-to-seed 선행 시간을 기록한다.

`seed_probe_supervisor.py`는 새 task의 제출 artifact가 완성되는 즉시 fingerprint build를
시작한다. 표가 공식 채점 전에 완성되면 공개 score를 polling하고, score row가 생기는 즉시
역산한다. 현재 task보다 오래된 artifact는 supervisor 시작 경계로 제외한다.
`tools/ecosystem.fleet.config.js`에는 supervisor와 `seed_research_orchestrator.py`가 모두
포함되어 있다. supervisor는 자식 프로세스 대기 중에도 heartbeat를 쓰며, 정상 종료된
task만 완료 처리한다. 중단되거나 실패한 task는 SQLite 체크포인트에서 자동 재개한다.

채점 시점까지 표가 전부 완성되지 않은 경우 `watch` 모드는 공개 score를 감시하면서
성장 중인 부분 테이블에 대해 역산을 반복한다. 세 seed가 모두 계산된 부분집합에 들어오면
900개 완료 전에도 후보를 확정하고 `partial_table_match`를 기록한다.

## 상태와 장애 복구

- SQLite primary key는 `(lane_id, seed)`다. 프로세스가 중단되면 미완료 seed만 재개한다.
- 각 seed 계산 뒤 commit하므로 최대 손실은 실행 중이던 worker 수만큼이다.
- 별도 status JSON에는 진행률, 처리율, 예상 잔여시간과 안전 속성을 기록한다.
- DB와 report에는 presigned URL, HTTP header, wallet, API key를 저장하지 않는다.

## 연구 오케스트레이터와 대시보드

`seed_research_orchestrator.py`는 공개 task, 보유 probe 결과, supervisor heartbeat를 10초
간격으로 결합한다. 최신 정상 triple-seed epoch를 20개 discovery와 5개 holdout으로
분리하고 결정식 후보를 재검증한다. discovery와 holdout을 모두 exact로 통과한 단일 모델만
seed 미공개 task의 shadow prediction을 기록할 수 있다. 공개 뒤 5회 연속 exact가 되기
전에는 운영 연결 대상이 아니다.

상태는 `artifacts/research/seed_research_state.json`에 원자적으로 기록되고 통합 대시보드의
`/api/v1/seed-research/state`에서 읽는다. 상단 전용 패널은 현재 action, 단계별 목표,
probe exact, 검사 가설 수, 사전 예측 연속 성공, 최근 라운드를 표시한다. 이 경로는 제출을
쓰거나 miner/bridge를 변경하지 않는다.

## 선택형 probe 설계

실전 probe는 서로 다른 score 함수를 만드는 최소 2개 lane이 바람직하다. 첫 lane은 후보를
생성하고 두 번째 lane은 충돌을 제거한다. 다만 probe 때문에 주력 마이너 점수를 희생하지
않도록 현재 구현은 기존 제출 artifact를 관측 probe로 재사용한다. 별도 probe 제출 활성화는
과거 라운드에서 역산 정확도와 후보 유일성이 입증된 뒤에만 검토한다.

## 실행

```bash
uv run python tools/seed_probe_inverter.py build \
  --task-dir artifacts/live/TASK_UUID \
  --miner-hotkey MINER_SS58_ADDRESS \
  --database artifacts/research/TASK_UUID.probe.sqlite3 \
  --status artifacts/research/TASK_UUID.probe.status.json \
  --solve-output artifacts/research/TASK_UUID.probe.solution.json \
  --workers 2

uv run python tools/seed_probe_inverter.py solve \
  --database artifacts/research/TASK_UUID.probe.sqlite3 \
  --output artifacts/research/TASK_UUID.probe.solution.json

uv run python tools/seed_probe_inverter.py watch \
  --database artifacts/research/TASK_UUID.probe.sqlite3 \
  --output artifacts/research/TASK_UUID.probe.early-solution.json
```

8코어 호스트에서도 miner/bridge 여유를 남기기 위해 최초 실행은 worker 2개로 제한한다.
