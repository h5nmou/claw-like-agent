"""
site_b.py — Mock 예약 사이트 B (Guardian Agent)

날짜별 가용 상태를 조회·변경할 수 있는 가상 사이트.
PATCH 엔드포인트는 Telco RS256 JWT 인증 필수.
Port: 8002
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx
import jwt
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("site_b")

app = FastAPI(title="Mock Site B - Guardian Agent", version="0.2.0")

# ── Telco Public Key (부팅 시 가져옴) ────────────────

_telco_public_key = None


async def _ensure_telco_key():
    """Telco Public Key가 없으면 가져오기를 시도."""
    global _telco_public_key
    if _telco_public_key is not None:
        return True
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://localhost:8003/public-key", timeout=3.0)
            if resp.status_code == 200:
                pem = resp.json()["telco_public_key"]
                _telco_public_key = load_pem_public_key(pem.encode("utf-8"))
                logger.info("Telco Public Key 로드 완료 (RS256 검증 활성화)")
                return True
    except Exception as e:
        logger.warning(f"Telco Public Key 로드 실패: {e}")
    return False


async def verify_token(request: Request) -> dict | None:
    """Authorization 헤더에서 JWT RS256 검증 + VPAL 세션 이중 검증."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]

    # Lazy loading: 키가 없으면 지금 가져오기 시도
    if _telco_public_key is None:
        loaded = await _ensure_telco_key()
        if not loaded:
            logger.warning("Telco Public Key 없음 — 인증 불가")
            return None

    try:
        payload = jwt.decode(token, _telco_public_key, algorithms=["RS256"])
    except jwt.ExpiredSignatureError:
        logger.warning("Token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid token: {e}")
        return None

    # VPAL 세션 이중 검증
    vpal_header = request.headers.get("X-VPAL-Session", "")
    vpal_in_token = payload.get("vpal_session_id", "")
    if vpal_in_token and vpal_header != vpal_in_token:
        logger.warning(f"VPAL 세션 불일치: header={vpal_header}, token={vpal_in_token}")
        return None

    logger.info(f"VPAL 검증 통과: session={vpal_header[:8]}...")
    return payload


# ── 데이터 모델 ──────────────────────────────────────

class AvailabilityUpdate(BaseModel):
    check_in: str
    check_out: str
    available: bool


# ── 인메모리 저장소 ──────────────────────────────────

ROOMS = {
    "room_101": "스위트 룸",
}

availability: dict[str, dict[str, bool]] = {}


def _init_availability() -> None:
    today = date.today()
    for room_id in ROOMS:
        availability[room_id] = {}
        for i in range(60):
            d = today + timedelta(days=i)
            availability[room_id][d.isoformat()] = True


_init_availability()


def _date_range(check_in: str, check_out: str) -> list[str]:
    ci = date.fromisoformat(check_in)
    co = date.fromisoformat(check_out)
    dates: list[str] = []
    current = ci
    while current < co:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


# ── API ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    today = date.today()
    auth_status = "RS256 활성" if _telco_public_key else "대기 중 (Telco 미연결)"
    auth_color = "#22c55e" if _telco_public_key else "#f59e0b"
    cards = ""

    for room_id, room_name in ROOMS.items():
        days_html = ""
        for i in range(30):
            d = today + timedelta(days=i)
            ds = d.isoformat()
            avail = availability.get(room_id, {}).get(ds, True)
            color = "#22c55e" if avail else "#ef4444"
            label = d.strftime("%m/%d")
            day_name = ["월", "화", "수", "목", "금", "토", "일"][d.weekday()]
            days_html += f'''
            <div style="display:flex;flex-direction:column;align-items:center;padding:0.3rem 0.15rem;min-width:42px;">
                <span style="font-size:0.6rem;color:#64748b;">{day_name}</span>
                <span style="font-size:0.7rem;color:#94a3b8;">{label}</span>
                <div style="width:10px;height:10px;border-radius:50%;background:{color};margin-top:0.2rem;box-shadow:0 0 6px {color}40;"></div>
            </div>'''

        cards += f"""
        <div class="card">
            <h2>{room_name} <span style="color:#64748b;font-weight:400">({room_id})</span></h2>
            <div style="display:flex;flex-wrap:wrap;gap:0.2rem;margin-top:0.5rem;">{days_html}</div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Site B — Guardian Agent</title>
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
            background: linear-gradient(90deg, #34d399, #38bdf8);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            margin-bottom: 0.5rem;
        }}
        .subtitle {{ color: #94a3b8; margin-bottom: 1.5rem; font-size: 0.9rem; }}
        .legend {{ display: flex; gap: 1.5rem; margin-bottom: 1.5rem; font-size: 0.8rem; color: #94a3b8; }}
        .legend-item {{ display: flex; align-items: center; gap: 0.4rem; }}
        .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; }}
        .card {{
            background: rgba(30, 41, 59, 0.8);
            border: 1px solid rgba(148, 163, 184, 0.1);
            border-radius: 12px; padding: 1.5rem; margin-bottom: 1rem;
        }}
        .card h2 {{ font-size: 1rem; color: #cbd5e1; margin-bottom: 0.5rem; }}
        .badge {{
            display: inline-flex; align-items: center; gap: 0.3rem;
            padding: 0.2rem 0.6rem; border-radius: 12px; font-size: 0.7rem; font-weight: 600;
        }}
    </style>
    <meta http-equiv="refresh" content="5">
</head>
<body>
    <div class="container">
        <h1>📅 Site B — Guardian Agent</h1>
        <p class="subtitle">
            Mock 동기화 대상 사이트 · 5초 새로고침 ·
            <span class="badge" style="background:rgba({','.join(['34,197,94' if _telco_public_key else '245,158,11'])},0.15);color:{auth_color};border:1px solid {auth_color}40;">
                🔒 {auth_status}
            </span>
        </p>
        <div class="legend">
            <div class="legend-item"><div class="legend-dot" style="background:#22c55e;"></div>예약 가능</div>
            <div class="legend-item"><div class="legend-dot" style="background:#ef4444;"></div>예약 불가 (차단됨)</div>
        </div>
        {cards}
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/rooms/{room_id}/availability")
async def get_availability(room_id: str, check_in: str = Query(None), check_out: str = Query(None)):
    """가용 상태 조회 — 인증 불필요."""
    if room_id not in availability:
        return {"error": f"Unknown room: {room_id}"}
    room_avail = availability[room_id]
    if check_in and check_out:
        dates = _date_range(check_in, check_out)
        result = {d: room_avail.get(d, True) for d in dates}
    else:
        result = room_avail
    return {"room_id": room_id, "room_name": ROOMS.get(room_id, room_id), "availability": result}


@app.patch("/rooms/{room_id}/availability")
async def update_availability(room_id: str, req: AvailabilityUpdate, request: Request):
    """가용 상태 변경 — Telco RS256 JWT 인증 필수."""
    payload = await verify_token(request)
    if payload is None:
        logger.warning(f"Unauthorized PATCH attempt for {room_id}")
        return JSONResponse(
            status_code=401,
            content={
                "error": "Unauthorized",
                "required": "Telco-Auth-Token",
                "message": "Telco Trust Server에서 RS256 서명된 JWT가 필요합니다.",
            },
        )

    logger.info(f"Authenticated: agent={payload.get('agent_id')}, policy={payload.get('policy')}, owner={payload.get('owner')}")

    if room_id not in availability:
        return {"error": f"Unknown room: {room_id}"}

    dates = _date_range(req.check_in, req.check_out)
    updated = []
    for d in dates:
        availability[room_id][d] = req.available
        updated.append(d)

    status = "available" if req.available else "blocked"
    logger.info(f"Room {room_id}: {status} for {updated}")

    return {
        "room_id": room_id,
        "updated_dates": updated,
        "available": req.available,
        "authenticated_by": payload.get("agent_id"),
        "policy": payload.get("policy"),
    }
