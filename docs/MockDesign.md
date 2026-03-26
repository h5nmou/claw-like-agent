# Mock Environment Design: Site A & Site B

## 1. 개요

PoC 검증을 위해 두 개의 가상 예약 사이트를 FastAPI로 구현한다.
각 사이트는 **인메모리 데이터 저장소**를 사용하며, 간단한 HTML UI + REST API를 제공한다.

---

## 2. 공통 데이터 모델

### Room (객실)
```
room_id: str          # 예: "room_101"
name: str             # 예: "디럭스 더블"
```

### Booking (예약)
```
booking_id: str       # UUID 자동 생성
room_id: str
guest_name: str
check_in: str         # "YYYY-MM-DD"
check_out: str        # "YYYY-MM-DD"
status: str           # "confirmed" | "cancelled"
```

### DateAvailability (날짜별 가용 상태)
```
room_id: str
date: str             # "YYYY-MM-DD"
available: bool       # true | false
```

---

## 3. Mock Site A — 예약 발생지

### 역할
- 고객이 예약을 생성하는 사이트.
- 예약 확정 시 Engine에 Webhook을 전송.

### API 엔드포인트

| Method | Path | 설명 |
|---|---|---|
| `GET` | `/` | 예약 현황을 보여주는 간단한 HTML 페이지 |
| `GET` | `/bookings` | 전체 예약 목록 JSON 반환 |
| `POST` | `/bookings` | 새 예약 생성. 성공 시 Engine Webhook 호출 |
| `DELETE` | `/bookings/{booking_id}` | 예약 취소. 성공 시 Engine Webhook 호출 |

### Webhook 동작
- 예약 생성/취소 시, 설정된 `ENGINE_WEBHOOK_URL`로 HTTP POST 전송.
- 실패 시 로그만 남기고 계속 진행 (PoC 단순화).

### 포트
- **8001**

---

## 4. Mock Site B — 동기화 대상

### 역할
- 동일 숙소의 다른 채널.
- Engine이 API를 통해 날짜별 가용 상태를 조회·변경.

### API 엔드포인트

| Method | Path | 설명 |
|---|---|---|
| `GET` | `/` | 객실별 가용 현황을 보여주는 간단한 HTML 페이지 |
| `GET` | `/rooms/{room_id}/availability` | 특정 객실의 날짜별 가용 상태 JSON 반환 |
| `PATCH` | `/rooms/{room_id}/availability` | 특정 날짜 범위의 가용 상태 변경 (`available: true/false`) |

### PATCH 요청 예시
```json
{
  "check_in": "2026-04-10",
  "check_out": "2026-04-12",
  "available": false
}
```

### 응답 예시
```json
{
  "room_id": "room_101",
  "updated_dates": ["2026-04-10", "2026-04-11"],
  "available": false
}
```

### 포트
- **8002**

---

## 5. 초기 데이터

두 사이트 모두 서버 시작 시 아래 객실을 인메모리로 초기화:

| room_id | name |
|---|---|
| `room_101` | 디럭스 더블 |
| `room_102` | 스탠다드 트윈 |
| `room_103` | 스위트 |

- Site B의 모든 날짜는 기본적으로 `available: true`로 설정.
- 가용 상태 범위: 오늘부터 30일간.

---

## 6. HTML UI 설계

### Site A (`/`)
- **예약 목록 테이블**: booking_id, room_id, guest_name, check_in, check_out, status
- **예약 추가 폼**: room_id (select), guest_name (input), check_in (date), check_out (date), 제출 버튼
- 예약 생성 시 자동 새로고침

### Site B (`/`)
- **객실별 달력 뷰**: 각 객실의 향후 30일 가용 상태를 색상으로 표시
  - 🟢 초록: 예약 가능
  - 🔴 빨강: 예약 불가 (차단됨)
- 클릭으로 상태 변경 불가 (API 전용)

---

## 7. Engine Webhook 엔드포인트

Engine 측에서 수신할 Webhook:

| Method | Path | 설명 |
|---|---|---|
| `POST` | `/webhook` | 사이트 A의 예약 이벤트 수신. Engine 메인 루프를 트리거. |

### 포트
- **8000** (Engine 자체 서버)

---

## 8. 기술 스택

| 구성 요소 | 기술 |
|---|---|
| Mock Sites | FastAPI + Uvicorn |
| HTML 렌더링 | Jinja2 Templates (간단한 인라인 HTML) |
| 데이터 저장 | Python dict (인메모리) |
| HTTP 호출 | httpx (async) |
