# Network Slicing 구현 해설

> 5G 네트워크 슬라이싱의 핵심 개념을 이 PoC에서 어떻게 애플리케이션 레벨로 시뮬레이션했는지 설명합니다.

---

## 1. 네트워크 슬라이싱이란?

하나의 물리 네트워크를 **논리적으로 분리**하여, 용도별 독립 가상 네트워크(Slice)를 만드는 5G 핵심 기술입니다.

```
물리 5G 인프라 (하나의 기지국 + 코어)
├── Slice 1: 자율주행 차량   (초저지연 1ms, 고신뢰 99.999%)
├── Slice 2: 대량 IoT 센서   (저전력, 대규모 연결)
└── Slice 3: 일반 모바일     (Best Effort)
```

각 Slice는:
- 서로의 트래픽에 **간섭 불가**
- **독립적으로 관리/회수** 가능
- 전용 **QoS 정책** 적용

---

## 2. PoC에서의 구현: VPAL (Virtual Private Agent Link)

실제 SDN/NFV 장비 없이, **3가지 핵심 속성**을 HTTP 레벨에서 시뮬레이션합니다.

### 2-1. 슬라이스 할당 = VPAL 세션 발급

```python
# telco_server.py — /auth/token 엔드포인트
vpal_sessions[req.agent_id] = {
    "session_id": str(uuid.uuid4()),   # ← "슬라이스 ID"
    "created_at": now.isoformat(),
    "request_count": 1,                # ← 트래픽 모니터링
    "last_request": now.isoformat(),
}

# JWT 토큰에 슬라이스 ID를 바인딩
payload = {
    "agent_id": req.agent_id,
    "action": req.action,
    "vpal_session_id": vpal["session_id"],  # ← 토큰과 슬라이스 결합
    ...
}
```

| 실제 5G | PoC |
|---|---|
| NSSF가 Slice 선택 | Telco가 정책 대조 후 세션 발급 |
| S-NSSAI 식별자 | `vpal_session_id` (UUID) |
| Slice 전용 UPF 할당 | 토큰에 세션 ID 포함 |

### 2-2. 슬라이스 격리 = 이중 검증

```python
# site_b.py — verify_token()
async def verify_token(request):
    # ── 1차 검증: 디지털 서명 (이 토큰이 통신사가 발급한 것인가?) ──
    payload = jwt.decode(token, telco_public_key, algorithms=["RS256"])

    # ── 2차 검증: 슬라이스 소속 (이 요청이 올바른 슬라이스에서 온 것인가?) ──
    vpal_header    = request.headers.get("X-VPAL-Session", "")   # Agent가 보낸 값
    vpal_in_token  = payload.get("vpal_session_id", "")          # 토큰 내 값

    if vpal_header != vpal_in_token:
        return None   # ← 슬라이스 불일치 → 서비스 자체가 "보이지 않음"
```

**이것이 핵심입니다:**
- 유효한 JWT가 있어도, 슬라이스 ID가 일치하지 않으면 **접근 자체가 불가능**
- 다른 슬라이스의 Agent는 이 서비스의 존재를 알 수 없음 → **네트워크 수준 격리**

```
Agent X (슬라이스 A)  →  Site B 요청  →  ❌ VPAL 불일치 → "Unauthorized"
Agent C (슬라이스 B)  →  Site B 요청  →  ✅ VPAL 일치   → 200 OK
```

### 2-3. 슬라이스 회수 = Kill-switch

```python
# telco_server.py — 자동 Kill-switch (Rate Limit 초과)
if vpal["request_count"] > max_requests_per_hour:
    isolated_agents.add(req.agent_id)    # Agent를 격리 목록에 추가
    del vpal_sessions[req.agent_id]      # ← 슬라이스 즉시 삭제 = 터널 파괴

# telco_server.py — 수동 Kill-switch
@app.post("/killswitch/{agent_id}/isolate")
async def killswitch_isolate(agent_id):
    isolated_agents.add(agent_id)
    del vpal_sessions[agent_id]          # ← 슬라이스 회수
```

| 실제 5G | PoC |
|---|---|
| SDN Controller가 Slice 리소스 회수 | `del vpal_sessions[agent_id]` |
| 물리 경로 차단 | `isolated_agents` set에 추가 → 토큰 발급 거부 |
| 관리자 개입 필요 | USIM PIN 재인증 후 재활성화 가능 |

### 2-4. 슬라이스 재할당

```python
# telco_server.py — /killswitch/{agent_id}/reactivate
isolated_agents.discard(agent_id)         # 격리 해제
vpal_sessions[agent_id] = {
    "session_id": str(uuid.uuid4()),      # ← 새로운 슬라이스 ID 발급
    "request_count": 0,                   # 카운터 초기화
    ...
}
```

> 재활성화 시 **새로운 UUID**가 발급됩니다. 이전 슬라이스 ID는 영구 폐기.

---

## 3. 데이터 흐름도

```
                 Public Internet              │    SKT Private Slice (MEC)
                                              │
  ┌──────────┐                                │   ┌──────────────┐
  │ Site A   │──예약 Webhook──►┐               │   │ Telco Trust  │
  │ :8001    │                 │               │   │ Server :8003 │
  └──────────┘                 ▼               │   └──────┬───────┘
                         ┌──────────┐          │          │
                         │ Engine   │──토큰요청──────────►│ ① 정책 대조
                         │ :8000    │◄─JWT+VPAL─────────◄│ ② 슬라이스 할당
                         └────┬─────┘          │          │ ③ RS256 서명
                              │                │          │
                              │ X-VPAL-Session  │   ┌──────┴───────┐
                              │ + Bearer JWT    │   │   Site B     │
                              └────────────────────►│   :8002      │
                                               │   │              │
                                               │   │ ④ RS256 검증  │
                                               │   │ ⑤ VPAL 검증   │
                                               │   │ ⑥ 데이터 변경  │
                                               │   └──────────────┘
```

---

## 4. 실제 매핑 표

| 5G 표준 컴포넌트 | 역할 | PoC 대응 | 코드 위치 |
|---|---|---|---|
| **NSSF** (Slice Selection) | 요청에 맞는 Slice 선택 | 정책 대조 → 토큰 발급 | `telco_server.py` `/auth/token` |
| **S-NSSAI** (Slice ID) | Slice 고유 식별자 | `vpal_session_id` (UUID) | JWT payload |
| **SMF** (Session Mgmt) | 세션 생성/해제 | `vpal_sessions` dict | `telco_server.py` |
| **UPF** (User Plane) | 데이터 경로 제어 | `X-VPAL-Session` 헤더 검증 | `site_b.py` |
| **PCF** (Policy Control) | QoS/Rate 정책 | `max_requests_per_hour` | `AVAILABLE_POLICIES` |
| **NEF** (Exposure) | 외부 API 노출 | Site B API 엔드포인트 | FastAPI 라우터 |
| **MEC** (Edge Computing) | 로컬 처리 | Telco+Site B 같은 Zone | `Architecture.md` §7.3 |

---

## 5. 한계와 향후 확장

| 항목 | 현재 PoC | 실제 구현 시 |
|---|---|---|
| 격리 수준 | HTTP 헤더 매칭 | SDN Flow Rule (물리 경로 분리) |
| 슬라이스 선택 | 정책 DB 매칭 | NSSF + NRF 연동 |
| QoS 보장 | Rate Limit만 | GBR/MBR 대역폭 보장 |
| 트래픽 감시 | 요청 카운터 | DPI/Probe 기반 실시간 분석 |
| 데이터 주권 | 같은 호스트 가정 | 실제 MEC 에지 데이터센터 배치 |
| Slice 간 통신 | 불가 (의도적) | Inter-Slice Routing 정책 |

---

## 6. 요약

> **"슬라이스 ID(UUID)를 JWT에 심고, HTTP 헤더로 전달하여, 토큰 인증과 네트워크 격리를 동시에 달성"**

```
토큰 없음      → 401 (서비스 존재 모름)     ← 인증 실패
토큰 있음 + VPAL 불일치 → 401 (슬라이스 밖)  ← 격리 위반
토큰 있음 + VPAL 일치   → 200 OK            ← 정상 통과
```
