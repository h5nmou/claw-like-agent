# Policy Schema: 자율권 규격 정의

## 1. 정책(Policy)이란?

사장님이 Agent에게 **"이 범위 안에서는 알아서 해"**라고 위임하는 권한의 규격입니다.

```
사장님 → "예약 사이트 동기화는 네가 알아서 해" → Policy: booking_sync
```

정책 없이는 어떤 요청에도 토큰을 발급하지 않습니다.

## 2. Policy 스키마

```json
{
    "policy_id": "policy_booking_sync_001",
    "name": "booking_sync",
    "description": "숙박 예약 사이트 간 가용 상태 동기화",

    "permissions": {
        "actions": ["block", "unblock", "query"],
        "resources": ["room_*"],
        "target_sites": ["site_b"]
    },

    "constraints": {
        "max_requests_per_hour": 100,
        "allowed_hours": {"start": "00:00", "end": "23:59"},
        "require_query_before_modify": true
    },

    "validity": {
        "issued_at": "2026-03-27T14:00:00Z",
        "expires_at": "2027-03-27T14:00:00Z",
        "renewable": true
    },

    "approved_by": {
        "sim_id": "sim_001",
        "owner": "홍길동",
        "carrier": "SKT",
        "approval_method": "USIM_PIN"
    }
}
```

## 3. 필드 상세

### permissions — 허용되는 행위

| 필드 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `actions` | string[] | 허용 동작 목록 | `["block", "unblock", "query"]` |
| `resources` | string[] | 대상 리소스 (와일드카드 지원) | `["room_*"]` = 모든 객실 |
| `target_sites` | string[] | 접근 가능 사이트 | `["site_b"]` |

### constraints — 제약 조건

| 필드 | 타입 | 설명 |
|---|---|---|
| `max_requests_per_hour` | int | 시간당 최대 토큰 발급 횟수 |
| `allowed_hours` | object | 허용 시간대 (KST) |
| `require_query_before_modify` | bool | 변경 전 조회 강제 여부 |

### validity — 유효기간

| 필드 | 타입 | 설명 |
|---|---|---|
| `issued_at` | ISO 8601 | 정책 승인 시점 |
| `expires_at` | ISO 8601 | 정책 만료 시점 |
| `renewable` | bool | 만료 시 갱신 가능 여부 |

## 4. 토큰 발급 시 정책 대조 로직

```
Agent가 토큰 요청:
  {action: "block", resource: "room_101", target: "site_b"}

Service D 검증 순서:
  1. Agent 등록 여부 확인           → ✅ registered
  2. 위임장 유효기간 확인           → ✅ 2027-03-27까지 유효
  3. 정책 매칭:
     ├─ action "block" ∈ ["block", "unblock", "query"]?  → ✅
     ├─ resource "room_101" matches "room_*"?             → ✅
     └─ target "site_b" ∈ ["site_b"]?                     → ✅
  4. 제약조건:
     ├─ 시간당 요청 횟수 초과?       → ✅ 3/100
     ├─ 허용 시간대 내?              → ✅ 14:30 KST
     └─ 사전 조회 수행?              → ✅ query 이력 있음
  
  결과: 토큰 발급 ✅ (사장님 추가 승인 불필요)
```

## 5. 정책 거부 시나리오

| 요청 | 정책 | 결과 |
|---|---|---|
| `action: "delete_room"` | actions에 없음 | ❌ 거부 |
| `resource: "user_db"` | `room_*`에 매칭 안 됨 | ❌ 거부 |
| `target: "site_c"` | target_sites에 없음 | ❌ 거부 |
| 시간당 101번째 요청 | max 100 초과 | ❌ 거부 |
| 위임장 만료 후 | expires_at 지남 | ❌ 재온보딩 필요 |

## 6. 확장 가능한 정책 예시

```json
[
    {
        "name": "booking_sync",
        "actions": ["block", "unblock", "query"],
        "resources": ["room_*"]
    },
    {
        "name": "pricing_update",
        "actions": ["update_price", "query_price"],
        "resources": ["rate_*"]
    },
    {
        "name": "guest_notification",
        "actions": ["send_message"],
        "resources": ["guest_*"],
        "constraints": { "max_requests_per_hour": 10 }
    }
]
```

사장님이 온보딩 시 여러 정책을 한 번에 승인하거나, 나중에 추가 정책만 별도로 승인할 수 있습니다.
