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
