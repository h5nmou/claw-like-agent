# Universal Agent Engine — Q&A 정리

---

## Q1. LLM이 Site B를 차단해야 한다는 판단은 어떤 방식으로?

**Scene(.md) + Tool(.py) + 이벤트의 조합으로 판단합니다.**

| 요소 | 역할 | 비유 |
|---|---|---|
| Scene (.md) | "예약 확정 → 다른 채널 차단" 같은 **규칙** 정의 | 업무 매뉴얼 |
| Tools (.py) | `block_site_b_dates` 같은 **수단** 제공 | 도구 상자 |
| LLM | 규칙 + 상황 + 수단을 보고 **판단** | 판단하는 직원 |

하드코딩된 if-else가 아니라, LLM이 자연어 규칙을 이해하고 상황에 맞는 도구를 선택하는 방식입니다. Scene 파일의 규칙을 바꾸면 코드 수정 없이 LLM의 행동이 달라집니다.

---

## Q2. Tool 목록과 규칙을 LLM에게 먼저 던져준다는 말?

**맞습니다.** OpenAI API 호출 시 구조:

```python
response = openai.chat.completions.create(
    messages=[
        {"role": "system", "content": scene_규칙},   # ← 규칙 (Scene)
        {"role": "user", "content": "이벤트 발생..."},  # ← 상황
    ],
    tools=tool_schemas,    # ← 사용 가능한 도구 목록
    tool_choice="auto",    # ← LLM이 알아서 판단
)
```

이벤트가 오기 전에 이미 **"너는 이런 규칙을 따르고, 이런 도구를 쓸 수 있어"가 세팅**되어 있고, 이벤트는 "이런 일이 발생했어, 어떻게 할래?"만 얹히는 구조입니다.

---

## Q3. 여러 Scene이 있을 때 어떤 Scene을 발동시킬지 어떻게 판단?

**3가지 접근법:**

### 방법 1: Rule-based Router
각 Scene 파일의 frontmatter에 트리거 조건을 명시하고 매칭합니다.
```yaml
# hotel_sync_scene.md
trigger:
  event: ["booking_confirmed", "booking_cancelled"]
```
빠르고 예측 가능하지만, 새 이벤트마다 수동 매핑이 필요합니다.

### 방법 2: Meta-Agent (Dispatcher LLM)
1차 LLM 호출로 "어떤 Scene을 쓸지" 판단하고, 2차 호출로 실제 작업을 수행합니다. 유연하지만 LLM 호출 1회 추가됩니다.

### 방법 3: 하이브리드 (추천)
Rule 매칭을 먼저 시도하고, 실패 시에만 Dispatcher LLM에게 위임합니다.

---

## Q4. 에이전트 실행 시점부터의 전체 타임라인

| 시점 | 내용 | 판단 주체 |
|---|---|---|
| **0: 서버 부팅** | Tool 모듈 import → @tool 등록, FastAPI 서버 기동 | 코드 (자동) |
| **1: Webhook 도착** | POST /webhook 수신 | — |
| **2: Scene 라우팅** | 어떤 Scene을 쓸지 결정 (Rule 매칭 or Dispatcher LLM) | Rule / LLM |
| **3: Brain 초기화** | Scene → system prompt, Tools → JSON schema 세팅 | 코드 (자동) |
| **4: 첫 LLM 호출** | 규칙 + 도구 + 이벤트 전달 → LLM이 첫 판단 | LLM |
| **5~7: 루프** | Tool 실행 → 결과 반환 → LLM 재판단 (반복) | LLM |
| **8: 완료** | LLM이 text로 응답 → 루프 종료 → 대기 복귀 | LLM |

---

## Q5. Webhook 오기 전에 Scene을 미리 알려주지 않는 이유?

**실제로는 "미리 vs 나중에"의 차이가 아닙니다.**

OpenAI API는 상태를 유지하지 않아서, 매 호출마다 `system prompt(Scene)`와 `user message(이벤트)`를 동시에 전송합니다. 같은 요청 안에서의 역할 구분(system=규칙, user=상황)일 뿐입니다.

"미리 알려주기"가 실질적 의미를 갖는 경우는 **Brain을 상주시켜 이전 이벤트의 맥락을 유지**하고 싶을 때입니다:

| | 매번 새 Brain | Brain 상주 |
|---|---|---|
| 장점 | 이전 대화 오염 없음, Scene 변경 즉시 반영 | 이전 맥락 기억 가능 |
| 단점 | 이전 이벤트 기억 못함 | 토큰 비용 증가, 맥락 혼란 가능 |
| Scene 전환 | Webhook마다 다른 Scene 선택 가능 | 전환 어려움 |
