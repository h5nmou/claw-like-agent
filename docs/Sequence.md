# Sequence: PoC 시나리오 — Multi-Channel Booking Sync

## 1. 개요

이 문서는 "사이트 A 예약 → 사이트 B 자동 차단" PoC의 상세 실행 흐름을 기술한다.

---

## 2. 액터 (Actors)

| 액터 | 설명 |
|---|---|
| **User** | 사이트 A에서 예약을 진행하는 고객 (PoC에서는 curl/API 호출로 시뮬레이션) |
| **Mock Site A** | 예약을 받는 가상 사이트. 예약 확정 시 Webhook을 엔진에 발송. |
| **Engine** | Universal Agent Engine. Brain(LLM) + Executor + Memory로 구성. |
| **Mock Site B** | 동일 숙소의 다른 채널. Engine이 API를 통해 해당 날짜를 차단. |

---

## 3. 시퀀스 다이어그램

```
User          Mock Site A       Engine (Brain)      Mock Site B
 │                │                  │                   │
 │─── 예약 요청 ──→│                  │                   │
 │                │── 예약 확정 ──────→│                   │
 │                │   (Webhook)      │                   │
 │                │                  │                   │
 │                │          ┌───────┴───────┐          │
 │                │          │ 1. Perceive   │          │
 │                │          │ - 이벤트 수신  │          │
 │                │          │ - 현재 상태 조회│          │
 │                │          └───────┬───────┘          │
 │                │                  │                   │
 │                │          ┌───────┴───────┐          │
 │                │          │ 2. Reason     │          │
 │                │          │ - LLM 판단    │          │
 │                │          │ "B 차단 필요" │          │
 │                │          └───────┬───────┘          │
 │                │                  │                   │
 │                │          ┌───────┴───────┐          │
 │                │          │ 3. Act        │          │
 │                │          │ - Tool 실행   │          │
 │                │          └───────┬───────┘          │
 │                │                  │                   │
 │                │                  │── block_dates ──→│
 │                │                  │                   │── 날짜 차단 완료
 │                │                  │←── 성공 응답 ─────│
 │                │                  │                   │
 │                │          ┌───────┴───────┐          │
 │                │          │ 4. Log        │          │
 │                │          │ - Memory 기록 │          │
 │                │          └───────────────┘          │
```

---

## 4. 상세 단계

### Step 1: Trigger (예약 확정)
- User가 Mock Site A의 API(`POST /bookings`)를 호출하여 예약 생성.
- Site A는 예약을 저장한 뒤, Engine의 Webhook 엔드포인트(`POST /webhook`)로 이벤트 전송.
- 이벤트 페이로드 예시:
```json
{
  "event": "booking_confirmed",
  "source": "site_a",
  "booking": {
    "guest_name": "홍길동",
    "room_id": "room_101",
    "check_in": "2026-04-10",
    "check_out": "2026-04-12"
  }
}
```

### Step 2: Perceive (상태 수집)
- Engine이 이벤트를 수신하면, 현재 Site B의 예약 상태를 조회하기 위해 Tool 함수 `get_site_b_availability(room_id, date_range)`를 Tool 목록에 포함.
- Brain에게 전달하는 메시지:
  - **System:** Scene 규칙 (hotel_sync_scene.md 내용)
  - **User:** 트리거 이벤트 + 현재 상태 요약

### Step 3: Reason (LLM 판단)
- Brain(LLM)이 Scene 규칙에 따라 판단:
  - "사이트 A에서 room_101이 4/10~4/12 예약됨"
  - "사이트 B의 동일 기간을 차단해야 함"
- 응답: `function_call` → `block_site_b_dates(room_id="room_101", check_in="2026-04-10", check_out="2026-04-12")`

### Step 4: Act (Tool 실행)
- Executor가 `block_site_b_dates` 함수를 실행.
- 내부적으로 Mock Site B의 API(`PATCH /rooms/{room_id}/availability`)를 호출.
- 결과를 Brain에 반환 → Brain이 "작업 완료" 판단 → 루프 종료.

### Step 5: Log (기록)
- Memory에 전체 세션 기록: 트리거, LLM 응답, 실행 결과, 최종 상태.

---

## 5. 에러 시나리오

| 상황 | 엔진 대응 |
|---|---|
| Site B API 호출 실패 | LLM이 재시도 판단 또는 에러 로그 기록 |
| 이미 차단된 날짜 | Tool이 "이미 차단됨" 반환 → LLM이 추가 작업 불필요 판단 |
| 알 수 없는 room_id | Tool이 에러 반환 → LLM이 관리자 알림 제안 |
