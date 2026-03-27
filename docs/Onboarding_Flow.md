# Onboarding Flow: 최초 1회 USIM 인증 및 키 교환

## 1. 온보딩 전제조건

| 요소 | 상태 | 비고 |
|---|---|---|
| 사장님 USIM | SKT 개통 완료, `sim_001` | Telco DB에 등록됨 |
| Agent C | 최초 구동, 미등록 상태 | RSA Keypair 미생성 |
| Service D | 가동 중 (port 8003) | Agent 등록 DB 비어있음 |
| Site B | 가동 중 (port 8002) | Telco Public Key 보유 |

## 2. 온보딩 6단계

### Step 1: Agent 최초 구동 감지

Agent C가 시작될 때 `.agent_cert` 파일 존재 여부를 확인합니다.

```python
# core/onboarding_manager.py
class OnboardingManager:
    CERT_PATH = ".agent_cert"

    def needs_onboarding(self) -> bool:
        return not os.path.exists(self.CERT_PATH)
```

- 파일 없음 → 온보딩 프로세스 시작
- 파일 있음 → Phase 1 (자율 실행) 진입

### Step 2: RSA Keypair 생성

Agent가 자신의 신원 증명용 키쌍을 생성합니다.

```
Agent C:
  ├─ Private Key → 로컬에만 저장 (agent_private.pem)
  └─ Public Key  → Telco에 전송할 예정
```

기술적으로:
```python
from cryptography.hazmat.primitives.asymmetric import rsa
private_key = rsa.generate_private_key(
    public_exponent=65537, key_size=2048
)
public_key = private_key.public_key()
```

### Step 3: 사장님에게 승인 요청

Agent가 사장님에게 "인증이 필요합니다" 메시지를 전달합니다. 사장님은 **통신사 앱 (Mock UI)** 을 통해 확인합니다.

```
┌──────────────────────────────────────┐
│  📱 SKT Agent 관리 앱               │
│                                      │
│  🆕 새 Agent 등록 요청               │
│                                      │
│  Agent ID:  agent_001                │
│  이름:      Universal Agent Engine   │
│  요청 권한: booking_sync             │
│  설명:      숙박 예약 사이트 간       │
│             가용 상태 동기화          │
│                                      │
│  ┌──────────────────────────────┐    │
│  │ 위 Agent에게 권한을 위임      │    │
│  │ 하시겠습니까?                 │    │
│  │                              │    │
│  │ USIM PIN: [____]             │    │
│  │                              │    │
│  │  [승인]          [거부]      │    │
│  └──────────────────────────────┘    │
│                                      │
└──────────────────────────────────────┘
```

### Step 4: USIM 인증 + 승인 전송

사장님이 PIN을 입력하고 [승인]을 누르면:

```
통신사 앱 → Service D:
POST /onboarding/approve
{
    "sim_id": "sim_001",
    "sim_pin": "1234",                    ← USIM 인증
    "agent_public_key": "MIIBIjAN...",    ← Agent 공개키 (PEM)
    "approved_policies": ["booking_sync"], ← 승인할 권한 목록
    "delegation_duration_days": 365        ← 위임 유효기간
}
```

### Step 5: Service D — Agent 등록 + 위임장 생성

Telco Trust Server가 수행하는 작업:

```
1. USIM 검증
   └─ sim_001이 DB에 있는가? → ✅
   └─ PIN이 맞는가? → ✅
   └─ SIM 상태가 active인가? → ✅

2. Agent 등록
   └─ agent_public_key를 registered_agents에 저장
   └─ 승인된 정책(policies) 연결
   └─ 유효기간 설정

3. 마스터 위임장 생성
   └─ Telco의 RSA Private Key로 서명
   └─ 내용: {agent_id, sim_owner, policies, expires}
```

응답:
```json
{
    "agent_id": "agent_001",
    "delegation_certificate": {
        "agent_id": "agent_001",
        "sim_id": "sim_001",
        "owner": "홍길동",
        "carrier": "SKT",
        "policies": ["booking_sync"],
        "issued_at": "2026-03-27T14:00:00Z",
        "expires_at": "2027-03-27T14:00:00Z",
        "telco_signature": "RSA_SHA256:..."
    },
    "telco_public_key": "MIIBIjAN..."
}
```

### Step 6: Agent — 위임장 저장 + 온보딩 완료

```python
# Agent가 위임장을 로컬에 저장
with open(".agent_cert", "w") as f:
    json.dump(delegation_certificate, f)

# Telco Public Key도 저장 (나중에 자체 검증용)
with open(".telco_public.pem", "w") as f:
    f.write(telco_public_key)
```

**온보딩 완료.** 이후 Agent는 자율 실행 가능.

## 3. 키 교환 요약

```
시점          Agent C              Service D (Telco)
────          ─────────            ─────────────────
온보딩 전     키 없음               RSA Keypair 보유

Step 2        RSA Keypair 생성      -
              (Private + Public)

Step 4        Public Key ────────→  Agent Public Key 저장

Step 5        -                     위임장 생성 (Private Key 서명)

Step 6        위임장 수신 ◄─────── 위임장 + Telco Public Key
              .agent_cert 저장
              .telco_public.pem 저장

온보딩 후     Private Key 보유      Agent Public Key 보유
              위임장 보유            정책 DB 보유
              Telco Public Key 보유
```

## 4. 보안 고려사항

| 위협 | 대응 |
|---|---|
| 위임장 탈취 | 위임장만으로는 토큰 발급 불가. Agent Private Key로 요청 서명 필요. |
| Agent Private Key 유출 | 사장님이 통신사 앱에서 Agent를 "폐기(revoke)" 처리. |
| 만료된 위임장 사용 | `expires_at` 필드 검증. 만료 시 재온보딩 필요. |
| 승인하지 않은 작업 | Policy 범위 외 요청은 Service D가 거부. |
