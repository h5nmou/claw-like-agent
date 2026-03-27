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
       ▼                  │ • Kill-switch 모니터링 │
┌──────────────┐          └───────┬───────────────┘
│  Agent C      │◄── 토큰 발급 ───┘
│  (The Brain)  │
│  Port: 8000   │
│               │         ════════════════════════
│ • LLM 판단    │         ║  VPAL Tunnel (암호화)  ║
│ • 온보딩 매니저│════════╗║                       ║
│ • RSA Keypair │        ║║                       ║
└──────────────┘        ║║                       ║
                         ║▼                       ║
                    ┌─────╨──────────────────┐    ║
                    │  Site B (Guardian)      │    ║
                    │  Port: 8002             │    ║
                    │                         │    ║
                    │ • VPAL 세션 검증         │    ║
                    │ • RS256 토큰 검증        │    ║
                    │ • 비인증 접근 비가시화    │    ║
                    └─────────────────────────┘    ║
                         ════════════════════════
                         ↑ SKT MEC Edge Zone 내부
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
  │                  │                  3. VPAL 세션 할당      │
  │                  │                  4. 트래픽 카운터 증가   │
  │                  │                  → 사장님 승인 불필요!   │
  │                  │                      │                  │
  │                  │                  5. JWT + VPAL 세션 발급 │
  │                  │◄── signed JWT ───────│                  │
  │                  │    + vpal_session_id  │                  │
  │                  │                      │                  │
  │                  │  6. VPAL 터널 통신    │                  │
  │                  │── Bearer JWT ─────────────────────────→│
  │                  │   X-VPAL-Session      │                  │
  │                  │                      │  7. VPAL + 서명  │
  │                  │                      │     이중 검증    │
  │                  │◄──────────────────────────── 200 OK ────│
  │                  │                      │                  │
  │                  │                  8. 공증 기록           │
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

---

## 7. Network Sandboxing & Isolation

### 7.1 Virtual Private Agent Link (VPAL)

Agent C와 Site B 간의 통신은 **공용 인터넷이 아닌 통신사 전용 사설 네트워크** 내에서 이루어집니다.

```
┌─────────────────────────────────────────────────────────────┐
│                  SKT Private Network                         │
│              (Virtual Private Agent Link)                    │
│                                                              │
│   ┌───────────┐    VPAL Tunnel    ┌───────────────────┐      │
│   │  Agent C   │◄═══════════════►│  Site B (Guardian)  │      │
│   │            │  X-VPAL-Session  │                    │      │
│   │            │  헤더로 세션 식별 │  VPAL 세션 없으면   │      │
│   └───────────┘                  │  인터페이스 비가시화 │      │
│        │                          └───────────────────┘      │
│        │                                                      │
│   ┌────▼──────────────┐                                      │
│   │  Service D (Telco) │                                      │
│   │  토큰 발급 시       │                                      │
│   │  VPAL 세션 ID 포함  │                                      │
│   └───────────────────┘                                      │
│                                                              │
└──────────────────────────────────────────────────────────────┘
          ╳ 공용 인터넷에서 Site B 에이전트 인터페이스 접근 불가
```

**작동 원리:**

| 단계 | 설명 |
|---|---|
| 1. VPAL 세션 할당 | Service D가 JWT 발급 시 `vpal_session_id`를 토큰에 포함 |
| 2. 터널 식별 | Agent → Site B 요청에 `X-VPAL-Session` 헤더 부착 |
| 3. 가시화 제어 | Site B는 유효한 VPAL 세션이 없는 요청의 에이전트 인터페이스를 숨김 |
| 4. 세션 격리 | 각 Agent는 자신의 VPAL 세션 내에서만 통신 (다른 Agent 트래픽과 격리) |

**PoC 시뮬레이션:**
- Service D가 토큰 발급 시 `vpal_session_id`를 JWT payload에 포함
- Agent의 `_with_token` Tool이 `X-VPAL-Session` 헤더를 자동 부착
- Site B가 토큰 내 `vpal_session_id`와 헤더값 일치를 이중 검증

---

### 7.2 Network-Level Kill-switch

Service D는 Agent의 트래픽을 **실시간 모니터링**하며, 정책 위반 감지 시 네트워크 슬라이스를 즉시 회수합니다.

```
정상 트래픽                           비정상 감지 시
────────────                         ────────────────
Agent → Service D → Site B           Agent → Service D ──╳──→ (차단됨)
       ↑                                       │
   모니터링                              Kill-switch 발동
   (rate, scope)                         │
                                         ├─ VPAL 세션 즉시 무효화
                                         ├─ 해당 Agent 토큰 거부
                                         ├─ 공증 기록: "agent_isolated"
                                         └─ 사장님에게 알림
```

**감지 기준:**

| 위반 유형 | 기준 | Kill-switch 동작 |
|---|---|---|
| 비정상 요청 빈도 | `> max_requests_per_hour` | 세션 일시 정지 (30분) |
| 권한 외 리소스 접근 | Policy에 없는 resource | 해당 요청 차단 + 경고 |
| 데이터 외부 반출 시도 | VPAL 외부 목적지 감지 | **즉시 세션 회수 + 격리** |
| 토큰 재사용 | 동일 JTI 재사용 | 토큰 거부 + 에이전트 정지 |

**격리 해제:** 사장님이 통신사 앱에서 직접 "재활성화" 승인 (USIM PIN 재인증).

**공증 기록:**
```json
{
  "timestamp": "2026-03-27T15:30:00Z",
  "event": "agent_isolated",
  "agent_id": "agent_001",
  "reason": "rate_limit_exceeded",
  "requests_count": 105,
  "policy_limit": 100,
  "action_taken": "vpal_session_revoked",
  "reactivation_required": "usim_pin"
}
```

---

### 7.3 MEC (Multi-access Edge Computing) 기반 처리

인증(Service D)과 실행(Site B)은 **통신사 에지(Edge) 환경** 내에서 처리됩니다.

```
┌──────────────────────────────────────────────────────────┐
│                    SKT MEC Edge Zone                      │
│              (통신사 에지 데이터센터 내부)                  │
│                                                           │
│  ┌────────────────┐    ┌────────────────────────────┐     │
│  │  Service D      │    │  Site B (Guardian Agent)   │     │
│  │  (Telco Trust)  │    │                            │     │
│  │                 │    │  • 가용 DB (인메모리)       │     │
│  │  • USIM 인증    │    │  • RS256 검증              │     │
│  │  • 정책 DB      │    │  • VPAL 세션 관리          │     │
│  │  • RSA 키쌍     │    │                            │     │
│  │  • 공증 기록    │    │  데이터가 Edge Zone 내에    │     │
│  │  • Kill-switch  │    │  머물러 외부 유출 불가      │     │
│  └────────┬───────┘    └───────────┬──────────────┘     │
│           │        VPAL            │                      │
│           └────────────────────────┘                      │
│                        ▲                                  │
│________________________│__________________________________│
                         │ VPAL Tunnel (암호화)
                         │
              ┌──────────┴──────────┐
              │  Agent C (외부)      │
              │  사장님의 서버/PC    │
              │                     │
              │  Edge Zone 내부에    │
              │  직접 접근 불가.     │
              │  오직 VPAL 터널을   │
              │  통해서만 통신.      │
              └─────────────────────┘
```

**MEC 보안 보장:**

| 보안 계층 | 설명 |
|---|---|
| **물리적 격리** | Service D, Site B는 통신사 에지 데이터센터에 위치 |
| **네트워크 격리** | VPAL 터널 외 접근 차단. 공용 인터넷 노출 없음 |
| **데이터 잔류** | 예약/가용 데이터는 Edge Zone 내에서만 처리·저장 |
| **처리 지연** | 에지 처리로 < 10ms 지연 (클라우드 대비 1/10) |
| **주권 보장** | 데이터가 국내 통신사 인프라를 벗어나지 않음 |

**현실 매핑:**

| PoC (localhost) | 실제 환경 |
|---|---|
| `localhost:8002` (Site B) | SKT MEC Zone 내 컨테이너 |
| `localhost:8003` (Service D) | SKT MEC Zone 내 인증 서버 |
| VPAL 세션 헤더 | 5G Network Slice + IPsec 터널 |
| Kill-switch (코드) | SDN 컨트롤러의 플로우 룰 삭제 |
