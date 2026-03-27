"""
site_b_api.py — 사이트 B 제어 Tool

Engine이 사이트 B의 가용 상태를 조회·변경할 때 사용.
401 응답 시 에러 대신 정보로 반환하여 LLM이 인증 재시도를 판단할 수 있도록 함.
"""

from __future__ import annotations

import os
import httpx
from core.executor import tool

SITE_B_URL = os.getenv("SITE_B_URL", "http://localhost:8002")


@tool
async def get_site_b_availability(room_id: str, check_in: str, check_out: str) -> dict:
    """사이트 B의 특정 객실·날짜 범위의 가용 상태를 조회합니다. 인증 없이 사용 가능합니다.

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
    """사이트 B의 특정 객실·날짜 범위를 예약 불가로 변경합니다. 인증 토큰 없이 호출하면 401 응답이 반환됩니다.

    Args:
        room_id: 객실 ID (예: "room_101")
        check_in: 체크인 날짜 (YYYY-MM-DD)
        check_out: 체크아웃 날짜 (YYYY-MM-DD)

    Returns:
        변경 결과 또는 인증 요구 메시지를 포함하는 dict
    """
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{SITE_B_URL}/rooms/{room_id}/availability",
            json={"check_in": check_in, "check_out": check_out, "available": False},
            timeout=10.0,
        )
        # 401은 에러가 아닌 정보로 반환 (LLM이 인증 재시도를 판단)
        if resp.status_code == 401:
            result = resp.json()
            result["status"] = 401
            return result
        resp.raise_for_status()
        return resp.json()


@tool
async def unblock_site_b_dates(room_id: str, check_in: str, check_out: str) -> dict:
    """사이트 B의 특정 객실·날짜 범위를 예약 가능으로 변경합니다. 인증 토큰 없이 호출하면 401 응답이 반환됩니다.

    Args:
        room_id: 객실 ID (예: "room_101")
        check_in: 체크인 날짜 (YYYY-MM-DD)
        check_out: 체크아웃 날짜 (YYYY-MM-DD)

    Returns:
        변경 결과 또는 인증 요구 메시지를 포함하는 dict
    """
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{SITE_B_URL}/rooms/{room_id}/availability",
            json={"check_in": check_in, "check_out": check_out, "available": True},
            timeout=10.0,
        )
        if resp.status_code == 401:
            result = resp.json()
            result["status"] = 401
            return result
        resp.raise_for_status()
        return resp.json()


@tool
async def block_site_b_dates_with_token(room_id: str, check_in: str, check_out: str, token: str, vpal_session_id: str) -> dict:
    """인증 토큰과 VPAL 세션을 포함하여 사이트 B의 특정 객실·날짜 범위를 예약 불가로 변경합니다. get_telco_auth_token으로 발급받은 token과 vpal_session_id를 모두 사용하세요.

    Args:
        room_id: 객실 ID (예: "room_101")
        check_in: 체크인 날짜 (YYYY-MM-DD)
        check_out: 체크아웃 날짜 (YYYY-MM-DD)
        token: Telco Trust Server에서 발급받은 JWT 토큰
        vpal_session_id: Telco에서 발급받은 VPAL 세션 ID

    Returns:
        변경 결과를 포함하는 dict
    """
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{SITE_B_URL}/rooms/{room_id}/availability",
            json={"check_in": check_in, "check_out": check_out, "available": False},
            headers={
                "Authorization": f"Bearer {token}",
                "X-VPAL-Session": vpal_session_id,
            },
            timeout=10.0,
        )
        if resp.status_code == 401:
            result = resp.json()
            result["status"] = 401
            return result
        resp.raise_for_status()
        return resp.json()


@tool
async def unblock_site_b_dates_with_token(room_id: str, check_in: str, check_out: str, token: str, vpal_session_id: str) -> dict:
    """인증 토큰과 VPAL 세션을 포함하여 사이트 B의 특정 객실·날짜 범위를 예약 가능으로 변경합니다. get_telco_auth_token으로 발급받은 token과 vpal_session_id를 모두 사용하세요.

    Args:
        room_id: 객실 ID (예: "room_101")
        check_in: 체크인 날짜 (YYYY-MM-DD)
        check_out: 체크아웃 날짜 (YYYY-MM-DD)
        token: Telco Trust Server에서 발급받은 JWT 토큰
        vpal_session_id: Telco에서 발급받은 VPAL 세션 ID

    Returns:
        변경 결과를 포함하는 dict
    """
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{SITE_B_URL}/rooms/{room_id}/availability",
            json={"check_in": check_in, "check_out": check_out, "available": True},
            headers={
                "Authorization": f"Bearer {token}",
                "X-VPAL-Session": vpal_session_id,
            },
            timeout=10.0,
        )
        if resp.status_code == 401:
            result = resp.json()
            result["status"] = 401
            return result
        resp.raise_for_status()
        return resp.json()
