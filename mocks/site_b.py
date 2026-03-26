"""
site_b.py — Mock 예약 사이트 B (동기화 대상)

날짜별 가용 상태를 조회·변경할 수 있는 가상 사이트.
Port: 8002
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("site_b")

app = FastAPI(title="Mock Site B - 가용 관리", version="0.1.0")

# ── 데이터 모델 ──────────────────────────────────────

class AvailabilityUpdate(BaseModel):
    check_in: str   # YYYY-MM-DD
    check_out: str  # YYYY-MM-DD
    available: bool


# ── 인메모리 저장소 ──────────────────────────────────

ROOMS = {
    "room_101": "스위트 룸",
}

# {room_id: {date_str: available}}
availability: dict[str, dict[str, bool]] = {}


def _init_availability() -> None:
    """오늘부터 60일간 모든 객실을 available=True로 초기화."""
    today = date.today()
    for room_id in ROOMS:
        availability[room_id] = {}
        for i in range(60):
            d = today + timedelta(days=i)
            availability[room_id][d.isoformat()] = True


_init_availability()


def _date_range(check_in: str, check_out: str) -> list[str]:
    """체크인~체크아웃 전날까지의 날짜 리스트."""
    ci = date.fromisoformat(check_in)
    co = date.fromisoformat(check_out)
    dates: list[str] = []
    current = ci
    while current < co:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


# ── API 엔드포인트 ───────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    today = date.today()
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
            <div style="
                display:flex; flex-direction:column; align-items:center;
                padding:0.3rem 0.15rem; min-width:42px;
            ">
                <span style="font-size:0.6rem; color:#64748b;">{day_name}</span>
                <span style="font-size:0.7rem; color:#94a3b8;">{label}</span>
                <div style="
                    width:10px; height:10px; border-radius:50%;
                    background:{color}; margin-top:0.2rem;
                    box-shadow: 0 0 6px {color}40;
                "></div>
            </div>'''

        cards += f"""
        <div class="card">
            <h2>{room_name} <span style="color:#64748b;font-weight:400">({room_id})</span></h2>
            <div style="display:flex; flex-wrap:wrap; gap:0.2rem; margin-top:0.5rem;">
                {days_html}
            </div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Site B — 가용 현황</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Inter', -apple-system, sans-serif;
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            color: #e2e8f0;
            min-height: 100vh;
            padding: 2rem;
        }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        h1 {{
            font-size: 1.8rem;
            background: linear-gradient(90deg, #34d399, #38bdf8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.5rem;
        }}
        .subtitle {{ color: #94a3b8; margin-bottom: 1.5rem; font-size: 0.9rem; }}
        .legend {{
            display: flex; gap: 1.5rem; margin-bottom: 1.5rem;
            font-size: 0.8rem; color: #94a3b8;
        }}
        .legend-item {{ display: flex; align-items: center; gap: 0.4rem; }}
        .legend-dot {{
            width: 10px; height: 10px; border-radius: 50%;
        }}
        .card {{
            background: rgba(30, 41, 59, 0.8);
            border: 1px solid rgba(148, 163, 184, 0.1);
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 1rem;
            backdrop-filter: blur(10px);
        }}
        .card h2 {{ font-size: 1rem; color: #cbd5e1; margin-bottom: 0.5rem; }}
    </style>
    <meta http-equiv="refresh" content="5">
</head>
<body>
    <div class="container">
        <h1>📅 Site B — 가용 현황</h1>
        <p class="subtitle">Mock 동기화 대상 사이트 B · 5초마다 자동 새로고침</p>

        <div class="legend">
            <div class="legend-item">
                <div class="legend-dot" style="background:#22c55e;"></div>
                예약 가능
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background:#ef4444;"></div>
                예약 불가 (차단됨)
            </div>
        </div>

        {cards}
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/rooms/{room_id}/availability")
async def get_availability(
    room_id: str,
    check_in: str = Query(None),
    check_out: str = Query(None),
):
    if room_id not in availability:
        return {"error": f"Unknown room: {room_id}"}

    room_avail = availability[room_id]

    if check_in and check_out:
        dates = _date_range(check_in, check_out)
        result = {d: room_avail.get(d, True) for d in dates}
    else:
        result = room_avail

    return {
        "room_id": room_id,
        "room_name": ROOMS.get(room_id, room_id),
        "availability": result,
    }


@app.patch("/rooms/{room_id}/availability")
async def update_availability(room_id: str, req: AvailabilityUpdate):
    if room_id not in availability:
        return {"error": f"Unknown room: {room_id}"}

    dates = _date_range(req.check_in, req.check_out)
    updated: list[str] = []

    for d in dates:
        availability[room_id][d] = req.available
        updated.append(d)

    status = "available" if req.available else "blocked"
    logger.info(f"Room {room_id}: {status} for {updated}")

    return {
        "room_id": room_id,
        "updated_dates": updated,
        "available": req.available,
    }
