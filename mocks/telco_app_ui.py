"""
telco_app_ui.py — Mock 통신사 앱 (사장님 승인 UI)

사장님이 폰에서 Agent를 승인하는 대화형 인터페이스.
온보딩 시 Agent가 이 URL을 안내하면, 사장님이 접속하여 PIN 입력 후 승인.
Port: 8004
"""

from __future__ import annotations

import logging

import httpx
from fastapi import FastAPI, Form as FastAPIForm
from fastapi.responses import HTMLResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("telco_app")

app = FastAPI(title="SKT Agent 관리 앱 (Mock)", version="0.1.0")

TELCO_SERVER_URL = "http://localhost:8003"

# 온보딩 대기 중인 Agent 정보 (Engine이 POST /pending로 전달)
pending_agent: dict | None = None


@app.post("/pending")
async def set_pending(data: dict):
    """Engine이 온보딩 대기 Agent 정보를 전달."""
    global pending_agent
    pending_agent = data
    logger.info(f"Pending agent set: {data}")
    return {"status": "pending_set"}


@app.get("/", response_class=HTMLResponse)
async def index():
    if not pending_agent:
        body = """
        <div class="empty-state">
            <div style="font-size:3rem;">📱</div>
            <h2>SKT Agent 관리</h2>
            <p>대기 중인 Agent 승인 요청이 없습니다.</p>
        </div>"""
    else:
        public_key_preview = pending_agent.get("public_key", "")[:80] + "..."
        policies = ", ".join(pending_agent.get("requested_policies", []))
        body = f"""
        <div class="card">
            <div class="alert">
                <span style="font-size:1.5rem;">🆕</span>
                <div>
                    <strong>새 Agent 등록 요청</strong>
                    <p style="font-size:0.8rem;color:#94a3b8;">아래 Agent에 권한을 위임하려면 USIM PIN을 입력해주세요.</p>
                </div>
            </div>

            <div class="info-grid">
                <div class="info-item">
                    <span class="info-label">Agent 이름</span>
                    <span>Universal Agent Engine</span>
                </div>
                <div class="info-item">
                    <span class="info-label">요청 권한</span>
                    <span style="color:#38bdf8;">{policies}</span>
                </div>
                <div class="info-item">
                    <span class="info-label">권한 설명</span>
                    <span style="font-size:0.85rem;">숙박 예약 사이트 간 가용 상태 동기화</span>
                </div>
                <div class="info-item">
                    <span class="info-label">Public Key</span>
                    <code style="font-size:0.7rem;word-break:break-all;">{public_key_preview}</code>
                </div>
            </div>

            <form method="post" action="/approve" style="margin-top:1.5rem;">
                <div class="pin-group">
                    <label>🔐 USIM PIN</label>
                    <input type="password" name="sim_pin" maxlength="4" placeholder="****" required
                           style="text-align:center; font-size:1.5rem; letter-spacing:0.5rem; width:150px;">
                </div>
                <button type="submit" class="btn-approve">✅ 승인 — 이 Agent에 권한 위임</button>
                <button type="button" class="btn-deny" onclick="window.location='/'">❌ 거부</button>
            </form>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SKT Agent 관리 앱</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Inter', -apple-system, sans-serif;
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            color: #e2e8f0;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 2rem;
        }}
        .container {{ max-width: 480px; width: 100%; }}
        .header {{
            text-align: center; margin-bottom: 2rem;
        }}
        .header h1 {{
            font-size: 1.4rem;
            background: linear-gradient(90deg, #e11d48, #f97316);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}
        .header .carrier {{ color: #94a3b8; font-size: 0.85rem; margin-top: 0.3rem; }}
        .card {{
            background: rgba(30, 41, 59, 0.8);
            border: 1px solid rgba(148, 163, 184, 0.1);
            border-radius: 16px;
            padding: 1.5rem;
            backdrop-filter: blur(10px);
        }}
        .alert {{
            display: flex; gap: 1rem; align-items: flex-start;
            background: rgba(245, 158, 11, 0.1);
            border: 1px solid rgba(245, 158, 11, 0.2);
            border-radius: 10px;
            padding: 1rem;
            margin-bottom: 1.5rem;
        }}
        .info-grid {{ display: flex; flex-direction: column; gap: 0.8rem; }}
        .info-item {{ display: flex; flex-direction: column; gap: 0.2rem; }}
        .info-label {{ font-size: 0.7rem; color: #64748b; text-transform: uppercase; font-weight: 600; }}
        .pin-group {{
            display: flex; flex-direction: column; align-items: center; gap: 0.5rem;
            margin-bottom: 1rem;
        }}
        .pin-group label {{ font-size: 0.85rem; color: #94a3b8; }}
        input {{
            background: rgba(15, 23, 42, 0.6);
            border: 1px solid rgba(148, 163, 184, 0.2);
            border-radius: 10px;
            padding: 0.8rem;
            color: #e2e8f0;
            font-size: 1rem;
        }}
        .btn-approve {{
            width: 100%;
            background: linear-gradient(135deg, #22c55e, #16a34a);
            border: none; border-radius: 10px;
            padding: 0.9rem; color: white;
            font-weight: 700; font-size: 0.95rem;
            cursor: pointer; transition: opacity 0.2s;
            margin-bottom: 0.5rem;
        }}
        .btn-approve:hover {{ opacity: 0.9; }}
        .btn-deny {{
            width: 100%;
            background: transparent;
            border: 1px solid rgba(239,68,68,0.3);
            border-radius: 10px;
            padding: 0.7rem; color: #f87171;
            font-weight: 600; font-size: 0.85rem;
            cursor: pointer;
        }}
        .empty-state {{ text-align: center; color: #64748b; padding: 3rem; }}
        .empty-state h2 {{ color: #94a3b8; margin: 1rem 0 0.5rem; }}
        .result {{ text-align: center; padding: 2rem; }}
        .result .icon {{ font-size: 3rem; margin-bottom: 1rem; }}
    </style>
    <meta http-equiv="refresh" content="3">
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📱 SKT Agent 관리</h1>
            <p class="carrier">SK Telecom · USIM 기반 Agent 인증</p>
        </div>
        {body}
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.post("/approve", response_class=HTMLResponse)
async def approve(sim_pin: str = FastAPIForm(...)):
    """사장님의 PIN 승인을 처리."""
    global pending_agent

    if not pending_agent:
        return HTMLResponse(content="<p>대기 중인 요청이 없습니다.</p>")

    # Telco Server에 온보딩 요청
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{TELCO_SERVER_URL}/onboarding/approve",
                json={
                    "sim_id": "sim_001",
                    "sim_pin": sim_pin,
                    "agent_public_key": pending_agent.get("public_key", ""),
                    "approved_policies": pending_agent.get("requested_policies", ["booking_sync"]),
                    "delegation_duration_days": 365,
                },
                timeout=10.0,
            )

        if resp.status_code != 200:
            error = resp.json().get("error", "Unknown error")
            html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>실패</title>
<style>body {{ font-family: sans-serif; background: #0f172a; color: #e2e8f0; display:flex; justify-content:center; align-items:center; min-height:100vh; }}
.card {{ background: rgba(30,41,59,0.8); border-radius:16px; padding:2rem; text-align:center; }}
</style></head><body><div class="card">
    <div style="font-size:3rem;">❌</div>
    <h2 style="color:#f87171;">승인 실패</h2>
    <p>{error}</p>
    <p style="margin-top:1rem;"><a href="/" style="color:#58a6ff;">다시 시도</a></p>
</div></body></html>"""
            return HTMLResponse(content=html)

        result = resp.json()
        agent_id = result.get("agent_id", "N/A")

        # Engine에 온보딩 결과 알림
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    "http://localhost:8000/onboarding/complete",
                    json=result,
                    timeout=10.0,
                )
        except Exception:
            pass  # Engine이 아직 안 떠있을 수 있음

        pending_agent = None

        html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>승인 완료</title>
<style>body {{ font-family: sans-serif; background: #0f172a; color: #e2e8f0; display:flex; justify-content:center; align-items:center; min-height:100vh; }}
.card {{ background: rgba(30,41,59,0.8); border-radius:16px; padding:2rem; text-align:center; max-width:400px; }}
</style></head><body><div class="card">
    <div style="font-size:3rem;">✅</div>
    <h2 style="color:#22c55e;">승인 완료!</h2>
    <p>Agent <code>{agent_id}</code>에 권한이 위임되었습니다.</p>
    <p style="font-size:0.8rem;color:#94a3b8;margin-top:1rem;">이후부터는 통신사가 보안을 자동으로 관리합니다.</p>
</div></body></html>"""
        return HTMLResponse(content=html)

    except Exception as e:
        return HTMLResponse(content=f"<p>에러: {e}</p>")
