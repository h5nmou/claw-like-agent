"""
site_a.py — Mock 예약 사이트 A

예약을 생성/취소하고, Engine에 Webhook을 전송하는 가상 사이트.
Port: 8001
"""

from __future__ import annotations

import os
import uuid
import logging
from datetime import date, timedelta

import httpx
from fastapi import FastAPI, Form as FastAPIForm
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("site_a")

app = FastAPI(title="Mock Site A - 예약 사이트", version="0.1.0")

ENGINE_WEBHOOK_URL = os.getenv("ENGINE_WEBHOOK_URL", "http://localhost:8000/webhook")

# ── 데이터 모델 ──────────────────────────────────────

class BookingRequest(BaseModel):
    room_id: str
    guest_name: str
    check_in: str  # YYYY-MM-DD
    check_out: str  # YYYY-MM-DD


class Booking(BaseModel):
    booking_id: str
    room_id: str
    guest_name: str
    check_in: str
    check_out: str
    status: str  # "confirmed" | "cancelled"


# ── 인메모리 저장소 ──────────────────────────────────

ROOMS = {
    "room_101": "스위트 룸",
}

bookings: dict[str, Booking] = {}

# 가용 현황 (Site B와 동일 구조)
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


def _mark_booked(room_id: str, check_in: str, check_out: str) -> None:
    """예약된 날짜를 가용 불가로 표시."""
    ci = date.fromisoformat(check_in)
    co = date.fromisoformat(check_out)
    current = ci
    while current < co:
        if room_id in availability:
            availability[room_id][current.isoformat()] = False
        current += timedelta(days=1)


def _mark_available(room_id: str, check_in: str, check_out: str) -> None:
    """취소된 날짜를 가용 가능으로 복원."""
    ci = date.fromisoformat(check_in)
    co = date.fromisoformat(check_out)
    current = ci
    while current < co:
        if room_id in availability:
            availability[room_id][current.isoformat()] = True
        current += timedelta(days=1)


# ── Webhook 전송 ─────────────────────────────────────

async def send_webhook(event_type: str, booking: Booking) -> None:
    payload = {
        "event": event_type,
        "source": "site_a",
        "booking": {
            "booking_id": booking.booking_id,
            "guest_name": booking.guest_name,
            "room_id": booking.room_id,
            "check_in": booking.check_in,
            "check_out": booking.check_out,
        },
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(ENGINE_WEBHOOK_URL, json=payload, timeout=30.0)
            logger.info(f"Webhook sent ({event_type}): {resp.status_code}")
    except Exception as e:
        logger.error(f"Webhook failed: {e}")


# ── API 엔드포인트 ───────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    today = date.today()

    # ── 예약 목록 테이블 ──
    rows = ""
    for b in bookings.values():
        status_badge = (
            '<span style="color:#22c55e;font-weight:600">✓ 확정</span>'
            if b.status == "confirmed"
            else '<span style="color:#ef4444;font-weight:600">✗ 취소</span>'
        )
        rows += f"""
        <tr>
            <td>{b.booking_id[:8]}...</td>
            <td>{b.guest_name}</td>
            <td>{b.check_in}</td>
            <td>{b.check_out}</td>
            <td>{status_badge}</td>
        </tr>"""

    # ── 가용 현황 달력 ──
    calendar_html = ""
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

        calendar_html += f"""
            <h2>{room_name} <span style="color:#64748b;font-weight:400">({room_id})</span></h2>
            <div class="legend">
                <div class="legend-item">
                    <div class="legend-dot" style="background:#22c55e;"></div>예약 가능
                </div>
                <div class="legend-item">
                    <div class="legend-dot" style="background:#ef4444;"></div>예약됨
                </div>
            </div>
            <div style="display:flex; flex-wrap:wrap; gap:0.2rem; margin-top:0.5rem;">
                {days_html}
            </div>"""

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Site A — 예약 관리</title>
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
            background: linear-gradient(90deg, #38bdf8, #818cf8);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.5rem;
        }}
        .subtitle {{ color: #94a3b8; margin-bottom: 2rem; font-size: 0.9rem; }}
        .card {{
            background: rgba(30, 41, 59, 0.8);
            border: 1px solid rgba(148, 163, 184, 0.1);
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 1.5rem;
            backdrop-filter: blur(10px);
        }}
        .card h2 {{ font-size: 1.1rem; color: #cbd5e1; margin-bottom: 1rem; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{
            padding: 0.75rem;
            text-align: left;
            border-bottom: 1px solid rgba(148, 163, 184, 0.1);
            font-size: 0.85rem;
        }}
        th {{ color: #94a3b8; font-weight: 600; text-transform: uppercase; font-size: 0.75rem; }}
        .form-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1rem;
        }}
        .form-group {{ display: flex; flex-direction: column; gap: 0.3rem; }}
        label {{ font-size: 0.8rem; color: #94a3b8; }}
        input, select {{
            background: rgba(15, 23, 42, 0.6);
            border: 1px solid rgba(148, 163, 184, 0.2);
            border-radius: 8px;
            padding: 0.6rem;
            color: #e2e8f0;
            font-size: 0.9rem;
        }}
        button {{
            grid-column: 1 / -1;
            background: linear-gradient(135deg, #3b82f6, #8b5cf6);
            border: none;
            border-radius: 8px;
            padding: 0.75rem;
            color: white;
            font-weight: 600;
            cursor: pointer;
            font-size: 0.9rem;
            transition: opacity 0.2s;
        }}
        button:hover {{ opacity: 0.9; }}
        .empty {{ color: #64748b; text-align: center; padding: 2rem; }}
        .legend {{
            display: flex; gap: 1.5rem; margin-bottom: 0.5rem;
            font-size: 0.8rem; color: #94a3b8;
        }}
        .legend-item {{ display: flex; align-items: center; gap: 0.4rem; }}
        .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🏨 Site A — 예약 관리</h1>
        <p class="subtitle">Mock 예약 사이트 A · 예약 생성 시 Engine에 Webhook 전송 · 5초마다 자동 새로고침</p>

        <div class="card" id="calendar-section">
            <h2>📅 가용 현황</h2>
            {calendar_html}
        </div>

        <div class="card">
            <h2>📋 예약 목록</h2>
            <div id="booking-list">{"<table><thead><tr><th>ID</th><th>투숙객</th><th>체크인</th><th>체크아웃</th><th>상태</th></tr></thead><tbody>" + rows + "</tbody></table>" if rows else '<p class="empty">예약이 없습니다</p>'}</div>
        </div>

        <div class="card">
            <h2>➕ 새 예약 생성</h2>
            <form method="post" action="/bookings-form" class="form-grid">
                <div class="form-group">
                    <label>투숙객 이름</label>
                    <input type="text" name="guest_name" required placeholder="홍길동">
                </div>
                <div class="form-group" style="display:none">
                    <input type="hidden" name="room_id" value="room_101">
                </div>
                <div class="form-group">
                    <label>체크인</label>
                    <input type="date" name="check_in" required>
                </div>
                <div class="form-group">
                    <label>체크아웃</label>
                    <input type="date" name="check_out" required>
                </div>
                <button type="submit">예약 생성 (Webhook 전송)</button>
            </form>
        </div>
    </div>
    <script>
    setInterval(async () => {{
        try {{
            const resp = await fetch('/partial');
            const data = await resp.json();
            document.getElementById('calendar-section').innerHTML = '<h2>📅 가용 현황</h2>' + data.calendar_html;
            document.getElementById('booking-list').innerHTML = data.bookings_html;
        }} catch(e) {{}}
    }}, 5000);
    </script>
</body>
</html>"""
    return HTMLResponse(content=html)


def _build_calendar_html() -> str:
    """달력 HTML을 반환 (부분 갱신용)."""
    today = date.today()
    html = ""
    for room_id, room_name in ROOMS.items():
        days_html = ""
        for i in range(30):
            d = today + timedelta(days=i)
            ds = d.isoformat()
            avail = availability.get(room_id, {}).get(ds, True)
            color = "#22c55e" if avail else "#ef4444"
            label = d.strftime("%m/%d")
            day_name = ["월", "화", "수", "목", "금", "토", "일"][d.weekday()]
            days_html += (
                f'<div style="display:flex;flex-direction:column;align-items:center;'
                f'padding:0.3rem 0.15rem;min-width:42px;">'
                f'<span style="font-size:0.6rem;color:#64748b;">{day_name}</span>'
                f'<span style="font-size:0.7rem;color:#94a3b8;">{label}</span>'
                f'<div style="width:10px;height:10px;border-radius:50%;'
                f'background:{color};margin-top:0.2rem;box-shadow:0 0 6px {color}40;"></div>'
                f'</div>'
            )
        html += (
            f'<h2>{room_name} <span style="color:#64748b;font-weight:400">({room_id})</span></h2>'
            f'<div class="legend">'
            f'<div class="legend-item"><div class="legend-dot" style="background:#22c55e;"></div>예약 가능</div>'
            f'<div class="legend-item"><div class="legend-dot" style="background:#ef4444;"></div>예약됨</div>'
            f'</div>'
            f'<div style="display:flex;flex-wrap:wrap;gap:0.2rem;margin-top:0.5rem;">{days_html}</div>'
        )
    return html


def _build_bookings_html() -> str:
    """예약 목록 HTML을 반환 (부분 갱신용)."""
    if not bookings:
        return '<p class="empty">예약이 없습니다</p>'
    rows = ""
    for b in bookings.values():
        status_badge = (
            '<span style="color:#22c55e;font-weight:600">✓ 확정</span>'
            if b.status == "confirmed"
            else '<span style="color:#ef4444;font-weight:600">✗ 취소</span>'
        )
        rows += (
            f"<tr><td>{b.booking_id[:8]}...</td>"
            f"<td>{b.guest_name}</td>"
            f"<td>{b.check_in}</td><td>{b.check_out}</td>"
            f"<td>{status_badge}</td></tr>"
        )
    return (
        "<table><thead><tr><th>ID</th><th>투숙객</th><th>체크인</th>"
        "<th>체크아웃</th><th>상태</th></tr></thead><tbody>"
        + rows + "</tbody></table>"
    )


@app.get("/partial")
async def partial():
    """JS로 부분 갱신할 달력 + 예약 목록 HTML을 JSON으로 반환."""
    return {
        "calendar_html": _build_calendar_html(),
        "bookings_html": _build_bookings_html(),
    }


@app.get("/bookings")
async def list_bookings():
    return {"bookings": [b.model_dump() for b in bookings.values()]}


@app.post("/bookings")
async def create_booking(req: BookingRequest):
    booking = Booking(
        booking_id=str(uuid.uuid4()),
        room_id=req.room_id,
        guest_name=req.guest_name,
        check_in=req.check_in,
        check_out=req.check_out,
        status="confirmed",
    )
    bookings[booking.booking_id] = booking
    _mark_booked(booking.room_id, booking.check_in, booking.check_out)
    logger.info(f"Booking created: {booking.booking_id}")

    # Webhook 전송 (비동기)
    await send_webhook("booking_confirmed", booking)

    return booking.model_dump()


@app.post("/bookings-form")
async def create_booking_form(
    room_id: str = FastAPIForm(...),
    guest_name: str = FastAPIForm(...),
    check_in: str = FastAPIForm(...),
    check_out: str = FastAPIForm(...),
):
    """HTML 폼에서의 예약 생성 (POST form-data)."""
    booking = Booking(
        booking_id=str(uuid.uuid4()),
        room_id=room_id,
        guest_name=guest_name,
        check_in=check_in,
        check_out=check_out,
        status="confirmed",
    )
    bookings[booking.booking_id] = booking
    _mark_booked(booking.room_id, booking.check_in, booking.check_out)
    logger.info(f"Booking created (form): {booking.booking_id}")

    await send_webhook("booking_confirmed", booking)

    return HTMLResponse(
        content='<html><head><meta http-equiv="refresh" content="0;url=/"></head></html>'
    )


@app.delete("/bookings/{booking_id}")
async def cancel_booking(booking_id: str):
    booking = bookings.get(booking_id)
    if not booking:
        return {"error": "Booking not found"}, 404

    booking.status = "cancelled"
    _mark_available(booking.room_id, booking.check_in, booking.check_out)
    logger.info(f"Booking cancelled: {booking_id}")

    await send_webhook("booking_cancelled", booking)

    return booking.model_dump()
