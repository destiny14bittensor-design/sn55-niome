# 활성 validator 협력형 seed attestation relay

## 목적

자체 alpha 재고로 NIOME backend의 validator 최소 stake 조건을 충족하지 못할 때,
이미 활동 중인 validator 운영자와 **명시적으로 협력**하여 그 validator가 정상적으로
받은 현재 task seed만 전달한다. backend 자격 증명, validator 지갑 파일, AWS key는
공유하지 않는다.

이 방식은 공개 task-history, W&B, score API보다 빠른 유일한 확인 경로다. 최신 관측
라운드에서 공개 score는 seed보다 약 85초 빨랐지만 모든 miner 채점이 끝난 뒤 한 번에
게시되어 same-round 갱신에는 이미 늦었다.

## 신뢰 경계

- 송신자는 허용 목록에 등록된 활성 validator hotkey여야 한다.
- 송신자는 자기 validator가 정상적으로 다시 받은 contract만 읽는다.
- relay는 backend API를 대신 호출하지 않으며 backend signature나 cookie를 받지 않는다.
- 수신자는 attestation 서명, task UUID, 시각, contract digest를 검증한다.
- seed가 없는 메시지, 과거 task, 만료 메시지, 재전송 nonce는 모두 거부한다.
- 서로 다른 허용 validator 두 곳을 쓸 수 있으면 동일 task/seed 합의 후 실행한다.

## 메시지 형식

서명 대상은 다음 객체의 RFC 8785/JCS canonical JSON 바이트다.

```json
{
  "protocol": "niome-seed-attestation-v1",
  "task_id": "uuid",
  "seeds": [123, 417, 715],
  "contract_sha256": "64-hex",
  "observed_at": "2026-09-27T17:12:40.123456Z",
  "expires_at": "2026-09-27T17:17:40.123456Z",
  "nonce": "128-bit-random-hex",
  "validator_hotkey": "ss58"
}
```

서명은 Bittensor hotkey로 만들고 별도 `signature` 필드에 hex로 붙인다. transport는
mTLS HTTPS 또는 WireGuard 내부 HTTPS를 사용한다. hotkey 서명은 발신자 인증이고,
mTLS/WireGuard는 전송 경로와 rate limit을 보호한다.

## 시간 순서

1. miner가 최초 presigned PUT을 연 뒤 64 KiB profile로 연결을 유지한다.
2. 협력 validator가 validation 단계에서 현재 task를 정상 재조회한다.
3. contract의 non-placeholder seed를 읽고 즉시 attestation을 전송한다.
4. 수신 bridge는 서명과 task 동일성을 검증하고 같은 메시지를 append-only로 보존한다.
5. 남은 URL/연결 시간과 로컬 최적화 예상 시간을 계산한다.
6. 충분할 때만 seed-aware submission을 만들고 exact local replay를 통과시킨다.
7. 기존 PUT body를 원자적으로 끝낸다. 부족하면 현재 제출을 유지하고 실패 폐쇄한다.

validator는 최신 관측상 task 생성 약 90분 뒤 validation을 시작한다. 그러므로 relay는
그 전에 배포되어야 하고, 장기 PUT은 validation 시작까지 살아 있어야 한다. relay를
validation 시작 뒤 설치하는 것은 현재 라운드를 구하지 못한다.

## 수신 조건

- `protocol` exact match
- `task_id`가 현재 열린 PUT의 task와 exact match
- `seeds`가 중복 없는 정수 3개이고 허용 범위 정책을 만족
- `observed_at` clock skew 30초 이내
- `expires_at`이 미래이고 최대 TTL 5분 이내
- `nonce` 미사용
- `validator_hotkey` allowlist match 및 서명 검증 성공
- 선택적으로 두 validator attestation의 seed/contract digest 일치
- 로컬 원본 contract에서 seed 외 필드의 canonical digest 일치

어느 하나라도 실패하면 기존 ordinary submission을 바꾸지 않는다.

## 운영상 필요한 외부 조건

이 경로는 활성 validator 운영자의 동의 없이는 실행할 수 없다. 필요한 것은 validator
지갑이나 backend key가 아니라 다음 세 가지다.

1. 허용할 validator hotkey
2. 송신 agent를 그 운영자의 validator 서버에서 실행할 합의
3. relay mTLS/WireGuard endpoint와 공개 인증서

파트너가 정해지기 전에는 receiver를 production에서 열지 않는다. 인터넷에 열린
무인증 webhook이나 validator 자격 증명 전달 방식은 사용하지 않는다.
