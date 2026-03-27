# Universal Agent Engine

Autonomous Agent Trust-Network PoC — 통신사(SKT) 인프라를 신뢰 거점으로 활용하는 Carrier-Grade 자율 협업 시스템.

## 프로젝트 구조

```
universal-agent-engine/
├── core/                        # 범용 에이전트 엔진
│   ├── engine.py                # 메인 루프 + Webhook + Carrier Dashboard (포트 8000)
│   ├── brain.py                 # OpenAI LLM 인터페이스 (function calling)
│   ├── executor.py              # @tool 데코레이터 + Tool Registry
│   ├── memory.py                # 세션 이벤트 로그
│   └── onboarding_manager.py    # RSA Keypair 생성 + 위임장 관리
├── scenes/                      # 시나리오 정의서 (.md → system prompt)
│   └── hotel_sync_scene.md      # 숙박 동기화 + Challenge-Response 인증 규칙
├── tools/                       # 시나리오별 Tool 모듈
│   ├── site_a_api.py            # Site A 예약 조회
│   ├── site_b_api.py            # Site B 가용 조회/변경 (VPAL 헤더 포함)
│   └── telco_auth_api.py        # Telco 정책 기반 JWT 토큰 발급
├── mocks/                       # PoC용 가상 서비스
│   ├── site_a.py                # 예약 사이트 (포트 8001)
│   ├── site_b.py                # Guardian Agent — RS256 + VPAL 검증 (포트 8002)
│   ├── telco_server.py          # Telco Trust Server — RSA/정책/공증 (포트 8003)
│   └── telco_app_ui.py          # SKT Agent 관리 앱 — 사장님 승인 UI (포트 8004)
├── docs/                        # 설계 문서 (SDD)
│   ├── Architecture.md          # 4대 요소 + VPAL + Kill-switch + MEC
│   ├── Onboarding_Flow.md       # 6단계 온보딩 절차
│   ├── Policy_Schema.md         # 정책 JSON 스키마
│   └── QA_Summary.md            # Q&A 정리
├── logs/                        # 공증 기록 (자동 생성)
│   └── carrier_notary.json      # 토큰 발급/거부/격리 이력
├── run.py                       # 전체 시스템 런처 (5개 프로세스)
├── requirements.txt
└── .env                         # API 키 설정
```

## 설치

```bash
cd universal-agent-engine
pip install -r requirements.txt
```

## 환경 설정

```bash
cp .env.example .env
```

`.env` 파일:
```
OPENAI_API_KEY=sk-your-api-key-here
```

## 서버 실행

### 한 번에 실행 (추천)

```bash
python3 run.py
```

5개 서버가 동시에 실행됩니다:

| 서버 | 포트 | URL | 역할 |
|---|---|---|---|
| 🔐 Telco Trust Server | 8003 | http://localhost:8003 | RSA 키쌍, 정책 기반 토큰 발급, 공증 |
| 📱 SKT Agent 관리 앱 | 8004 | http://localhost:8004 | 사장님 USIM PIN 승인 UI |
| 📅 Site B (Guardian) | 8002 | http://localhost:8002 | RS256 + VPAL 이중 검증 |
| 🧠 Engine | 8000 | http://localhost:8000 | Carrier Dashboard + Agent 루프 |
| 🏨 Site A | 8001 | http://localhost:8001 | 예약 생성/취소 |

종료: `Ctrl+C`

### 개별 실행 (디버깅 시)

각각 별도 터미널에서 **아래 순서대로** 실행:

```bash
# 터미널 1 — Telco Trust Server (먼저)
PYTHONPATH=. python3 -m uvicorn mocks.telco_server:app --port 8003

# 터미널 2 — SKT Agent 관리 앱
PYTHONPATH=. python3 -m uvicorn mocks.telco_app_ui:app --port 8004

# 터미널 3 — Site B (Telco Public Key 필요 → Telco 이후)
PYTHONPATH=. python3 -m uvicorn mocks.site_b:app --port 8002

# 터미널 4 — Engine (온보딩 시 8004 필요)
PYTHONPATH=. python3 -m uvicorn core.engine:app --port 8000

# 터미널 5 — Site A
PYTHONPATH=. python3 -m uvicorn mocks.site_a:app --port 8001
```

## 실행 시나리오

### Phase 0: 최초 1회 온보딩

> `.agent_cert` 파일이 없을 때 자동으로 진행됩니다.

1. `python3 run.py` 실행
2. Engine이 자동으로 RSA Keypair 생성 → 통신사 앱(8004)에 승인 요청 전송
3. **http://localhost:8004** 접속 → PIN `1234` 입력 → [승인] 클릭
4. 위임장 발급 + Engine에 `.agent_cert` 저장
5. http://localhost:8000 대시보드에서 "🎉 온보딩 완료" 확인

### Phase 1: 자율 실행 (Zero-Touch)

> 온보딩 완료 후에는 사장님 개입 없이 자동 동작합니다.

1. **http://localhost:8001** — Site A에서 예약 생성
2. **http://localhost:8000** — 대시보드에서 실시간 흐름 확인:
   ```
   [SITE A]   Webhook 수신 → booking_confirmed
   [AGENT C]  LLM 판단 → block_site_b_dates 호출
   [SITE B]   401 Unauthorized — 토큰 필요
   [AGENT C]  LLM 판단 → get_telco_auth_token 호출
   [TELCO D]  Policy Check ✅ 정책 [booking_sync] 적용됨
   [TELCO D]  VPAL 세션 할당 (Private Slice 터널)
   [TELCO D]  RS256 디지털 서명 토큰 발행
   [AGENT C]  block_site_b_dates_with_token 재시도
   [SITE B]   VPAL + RS256 이중 검증 → 200 OK ✅
   ```
3. **http://localhost:8002** — Site B에서 해당 날짜 차단 확인
4. **http://localhost:8003** — Telco 대시보드에서 공증 기록 확인

### 예약 취소 흐름

1. Site A(8001)에서 예약 목록의 [취소] 클릭
2. Agent가 Site B의 차단을 자동으로 해제 (unblock)

## 유틸리티 명령어

```bash
# 온보딩 초기화 (재테스트)
curl -X POST http://localhost:8000/onboarding/reset

# Kill-switch: Agent 즉시 격리
curl -X POST http://localhost:8003/killswitch/agent_001/isolate

# Kill-switch 해제 (USIM PIN 필요)
curl -X POST http://localhost:8003/killswitch/agent_001/reactivate \
  -H "Content-Type: application/json" \
  -d '{"sim_pin": "1234"}'

# Agent 트래픽 모니터링
curl http://localhost:8003/traffic/agent_001

# 공증 기록 조회
curl http://localhost:8003/notary

# Site B 상태 확인
curl "http://localhost:8002/rooms/room_101/availability?check_in=2026-04-10&check_out=2026-04-12"

# 수동 이벤트 전달 (디버깅)
curl -X POST http://localhost:8000/run \
  -H "Content-Type: application/json" \
  -d '{"event":"booking_confirmed","room_id":"room_101","check_in":"2026-04-10","check_out":"2026-04-12"}'
```

## 기술 스택

| 분류 | 기술 |
|---|---|
| 언어 | Python 3.10+ |
| 프레임워크 | FastAPI + Uvicorn |
| AI | OpenAI API (GPT-4o, function calling) |
| HTTP | httpx (async) |
| 인증 | PyJWT (RS256), cryptography (RSA 2048-bit) |
| 실시간 | SSE (Server-Sent Events) |
| 네트워크 | VPAL 세션 (X-VPAL-Session 헤더) |

## 설계 문서

| 문서 | 내용 |
|---|---|
| [Architecture.md](docs/Architecture.md) | 4대 요소 + VPAL + Kill-switch + MEC |
| [Onboarding_Flow.md](docs/Onboarding_Flow.md) | 6단계 온보딩 절차 + 키 교환 |
| [Policy_Schema.md](docs/Policy_Schema.md) | 정책 JSON 스키마 + 대조 로직 |
