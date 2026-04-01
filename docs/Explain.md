# Explain: Autonomous Agent Trust-Network PoC 상세 해설

> 이 문서는 프로젝트의 모든 구성 요소를 **서버 종류 → 구동 순서 → 각 단계의 의미 → 사용 기술**까지 한 문서에서 설명합니다.

---

## 1. 서버 구성 (5개)

| # | 서버 이름 | 포트 | 역할 요약 | 현실 세계 대응 |
|---|---|---|---|---|
| ① | **Telco Trust Server** (Service D) | 8003 | 통신사의 인증·서명·공증 서버 | SKT 인증 플랫폼 |
| ② | **SKT Agent 관리 앱** | 8004 | 사장님이 Agent를 승인하는 모바일 앱 | 통신사 고객 앱 |
| ③ | **Site B** (Guardian Agent) | 8002 | 인증 없이는 데이터 변경을 거부하는 보호 사이트 | B 예약 플랫폼 API |
| ④ | **Engine** (Agent C) | 8000 | LLM 기반 자율 판단 엔진 + 대시보드 | AI Agent 서버 |
| ⑤ | **Site A** (Trigger Source) | 8001 | 예약 이벤트가 발생하는 소스 사이트 | A 예약 플랫폼 |

---

## 2. 서버 구동 순서와 이유

```bash
python3 run.py
```

```
① Telco (8003)  ←── 가장 먼저. RSA 키쌍을 생성해야 다른 서버가 Public Key를 가져갈 수 있음
       │
② Telco App (8004)  ←── Telco에 의존. 승인 시 Telco에 요청을 보냄
       │
③ Site B (8002)  ←── Telco의 Public Key를 가져와 토큰 검증에 사용
       │
④ Engine (8000)  ←── 시작 시 .agent_cert 확인 → 없으면 Telco App(8004)에 승인 요청
       │
⑤ Site A (8001)  ←── 마지막. 예약 생성 시 Engine(8000)에 Webhook을 보내므로 Engine이 먼저 떠야 함
```

**핵심:** 의존성 방향은 `Telco → Site B → Engine → Site A` 입니다.

---

## 3. Phase 0: 최초 1회 온보딩 (The Handshake)

> 사장님이 **딱 한 번만** 수행하는 신뢰 설정 과정.

### Step-by-Step

| 단계 | 누가 | 무엇을 | 왜 | 기술 |
|---|---|---|---|---|
| **3-1** | Engine | `.agent_cert` 파일 존재 확인 | 이전에 온보딩했는지 판단 | `pathlib` 파일 존재 체크 |
| **3-2** | Engine | RSA 2048-bit Keypair 생성 | Agent의 고유 신원증명용 키쌍 | `cryptography.hazmat.primitives.asymmetric.rsa` |
| **3-3** | Engine | 통신사 앱(8004)에 승인 요청 전송 | 사장님에게 "이 Agent 승인해주세요" | `httpx` POST → `/pending` |
| **3-4** | 사장님 | 통신사 앱(8004) 접속 → PIN 입력 → [승인] | USIM 기반 본인 인증 + 권한 위임 | HTML Form → FastAPI `/approve` |
| **3-5** | Telco App | Telco Server(8003)에 온보딩 요청 | USIM 검증 + Agent 등록 + 위임장 발급 | `httpx` POST → `/onboarding/approve` |
| **3-6** | Telco Server | USIM DB 검증 → Agent 등록 → 위임장 생성 | 통신사가 Agent를 공식 인증 | `pydantic` 검증, `dict` DB |
| **3-7** | Telco App | Engine(8000)에 위임장 전달 | Agent가 위임장을 로컬에 저장 | `httpx` POST → `/onboarding/complete` |
| **3-8** | Engine | `.agent_cert` + `.telco_public.pem` 저장 | 이후 재시작 시 온보딩 불필요 | `json.dump`, `pathlib.write_text` |

### 생성되는 파일들

| 파일 | 내용 | 생성자 |
|---|---|---|
| `.agent_private.pem` | Agent RSA Private Key | Engine |
| `.agent_public.pem` | Agent RSA Public Key | Engine |
| `.agent_cert` | 위임장 (Telco 서명, 정책, 만료일) | Telco → Engine |
| `.telco_public.pem` | Telco의 Public Key | Telco → Engine |

---

## 4. Phase 1: 자율 실행 (Zero-Touch Autonomous Loop)

> 온보딩 완료 후, **사장님 개입 없이** 자동으로 동작하는 핵심 루프.

### 예: "Site A에서 예약 생성 → Site B에 날짜 차단"

| 단계 | 대시보드 표시 | 의미 | 기술 |
|---|---|---|---|
| **4-1** | `📩 [SITE A] Webhook 수신` | Site A에서 예약이 확정됨. Engine에 Webhook으로 알림 | FastAPI `POST /webhook`, JSON |
| **4-2** | `📤 [AGENT C] LLM 요청 구성` | Scene(규칙) + Tools(함수) + Event(데이터)를 LLM에 전달 | OpenAI Chat Completions API |
| **4-3** | `🧠 [AGENT C] Brain` | LLM이 상황을 분석하고 어떤 Tool을 호출할지 판단 | GPT-4o function calling |
| **4-4** | `💡 [AGENT C] Reasoning` | LLM 판단: "먼저 Site B 상태를 조회해야 함" | function calling response |
| **4-5** | `🔧 [SITE B] get_site_b_availability` | Site B의 현재 가용 상태를 조회 (인증 불필요) | `httpx` GET → Site B API |
| **4-6** | `🔧 [SITE B] block_site_b_dates` | 날짜 차단 시도 → **401 Unauthorized** 반환 | `httpx` PATCH → Site B (토큰 없음) |
| **4-7** | `🔧 [TELCO D] get_telco_auth_token` | 통신사에 토큰 발급 요청 | `httpx` POST → Telco `/auth/token` |
| **4-8** | `📜 [TELCO D] Policy Check` | 사장님이 사전 승인한 정책 [booking_sync]에 매칭 확인 | 정책 DB 대조 (action/resource/target) |
| **4-9** | `🌐 [TELCO D] Network Scan` | VPAL 세션 할당 — Private Slice 터널 활성화 | `uuid4()` 세션 생성 |
| **4-10** | `🔐 [TELCO D] Signature Issuance` | RS256 Private Key로 JWT 디지털 서명 | `PyJWT` RS256 + `cryptography` RSA |
| **4-11** | `🔧 [SITE B] block_with_token` | 토큰 + VPAL 세션 포함하여 재시도 | `httpx` PATCH + `Authorization` + `X-VPAL-Session` |
| **4-12** | Site B 내부 | JWT 서명 검증 (Telco Public Key) + VPAL 세션 이중 확인 | `PyJWT` RS256 decode, 헤더 매칭 |
| **4-13** | `📋 [SITE B] 200 OK` | 날짜 차단 성공 | FastAPI JSONResponse |
| **4-14** | `✅ 작업 완료` | LLM이 결과를 요약하여 텍스트로 응답 | GPT-4o 텍스트 응답 |

---

## 5. 보안 계층 상세

### 5-1. JWT 토큰 (RS256)

```
┌─ Header ─────────────────────────┐
│ alg: RS256                        │  ← RSA + SHA-256
│ typ: JWT                          │
├─ Payload ────────────────────────┤
│ agent_id: "agent_001"             │  ← 누가
│ action: "block"                   │  ← 무엇을
│ resource: "room_101"              │  ← 어디에
│ policy: "booking_sync"            │  ← 어떤 권한으로
│ vpal_session_id: "bdf295aa-..."   │  ← 어떤 터널로
│ exp: 1774594549                   │  ← 5분 유효
├─ Signature ──────────────────────┤
│ RSA_SHA256(header + payload,      │
│            Telco Private Key)     │  ← 통신사만 서명 가능
└──────────────────────────────────┘
```

| 항목 | 설명 |
|---|---|
| **서명자** | Telco Trust Server (Private Key 보유) |
| **검증자** | Site B (Public Key만 보유) |
| **핵심** | 서명자 ≠ 검증자 — Site B는 서명할 수 없고 검증만 가능 |

### 5-2. VPAL (Virtual Private Agent Link)

```
Public Internet                    SKT Private Slice
┌───────────┐     VPAL 터널      ┌─────────────────┐
│ Agent C    │═══════════════════►│ Telco D + Site B │
│ Site A     │  X-VPAL-Session   │ (MEC Edge Zone)  │
└───────────┘                    └─────────────────┘
     ╳ 공용 인터넷으로 Site B 직접 접근 불가
```

- 토큰 발급 시 `vpal_session_id` 포함
- Agent가 `X-VPAL-Session` 헤더로 전송
- Site B가 토큰 내 값과 헤더를 이중 비교

### 5-3. Kill-switch

| 감지 | 동작 | 해제 |
|---|---|---|
| 시간당 100회 초과 | 자동 격리 + VPAL 세션 회수 | 사장님 USIM PIN 재인증 |
| 권한 외 리소스 접근 | 요청 거부 + 경고 | 자동 해제 없음 |
| 수동 발동 | `POST /killswitch/{id}/isolate` | `POST /killswitch/{id}/reactivate` |

### 5-4. Carrier Notary (공증 기록)

모든 인증 이벤트가 `logs/carrier_notary.json`에 기록됩니다:

| 기록 항목 | 예시 |
|---|---|
| 타임스탬프 | `2026-03-27T15:00:00Z` |
| 이벤트 | `token_issued`, `agent_isolated`, `agent_registered` |
| Agent | `agent_001` |
| 정책 | `booking_sync` |
| 토큰 JTI | 고유 식별자 (재사용 감지용) |

---

## 6. 사용 기술 총정리

### 서버별 기술

| 서버 | 프레임워크 | 핵심 기술 | 라이브러리 |
|---|---|---|---|
| Telco (8003) | FastAPI | RSA 키 생성, RS256 JWT 서명, 정책 DB, VPAL 세션 | `cryptography`, `PyJWT`, `uuid` |
| Telco App (8004) | FastAPI | HTML Form, USIM PIN 인증 대행 | `httpx` (Telco/Engine 연동) |
| Site B (8002) | FastAPI | RS256 JWT 검증, VPAL 이중 검증, Lazy Key Loading | `PyJWT`, `cryptography`, `httpx` |
| Engine (8000) | FastAPI | LLM 연동, SSE 실시간 로그, 온보딩 매니저 | `openai`, `sse-starlette`, `httpx` |
| Site A (8001) | FastAPI | 예약 CRUD, Webhook 전송 | `httpx`, HTML UI |

### 공통 기술

| 분류 | 기술 | 용도 |
|---|---|---|
| 비대칭 암호 | RSA 2048-bit | Agent 신원증명, 토큰 서명/검증 |
| 토큰 | JWT (RFC 7519) | 인증 정보를 자기 완결적으로 전달 |
| 서명 알고리즘 | RS256 | RSA + SHA-256 디지털 서명 |
| 네트워크 격리 | VPAL 세션 | 헤더 기반 가상 사설 터널 시뮬레이션 |
| AI 추론 | GPT-4o function calling | 상황 분석 → Tool 선택 → 실행 |
| 실시간 | SSE (Server-Sent Events) | 대시보드 로그 스트리밍 |
| HTTP | httpx (async) | 서버 간 비동기 통신 |
| 프로세스 | multiprocessing | 5개 서버 동시 구동 |

### 암호학 파이프라인

```
[온보딩]
cryptography.rsa.generate_private_key(2048)
  → Private Key (Agent 로컬)
  → Public Key (Telco에 등록)

[토큰 발급]
Telco Private Key + JWT Payload
  → PyJWT.encode(payload, private_key, algorithm="RS256")
  → 서명된 JWT 토큰

[토큰 검증]
Site B가 Telco Public Key로 검증
  → PyJWT.decode(token, public_key, algorithms=["RS256"])
  → payload 추출 + 서명 유효성 확인
```

---

## 7. 대시보드 UI 구성

| 영역 | 내용 |
|---|---|
| **상단 — Network Topology** | Public Internet ↔ VPAL ↔ SKT Private Slice 도식 (애니메이션 점) |
| **중앙 — Semantic Log** | `[SITE A]` `[AGENT C]` `[TELCO D]` `[SITE B]` 역할별 컬러 로그 |
| **하단 — Carrier Notary** | 공증 기록 테이블 (3초 폴링, 이벤트/인증/정책/서명/결과) |

### 로그 컬러 체계

| 로그 타입 | 색상 | 의미 |
|---|---|---|
| `📩 [SITE A]` | 🟡 골드 | 예약 이벤트 발생 |
| `🧠 [AGENT C]` | 🟣 보라 | LLM 판단/추론 |
| `📜 [TELCO D] Policy Check` | 🟠 앰버 (강조) | 정책 자동 승인 사유 |
| `🌐 [TELCO D] Network Scan` | 🔵 시안 (강조) | VPAL 세션 할당 |
| `🔐 [TELCO D] Signature` | 💜 라벤더 (강조) | RS256 서명 발행 |
| `📋 [SITE B]` | 🟢 초록 | 실행 결과 |
| `✅ 작업 완료` | 🟢 진녹 | 전체 루프 완료 |
