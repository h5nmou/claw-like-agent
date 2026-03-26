"""
site_b_api.py — 사이트 B 제어 Tool

Engine이 사이트 B의 가용 상태를 조회·변경할 때 사용.
"""

from __future__ import annotations

import os
import httpx
from core.executor import tool

SITE_B_URL = os.getenv("SITE_B_URL", "http://localhost:8002")


@tool
async def get_site_b_availability(room_id: str, check_in: str, check_out: str) -> dict:
    """사이트 B의 특정 객실·날짜 범위의 가용 상태를 조회합니다.

    Args:
        room_id: 객실 ID (예: "room_101")
        check_in: 체크인 날짜 (YYYY-MM-DD)
        check_out: 체크아웃 날짜 (YYYY-MM-DD)

    Returns:
        날짜별 가용 상태를 포함하는 dict
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SITE_B_URL}/rooms/{room_id}/availability",
            params={"check_in": check_in, "check_out": check_out},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()


@tool
async def block_site_b_dates(room_id: str, check_in: str, check_out: str) -> dict:
    """사이트 B의 특정 객실·날짜 범위를 예약 불가로 변경합니다.

    Args:
        room_id: 객실 ID (예: "room_101")
        check_in: 체크인 날짜 (YYYY-MM-DD)
        check_out: 체크아웃 날짜 (YYYY-MM-DD)

    Returns:
        변경 결과를 포함하는 dict
    """
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{SITE_B_URL}/rooms/{room_id}/availability",
            json={"check_in": check_in, "check_out": check_out, "available": False},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()


@tool
async def unblock_site_b_dates(room_id: str, check_in: str, check_out: str) -> dict:
    """사이트 B의 특정 객실·날짜 범위를 예약 가능으로 변경합니다.

    Args:
        room_id: 객실 ID (예: "room_101")
        check_in: 체크인 날짜 (YYYY-MM-DD)
        check_out: 체크아웃 날짜 (YYYY-MM-DD)

    Returns:
        변경 결과를 포함하는 dict
    """
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{SITE_B_URL}/rooms/{room_id}/availability",
            json={"check_in": check_in, "check_out": check_out, "available": True},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()
