# Universal Agent Engine

OpenClaw 스타일의 자율형 에이전트 시스템. 범용 엔진 + 교체 가능한 Scene & Tool 구조.

## 프로젝트 구조

```
universal-agent-engine/
├── core/           # 범용 에이전트 엔진
│   ├── engine.py   # 메인 루프 + Webhook 서버 + 로그 대시보드 (포트 8000)
│   ├── brain.py    # OpenAI LLM 인터페이스 (function calling)
│   ├── executor.py # @tool 데코레이터 + Tool Registry
│   └── memory.py   # 세션 이벤트 로그
├── scenes/         # 시나리오 정의서 (.md → system prompt)
├── tools/          # 시나리오별 Tool 모듈 (.py)
├── mocks/          # PoC용 가상 예약 사이트
│   ├── site_a.py   # 예약 사이트 (포트 8001)
│   └── site_b.py   # 가용 관리 사이트 (포트 8002)
├── docs/           # 설계 문서
├── run.py          # 전체 시스템 런처
└── requirements.txt
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

`.env` 파일을 열고 OpenAI API 키를 입력:

```
OPENAI_API_KEY=sk-your-api-key-here
```

## 서버 실행

### 방법 1: 한 번에 실행 (추천)

```bash
python3 run.py
```

3개 서버가 동시에 실행됩니다:

| 서버 | 포트 | URL |
|---|---|---|
| Engine (로그 대시보드) | 8000 | http://localhost:8000 |
| Mock Site A (예약 사이트) | 8001 | http://localhost:8001 |
| Mock Site B (가용 관리) | 8002 | http://localhost:8002 |

종료: `Ctrl+C`

### 방법 2: 개별 실행 (디버깅 시)

각각 별도 터미널에서 실행:

```bash
# 터미널 1
PYTHONPATH=. python3 -m uvicorn mocks.site_b:app --port 8002

# 터미널 2
PYTHONPATH=. python3 -m uvicorn core.engine:app --port 8000

# 터미널 3
PYTHONPATH=. python3 -m uvicorn mocks.site_a:app --port 8001
```

## PoC 테스트

### 브라우저

1. http://localhost:8000 — 실시간 로그 대시보드 열기
2. http://localhost:8001 — Site A에서 예약 생성
3. http://localhost:8002 — Site B에서 해당 날짜가 차단되었는지 확인

### curl

```bash
# Site A에 예약 생성 → Webhook → Engine → Site B 차단
curl -X POST http://localhost:8001/bookings \
  -H "Content-Type: application/json" \
  -d '{"room_id":"room_101","guest_name":"홍길동","check_in":"2026-04-10","check_out":"2026-04-12"}'

# Site B 상태 확인
curl "http://localhost:8002/rooms/room_101/availability?check_in=2026-04-10&check_out=2026-04-12"
```

## 기술 스택

- Python 3.10+
- FastAPI + Uvicorn
- OpenAI API (GPT-4o, function calling)
- httpx (async HTTP)
- SSE (실시간 로그 스트리밍)
