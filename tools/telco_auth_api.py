"""
telco_auth_api.py — Telco 인증 토큰 발급 Tool

Agent가 Site B 인증에 필요한 JWT 토큰을 Telco Trust Server에서 발급받을 때 사용.
온보딩 완료 후 자동으로 agent_id를 사용하여 정책 기반 토큰을 요청.
"""

from __future__ import annotations

import os
import httpx
from core.executor import tool

TELCO_AUTH_URL = os.getenv("TELCO_AUTH_URL", "http://localhost:8003")


@tool
async def get_telco_auth_token(action: str, resource: str, target_site: str) -> dict:
    """Telco Trust Server에서 정책 기반 JWT 인증 토큰을 발급받습니다. Site B에서 401 Unauthorized 응답을 받았을 때 이 도구를 사용하세요.

    Args:
        action: 수행할 작업 (예: "block", "unblock", "query")
        resource: 대상 리소스 (예: "room_101")
        target_site: 대상 사이트 (예: "site_b")

    Returns:
        토큰 정보를 포함하는 dict (token, expires_in, token_type, policy_matched)
    """
    # Agent ID를 위임장에서 로드
    from core.onboarding_manager import OnboardingManager
    manager = OnboardingManager()
    agent_id = manager.get_agent_id()

    if not agent_id:
        return {"error": "온보딩이 완료되지 않았습니다. 먼저 온보딩을 진행해주세요."}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{TELCO_AUTH_URL}/auth/token",
            json={
                "agent_id": agent_id,
                "action": action,
                "resource": resource,
                "target_site": target_site,
            },
            timeout=10.0,
        )
        if resp.status_code == 403:
            return resp.json()
        resp.raise_for_status()
        return resp.json()
