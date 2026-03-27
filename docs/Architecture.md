# Architecture: Autonomous Agent Trust-Network

## 1. 시스템 개요

```
사장님이 처음 단 한 번만 승인하면, 이후로는 통신망이 알아서 보안을 책임진다.
```

4대 요소가 **비대칭 키 암호(RSA)** 와 **정책 기반 자율 위임**을 통해 Zero-Touch 자율 협업을 수행합니다.

## 2. 4대 요소

```
┌──────────────┐         ┌──────────────────────┐
│  Site A       │         │  Service D            │
│  (Trigger)    │         │  (Telco Trust Server) │
│  Port: 8001   │         │  Port: 8003           │
│               │         │                       │
│ 예약 이벤트    │         │ • USIM 인증            │
│ 발생 → Webhook│         │ • 정책(Policy) 저장    │
└──────┬────────┘         │ • RSA Private Key 서명 │
       │                  │ • 공증 기록 (Notary)   │
       ▼                  └───────┬───────────────┘
┌──────────────┐                  │
│  Agent C      │◄── 토큰 발급 ───┘
│  (The Brain)  │
│  Port: 8000   │
│               │
│ • LLM 판단    │
│ • 온보딩 매니저│──── 인증된 토큰 ────┐
│ • RSA Keypair │                     │
└──────────────┘                     ▼
                          ┌──────────────────────┐
                          │  Site B               │
                          │  (Guardian Agent)     │
                          │  Port: 8002           │
                          │                       │
                          │ • 토큰 없으면 거부(401)│
                          │ • Public Key로 서명 검증│
                          │ • 정책 범위 내만 허용   │
                          └──────────────────────┘
```

## 3. 신뢰 모델: 2-Phase 설계

### Phase 0: 최초 1회 온보딩 (The Handshake)

```
사장님(홍길동)                 Agent C               Service D (Telco)
      │                          │                         │
      │  1. Agent 최초 구동       │                         │
      │◄── "승인이 필요합니다" ───│                         │
      │                          │                         │
      │  2. 통신사 앱(Mock) 열기  │                         │
      │────── USIM PIN 입력 ────────────────────────────→  │
      │                          │                  USIM 검증│
      │                          │                         │
      │  3. 에이전트 권한 확인    │                         │
      │◄── "booking_sync 권한   ─────────────────────────  │
      │     승인하시겠습니까?" ──│                         │
      │                          │                         │
      │  4. 승인 (USIM 서명)     │                         │
      │──────────────────────────────────────────────────→  │
      │                          │                         │
      │                          │  5. Agent Public Key 등록│
      │                          │──── RSA 공개키 전송 ──→  │
      │                          │                         │
      │                          │  6. 마스터 위임장 수신   │
      │                          │◄── {agent_id,           │
      │                          │     policies: [...],     │
      │                          │     expires: 365d,       │
      │                          │     signed_by: Telco} ──│
      │                          │                         │
      ▼                          ▼                         ▼
  "세팅 완료"            위임장 저장 (.agent_cert)    Agent 등록 완료
```

**핵심:** 이 과정은 **딱 1회만** 수행됩니다. 이후 Agent는 위임장(`.agent_cert`)을 가지고 있으므로 사장님의 추가 개입 없이 자동으로 토큰을 발급받습니다.

### Phase 1: 자율 실행 루프 (The Autonomous Loop)

```
Site A             Agent C              Service D           Site B
  │                  │                      │                  │
  │ booking_confirmed│                      │                  │
  │─── Webhook ─────→│                      │                  │
  │                  │                      │                  │
  │                  │ 1. 토큰 요청         │                  │
  │                  │   {agent_id,         │                  │
  │                  │    action: "block",  │                  │
  │                  │    resource: room_101}│                  │
  │                  │─────────────────────→│                  │
  │                  │                      │                  │
  │                  │                  2. 정책 대조           │
  │                  │                  "booking_sync 권한 있음"│
  │                  │                  "유효기간 내"           │
  │                  │                  → 사장님 승인 불필요!   │
  │                  │                      │                  │
  │                  │                  3. JWT 생성 + RSA 서명  │
  │                  │◄── signed JWT ───────│                  │
  │                  │                      │                  │
  │                  │  4. 인증된 요청       │                  │
  │                  │─────────────────────────── Bearer JWT ─→│
  │                  │                      │                  │
  │                  │                      │  5. 서명 검증     │
  │                  │                      │  Telco Public Key│
  │                  │                      │  로 JWT 검증     │
  │                  │                      │  → 유효! 실행    │
  │                  │◄──────────────────────────── 200 OK ────│
  │                  │                      │                  │
  │                  │                  6. 공증 기록           │
  │                  │──── notary log ─────→│                  │
```

## 4. 암호학적 키 구조

```
┌─────────────────────────┐
│   Telco Trust Server    │
│                         │
│  RSA Keypair:           │
│  ├─ Private Key (서명용) │ ← 절대 외부 유출 안 됨
│  └─ Public Key (검증용)  │ ← Site B에 공개 배포
│                         │
│  저장소:                 │
│  ├─ registered_agents{} │ ← Agent 공개키 + 정책
│  └─ notary_log[]        │ ← 공증 기록
└─────────────────────────┘

┌─────────────────────────┐
│   Agent C               │
│                         │
│  RSA Keypair:           │
│  ├─ Private Key (요청서명)│ ← Agent만 보유
│  └─ Public Key           │ ← 온보딩 시 Telco에 등록
│                         │
│  .agent_cert:           │
│  └─ 마스터 위임장        │ ← Telco가 서명한 인증서
└─────────────────────────┘

┌─────────────────────────┐
│   Site B                │
│                         │
│  Telco Public Key 보유  │ ← 토큰 서명 검증용
│  (하드코딩 또는 배포)    │
└─────────────────────────┘
```

## 5. HS256 → RS256 전환

| 항목 | 현재 PoC (HS256) | 신뢰 모델 (RS256) |
|---|---|---|
| 서명 방식 | 공유 시크릿 (대칭키) | RSA Private/Public (비대칭키) |
| 검증 주체 | 같은 시크릿 보유자 | Public Key만 있으면 누구나 검증 |
| 보안 수준 | Site B가 시크릿 알아야 함 | Site B는 Public Key만 필요 |
| 핵심 차이 | **서명자 = 검증자** | **서명자 ≠ 검증자 (분리)** |

## 6. 공증 기록 (Carrier Notary)

모든 토큰 발급·사용 이력이 `logs/carrier_notary.json`에 기록됩니다:

```json
{
  "timestamp": "2026-03-27T14:30:00Z",
  "event": "token_issued",
  "agent_id": "agent_001",
  "sim_owner": "홍길동",
  "action": "block_site_b_dates",
  "resource": "room_101",
  "policy_matched": "booking_sync",
  "token_jti": "unique-token-id",
  "telco_signature": "RSA_SHA256:abc123..."
}
```
