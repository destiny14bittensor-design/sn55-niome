# NIOME VPS 이전 절차

이 문서는 코드와 런타임 상태를 다른 VPS에서 최대한 동일하게 재현하기
위한 체크리스트다. Git은 소스·테스트·문서만 전달한다. 지갑, 자격증명,
마이너 산출물, 연구 데이터와 `chr11.fa`는 의도적으로 Git에서 제외한다.

## 고정된 실행 기준

- Ubuntu 계열 호스트
- Python `3.12` 가상환경 (`uv.lock`의 frozen dependency 사용)
- Node.js `22` 및 PM2 `7`
- Bittensor network `finney`, netuid `55`
- 마이너 포트 `8091..8094`, 대시보드 포트 `8111`
- 정책 매핑:
  - tao1: `champion-v1` (control)
  - tao2: `champion-reservoir001-cas55-v3`
  - won1: `champion-reservoir003-cas65-v3`
  - won2: `champion-reservoir005-v3`

실제 지갑 경로, 지갑명, 핫키명, 공개 IP는 `.env` 또는 셸 환경변수로
지정한다. `tools/ecosystem.fleet.config.js`는 `NIOME_WALLET_PATH`,
`NIOME_EXTERNAL_IP`와 lane별 `NIOME_*_WALLET`, `NIOME_*_HOTKEY`를
지원한다.

## Git으로 이전되는 것

- 일반 제출 빌더와 네 개 정책 포트폴리오
- 등록/설정 gate 및 portfolio audit/backtest
- 누출 방지형 로컬 점수 보정 코드와 builder
- 사전 seed 부분예측·점수 replay 연구 코드, 테스트 및 결론 문서
- 간소화된 일반 제출 대시보드
- PM2 fleet/dashboard 설정

## Git과 별도로 이전해야 하는 것

다음은 `.gitignore`에 포함되므로 기존 VPS를 폐기하기 전에 별도 전송한다.

- `artifacts/` (현재 태스크 이력, 로컬 검증, 연구 데이터, 보정 모델)
- `data/chr11.fa`
- 원격 서버에 아직 없는 경우에만 W&B 설정 등 외부 자격증명

지갑 디렉터리는 아카이브하거나 Git에 넣지 않는다. 원격 서버에 이미 있는
지갑을 사용하며 coldkey/hotkey 파일 권한을 유지한다.

예시(기존 VPS에서 실행하되 목적지는 실제 원격 계정/주소로 바꾼다):

```bash
cd /home/administrator/workspace/subnet-niome
tar --zstd -cf /tmp/niome-runtime-2026-10-02.tar.zst artifacts data/chr11.fa
sha256sum /tmp/niome-runtime-2026-10-02.tar.zst \
  > /tmp/niome-runtime-2026-10-02.tar.zst.sha256
scp /tmp/niome-runtime-2026-10-02.tar.zst* REMOTE_USER@REMOTE_HOST:/tmp/
```

원격에서는 정확한 Git 커밋을 checkout한 뒤 저장소 루트에서 아카이브를
푼다. 소유권과 쓰기 권한을 확인하고, 절대경로가 남아 있지 않은지
`rg '/home/administrator' artifacts -g '*.json'`으로 점검한다.

## 안전한 전환 순서

1. 원격의 기존 PM2 목록, 환경변수, 지갑 경로, 핫키 SS58와 netuid 55 등록
   상태를 읽기 전용으로 기록한다.
2. 저장소를 정확한 전달 커밋으로 맞추고 `uv sync --frozen`을 실행한다.
3. 런타임 아카이브를 복원하고 `.env.example`을 참고해 호스트 값만 설정한다.
4. 전체 관련 테스트와 `tools/miner_gate0.py`를 통과시킨다.
5. 실행 중인 PUT/제출이 없는 라운드 경계에서 기존 프로세스를 중지한다.
6. 먼저 dashboard, 다음 control(tao1), 마지막 세 canary를 순차 기동한다.
7. 포트 listen, PM2 env의 정책명, axon 광고 IP, 신규 쿼리의
   `builder_diagnostics.json`을 확인한다.
8. 한 라운드의 제출 및 공식 점수까지 확인한 뒤 `pm2 save`한다.

과거 seed 연구 supervisor와 probe collector는 중지 상태를 유지한다. 현재
운영 목표는 권위 seed 재제출이 아니라 네 개의 검증된 일반 제출 정책을
분산 운용해 best-of-four rank 80 이내를 달성하는 것이다.
