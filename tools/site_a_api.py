"""
site_a_api.py — 사이트 A 조회 Tool

Engine이 사이트 A의 예약 정보를 조회할 때 사용.
"""

from __future__ import annotations

import os
import httpx
from core.executor import tool

SITE_A_URL = os.getenv("SITE_A_URL", "http://localhost:8001")


@tool
async def get_site_a_bookings() -> dict:
    """사이트 A의 현재 예약 목록을 조회합니다.

    Returns:
        예약 목록을 포함하는 dict
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{SITE_A_URL}/bookings", timeout=10.0)
        resp.raise_for_status()
        return resp.json()


@tool
async def create_site_a_booking(room_id: str, guest_name: str, check_in: str, check_out: str) -> dict:
    """사이트 A에 새로운 예약을 생성합니다. (수동 예약 반영용)
    
    Args:
        room_id: 대상 객실 (예: "room_101")
        guest_name: 투숙객 이름
        check_in: 체크인 날짜 (YYYY-MM-DD)
        check_out: 체크아웃 날짜 (YYYY-MM-DD)
    """
    payload = {
        "room_id": room_id,
        "guest_name": guest_name,
        "check_in": check_in,
        "check_out": check_out,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{SITE_A_URL}/bookings", json=payload, timeout=10.0)
        resp.raise_for_status()
        return resp.json()
