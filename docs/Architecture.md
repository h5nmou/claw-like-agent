# Architecture: Universal Agent Engine

## 1. 설계 철학

Universal Agent Engine은 **"범용 엔진 + 교체 가능한 시나리오"** 패턴을 따른다.
에이전트의 핵심 사고 루프(Perceive → Reason → Act)는 도메인에 독립적이며,
특정 비즈니스 로직은 **Scene(시나리오 정의서)** 과 **Tool(실행 모듈)** 로 분리한다.

```
┌──────────────────────────────────────────────┐
│              Universal Agent Engine          │
│  ┌────────┐  ┌──────────┐  ┌─────────────┐  │
│  │ Brain  │→│ Executor │→│   Memory    │  │
│  │ (LLM)  │  │          │  │ (State Log) │  │
│  └────────┘  └──────────┘  └─────────────┘  │
│       ↑            ↓                         │
│  ┌────────────────────────────┐              │
│  │      Scene Loader         │              │
│  │  (MD → System Prompt)     │              │
│  └────────────────────────────┘              │
│       ↑            ↓                         │
│  ┌────────────────────────────┐              │
│  │      Tool Registry        │              │
│  │ (Python functions → JSON) │              │
│  └────────────────────────────┘              │
└──────────────────────────────────────────────┘
        ↕                ↕
   ┌─────────┐     ┌─────────┐
   │ Scene   │     │  Tools  │
   │  (.md)  │     │  (.py)  │
   └─────────┘     └─────────┘
```

---

## 2. 계층 구분

### 2.1 Core Layer (범용 엔진) — `core/`

| 모듈 | 역할 |
|---|---|
| `brain.py` | LLM과의 대화를 관리. Scene에서 로드된 시스템 프롬프트와 Tool 스키마를 OpenAI API의 `function_calling`으로 전달. 응답에서 함수 호출 요청을 파싱. |
| `executor.py` | Brain이 결정한 함수 호출(tool name + arguments)을 실제 Python 함수로 매핑·실행. 결과를 다시 Brain에게 반환하여 루프를 완성. |
| `memory.py` | 현재 세션의 이벤트 로그를 관리 (단기 메모리). 향후 벡터 DB 기반 장기 메모리로 확장 가능. |
| `engine.py` | 위 모듈들을 조합하는 메인 루프. `Trigger → Perceive → Reason → Act → Log` 사이클 관리. |

### 2.2 Scene Layer — `scenes/`

- **형식:** Markdown 파일 (`.md`)
- **역할:** 특정 도메인의 규칙·목표·제약 조건을 자연어로 기술.
- **로딩:** `engine.py`가 지정된 Scene 파일을 읽어 LLM의 `system` 프롬프트로 주입.
- **교체 가능:** Scene 파일만 바꾸면 동일 엔진이 호텔 예약, 재고 관리, CS 응답 등 다양한 시나리오를 수행.

### 2.3 Tool Layer — `tools/`

- **형식:** Python 모듈 (`.py`)
- **역할:** 각 함수에 docstring + type hint를 부여. Tool Registry가 이를 파싱하여 OpenAI function schema(JSON)로 자동 변환.
- **등록:** 데코레이터(`@tool`) 또는 명시적 레지스트리로 등록.

---

## 3. 실행 흐름 (High-Level)

```
1. Engine 시작 시, Scene(.md) 로드 → system prompt 구성
2. Engine 시작 시, Tools(.py) 스캔 → function schema 구성
3. Trigger 수신 (Webhook / Polling / Manual)
4. Brain에게 [system prompt + tool schemas + trigger event] 전달
5. LLM 응답 파싱:
   a. 텍스트 → 로그 기록
   b. function_call → Executor가 해당 Tool 실행 → 결과를 Brain에 재전달
6. 루프 반복 (LLM이 "완료" 판단 시 종료)
7. Memory에 전체 세션 기록
```

---

## 4. 확장 전략

| 확장 항목 | 방법 |
|---|---|
| 새 시나리오 추가 | `scenes/`에 새 `.md` 파일 작성 |
| 새 도구 추가 | `tools/`에 새 `.py` 모듈 + `@tool` 데코레이터 |
| 멀티 에이전트 | Engine 인스턴스를 다중 실행, 이벤트 버스로 연결 |
| 장기 메모리 | `memory.py`에 ChromaDB/FAISS 어댑터 추가 |
| UI | FastAPI 기반 대시보드를 별도 레이어로 추가 |
