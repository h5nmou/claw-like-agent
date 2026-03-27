"""
telco_server.py — Telco Trust Server (Service D)

RSA 기반 신뢰 인프라:
- 최초 온보딩: USIM 인증 + Agent 등록 + 위임장 발급
- 자율 실행: 정책 기반 JWT 발급 (RS256 서명)
- 공증 기록: 모든 이력 로깅
Port: 8003
"""

from __future__ import annotations

import json
import os
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("telco_trust")

app = FastAPI(title="Telco Trust Server (Service D)", version="0.2.0")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NOTARY_LOG_PATH = PROJECT_ROOT / "logs" / "carrier_notary.json"

# ── RSA Keypair (Telco 서버 시작 시 생성) ────────────

_telco_private_key = rsa.generate_private_key(
    public_exponent=65537, key_size=2048
)
_telco_public_key = _telco_private_key.public_key()

TELCO_PRIVATE_PEM = _telco_private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")

TELCO_PUBLIC_PEM = _telco_public_key.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode("utf-8")

logger.info("Telco RSA Keypair 생성 완료")

# ── USIM DB (Mock) ───────────────────────────────────

REGISTERED_SIMS = {
    "sim_001": {
        "carrier": "SKT",
        "phone": "010-1234-5678",
        "owner": "홍길동",
        "pin": "1234",
        "status": "active",
    },
}

# ── Agent 등록 DB ────────────────────────────────────

registered_agents: dict[str, dict] = {}
#  {agent_id: {public_key_pem, policies, sim_id, owner, registered_at, expires_at, status}}

# ── VPAL 세션 & 트래픽 모니터링 ──────────────────────

vpal_sessions: dict[str, dict] = {}
#  {agent_id: {session_id, created_at, request_count, last_request}}

isolated_agents: set[str] = set()
#  Kill-switch로 격리된 Agent

# ── 공증 기록 ────────────────────────────────────────

notary_log: list[dict] = []


def _append_notary(event: str, data: dict) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **data,
    }
    notary_log.append(entry)
    # 파일에도 저장
    NOTARY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTARY_LOG_PATH.write_text(
        json.dumps(notary_log, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"Notary: {event} — {data.get('agent_id', 'N/A')}")


# ── 정책 DB ──────────────────────────────────────────

AVAILABLE_POLICIES = {
    "booking_sync": {
        "policy_id": "policy_booking_sync_001",
        "name": "booking_sync",
        "description": "숙박 예약 사이트 간 가용 상태 동기화",
        "permissions": {
            "actions": ["block", "unblock", "query"],
            "resources": ["room_*"],
            "target_sites": ["site_b"],
        },
        "constraints": {
            "max_requests_per_hour": 100,
        },
    },
}

# ── 데이터 모델 ──────────────────────────────────────

class OnboardingRequest(BaseModel):
    sim_id: str
    sim_pin: str
    agent_public_key: str  # PEM
    approved_policies: list[str]
    delegation_duration_days: int = 365


class TokenRequest(BaseModel):
    agent_id: str
    action: str
    resource: str
    target_site: str


# ── API: 온보딩 ──────────────────────────────────────

@app.post("/onboarding/approve")
async def onboarding_approve(req: OnboardingRequest):
    """사장님의 USIM 인증 + Agent 등록 + 위임장 발급."""

    # 1. USIM 검증
    sim = REGISTERED_SIMS.get(req.sim_id)
    if not sim:
        return JSONResponse(status_code=403, content={"error": f"Unknown SIM: {req.sim_id}"})
    if sim["status"] != "active":
        return JSONResponse(status_code=403, content={"error": "SIM is not active"})
    if sim["pin"] != req.sim_pin:
        return JSONResponse(status_code=403, content={"error": "Invalid SIM PIN"})

    # 2. 정책 검증
    for policy_name in req.approved_policies:
        if policy_name not in AVAILABLE_POLICIES:
            return JSONResponse(status_code=400, content={"error": f"Unknown policy: {policy_name}"})

    # 3. Agent ID 생성 & 등록
    agent_id = f"agent_{len(registered_agents) + 1:03d}"
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=req.delegation_duration_days)

    registered_agents[agent_id] = {
        "public_key_pem": req.agent_public_key,
        "policies": req.approved_policies,
        "sim_id": req.sim_id,
        "owner": sim["owner"],
        "carrier": sim["carrier"],
        "registered_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }

    logger.info(f"Agent registered: {agent_id} (owner={sim['owner']}, policies={req.approved_policies})")

    # 4. 위임장 생성
    delegation_certificate = {
        "agent_id": agent_id,
        "sim_id": req.sim_id,
        "owner": sim["owner"],
        "carrier": sim["carrier"],
        "policies": req.approved_policies,
        "issued_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }

    # 5. 공증 기록
    _append_notary("agent_registered", {
        "agent_id": agent_id,
        "sim_id": req.sim_id,
        "owner": sim["owner"],
        "policies": req.approved_policies,
        "expires_at": expires_at.isoformat(),
    })

    return {
        "agent_id": agent_id,
        "delegation_certificate": delegation_certificate,
        "telco_public_key": TELCO_PUBLIC_PEM,
    }


# ── API: 토큰 발급 ──────────────────────────────────

@app.post("/auth/token")
async def issue_token(req: TokenRequest):
    """정책 기반 JWT 토큰 발급 (RS256 서명)."""

    # 1. Agent 등록 확인
    agent = registered_agents.get(req.agent_id)
    if not agent:
        return JSONResponse(status_code=403, content={"error": f"Unknown agent: {req.agent_id}"})

    # 2. 위임장 유효기간 확인
    expires_at = datetime.fromisoformat(agent["expires_at"])
    now = datetime.now(timezone.utc)
    if now > expires_at:
        return JSONResponse(status_code=403, content={"error": "위임장이 만료되었습니다. 재온보딩이 필요합니다."})

    # 3. 정책 대조
    policy_matched = None
    for policy_name in agent["policies"]:
        policy = AVAILABLE_POLICIES.get(policy_name)
        if not policy:
            continue
        perms = policy["permissions"]

        # action 매칭
        if req.action not in perms["actions"]:
            continue
        # target_site 매칭
        if req.target_site not in perms["target_sites"]:
            continue
        # resource 매칭 (와일드카드)
        resource_match = False
        for pattern in perms["resources"]:
            if pattern.endswith("*") and req.resource.startswith(pattern[:-1]):
                resource_match = True
                break
            elif pattern == req.resource:
                resource_match = True
                break
        if not resource_match:
            continue

        policy_matched = policy_name
        break

    if not policy_matched:
        _append_notary("token_denied", {
            "agent_id": req.agent_id,
            "action": req.action,
            "resource": req.resource,
            "reason": "no_matching_policy",
        })
        return JSONResponse(status_code=403, content={
            "error": "Policy denied",
            "message": f"요청된 action={req.action}, resource={req.resource}에 대한 권한이 없습니다.",
        })

    # 4. Kill-switch 확인
    if req.agent_id in isolated_agents:
        _append_notary("token_denied_isolated", {
            "agent_id": req.agent_id,
            "reason": "agent_isolated",
        })
        return JSONResponse(status_code=403, content={
            "error": "Agent isolated",
            "message": "Kill-switch가 발동되어 격리된 Agent입니다. 통신사 앱에서 재활성화하세요.",
        })

    # 5. 트래픽 모니터링 (Rate Limit)
    vpal = vpal_sessions.get(req.agent_id)
    if vpal:
        vpal["request_count"] += 1
        vpal["last_request"] = now.isoformat()
        max_rph = AVAILABLE_POLICIES.get(policy_matched, {}).get("constraints", {}).get("max_requests_per_hour", 100)
        if vpal["request_count"] > max_rph:
            isolated_agents.add(req.agent_id)
            _append_notary("agent_isolated", {
                "agent_id": req.agent_id,
                "reason": "rate_limit_exceeded",
                "requests_count": vpal["request_count"],
                "policy_limit": max_rph,
                "action_taken": "vpal_session_revoked",
            })
            logger.warning(f"Kill-switch 발동: {req.agent_id} (rate={vpal['request_count']}/{max_rph})")
            return JSONResponse(status_code=403, content={
                "error": "Kill-switch activated",
                "message": f"비정상 요청 빈도 감지 ({vpal['request_count']}/{max_rph}). Agent가 격리되었습니다.",
            })
    else:
        vpal_sessions[req.agent_id] = {
            "session_id": str(uuid.uuid4()),
            "created_at": now.isoformat(),
            "request_count": 1,
            "last_request": now.isoformat(),
        }
        vpal = vpal_sessions[req.agent_id]

    # 6. JWT 생성 (RS256 + VPAL 세션)
    payload = {
        "agent_id": req.agent_id,
        "sim_id": agent["sim_id"],
        "owner": agent["owner"],
        "carrier": agent["carrier"],
        "action": req.action,
        "resource": req.resource,
        "target_site": req.target_site,
        "policy": policy_matched,
        "vpal_session_id": vpal["session_id"],
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "jti": f"{req.agent_id}-{now.timestamp()}",
    }

    token = jwt.encode(payload, _telco_private_key, algorithm="RS256")

    logger.info(f"Token issued: agent={req.agent_id}, action={req.action}, resource={req.resource}, policy={policy_matched}")

    # 5. 공증 기록
    _append_notary("token_issued", {
        "agent_id": req.agent_id,
        "owner": agent["owner"],
        "action": req.action,
        "resource": req.resource,
        "policy_matched": policy_matched,
        "token_jti": payload["jti"],
    })

    return {
        "token": token,
        "vpal_session_id": vpal["session_id"],
        "expires_in": 300,
        "token_type": "Bearer",
        "policy_matched": policy_matched,
    }


# ── API: 공증 기록 조회 ──────────────────────────────

@app.get("/notary")
async def get_notary():
    return {"records": notary_log}


# ── API: Telco Public Key (Site B가 가져감) ──────────

@app.get("/public-key")
async def get_public_key():
    return {"telco_public_key": TELCO_PUBLIC_PEM}


@app.post("/killswitch/{agent_id}/isolate")
async def killswitch_isolate(agent_id: str):
    """Kill-switch: Agent 즉시 격리."""
    isolated_agents.add(agent_id)
    if agent_id in vpal_sessions:
        del vpal_sessions[agent_id]
    _append_notary("agent_isolated", {
        "agent_id": agent_id,
        "reason": "manual_killswitch",
        "action_taken": "vpal_session_revoked",
    })
    logger.warning(f"Kill-switch 수동 발동: {agent_id}")
    return {"status": "isolated", "agent_id": agent_id}


@app.post("/killswitch/{agent_id}/reactivate")
async def killswitch_reactivate(agent_id: str, data: dict):
    """Kill-switch 해제: USIM PIN 재인증 필요."""
    sim_pin = data.get("sim_pin", "")
    agent = registered_agents.get(agent_id)
    if not agent:
        return JSONResponse(status_code=404, content={"error": "Agent not found"})
    sim = REGISTERED_SIMS.get(agent["sim_id"])
    if not sim or sim["pin"] != sim_pin:
        return JSONResponse(status_code=403, content={"error": "Invalid SIM PIN"})
    isolated_agents.discard(agent_id)
    vpal_sessions[agent_id] = {
        "session_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "request_count": 0,
        "last_request": None,
    }
    _append_notary("agent_reactivated", {
        "agent_id": agent_id,
        "new_vpal_session": vpal_sessions[agent_id]["session_id"],
    })
    return {"status": "reactivated", "agent_id": agent_id}


@app.get("/traffic/{agent_id}")
async def traffic_monitor(agent_id: str):
    """Agent 트래픽 모니터링 현황."""
    vpal = vpal_sessions.get(agent_id)
    return {
        "agent_id": agent_id,
        "vpal_session": vpal,
        "isolated": agent_id in isolated_agents,
    }


# ── Dashboard UI ─────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    # 등록된 Agent 목록
    agent_rows = ""
    for aid, info in registered_agents.items():
        policies = ", ".join(info["policies"])
        agent_rows += f"""
        <tr>
            <td><code>{aid}</code></td>
            <td>{info['owner']}</td>
            <td>{info['carrier']}</td>
            <td>{policies}</td>
            <td><span class="status-active">Active</span></td>
            <td style="font-size:0.7rem;color:#64748b;">{info['expires_at'][:10]}</td>
        </tr>"""

    if not agent_rows:
        agent_rows = '<tr><td colspan="6" style="text-align:center;color:#64748b;padding:1.5rem;">아직 등록된 Agent가 없습니다. 온보딩을 진행하세요.</td></tr>'

    # 공증 기록
    notary_rows = ""
    for entry in reversed(notary_log[-20:]):
        event_color = "#22c55e" if entry["event"] == "token_issued" else "#58a6ff" if entry["event"] == "agent_registered" else "#f87171"
        notary_rows += f"""
        <tr>
            <td style="font-size:0.7rem;color:#64748b;">{entry['timestamp'][:19]}</td>
            <td><span style="color:{event_color};font-weight:600;">{entry['event']}</span></td>
            <td>{entry.get('agent_id', '-')}</td>
            <td style="font-size:0.75rem;color:#94a3b8;">{entry.get('policy_matched', entry.get('policies', '-'))}</td>
        </tr>"""

    if not notary_rows:
        notary_rows = '<tr><td colspan="4" style="text-align:center;color:#64748b;padding:1.5rem;">공증 기록이 없습니다.</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Telco Trust Server — Service D</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Inter', -apple-system, sans-serif;
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            color: #e2e8f0; min-height: 100vh; padding: 2rem;
        }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        h1 {{
            font-size: 1.8rem;
            background: linear-gradient(90deg, #f59e0b, #ef4444);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            margin-bottom: 0.5rem;
        }}
        .subtitle {{ color: #94a3b8; margin-bottom: 2rem; font-size: 0.9rem; }}
        .card {{
            background: rgba(30, 41, 59, 0.8);
            border: 1px solid rgba(148, 163, 184, 0.1);
            border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem;
        }}
        .card h2 {{ font-size: 1.1rem; color: #cbd5e1; margin-bottom: 1rem; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{
            padding: 0.6rem; text-align: left;
            border-bottom: 1px solid rgba(148, 163, 184, 0.1);
            font-size: 0.82rem;
        }}
        th {{ color: #94a3b8; font-weight: 600; font-size: 0.75rem; text-transform: uppercase; }}
        .status-active {{ color: #22c55e; font-weight: 600; }}
        code {{ background: rgba(0,0,0,0.3); padding: 0.15rem 0.4rem; border-radius: 4px; font-size: 0.8rem; }}
        .badge {{
            display: inline-flex; align-items: center; gap: 0.3rem;
            padding: 0.2rem 0.6rem; border-radius: 12px; font-size: 0.7rem; font-weight: 600;
        }}
        .badge-rsa {{ background: rgba(34,197,94,0.15); color: #22c55e; border: 1px solid rgba(34,197,94,0.3); }}
    </style>
    <meta http-equiv="refresh" content="5">
</head>
<body>
    <div class="container">
        <h1>🔐 Telco Trust Server (Service D)</h1>
        <p class="subtitle">
            SKT USIM 기반 Agent 인증 · RS256 서명 ·
            <span class="badge badge-rsa">🔑 RSA 2048-bit</span>
        </p>

        <div class="card">
            <h2>🤖 등록된 Agent</h2>
            <table>
                <thead><tr><th>Agent ID</th><th>소유자</th><th>통신사</th><th>정책</th><th>상태</th><th>만료일</th></tr></thead>
                <tbody>{agent_rows}</tbody>
            </table>
        </div>

        <div class="card">
            <h2>📜 공증 기록 (Carrier Notary)</h2>
            <table>
                <thead><tr><th>시각</th><th>이벤트</th><th>Agent</th><th>정책</th></tr></thead>
                <tbody>{notary_rows}</tbody>
            </table>
        </div>

        <div class="card">
            <h2>📱 등록된 USIM</h2>
            <table>
                <thead><tr><th>SIM ID</th><th>통신사</th><th>소유자</th><th>상태</th></tr></thead>
                <tbody>
                    <tr>
                        <td><code>sim_001</code></td>
                        <td>SKT</td>
                        <td>홍길동</td>
                        <td><span class="status-active">Active</span></td>
                    </tr>
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)
