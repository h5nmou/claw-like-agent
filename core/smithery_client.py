"""
smithery_client.py — Smithery Registry REST API Client

Smithery.ai의 MCP 레지스트리를 직접 연동하여:
  1. 시맨틱 검색으로 MCP 서버 탐색
  2. 서버 상세 정보 및 도구 목록 조회
  3. 매니지드 프록시 연결 생성/관리
  4. 프록시를 통한 MCP 도구 호출

환경변수: SMITHERY_API_KEY (없으면 비활성화, 웹 검색 폴백)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger("smithery_client")

# ── Smithery REST API Client ────────────────────────────


class SmitheryClient:
    """Smithery Registry REST API 클라이언트."""

    BASE_URL = "https://api.smithery.ai"

    def __init__(self) -> None:
        # qualifiedName -> {"connectionId": str, "namespace": str}
        self._connection_cache: dict[str, dict[str, str]] = {}

    @property
    def api_key(self) -> str:
        """매번 os.environ에서 읽어 reload_env 후 즉시 반영."""
        return os.getenv("SMITHERY_API_KEY", "")

    @property
    def is_available(self) -> bool:
        """API 키가 설정되어 있으면 True."""
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ── 1. 서버 검색 ───────────────────────────────────────

    async def search_servers(
        self, query: str, page_size: int = 5
    ) -> list[dict] | None:
        """Smithery 레지스트리에서 MCP 서버를 시맨틱 검색.

        Args:
            query: 자연어 검색 쿼리 (시맨틱 검색, 프롬프트처럼 전달 가능)
            page_size: 결과 수 (기본 5)

        Returns:
            서버 목록 또는 None (API 키 미설정/오류)
        """
        if not self.is_available:
            return None

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self.BASE_URL}/servers",
                    params={"q": query, "pageSize": page_size},
                    headers=self._headers(),
                )
                if resp.status_code != 200:
                    logger.warning(
                        f"Smithery 검색 실패: HTTP {resp.status_code} — {resp.text[:200]}"
                    )
                    return None

                data = resp.json()
                servers = data.get("servers", [])
                logger.info(
                    f"Smithery 검색 '{query}' → {len(servers)}개 서버 발견"
                )
                return servers

        except Exception as e:
            logger.warning(f"Smithery 검색 오류: {e}")
            return None

    # ── 2. 서버 상세 정보 ──────────────────────────────────

    async def get_server_details(
        self, qualified_name: str
    ) -> dict | None:
        """특정 MCP 서버의 상세 정보 (도구 목록 포함) 조회.

        Args:
            qualified_name: 서버 식별자 (예: "@anthropic/weather-server")

        Returns:
            서버 상세 정보 dict 또는 None
        """
        if not self.is_available:
            return None

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self.BASE_URL}/servers/{qualified_name}",
                    headers=self._headers(),
                )
                if resp.status_code != 200:
                    logger.warning(
                        f"Smithery 서버 상세 조회 실패: {qualified_name} "
                        f"HTTP {resp.status_code}"
                    )
                    return None

                details = resp.json()
                tools = details.get("tools", [])
                logger.info(
                    f"Smithery 서버 상세: {qualified_name} — "
                    f"{len(tools)}개 도구"
                )
                return details

        except Exception as e:
            logger.warning(f"Smithery 서버 상세 조회 오류: {e}")
            return None

    # ── 3. 연결 생성/재사용 ────────────────────────────────

    async def get_or_create_connection(
        self,
        qualified_name: str,
        server_url: str = "",
        config: dict | None = None,
    ) -> str | None:
        """Smithery 매니지드 프록시 연결을 생성하거나 캐시에서 반환.

        Args:
            qualified_name: 서버 식별자
            server_url: MCP 서버 URL (배포된 서버의 URL)
            config: 추가 설정 (API 키 등)

        Returns:
            connectionId 문자열 또는 None
        """
        if not self.is_available:
            return None

        # 캐시에 있으면 재사용
        if qualified_name in self._connection_cache:
            cached = self._connection_cache[qualified_name]
            cached_id = cached["connectionId"]
            namespace = cached["namespace"]
            # 유효성 검증: tools/list 호출로 확인
            test = await self._raw_call(namespace, cached_id, "tools/list", {})
            if test is not None:
                logger.info(
                    f"Smithery 연결 캐시 히트: {qualified_name} → {cached_id}"
                )
                return cached_id
            else:
                # 만료된 연결 제거
                del self._connection_cache[qualified_name]
                logger.info(f"Smithery 연결 캐시 만료: {qualified_name}")

        # 새 연결 생성 — POST /connect/{namespace}
        # namespace = qualifiedName (예: "@anthropic/weather-server")
        try:
            if not server_url:
                logger.warning(
                    f"Smithery 연결 생성 실패: mcpUrl(server_url) 필수 — {qualified_name}"
                )
                return None

            payload: dict[str, Any] = {"mcpUrl": server_url}
            if config:
                payload["config"] = config

            import urllib.parse
            # namespace = qualifiedName을 URL-safe하게 인코딩
            namespace = qualified_name
            encoded_namespace = urllib.parse.quote(namespace, safe="")

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self.BASE_URL}/connect/{encoded_namespace}",
                    headers=self._headers(),
                    json=payload,
                )
                if resp.status_code not in (200, 201):
                    logger.warning(
                        f"Smithery 연결 생성 실패: {qualified_name} "
                        f"HTTP {resp.status_code} — {resp.text[:300]}"
                    )
                    return None

                data = resp.json()
                conn_id = data.get("connectionId") or data.get("id", "")
                if conn_id:
                    self._connection_cache[qualified_name] = {
                        "connectionId": conn_id,
                        "namespace": namespace,
                    }
                    logger.info(
                        f"Smithery 연결 생성 완료: {qualified_name} → {conn_id}"
                    )
                    return conn_id

                logger.warning(f"Smithery 연결 응답에 connectionId 없음: {data}")
                return None

        except Exception as e:
            logger.warning(f"Smithery 연결 생성 오류: {e}")
            return None

    # ── 4. 도구 목록 조회 ──────────────────────────────────

    def _get_namespace_for_connection(self, connection_id: str) -> str | None:
        """캐시에서 connectionId에 해당하는 namespace를 찾는다."""
        for _qn, cached in self._connection_cache.items():
            if cached["connectionId"] == connection_id:
                return cached["namespace"]
        return None

    async def list_tools(self, connection_id: str) -> list[dict]:
        """Smithery 프록시를 통해 MCP tools/list 호출.

        Args:
            connection_id: Smithery 연결 ID

        Returns:
            도구 목록 (비어있을 수 있음)
        """
        namespace = self._get_namespace_for_connection(connection_id)
        if not namespace:
            logger.warning(f"Smithery namespace를 찾을 수 없음: {connection_id}")
            return []
        result = await self._raw_call(namespace, connection_id, "tools/list", {})
        if result is None:
            return []
        return result.get("tools", [])

    # ── 5. 도구 실행 ───────────────────────────────────────

    async def call_tool(
        self,
        connection_id: str,
        tool_name: str,
        arguments: dict,
    ) -> dict:
        """Smithery 프록시를 통해 MCP tools/call 호출.

        Args:
            connection_id: Smithery 연결 ID
            tool_name: 호출할 도구 이름
            arguments: 도구 인자

        Returns:
            MCP 도구 실행 결과
        """
        namespace = self._get_namespace_for_connection(connection_id)
        if not namespace:
            return {"error": f"Smithery namespace를 찾을 수 없음: {connection_id}"}

        result = await self._raw_call(
            namespace,
            connection_id,
            "tools/call",
            {"name": tool_name, "arguments": arguments},
        )
        if result is None:
            return {"error": "Smithery 프록시 호출 실패"}

        # MCP 표준 응답: result.content[].text 추출
        content_list = result.get("content", [])
        texts = [c.get("text", "") for c in content_list if c.get("type") == "text"]
        return {
            "result": "\n".join(texts) if texts else str(result),
            "raw": result,
        }

    # ── 내부: JSON-RPC 호출 ────────────────────────────────

    async def _raw_call(
        self,
        namespace: str,
        connection_id: str,
        method: str,
        params: dict,
    ) -> dict | None:
        """Smithery 프록시로 JSON-RPC 요청 전송.

        엔드포인트: POST /connect/{namespace}/{connectionId}/mcp
        """
        import urllib.parse

        encoded_ns = urllib.parse.quote(namespace, safe="")
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{self.BASE_URL}/connect/{encoded_ns}/{connection_id}/mcp",
                    headers=self._headers(),
                    json={
                        "jsonrpc": "2.0",
                        "method": method,
                        "params": params,
                        "id": 1,
                    },
                )
                if resp.status_code not in (200, 202):
                    logger.warning(
                        f"Smithery RPC 실패: {method} "
                        f"HTTP {resp.status_code}"
                    )
                    return None

                if resp.status_code == 202:
                    # Notification accepted, no body
                    return {}

                data = resp.json()
                if "error" in data:
                    logger.warning(f"Smithery RPC 오류: {data['error']}")
                    return None

                return data.get("result", {})

        except Exception as e:
            logger.warning(f"Smithery RPC 예외: {e}")
            return None


# ── 싱글톤 ───────────────────────────────────────────────

_smithery_client: Optional[SmitheryClient] = None


def get_smithery_client() -> SmitheryClient:
    """SmitheryClient 싱글톤 반환."""
    global _smithery_client
    if _smithery_client is None:
        _smithery_client = SmitheryClient()
    return _smithery_client
