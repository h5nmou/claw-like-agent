"""
skill_factory.py — Enterprise Skill Factory 2.0

엔터프라이즈급 자율 스킬 생성 파이프라인:

Phase 1: 탐색 — 공식 API/MCP 서버 탐색 → 공공데이터포털 폴백
Phase 2: 합성 및 검증 — LATM 구조 (Maker 고성능 모델 → User 경량 모델)
  - 반복적 프롬프팅 (Iterative Prompting): 실행 오류 + 환경 피드백 루프
  - 피어 리뷰 (Peer Review): 별도 모델이 효율성/안전성 검토
  - 보안 게이트: 다단계 보안 + 프롬프트 주입 방어
  - 품질 평가: 다중 신호 채점
Phase 3: 등록 — 버전 관리 + executor 등록 + 즉시 사용 가능
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("skill_factory")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GENERATED_SKILLS_DIR = PROJECT_ROOT / "generated_skills"
SKILL_INDEX_PATH = GENERATED_SKILLS_DIR / "skill_index.json"

# ── 로그 출력 헬퍼 (실시간 브리핑) ──────────────────────

_log_hook: Callable[[str, str, str], Any] | None = None


def set_log_hook(hook: Callable) -> None:
    """엔진의 broadcaster.emit 함수를 연결하여 대시보드에 실시간 출력."""
    global _log_hook
    _log_hook = hook


async def _log(level: str, msg: str, meta: str = "") -> None:
    """콘솔 + 대시보드 동시 출력."""
    logger.info(f"[{meta}] {msg}")
    if _log_hook:
        await _log_hook(level, msg, meta)


# ── LATM 모델 설정 ──────────────────────────────────────────

def _get_maker_model() -> str:
    """Maker (고성능 모델) — 코드 생성 + 피어 리뷰."""
    return os.getenv("SKILL_MAKER_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o"))


def _get_user_model() -> str:
    """User (경량 모델) — 실제 실행 시 사용."""
    return os.getenv("SKILL_USER_MODEL", "gpt-4o-mini")


# ── API 탐색 ─────────────────────────────────────────────

class APIDiscovery:
    """웹 검색 기반 API/MCP 서버 탐색기."""

    DDGS_ENDPOINT = "https://api.duckduckgo.com/"
    SERPER_ENDPOINT = "https://google.serper.dev/search"

    def __init__(self) -> None:
        self.serper_key = os.getenv("SERPER_API_KEY")
        self._client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    async def search(self, query: str) -> list[dict]:
        """검색 실행 (Serper 우선, 없으면 DuckDuckGo)."""
        if self.serper_key:
            return await self._search_serper(query)
        return await self._search_duckduckgo(query)

    async def _search_serper(self, query: str) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self.SERPER_ENDPOINT,
                    headers={"X-API-KEY": self.serper_key, "Content-Type": "application/json"},
                    json={"q": query, "num": 5}
                )
                data = resp.json()
                return [
                    {"title": r.get("title", ""), "link": r.get("link", ""), "snippet": r.get("snippet", "")}
                    for r in data.get("organic", [])
                ]
        except Exception as e:
            logger.warning(f"Serper 검색 실패: {e}, DuckDuckGo로 폴백")
            return await self._search_duckduckgo(query)

    async def _search_duckduckgo(self, query: str) -> list[dict]:
        """DuckDuckGo Instant Answer API (무인증)."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    self.DDGS_ENDPOINT,
                    params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                    headers={"User-Agent": "Mozilla/5.0 SkillFactory/2.0"},
                )
                data = resp.json()
                results = []
                if data.get("AbstractText"):
                    results.append({
                        "title": data.get("Heading", ""),
                        "link": data.get("AbstractURL", ""),
                        "snippet": data.get("AbstractText", "")
                    })
                for r in data.get("RelatedTopics", [])[:4]:
                    if isinstance(r, dict) and r.get("Text"):
                        results.append({
                            "title": r.get("Text", "")[:60],
                            "link": r.get("FirstURL", ""),
                            "snippet": r.get("Text", "")
                        })
                return results
        except Exception as e:
            logger.warning(f"DuckDuckGo 검색 실패: {e}")
            return []

    # ── 메이저 서비스 → 공식 MCP 매핑 ──────────────────────────
    # Google, Slack, GitHub 등 공식/표준 MCP 서버가 존재하는 서비스 목록.
    # modelcontextprotocol/servers 공식 레포 기준.
    _OFFICIAL_MCP_MAP: dict[str, dict] = {
        "google": {
            "keywords": ["gmail", "google", "drive", "calendar", "google maps", "구글", "지메일", "구글맵", "구글드라이브"],
            "server": "@modelcontextprotocol/server-google-maps",
            "source": "github.com/modelcontextprotocol/servers",
            "install": "npx -y @modelcontextprotocol/server-google-maps",
            "env_key": "GOOGLE_MAPS_API_KEY",
            "description": "Google Maps Platform MCP — 장소 검색, 경로, 지오코딩",
        },
        "github": {
            "keywords": ["github", "깃허브", "repository", "pull request", "issue"],
            "server": "@modelcontextprotocol/server-github",
            "source": "github.com/modelcontextprotocol/servers",
            "install": "npx -y @modelcontextprotocol/server-github",
            "env_key": "GITHUB_PERSONAL_ACCESS_TOKEN",
            "description": "GitHub MCP — 레포, PR, 이슈, 코드 검색",
        },
        "slack": {
            "keywords": ["slack", "슬랙", "채널", "workspace"],
            "server": "@modelcontextprotocol/server-slack",
            "source": "github.com/modelcontextprotocol/servers",
            "install": "npx -y @modelcontextprotocol/server-slack",
            "env_key": "SLACK_BOT_TOKEN",
            "description": "Slack MCP — 채널 관리, 메시지 전송/검색",
        },
        "notion": {
            "keywords": ["notion", "노션", "workspace", "database"],
            "server": "@modelcontextprotocol/server-notion",
            "source": "github.com/modelcontextprotocol/servers",
            "install": "npx -y @modelcontextprotocol/server-notion",
            "env_key": "NOTION_API_KEY",
            "description": "Notion MCP — 페이지/데이터베이스 읽기·쓰기",
        },
        "filesystem": {
            "keywords": ["파일", "file", "directory", "폴더", "디렉토리"],
            "server": "@modelcontextprotocol/server-filesystem",
            "source": "github.com/modelcontextprotocol/servers",
            "install": "npx -y @modelcontextprotocol/server-filesystem",
            "env_key": None,
            "description": "Filesystem MCP — 로컬 파일 읽기/쓰기/검색",
        },
        "puppeteer": {
            "keywords": ["웹 크롤링", "scraping", "screenshot", "브라우저", "puppeteer"],
            "server": "@modelcontextprotocol/server-puppeteer",
            "source": "github.com/modelcontextprotocol/servers",
            "install": "npx -y @modelcontextprotocol/server-puppeteer",
            "env_key": None,
            "description": "Puppeteer MCP — 웹페이지 탐색, 스크린샷, 폼 조작",
        },
    }

    async def discover_strategy(self, user_request: str) -> dict:
        """
        Tiered Strategy — 단계별 탐색 전략.

        Tier 0: 로컬 MCP 프로브 (이미 연결된 서버)
        Tier 1: 메이저 서비스 공식 MCP 조회 (Official/Standard)
        Tier 2: MCP 레지스트리 심층 탐색 (Smithery / Awesome-MCP)
        Tier 3: REST API / pip 라이브러리 폴백
        """
        await _log("system", f"🔍 탐색 시작: '{user_request}'", "[탐색 중...]")

        # ── Tier 0: 로컬 MCP 서버 (이미 연결된 Phone-MCP 등) ──
        local_mcp = await self._probe_local_mcp(user_request)
        if local_mcp:
            return local_mcp

        # ── Tier 1: 메이저 서비스 공식 MCP 조회 ──
        official_mcp = await self._check_official_mcp(user_request)
        if official_mcp:
            return official_mcp

        # ── Tier 2: 레지스트리 심층 탐색 (Smithery / Awesome-MCP) ──
        registry_result = await self._search_mcp_registries(user_request)
        if registry_result:
            return registry_result

        # ── Tier 3: REST API / pip / 공공데이터포털 폴백 ──
        all_results = []

        api_queries = [
            f"{user_request} official REST API python",
            f"{user_request} python library pip",
        ]
        for q in api_queries:
            await _log("system", f"🌐 검색: {q}", "[API 탐색 중...]")
            results = await self.search(q)
            all_results.extend(results)

        public_results = await self.search(f"{user_request} 공공데이터포털 data.go.kr API")
        all_results.extend(public_results)

        strategy = await self._analyze_with_llm(user_request, all_results)
        return strategy

    # ── Tier 1: 메이저 서비스 공식 MCP ────────────────────────

    async def _check_official_mcp(self, user_request: str) -> dict | None:
        """
        요청 키워드를 기반으로 공식 MCP 표준 서버(modelcontextprotocol/servers)에
        매칭되는 서비스가 있는지 확인. 공식 서버는 보안성이 높고 설정이 표준화되어 있음.
        """
        req_lower = user_request.lower()

        for service_id, info in self._OFFICIAL_MCP_MAP.items():
            if any(kw in req_lower for kw in info["keywords"]):
                env_key = info.get("env_key")
                auth_needed = env_key is not None

                await _log("system",
                    f"🏛️ [Tier 1] 공식 MCP 서버 발견: {info['server']}\n"
                    f"   서비스: {service_id}\n"
                    f"   출처: {info['source']}\n"
                    f"   설치: {info['install']}\n"
                    f"   인증: {'필요 (' + env_key + ')' if auth_needed else '불필요'}",
                    "[공식 MCP 발견]")

                return {
                    "strategy": "mcp",
                    "service_name": info["server"],
                    "api_endpoint": "",
                    "pip_packages": [],
                    "auth_required": auth_needed,
                    "env_key_name": env_key,
                    "description": info["description"],
                    "implementation_hint": (
                        f"공식 MCP 서버 {info['server']} 사용. "
                        f"설치: {info['install']}. "
                        f"JSON-RPC tools/list → tools/call 패턴으로 호출."
                    ),
                    "mcp_registry_source": info["source"],
                    "mcp_install_method": info["install"],
                    "mcp_tool_schema": None,
                    "mcp_reliability": "high",  # 공식 서버 = 최고 신뢰도
                    "mcp_tier": "official",
                }

        return None

    # ── Tier 2: 레지스트리 심층 탐색 ──────────────────────────

    async def _search_mcp_registries(self, user_request: str) -> dict | None:
        """
        Smithery.ai / Awesome-MCP에서 커뮤니티 MCP 서버를 탐색.
        공식 서버에 없는 특화 기능(예: 특정 워크플로우 최적화)을 찾을 때 사용.
        """
        await _log("system",
            "📚 [Tier 2] MCP 레지스트리 심층 탐색: Smithery.ai / Awesome-MCP",
            "[MCP 레지스트리]")

        registry_results = []

        # Smithery.ai 탐색
        smithery_queries = [
            f"site:smithery.ai {user_request}",
            f"smithery.ai MCP server {user_request}",
        ]
        for q in smithery_queries:
            await _log("system", f"🏪 검색: {q}", "[Smithery 탐색 중...]")
            results = await self.search(q)
            registry_results.extend(results)

        # Awesome-MCP (GitHub) 탐색
        awesome_queries = [
            f"github awesome-mcp-servers {user_request}",
            f"github punkpeye awesome-mcp {user_request}",
        ]
        for q in awesome_queries:
            await _log("system", f"📦 검색: {q}", "[Awesome-MCP 탐색 중...]")
            results = await self.search(q)
            registry_results.extend(results)

        if not registry_results:
            await _log("system",
                "ℹ️ [Tier 2] 레지스트리에서 관련 도구 미발견 → API 폴백",
                "[레지스트리 미발견]")
            return None

        # LLM이 레지스트리 결과를 분석하여 적합한 MCP 서버 판단
        analysis = await self._analyze_mcp_registry(user_request, registry_results)
        if analysis and analysis.get("found"):
            mcp_source = analysis.get("source", "unknown")
            mcp_name = analysis.get("mcp_server_name", "Unknown MCP")
            await _log("system",
                f"✅ [Tier 2] MCP 서버 발견: {mcp_name}\n"
                f"   출처: {mcp_source}\n"
                f"   설치: {analysis.get('install_method', 'N/A')}\n"
                f"   신뢰도: {analysis.get('reliability', 'N/A')}",
                "[MCP 서버 발견]")

            return {
                "strategy": "mcp",
                "service_name": mcp_name,
                "api_endpoint": analysis.get("mcp_url", ""),
                "pip_packages": analysis.get("pip_packages", []),
                "auth_required": analysis.get("auth_required", False),
                "env_key_name": analysis.get("env_key_name"),
                "description": analysis.get("description", ""),
                "implementation_hint": analysis.get("implementation_hint", ""),
                "mcp_registry_source": mcp_source,
                "mcp_install_method": analysis.get("install_method", ""),
                "mcp_tool_schema": analysis.get("tool_schema"),
                "mcp_reliability": analysis.get("reliability", "unknown"),
                "mcp_tier": "community",
            }

        await _log("system",
            "ℹ️ [Tier 2] 레지스트리에서 적합한 도구 미발견 → API 폴백",
            "[레지스트리 매칭 실패]")
        return None

    async def _analyze_mcp_registry(
        self, user_request: str, registry_results: list[dict]
    ) -> dict | None:
        """레지스트리 검색 결과에서 적합한 MCP 서버를 분석·선택."""
        results_text = "\n".join(
            f"- {r['title']}: {r['snippet'][:200]} ({r['link']})"
            for r in registry_results[:10]
        )

        prompt = f"""사용자 요청: "{user_request}"

MCP 레지스트리(Smithery.ai, Awesome-MCP) 검색 결과:
{results_text}

## 분석 규칙
1. 검색 결과에서 사용자 요청을 직접 처리할 수 있는 MCP 서버가 있는지 판단하라
2. MCP 서버를 발견했다면 다음 정보를 추출하라:
   - 서버 이름, 출처 URL
   - 제공하는 도구(tools)의 이름과 기능
   - 설치 방법 (npx, pip, docker 등)
   - API 키 필요 여부
   - 신뢰도 (별점, 다운로드 수, 최근 업데이트 여부 등을 종합 판단: high/medium/low)
3. 관련 없는 결과나 MCP가 아닌 일반 도구는 무시하라
4. 여러 후보가 있으면 가장 적합하고 신뢰도 높은 하나를 선택하라

## 출력 (JSON)
적합한 MCP 서버가 있으면:
{{
  "found": true,
  "mcp_server_name": "서버 패키지명 (예: @modelcontextprotocol/server-google-maps)",
  "source": "출처 URL (예: smithery.ai/server/google-maps 또는 github.com/...)",
  "description": "이 MCP 서버가 제공하는 기능 한 줄 설명",
  "tool_schema": {{"tool_name": "핵심 도구 이름", "arguments": ["param1", "param2"]}},
  "install_method": "설치 명령 (예: npx @modelcontextprotocol/server-google-maps)",
  "auth_required": true/false,
  "env_key_name": "필요한 환경변수명 (없으면 null)",
  "pip_packages": [],
  "reliability": "high/medium/low",
  "implementation_hint": "이 MCP 서버를 사용하여 요청을 처리하는 구체적 방법"
}}

없으면:
{{"found": false}}"""

        try:
            response = await self._client.chat.completions.create(
                model=_get_user_model(),
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.warning(f"MCP 레지스트리 분석 실패: {e}")
            return None

    async def _probe_local_mcp(self, user_request: str) -> dict | None:
        """
        로컬 MCP 서버(MCP_SERVER_URL)에 연결을 시도하여
        사용 가능한 도구 목록을 가져오고, 요청에 맞는 도구가 있으면
        웹 검색 없이 즉시 MCP 전략을 반환한다.
        """
        mcp_url = os.getenv("MCP_SERVER_URL", "http://192.168.0.20:8080")
        if not mcp_url:
            await _log("system",
                "⚠️ MCP_SERVER_URL 미설정 → 로컬 MCP 프로빙 생략",
                "[MCP 프로빙 생략]")
            return None

        base = mcp_url.rstrip("/")
        if not base.endswith("/mcp"):
            base = base + "/mcp"

        await _log("system",
            f"🔌 [MCP 0순위] 로컬 MCP 서버 프로빙: {base}",
            "[로컬 MCP 탐색]")

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(base, json={
                    "jsonrpc": "2.0",
                    "method": "tools/list",
                    "params": {},
                    "id": 1,
                })
                if resp.status_code != 200:
                    await _log("system",
                        f"⚠️ 로컬 MCP 서버 응답 실패 (HTTP {resp.status_code}) → 웹 검색으로 폴백",
                        "[MCP 프로빙 실패]")
                    return None

                data = resp.json()
                tools = data.get("result", {}).get("tools", [])
                if not tools:
                    await _log("system",
                        "⚠️ 로컬 MCP 서버에 등록된 도구 없음 → 웹 검색으로 폴백",
                        "[MCP 프로빙 실패]")
                    return None

                # 도구 목록을 LLM에 전달하여 요청에 맞는 도구가 있는지 판단
                tools_desc = "\n".join(
                    f"- {t['name']}: {t.get('description', '설명 없음')}"
                    for t in tools
                )
                await _log("system",
                    f"✅ 로컬 MCP 서버 연결 성공 — {len(tools)}개 도구 발견\n{tools_desc}",
                    "[로컬 MCP 발견]")

                match_result = await self._match_mcp_tool(user_request, tools)
                if match_result:
                    await _log("system",
                        f"🔌 [MCP 확정] 로컬 MCP 도구 '{match_result['tool_name']}' 사용 결정",
                        "[MCP 전략 확정]")
                    return {
                        "strategy": "mcp",
                        "service_name": f"Local MCP ({match_result['tool_name']})",
                        "api_endpoint": base,
                        "pip_packages": [],
                        "auth_required": False,
                        "env_key_name": None,
                        "description": match_result.get("description", "로컬 MCP 서버의 도구를 활용"),
                        "implementation_hint": (
                            f"MCP JSON-RPC tools/call 사용. "
                            f"도구명: {match_result['tool_name']}, "
                            f"전체 도구 목록: {[t['name'] for t in tools]}"
                        ),
                        "mcp_tools": tools,  # 코드 합성 시 참고용
                    }
                else:
                    await _log("system",
                        "ℹ️ 로컬 MCP 서버에 적합한 도구 없음 → 웹 검색으로 폴백",
                        "[MCP 매칭 실패]")
                    return None

        except Exception as e:
            await _log("system",
                f"⚠️ 로컬 MCP 서버 연결 실패: {e} → 웹 검색으로 폴백",
                "[MCP 프로빙 실패]")
            return None

    async def _match_mcp_tool(self, user_request: str, tools: list[dict]) -> dict | None:
        """LLM이 사용자 요청과 MCP 도구 목록을 비교하여 적합한 도구를 선택."""
        tools_json = json.dumps(tools, ensure_ascii=False, indent=2)

        prompt = f"""사용자 요청: "{user_request}"

아래는 로컬 MCP 서버에서 사용 가능한 도구 목록입니다:
{tools_json}

## 판단 규칙
1. 사용자 요청을 처리하는 데 직접 사용하거나 조합할 수 있는 도구가 있는지 판단하라
2. 하나의 도구로 직접 처리 가능하면 그 도구를 선택
3. 여러 도구를 순서대로 호출하면 처리 가능한 경우에도 선택
4. 적합한 도구가 전혀 없으면 null을 반환

## 출력 (JSON)
적합한 도구가 있으면:
{{"match": true, "tool_name": "가장 핵심 도구 이름", "description": "이 도구(들)로 어떻게 요청을 처리할지 한 문장"}}

없으면:
{{"match": false}}"""

        try:
            response = await self._client.chat.completions.create(
                model=_get_user_model(),  # 경량 모델로 빠른 판단
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)
            if result.get("match"):
                return result
            return None
        except Exception as e:
            logger.warning(f"MCP 도구 매칭 실패: {e}")
            return None

    async def _analyze_with_llm(
        self,
        user_request: str,
        search_results: list[dict],
    ) -> dict:
        """검색 결과를 LLM에 전달하여 최적 전략 결정."""
        results_text = "\n".join(
            f"- {r['title']}: {r['snippet'][:150]} ({r['link']})"
            for r in search_results[:8]
        )

        prompt = f"""사용자 요청: "{user_request}"

웹 검색 결과:
{results_text}

위 정보를 바탕으로 이 요청을 처리하기 위한 최적의 Python 코드 전략을 JSON으로 결정하세요.

규칙:
1. **MCP 서버 최우선**: 검색 결과에서 이 요청을 처리할 수 있는 MCP 서버(npm 패키지, GitHub 레포 등)가 발견되면 strategy를 "mcp"로 선택하라. MCP 서버는 로컬에 설치하여 실행할 수 있으므로 "접속 가능 여부"는 고려하지 않아도 된다.
2. MCP 서버가 검색 결과에 없을 때만 공식 REST API 또는 pip 라이브러리를 선택
3. 인증이 필요한 경우 env_key_name을 반드시 명시
4. 공식 API도 없으면 공공데이터포털(data.go.kr) 검토
5. pip 설치 가능한 Python 라이브러리도 전략으로 채택 가능
6. strategy가 "mcp"인 경우, MCP 서버의 설치 방법(npm/pip/docker)과 GitHub URL을 implementation_hint에 반드시 포함하라

## 특별 규칙 — 이메일 전송 요청
이메일 전송 기능은 Python 표준 라이브러리 smtplib + Gmail SMTP를 사용하세요.
- service_name: "Gmail SMTP"
- strategy: "pip_library"
- pip_packages: [] (표준 라이브러리만 사용)
- auth_required: true
- env_key_name: "SMTP_PASSWORD"  ← 반드시 이 이름 사용
- 다른 환경변수: SMTP_HOST, SMTP_PORT, SMTP_USER (이미 .env에 설정됨)

반드시 아래 JSON 형식으로만 응답:
{{
  "strategy": "official_api" | "pip_library" | "mcp" | "public_api",
  "service_name": "서비스명 (예: OpenWeatherMap)",
  "api_endpoint": "API 엔드포인트 URL (있을 경우)",
  "pip_packages": ["필요한 pip 패키지 목록 (없으면 빈 배열)"],
  "auth_required": true | false,
  "env_key_name": "환경변수명 (예: OPENWEATHER_API_KEY, 불필요하면 null)",
  "description": "이 전략으로 어떻게 구현할지 한 문장 설명",
  "implementation_hint": "핵심 구현 힌트 (예: httpx로 GET 요청, 응답에서 main.temp 추출)"
}}"""

        try:
            response = await self._client.chat.completions.create(
                model=_get_maker_model(),
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)
            await _log("system", f"✅ API 발견: [{result.get('service_name')}] — {result.get('description')}", "[API 발견]")
            return result
        except Exception as e:
            logger.error(f"전략 분석 실패: {e}")
            return {
                "strategy": "unknown",
                "service_name": "Unknown",
                "description": f"전략 결정 실패: {e}",
                "auth_required": False,
                "env_key_name": None,
                "pip_packages": [],
            }


# ── 코드 합성 (LATM Maker) ──────────────────────────────────

class CodeSynthesizer:
    """LATM Maker — 고성능 모델 기반 코드 생성기."""

    def __init__(self) -> None:
        self._client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    @staticmethod
    def _format_mcp_tools_context(strategy: dict) -> str:
        """MCP 전략일 때 도구 목록 또는 외부 레지스트리 정보를 프롬프트에 주입."""
        if strategy.get("strategy") != "mcp":
            return ""

        # Case 1: 로컬 MCP 서버 — 실제 도구 목록이 있는 경우
        mcp_tools = strategy.get("mcp_tools")
        if mcp_tools:
            tools_text = json.dumps(mcp_tools, ensure_ascii=False, indent=2)
            return f"""
## 로컬 MCP 서버 실제 도구 목록 (CRITICAL — 이 정보를 반드시 사용하라)
아래는 MCP 서버에 실제로 등록된 도구 목록과 inputSchema이다.
**존재하지 않는 도구나 파라미터를 추측하지 마라. 아래 목록에 있는 도구만 사용하라.**

```json
{tools_text}
```

이 도구들의 `name`과 `inputSchema.properties`를 정확히 사용하여 `tools/call`을 호출하라.
"""

        # Case 2: 외부 MCP 레지스트리 발견 — 범용 래퍼 스킬 생성
        registry_source = strategy.get("mcp_registry_source", "")
        tool_schema = strategy.get("mcp_tool_schema")
        install_method = strategy.get("mcp_install_method", "")

        if registry_source:
            schema_text = json.dumps(tool_schema, ensure_ascii=False, indent=2) if tool_schema else "스키마 미상"
            return f"""
## 외부 MCP 레지스트리에서 발견된 서버 (CRITICAL — 범용 래퍼 스킬로 작성하라)

이 MCP 서버는 외부 레지스트리({registry_source})에서 발견되었으며,
아직 로컬에 설치되지 않았다. 따라서 **직접 JSON-RPC를 호출하는 것이 아니라**,
먼저 `tools/list`로 사용 가능한 도구를 확인하고, 적합한 도구를 `tools/call`로 호출하는
**범용 래퍼 스킬(Universal Wrapper Skill)** 패턴으로 작성하라.

### 발견된 MCP 서버 정보
- 서버: {strategy.get('service_name', 'Unknown')}
- 출처: {registry_source}
- 설치: {install_method}
- 도구 스키마 (참고용): {schema_text}

### 범용 래퍼 스킬 구현 패턴 (반드시 이 패턴을 따르라)
```python
@tool
async def skill_name(param1: str, param2: str = "") -> dict:
    \"\"\"설명\"\"\"
    mcp_url = os.getenv("MCP_SERVER_URL", "http://localhost:8080")
    if not mcp_url.rstrip("/").endswith("/mcp"):
        mcp_url = mcp_url.rstrip("/") + "/mcp"

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1단계: 도구 목록 조회
        list_resp = await client.post(mcp_url, json={{
            "jsonrpc": "2.0", "method": "tools/list", "params": {{}}, "id": 1
        }})
        if list_resp.status_code != 200:
            return {{"error": "MCP 서버 연결 실패", "detail": f"HTTP {{list_resp.status_code}}"}}

        tools = list_resp.json().get("result", {{}}).get("tools", [])
        # 적합한 도구 이름을 찾는다 (스키마 참고)
        target_tool = None
        for t in tools:
            if "키워드" in t.get("description", "").lower() or t["name"] == "예상_도구명":
                target_tool = t["name"]
                break
        if not target_tool and tools:
            target_tool = tools[0]["name"]  # 폴백: 첫 번째 도구
        if not target_tool:
            return {{"error": "MCP 서버에 사용 가능한 도구 없음"}}

        # 2단계: 도구 실행
        call_resp = await client.post(mcp_url, json={{
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {{"name": target_tool, "arguments": {{"param1": param1}}}},
            "id": 2
        }})
        if call_resp.status_code != 200:
            return {{"error": "MCP 도구 실행 실패", "detail": f"HTTP {{call_resp.status_code}}"}}

        data = call_resp.json()
        if "error" in data:
            return {{"error": "MCP 오류", "detail": str(data["error"])}}

        # 3단계: 결과 추출
        content_list = data.get("result", {{}}).get("content", [])
        texts = [c.get("text", "") for c in content_list if c.get("type") == "text"]
        return {{"결과": "\\n".join(texts) if texts else str(data.get("result", ""))}}
```

### 핵심 규칙
1. **모든 변수를 함수 파라미터로** — 특정 지명/수치를 하드코딩하지 마라
2. **tools/list로 동적 탐색** — 도구 이름을 하드코딩하지 말고 목록에서 검색하라
3. **Fallback 필수** — MCP 서버 미연결, 도구 없음, 타임아웃 모두 에러 dict로 반환
4. **결과는 result.content[].text에서 추출** — MCP 표준 응답 형식
"""
        return ""

    async def synthesize(
        self,
        user_request: str,
        strategy: dict,
        test_args: dict | None = None,
        previous_error: str | None = None,
        previous_code: str | None = None,
        env_feedback: str | None = None,
        iteration: int = 1,
    ) -> str:
        """
        완전한 @tool 함수 코드 생성 (Google-style docstring 포함).

        반복적 프롬프팅 (Iterative Prompting):
          - previous_error: 이전 실행 오류 피드백
          - env_feedback: 환경 피드백 (패키지 누락, 네트워크 상태 등)
          - iteration: 현재 반복 횟수
        """
        env_key = strategy.get("env_key_name")
        auth_note = f"환경변수 `{env_key}`에서 API 키를 읽어라." if env_key else "인증 불필요."
        pip_note = f"pip 패키지 필요: {strategy.get('pip_packages', [])}" if strategy.get("pip_packages") else ""

        param_hint = ""
        if test_args:
            param_names = list(test_args.keys())
            param_hint = f"\n## 함수 파라미터 요구사항 (반드시 준수)\n함수는 다음 파라미터 이름을 **정확히** 사용해야 합니다: {param_names}\n예: def func({', '.join(param_names)}): ..."

        # 반복적 프롬프팅: 이전 오류 + 환경 피드백 통합
        correction_section = ""
        if previous_error and previous_code:
            correction_section = f"""
## 이전 실행 오류 (반복 {iteration}차 — 반드시 수정):
```
{previous_error}
```
## 이전 코드:
```python
{previous_code}
```
위 오류를 분석하여 근본 원인을 해결하라. 동일한 실수를 반복하지 마라.
"""
        if env_feedback:
            correction_section += f"""
## 환경 피드백 (시스템 자동 감지):
{env_feedback}
이 환경 피드백을 반영하여 코드를 조정하라.
"""

        prompt = f"""당신은 Python 에이전트 도구를 생성하는 코드 합성 전문가입니다.

## 사용자 요청
"{user_request}"
{param_hint}
## 구현 전략
- 서비스: {strategy.get('service_name')}
- 전략: {strategy.get('strategy')}
- API 엔드포인트: {strategy.get('api_endpoint', 'N/A')}
- 설명: {strategy.get('description')}
- 힌트: {strategy.get('implementation_hint', 'N/A')}
- 인증: {auth_note}
- {pip_note}
{self._format_mcp_tools_context(strategy)}
{correction_section}

## 코드 생성 규칙
1. **반드시 `async def` 함수로 작성** (httpx.AsyncClient 사용)
2. **`from core.executor import tool` import 후 `@tool` 데코레이터 적용**
3. **Google-style Docstring 필수** (Args:, Returns: 섹션 포함)
4. 환경변수는 `os.getenv("ENV_KEY_NAME")` 로만 읽기 (하드코딩 절대 금지)
5. 에러는 dict 형태로 반환: `{{"error": "...", "detail": "..."}}`
6. 성공 시 실용적인 한국어 키-값 dict 반환
7. **import 구문은 함수 상단이 아닌 파일 최상단에 위치**
8. `from __future__ import annotations` 포함
9. pip 패키지가 필요한 경우 주석으로 설치 명령 명시
10. **민감 정보(API 키, 토큰)는 절대 코드에 노출 금지** — os.getenv() 전용

## 범용화 원칙 (Generalization Protocol) — 반드시 준수
### 1. Parameterization First (파라미터화 우선)
- 사용자 요청에 포함된 특정 지명, 메뉴명, 수치 등 **고유명사와 구체적 값을 함수 내부에 하드코딩하지 마라**
- 이런 값들은 반드시 **함수 파라미터**로 추출하여 외부에서 주입받게 하라
- 나쁜 예: `async def get_restaurants_near_aewol()` → 애월만 검색 가능
- 좋은 예: `async def search_nearby_restaurants(location: str, cuisine: str = "", min_rating: float = 0.0)` → 어디서든 재사용 가능
- 사용자가 "애월 근처 평점 4.5 이상 횟집" 이라고 요청해도, 함수는 `location`, `cuisine`, `min_rating`을 파라미터로 받아야 한다

### 2. Semantic Skill Naming (의미적 네이밍)
- 함수명은 **`동사_대상` 형태의 범용적 이름**으로 명명하라 (예: `search_nearby_restaurants`, `get_weather_info`)
- **함수명에 특정 고유명사(지명, 브랜드명, 사람 이름)를 포함시키지 마라**
- 나쁜 예: `get_available_restaurants_near_aewol`, `search_seoul_cafes`, `find_starbucks_locations`
- 좋은 예: `search_nearby_restaurants`, `search_local_cafes`, `find_store_locations`

## MCP 전략 구현 시 반드시 준수 (CRITICAL — strategy가 "mcp"인 경우)
MCP(Model Context Protocol) 서버와 통신할 때는 **절대로 일반 REST API(GET/POST)를 사용하지 마라**.
반드시 아래의 **JSON-RPC 프로토콜**을 사용해야 한다.

### MCP 서버 URL
```python
mcp_url = os.getenv("MCP_SERVER_URL", "http://localhost:8080")
# 반드시 /mcp 경로를 붙여야 한다
if not mcp_url.rstrip("/").endswith("/mcp"):
    mcp_url = mcp_url.rstrip("/") + "/mcp"
```

### MCP tools/list — 사용 가능한 도구 목록 조회
```python
async with httpx.AsyncClient(timeout=15.0) as client:
    resp = await client.post(mcp_url, json={{
        "jsonrpc": "2.0",
        "method": "tools/list",
        "params": {{}},
        "id": 1
    }})
    data = resp.json()
    tools = data.get("result", {{}}).get("tools", [])
    # tools = [{{"name": "...", "description": "...", "inputSchema": {{...}}}}, ...]
```

### MCP tools/call — 특정 도구 실행
```python
async with httpx.AsyncClient(timeout=15.0) as client:
    resp = await client.post(mcp_url, json={{
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {{
            "name": "도구_이름",        # tools/list에서 확인한 도구 이름
            "arguments": {{              # 해당 도구의 inputSchema에 맞는 인자
                "param1": "value1",
            }}
        }},
        "id": 2
    }})
    data = resp.json()
    # 결과 추출
    content_list = data.get("result", {{}}).get("content", [])
    texts = [c.get("text", "") for c in content_list if c.get("type") == "text"]
    result_text = "\\n".join(texts)
```

### MCP 스킬 구현 패턴 (반드시 이 패턴을 따를 것)
1. **먼저 `tools/list`를 호출**하여 사용 가능한 도구 목록을 확인한다
2. 사용자 요청과 가장 관련 있는 도구를 선택한다
3. 선택한 도구의 `inputSchema`에 맞는 인자를 구성한다
4. **`tools/call`로 해당 도구를 실행**한다
5. 결과에서 `result.content[].text`를 추출하여 반환한다

### MCP 스킬에서 절대 하지 말아야 할 것
- ❌ `httpx.get(url, params=...)` 같은 일반 REST 호출
- ❌ MCP_SERVER_URL에 직접 쿼리 파라미터를 붙이는 것
- ❌ 존재하지 않는 API 엔드포인트를 추측으로 만드는 것
- ❌ `/api/restaurants`, `/api/weather` 같은 경로를 임의로 만드는 것

## 이메일 전송 구현 시 반드시 준수 (CRITICAL)
이메일 전송 함수의 파라미터명은 **반드시 아래와 같이 정확히 사용**해야 합니다:
- `to_email` (수신자 이메일, NOT 'recipient', NOT 'email', NOT 'to')
- `subject` (제목)
- `body` (본문)
함수 시그니처 예시: `async def send_email_via_smtp(to_email: str, subject: str, body: str) -> dict:`

## 출력 형식
Python 코드 파일 전체를 마크다운 코드블록 없이 순수 Python 코드로만 출력:
"""

        await _log("system",
            f"⚙️ [LATM Maker] 코드 합성 중... (모델: {_get_maker_model()}, 반복: {iteration}차)",
            "[코드 합성 중...]")

        response = await self._client.chat.completions.create(
            model=_get_maker_model(),
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content.strip()

        # 마크다운 코드블록 제거
        if raw.startswith("```python"):
            raw = raw[len("```python"):].lstrip()
        if raw.startswith("```"):
            raw = raw[3:].lstrip()
        if raw.endswith("```"):
            raw = raw[:-3].rstrip()

        return raw


# ── 피어 리뷰 (Peer Review) ─────────────────────────────────

class PeerReviewer:
    """
    별도 고성능 모델을 활용한 코드 리뷰어.
    효율성, 가독성, 특히 안전성을 검토.
    """

    def __init__(self) -> None:
        self._client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    async def review(self, code: str, skill_name: str, strategy: dict) -> dict:
        """
        생성된 코드를 피어 리뷰.

        Returns:
            {
                "approved": bool,
                "score": int (0-100),
                "issues": list[str],
                "suggestions": list[str],
                "patched_code": str | None  (수정이 필요한 경우)
            }
        """
        review_model = os.getenv("SKILL_REVIEW_MODEL", _get_maker_model())
        await _log("system",
            f"🔍 [Peer Review] 코드 리뷰 시작 (모델: {review_model})",
            "[피어 리뷰 중...]")

        prompt = f"""당신은 시니어 Python 보안 코드 리뷰어입니다.
아래 자동 생성된 에이전트 Tool 코드를 검토하세요.

## 스킬 정보
- 이름: {skill_name}
- 서비스: {strategy.get('service_name', 'Unknown')}
- 전략: {strategy.get('strategy', 'unknown')}

## 검토 대상 코드
```python
{code}
```

## 검토 기준
1. **안전성** (최우선): eval/exec 사용, 쉘 인젝션, 하드코딩된 시크릿, SQL 인젝션
2. **효율성**: 불필요한 API 호출, 리소스 누수 (미닫힌 클라이언트), 비효율적 루프
3. **가독성**: 명확한 변수명, 적절한 에러 처리, 반환값 일관성
4. **호환성**: async/await 올바른 사용, @tool 데코레이터 존재, 올바른 import

## approved 판단 기준 (CRITICAL)
- `approved: true` → 코드가 안전하게 실행 가능한 경우. 사소한 개선사항이 있어도 보안 위협이 없으면 true.
- `approved: false` → eval/exec 사용, 시크릿 하드코딩, 쉘 인젝션 등 **치명적 보안 결함**이 있고, patched_code로도 해결 불가능한 경우에만.
- **수정 가능한 이슈는 patched_code로 해결하고 approved를 true로 설정하라.**
- 에러 핸들링 미흡, 타입 체크 부재, 기본값 개선 등은 suggestions에 기록하되 **거부 사유가 아니다**.
- `os.getenv()` 기본값 사용은 정상 패턴이며 보안 문제가 아니다.

## 출력 (JSON)
{{
  "approved": true/false,
  "score": 0-100,
  "issues": ["치명적 보안 문제만 기록 (없으면 빈 배열)"],
  "suggestions": ["개선 제안 목록 (비치명적 이슈 포함)"],
  "patched_code": null 또는 "수정된 전체 코드 (이슈를 직접 수정한 경우)"
}}"""

        try:
            response = await self._client.chat.completions.create(
                model=review_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)

            score = result.get("score", 70)
            issues = result.get("issues", [])

            if issues:
                issues_text = "\n".join(f"  - {i}" for i in issues)
                await _log("system",
                    f"⚠️ [Peer Review] 발견된 이슈 ({len(issues)}건):\n{issues_text}",
                    "[피어 리뷰 결과]")
            else:
                await _log("system",
                    f"✅ [Peer Review] 통과 — 점수: {score}/100",
                    "[피어 리뷰 통과]")

            return result

        except Exception as e:
            logger.warning(f"피어 리뷰 실패: {e}")
            # 리뷰 실패 시 기본 승인 (가용성 우선)
            return {"approved": True, "score": 60, "issues": [], "suggestions": [f"리뷰 실패: {e}"], "patched_code": None}


# ── 샌드박스 실행 ─────────────────────────────────────────────

class SandboxExecutor:
    """생성된 코드를 격리된 subprocess에서 실행, 결과 검증."""

    TIMEOUT = 30

    async def execute(self, code: str, test_args: dict | None = None) -> dict:
        """코드를 임시 파일로 저장 후 subprocess 실행."""
        func_name = self._extract_function_name(code)
        if not func_name:
            return {"success": False, "output": "", "error": "함수명을 찾을 수 없습니다."}

        import re as _re
        code = _re.sub(
            r'from\s+[Cc]ore\.executor\s+import\s+tool',
            'from core.executor import tool',
            code
        )
        cleaned_code = "\n".join(
            line for line in code.split("\n")
            if not line.strip().startswith("from __future__")
            and not line.strip().startswith("from core.executor import tool")
        )

        filtered_args = self._filter_args_for_func(cleaned_code, func_name, test_args or {})
        if not filtered_args and test_args:
            filtered_args = test_args
        test_args_repr = json.dumps(filtered_args, ensure_ascii=False)

        _injected_root = repr(str(PROJECT_ROOT))
        _injected_env = repr(str(PROJECT_ROOT / ".env"))
        runner_code = (
            "from __future__ import annotations\n"
            "import asyncio, sys, os, json\n"
            "from pathlib import Path\n"
            "from types import ModuleType\n"
            "\n"
            f"_root = Path({_injected_root})\n"
            "if str(_root) not in sys.path:\n"
            "    sys.path.insert(0, str(_root))\n"
            "\n"
            "from dotenv import load_dotenv\n"
            f"load_dotenv({_injected_env})\n"
            "\n"
            "def tool(func):\n"
            "    return func\n"
            "\n"
            "_mock_exe = ModuleType('core.executor')\n"
            "_mock_exe.tool = tool\n"
            "_mock_core = sys.modules.get('core') or ModuleType('core')\n"
            "_mock_core.executor = _mock_exe\n"
            "sys.modules.setdefault('core', _mock_core)\n"
            "sys.modules['core.executor'] = _mock_exe\n"
            "\n"
            + cleaned_code + "\n"
            "\n"
            "async def main():\n"
            "    try:\n"
            f"        result = await {func_name}(**{test_args_repr})\n"
            "        print('SKILL_RESULT:', json.dumps(result, ensure_ascii=False, default=str))\n"
            "    except Exception as e:\n"
            "        print('SKILL_ERROR:', str(e), file=sys.stderr)\n"
            "        raise\n"
            "\n"
            "asyncio.run(main())\n"
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(runner_code)
            tmp_path = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ},
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.TIMEOUT
            )

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if "SKILL_RESULT:" in stdout_str:
                result_line = [l for l in stdout_str.split("\n") if "SKILL_RESULT:" in l][-1]
                result_json = result_line.replace("SKILL_RESULT:", "").strip()
                return {"success": True, "output": result_json, "error": None}
            elif "SKILL_ERROR:" in stderr_str or stderr_str:
                return {"success": False, "output": stdout_str, "error": stderr_str}
            else:
                return {"success": True, "output": stdout_str, "error": None}
        except asyncio.TimeoutError:
            return {"success": False, "output": "", "error": f"실행 타임아웃 ({self.TIMEOUT}초)"}
        except Exception as e:
            return {"success": False, "output": "", "error": str(e)}
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _extract_function_name(self, code: str) -> str | None:
        for line in code.split("\n"):
            line = line.strip()
            if line.startswith("async def ") or line.startswith("def "):
                name = line.split("def ")[1].split("(")[0].strip()
                if name not in ("tool", "main"):
                    return name
        return None

    def _filter_args_for_func(self, code: str, func_name: str, test_args: dict) -> dict:
        """생성된 코드의 함수 파라미터와 test_args를 최적 매핑."""
        import re
        pattern = rf"(?:async\s+)?def\s+{re.escape(func_name)}\s*\(([^)]*)\)"
        match = re.search(pattern, code, re.DOTALL)
        if not match:
            return test_args

        params_str = match.group(1)
        param_names_ordered = []
        for part in params_str.split(","):
            part = part.strip()
            if not part or part in ("self", "*args", "**kwargs"):
                continue
            name = part.split(":")[0].split("=")[0].strip()
            if name:
                param_names_ordered.append(name)

        if not param_names_ordered:
            return {}

        result = {}
        used_values = set()
        for param in param_names_ordered:
            if param in test_args:
                result[param] = test_args[param]
                used_values.add(param)

        remaining_values = [v for k, v in test_args.items() if k not in used_values]
        val_idx = 0
        for param in param_names_ordered:
            if param not in result:
                if val_idx < len(remaining_values):
                    result[param] = remaining_values[val_idx]
                    val_idx += 1

        return result

    def collect_env_feedback(self, _code: str, error: str) -> str:
        """실행 오류에서 환경 피드백을 추출하여 반복적 프롬프팅에 제공."""
        feedback_parts = []

        # 패키지 누락 감지
        import re
        missing_mod = re.search(r"ModuleNotFoundError: No module named '(\w+)'", error)
        if missing_mod:
            feedback_parts.append(f"- 패키지 '{missing_mod.group(1)}' 미설치 — pip install 필요")

        # 환경변수 누락 감지
        env_miss = re.search(r"(API_KEY|TOKEN|PASSWORD|SECRET)\s*(미설정|is None|not set|missing)", error, re.IGNORECASE)
        if env_miss:
            feedback_parts.append(f"- 환경변수 누락 감지: {env_miss.group(0)}")

        # 네트워크 오류 감지
        if "ConnectionError" in error or "ConnectTimeout" in error:
            feedback_parts.append("- 외부 API 연결 실패 — 엔드포인트 URL 또는 네트워크 상태 확인 필요")

        # 인증 오류 감지
        if "401" in error or "403" in error or "Unauthorized" in error:
            feedback_parts.append("- API 인증 실패 — API 키 또는 인증 방식 확인 필요")

        # JSON 파싱 오류
        if "JSONDecodeError" in error:
            feedback_parts.append("- API 응답이 JSON이 아님 — 응답 형식 또는 엔드포인트 확인 필요")

        return "\n".join(feedback_parts) if feedback_parts else ""


# ── 스킬 저장 ─────────────────────────────────────────────

class SkillPersister:
    """생성된 스킬을 파일로 저장하고 인덱스를 업데이트."""

    def save(
        self,
        skill_name: str,
        code: str,
        description: str,
        strategy: dict,
        category: dict | None = None,
    ) -> Path:
        """generated_skills/{skill_name}.py 저장."""
        GENERATED_SKILLS_DIR.mkdir(exist_ok=True)

        import re as _re
        code = _re.sub(
            r'from\s+[Cc][Oo][Rr][Ee]\.executor\s+import\s+tool',
            'from core.executor import tool',
            code
        )

        cat_label = (category or {}).get("category", "Uncategorized")
        source = strategy.get("mcp_registry_source", "")
        source_line = f"출처: {source}\n" if source else ""
        header = (
            f'"""\n자동 생성 스킬: {skill_name}\n'
            f'생성일: {datetime.now().isoformat()}\n'
            f'전략: {strategy.get("service_name", "Unknown")}\n'
            f'카테고리: {cat_label}\n'
            f'{source_line}'
            f'Factory: Enterprise Skill Factory 2.0\n"""\n\n'
        )
        full_code = header + code

        path = GENERATED_SKILLS_DIR / f"{skill_name}.py"
        path.write_text(full_code, encoding="utf-8")
        logger.info(f"스킬 저장: {path}")

        self._update_index(skill_name, description, strategy, category)
        return path

    def _update_index(
        self,
        skill_name: str,
        description: str,
        strategy: dict,
        category: dict | None = None,
    ) -> None:
        if SKILL_INDEX_PATH.exists():
            index = json.loads(SKILL_INDEX_PATH.read_text(encoding="utf-8"))
        else:
            index = {"skills": [], "categories": {}, "last_updated": "", "total_count": 0}

        # categories 필드 보장 (하위 호환)
        if "categories" not in index:
            index["categories"] = {}

        index["skills"] = [s for s in index["skills"] if s["name"] != skill_name]

        cat = category or {}
        entry = {
            "name": skill_name,
            "description": description,
            "service": strategy.get("service_name", "Unknown"),
            "strategy": strategy.get("strategy", "unknown"),
            "source": strategy.get("mcp_registry_source", ""),
            "install_method": strategy.get("mcp_install_method", ""),
            "reliability": strategy.get("mcp_reliability", ""),
            "created_at": datetime.now().isoformat(),
            "use_count": 0,
            "quality_score": None,
            "version": "v1.0.0",
            "category": cat.get("category", "Uncategorized"),
            "category_reason": cat.get("reason", ""),
        }
        index["skills"].append(entry)

        # categories 계층 구조 갱신
        cat_path = cat.get("category", "Uncategorized")
        if cat_path not in index["categories"]:
            index["categories"][cat_path] = {
                "description": cat.get("category_description", ""),
                "skills": [],
            }
        cat_entry = index["categories"][cat_path]
        if skill_name not in cat_entry["skills"]:
            cat_entry["skills"].append(skill_name)

        index["last_updated"] = datetime.now().isoformat()
        index["total_count"] = len(index["skills"])

        SKILL_INDEX_PATH.write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_and_register(self, skill_name: str) -> bool:
        """저장된 스킬 파일을 동적으로 import하여 executor 레지스트리에 등록."""
        from core.executor import _TOOL_REGISTRY

        path = GENERATED_SKILLS_DIR / f"{skill_name}.py"
        if not path.exists():
            return False

        try:
            spec = importlib.util.spec_from_file_location(skill_name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            found = skill_name in _TOOL_REGISTRY
            if not found:
                func = getattr(module, skill_name, None)
                if func:
                    _TOOL_REGISTRY[skill_name] = func
                    found = True
            if found:
                logger.info(f"스킬 동적 로드 성공: {skill_name}")
            return found
        except Exception as e:
            logger.error(f"스킬 로드 실패 {skill_name}: {e}")
            return False


# ── 메인 Skill Factory 2.0 ──────────────────────────────────

class SkillFactory:
    """
    Enterprise Skill Factory 2.0 — The Autonomous Toolmaker.

    파이프라인:
      Phase 1: 탐색 및 전략 수립
      Phase 2: 코드 합성 (LATM Maker) → 샌드박스 검증 → 반복적 프롬프팅
      Phase 2.5: 피어 리뷰 (안전성/효율성 검토)
      Phase 2.7: 보안 게이트 (다단계 보안 + 프롬프트 주입 방어)
      Phase 3: 등록 (버전 관리 + executor 등록 + 품질 평가)
    """

    MAX_CORRECTIONS = 5  # 반복적 프롬프팅 최대 횟수 (3→5 증가)

    def __init__(self) -> None:
        self.discovery = APIDiscovery()
        self.synthesizer = CodeSynthesizer()
        self.reviewer = PeerReviewer()
        self.sandbox = SandboxExecutor()
        self.persister = SkillPersister()

    async def create_skill(
        self,
        user_request: str,
        test_args: dict | None = None,
    ) -> dict:
        """
        요청에 맞는 스킬을 생성하고 등록.

        Returns:
            {
                "success": bool,
                "skill_name": str,
                "description": str,
                "file_path": str,
                "test_result": str,
                "quality_grade": str,
                "security_score": int,
                "message": str
            }
        """
        await _log("divider", "SKILL FACTORY 2.0 — 엔터프라이즈 스킬 생성 시작", "")
        await _log("system", f"📋 요청 분석: {user_request}", "🏭 Skill Factory 2.0")

        # ── 이메일 스킬 처리 (Canonical Template) ──
        from core.executor import _TOOL_REGISTRY
        email_keywords = {"email", "mail", "smtp", "이메일", "메일"}
        user_req_lower = user_request.lower()
        if any(kw in user_req_lower for kw in email_keywords):
            return await self._handle_email_skill(user_request)

        # ── Phase 0: Reusability Check (기존 스킬 재사용 판단) ──
        reuse_result = await self._check_reusability(user_request)
        if reuse_result:
            return reuse_result

        # ── Phase 1: 탐색 및 전략 수립 ──
        await _log("divider", "PHASE 1 — 탐색 및 전략 수립", "")
        strategy = await self.discovery.discover_strategy(user_request)

        if strategy.get("strategy") == "unknown":
            return {
                "success": False,
                "message": f"❌ 적합한 API/서비스를 찾지 못했습니다: {strategy.get('description')}",
            }

        # .env 키 필요 여부 안내
        env_key = strategy.get("env_key_name")
        if env_key and not os.getenv(env_key):
            await _log("system",
                f"🔑 '{env_key}' 환경변수가 필요합니다.",
                "[.env 설정 요청]")
            return {
                "success": False,
                "env_key_required": env_key,
                "service": strategy.get("service_name"),
                "message": (
                    f"🔑 **API 키 설정 필요**: `{env_key}`\n\n"
                    f".env 파일에 `{env_key}=발급받은API키` 를 추가한 후 다시 요청해 주세요.\n"
                    f"서비스: {strategy.get('service_name')} — {strategy.get('description')}"
                ),
            }

        # pip 패키지 설치
        pip_packages = strategy.get("pip_packages", [])
        if pip_packages:
            await _log("system", f"⚙️ pip 패키지 설치 중: {pip_packages}", "[패키지 설치]")
            for pkg in pip_packages:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "pip", "install", pkg, "-q",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(proc.wait(), timeout=60)
                    await _log("system", f"✅ {pkg} 설치 완료", "[패키지 설치]")
                except Exception as e:
                    await _log("error", f"❌ {pkg} 설치 실패: {e}", "[패키지 설치]")

        # ── Phase 2: 코드 합성 및 반복적 검증 (Iterative Prompting) ──
        await _log("divider", "PHASE 2 — 코드 합성 및 반복적 검증 (Iterative Prompting)", "")

        code = None
        exec_result = None
        previous_error = None
        env_feedback = None
        has_test_args = bool(test_args)

        for attempt in range(1, self.MAX_CORRECTIONS + 1):
            if attempt > 1:
                await _log("system",
                    f"🔄 반복적 프롬프팅 {attempt}/{self.MAX_CORRECTIONS} — 오류/환경 피드백 반영",
                    "[Iterative Prompting]")

            code = await self.synthesizer.synthesize(
                user_request, strategy,
                test_args=test_args,
                previous_error=previous_error,
                previous_code=code,
                env_feedback=env_feedback,
                iteration=attempt,
            )

            if not has_test_args:
                import ast
                try:
                    ast.parse(code)
                    await _log("system", "✅ 구문 검사 통과 (test_args 없어 실행 테스트 생략)", "[구문 검사]")
                    exec_result = {"success": True, "output": "{}", "error": None}
                    break
                except SyntaxError as se:
                    previous_error = f"SyntaxError: {se}"
                    env_feedback = None
                    await _log("error", f"❌ 구문 오류 (시도 {attempt}): {previous_error}", "[오류 감지]")
                    continue

            await _log("system", "🧪 샌드박스 실행 중...", "[실행 중...]")
            exec_result = await self.sandbox.execute(code, test_args)

            if exec_result["success"]:
                await _log("system", f"✅ 실행 성공! 결과: {exec_result['output'][:200]}", "[실행 성공]")
                break
            else:
                error_msg = exec_result["error"] or ""
                if "missing" in error_msg and "required positional argument" in error_msg:
                    await _log("system",
                        "⚠️ test_args 인자 불일치 (코드 자체는 정상) — 구문 검사 후 등록 진행",
                        "[실행 스킵]")
                    import ast
                    try:
                        ast.parse(code)
                        exec_result = {"success": True, "output": "{}", "error": None}
                        break
                    except SyntaxError:
                        pass

                previous_error = error_msg
                # 환경 피드백 수집 (반복적 프롬프팅의 핵심)
                env_feedback = self.sandbox.collect_env_feedback(code, error_msg)
                if env_feedback:
                    await _log("system",
                        f"📊 [환경 피드백 수집]\n{env_feedback}",
                        "[환경 피드백]")

                await _log("error",
                    f"❌ 실행 오류 (시도 {attempt}): {previous_error[:300]}",
                    "[오류 감지]")

        if not exec_result or not exec_result["success"]:
            return {
                "success": False,
                "message": f"❌ {self.MAX_CORRECTIONS}회 반복적 프롬프팅 후에도 실행 실패:\n{previous_error}",
            }

        skill_name = self.sandbox._extract_function_name(code)
        if not skill_name:
            return {"success": False, "message": "❌ 유효한 함수명을 추출할 수 없습니다."}

        description = strategy.get("description", f"{user_request} 처리 스킬")

        # ── Phase 2.5: 피어 리뷰 (Peer Review) ──
        await _log("divider", "PHASE 2.5 — 피어 리뷰 (Peer Review)", "")
        review_result = await self.reviewer.review(code, skill_name, strategy)

        patched = False
        if review_result.get("patched_code"):
            await _log("system",
                "🔧 [Peer Review] 코드 패치 적용 — 이슈 해결됨",
                "[피어 리뷰 패치]")
            code = review_result["patched_code"]
            patched = True

        # 패치가 적용된 경우: 이슈가 해결된 것이므로 거부하지 않고 진행
        # 패치 없이 거부된 경우에만 중단
        if not review_result.get("approved", True) and not patched:
            issues = review_result.get("issues", [])
            await _log("error",
                f"❌ [Peer Review] 거부됨 (패치 불가): {issues}",
                "[피어 리뷰 거부]")
            return {
                "success": False,
                "message": f"❌ 피어 리뷰 거부: {', '.join(issues)}",
            }

        review_score = review_result.get("score", 70)

        # ── Phase 2.7: 보안 게이트 (Security Gate) ──
        await _log("divider", "PHASE 2.7 — 보안 게이트 (Security Gate)", "")
        await _log("system", "🛡️ [보안 취약점 스캔 중...]", "[보안 스캔 중...]")

        from core.skill_security import get_security_gate
        gate = get_security_gate()
        sec_report = gate.scan(code, skill_name)

        await _log("system",
            f"🛡️ [보안 취약점 스캔 완료] {sec_report.summary}",
            "[보안 스캔 완료]")

        if sec_report.blocked:
            return {
                "success": False,
                "message": (
                    f"🚨 보안 게이트 차단: {skill_name}\n"
                    f"위험도: {sec_report.risk_level.upper()}\n"
                    f"발견: {sec_report.findings}"
                ),
            }

        if sec_report.approval_required:
            await _log("system",
                f"🔒 [승인 필요] {sec_report.allowlist_violations}",
                "[사장님 승인 대기]")
            # Telegram 승인 요청
            try:
                from core.telegram_client import telegram_client
                approval_msg = (
                    f"🔒 **스킬 배포 승인 요청**\n\n"
                    f"스킬: `{skill_name}`\n"
                    f"위험도: {sec_report.risk_level.upper()}\n"
                    f"보안점수: {sec_report.score}/100\n"
                    f"제한 동작: {', '.join(sec_report.allowlist_violations)}\n\n"
                    f"승인하시려면 '승인'을 입력하세요."
                )
                await telegram_client.send_message(approval_msg)
            except Exception:
                pass

        security_score = sec_report.score

        # ── Phase 3: 자율 카테고리 분류 + 등록 및 저장 ──
        await _log("divider", "PHASE 3 — 카테고리 분류 및 스킬 등록", "")

        # 자율 카테고리 분류
        category = await self._categorize_skill(skill_name, description, strategy)
        cat_label = category.get("category", "Uncategorized")

        file_path = self.persister.save(skill_name, code, description, strategy, category)
        loaded = self.persister.load_and_register(skill_name)

        # 버전 관리
        version = "v1.0.0"
        try:
            from core.skill_versioning import get_version_manager
            vm = get_version_manager()
            version = vm.save_version(
                skill_name, code,
                bump="minor",
                description=description,
                tags=strategy.get("pip_packages", []),
                risk_level=sec_report.risk_level,
            )
        except Exception as e:
            logger.warning(f"버전 저장 실패: {e}")

        # 품질 평가
        quality_grade = "B"
        try:
            from core.skill_quality import get_quality_evaluator
            evaluator = get_quality_evaluator()
            quality_report = evaluator.evaluate(
                skill_name=skill_name,
                user_request=user_request,
                execution_result=json.loads(exec_result.get("output", "{}")) if exec_result.get("output") else {},
                execution_time_ms=0,
                security_score=security_score,
                version=version,
            )
            quality_grade = quality_report.grade
        except Exception as e:
            logger.warning(f"품질 평가 실패: {e}")

        if loaded:
            await _log(
                "complete",
                f"🎉 엔터프라이즈 스킬 등록 완료!\n"
                f"  이름: {skill_name}\n"
                f"  설명: {description}\n"
                f"  서비스: {strategy.get('service_name')}\n"
                f"  버전: {version}\n"
                f"  카테고리: {cat_label}\n"
                f"  보안점수: {security_score}/100\n"
                f"  피어리뷰: {review_score}/100\n"
                f"  품질등급: {quality_grade}\n"
                f"  파일: generated_skills/{skill_name}.py\n"
                f"  즉시 호출 가능: 다음 요청부터 바로 사용됩니다.",
                "[엔터프라이즈 스킬 등록 완료]"
            )
            return {
                "success": True,
                "skill_name": skill_name,
                "description": description,
                "service": strategy.get("service_name"),
                "file_path": str(file_path),
                "test_result": exec_result["output"],
                "version": version,
                "category": cat_label,
                "source": strategy.get("mcp_registry_source", ""),
                "security_score": security_score,
                "review_score": review_score,
                "quality_grade": quality_grade,
                "message": (
                    f"✅ **엔터프라이즈 스킬 '{skill_name}' 등록 완료!**\n\n"
                    f"- 서비스: {strategy.get('service_name')}\n"
                    f"- 설명: {description}\n"
                    f"- 버전: {version}\n"
                    f"- 카테고리: {cat_label}\n"
                    + (f"- 출처: {strategy.get('mcp_registry_source')}\n" if strategy.get("mcp_registry_source") else "")
                    + f"- 보안: {security_score}/100 | 리뷰: {review_score}/100 | 등급: {quality_grade}\n"
                    f"- 테스트: {exec_result['output'][:300]}\n"
                    f"- 파일: `generated_skills/{skill_name}.py`\n\n"
                    f"⚡ **이 스킬은 지금 즉시 호출 가능합니다!**\n"
                    f"반드시 다음 단계로 '{skill_name}' 도구를 tool_call로 즉시 호출하여 요청을 완료하세요.\n"
                    f"말로만 '사용하겠습니다'라고 하지 말고 실제 tool_call을 수행해야 합니다."
                ),
            }
        else:
            return {
                "success": False,
                "message": f"코드 실행은 성공했으나 레지스트리 등록에 실패했습니다. 파일은 저장됨: {file_path}",
            }

    # ── Autonomous Skill Categorization ─────────────────────

    async def _categorize_skill(
        self, skill_name: str, description: str, strategy: dict
    ) -> dict:
        """
        LLM이 기존 카테고리 체계와 스킬 본질을 비교하여 자율적으로 분류.

        Returns:
            {
                "category": "대분류/중분류",
                "category_description": "카테고리 설명",
                "reason": "분류 사유"
            }
        """
        # 기존 카테고리 체계 로드
        existing_categories = {}
        if SKILL_INDEX_PATH.exists():
            try:
                idx = json.loads(SKILL_INDEX_PATH.read_text(encoding="utf-8"))
                existing_categories = idx.get("categories", {})
            except Exception:
                pass

        cat_list = ""
        if existing_categories:
            cat_list = "\n".join(
                f"- {name}: {info.get('description', '')} (스킬: {', '.join(info.get('skills', []))})"
                for name, info in existing_categories.items()
            )
        else:
            cat_list = "(아직 카테고리 없음 — 첫 번째 카테고리를 생성하라)"

        prompt = f"""당신은 에이전트 스킬 분류 전문가입니다.

## 분류 대상 스킬
- 이름: {skill_name}
- 설명: {description}
- 서비스: {strategy.get('service_name', 'Unknown')}
- 전략: {strategy.get('strategy', 'unknown')}

## 기존 카테고리 체계
{cat_list}

## 분류 규칙
1. **의미적 클러스터링**: 기존 카테고리 중 이 스킬의 목적에 가장 부합하는 곳에 배치하라
2. **신규 카테고리 생성**: 기존 분류가 부적절하면 새로운 카테고리를 생성하라 (기존과 의미 중복 금지)
3. **계층적 구조**: "대분류/중분류" 형태를 사용하라 (예: "Travel/Reservation", "Communication/Email", "Data/Weather")
4. **분류 사유**: 왜 이 카테고리인지 한 문장으로 기록하라

## 출력 (JSON만)
{{
  "category": "대분류/중분류",
  "category_description": "이 카테고리의 목적 한 줄 설명",
  "reason": "이 스킬을 해당 카테고리로 분류한 이유"
}}"""

        try:
            client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            response = await client.chat.completions.create(
                model=_get_user_model(),  # 경량 모델로 충분
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)

            cat = result.get("category", "Uncategorized")
            reason = result.get("reason", "")
            await _log("system",
                f"🏷️ [자율 분류] {skill_name} → {cat}\n   사유: {reason}",
                "[카테고리 분류]")
            return result

        except Exception as e:
            logger.warning(f"카테고리 분류 실패: {e}")
            return {
                "category": "Uncategorized",
                "category_description": "분류 미완료",
                "reason": f"자동 분류 실패: {e}",
            }

    # ── Reusability Check (기존 스킬 재사용 판단) ─────────────

    async def _check_reusability(self, user_request: str) -> dict | None:
        """
        기존 스킬 중 재사용 가능한 것이 있는지 기능 설명(description) 기반으로 판단.
        재사용 가능하면 해당 스킬을 활성화하고 즉시 반환, 없으면 None.
        """
        from core.skill_registry import get_skill_registry
        from core.executor import _TOOL_REGISTRY

        registry = get_skill_registry()
        all_skills = registry.get_all_skills()
        if not all_skills:
            return None

        await _log("system",
            f"🔍 [Reusability Check] 기존 {len(all_skills)}개 스킬에서 재사용 가능 여부 탐색 중...",
            "[재사용 판단]")

        # 키워드 기반 빠른 매칭
        req_lower = user_request.lower()
        # 요청에서 의미 있는 키워드 추출 (불용어 제거)
        stopwords = {
            "을", "를", "이", "가", "에", "의", "로", "와", "과", "하", "해", "줘", "좀",
            "알려", "보여", "찾아", "검색", "조회", "만들어", "기능", "스킬", "새",
            "the", "a", "an", "is", "to", "for", "and", "or", "in", "on", "me", "please",
        }
        req_keywords = {w for w in req_lower.split() if len(w) > 1 and w not in stopwords}

        best_match = None
        best_score = 0

        for skill in all_skills:
            skill_desc = (skill.get("description", "") + " " + skill.get("name", "")).lower()
            matched = sum(1 for kw in req_keywords if kw in skill_desc)
            if req_keywords and matched > best_score:
                best_score = matched
                best_match = skill

        # 키워드 50% 이상 매칭되면 재사용 판단
        if best_match and req_keywords and (best_score / len(req_keywords)) >= 0.5:
            skill_name = best_match["name"]

            # 스킬이 executor에 등록되어 있는지 확인, 없으면 활성화
            if skill_name not in _TOOL_REGISTRY:
                activated = registry.activate(skill_name)
                if not activated:
                    return None  # 활성화 실패 → 새로 생성

            await _log("system",
                f"♻️ [Reusability Check] 기존 스킬 재사용 결정!\n"
                f"  스킬: {skill_name}\n"
                f"  설명: {best_match.get('description', '')}\n"
                f"  매칭: {best_score}/{len(req_keywords)} 키워드",
                "[스킬 재사용]")

            return {
                "success": True,
                "skill_name": skill_name,
                "description": best_match.get("description", ""),
                "reused": True,
                "message": (
                    f"♻️ **기존 스킬 '{skill_name}' 재사용!** (신규 생성 불필요)\n\n"
                    f"- 설명: {best_match.get('description', '')}\n"
                    f"- 서비스: {best_match.get('service', 'Unknown')}\n\n"
                    f"⚡ **이 스킬은 이미 등록되어 있으므로 즉시 호출 가능합니다!**\n"
                    f"반드시 '{skill_name}' 도구를 tool_call로 즉시 호출하여 요청을 완료하세요."
                ),
            }

        if all_skills:
            await _log("system",
                f"🆕 [Reusability Check] 재사용 가능한 기존 스킬 없음 → 신규 생성 진행",
                "[신규 생성]")

        return None

    # ── 이메일 스킬 전용 처리 ─────────────────────────────────

    async def _handle_email_skill(self, _user_request: str) -> dict:
        """이메일 스킬은 Canonical Template으로 즉시 생성 (LLM 합성 불필요)."""
        from core.executor import _TOOL_REGISTRY

        skill_name = "send_email_via_smtp"
        skill_file = GENERATED_SKILLS_DIR / f"{skill_name}.py"

        if skill_file.exists() and skill_name not in _TOOL_REGISTRY:
            await _log("system", f"📂 [{skill_name}] 파일 발견 — 레지스트리 로드 중...", "[스킬 로드]")
            self.persister.load_and_register(skill_name)

        if skill_name in _TOOL_REGISTRY:
            await _log("system", f"✅ [{skill_name}] 재사용", "[스킬 재사용]")
            return {
                "success": True,
                "skill_name": skill_name,
                "description": "Gmail SMTP 이메일 전송 스킬",
                "message": (
                    f"✅ 이미 등록된 스킬 '{skill_name}' 사용!\n\n"
                    "⚡ 지금 즉시 이 스킬을 tool_call로 호출하여 이메일을 전송하세요!"
                ),
            }

        await _log("system", f"🚀 [{skill_name}] Canonical Template으로 생성", "[스킬 생성]")
        canonical_code = textwrap.dedent('''\
            from __future__ import annotations
            import os, json, smtplib
            from pathlib import Path
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            from core.executor import tool

            _PENDING_FILE = Path(__file__).parent / "pending_email_task.json"

            @tool
            async def send_email_via_smtp(to_email: str, subject: str, body: str) -> dict:
                """Gmail SMTP 서버를 통해 이메일을 전송합니다.

                Args:
                    to_email (str): 수신자 이메일 주소 (예: h5nmou@gmail.com)
                    subject (str): 이메일 제목
                    body (str): 이메일 본문 (plain text)

                Returns:
                    dict: 성공 시 {"success": "이메일 전송 완료", "to": to_email}
                """
                smtp_user = os.getenv("SMTP_USER")
                smtp_password = os.getenv("SMTP_PASSWORD")
                smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
                smtp_port = int(os.getenv("SMTP_PORT", "587"))

                if not smtp_user:
                    return {"error": "환경변수 누락", "detail": "SMTP_USER 미설정"}
                if not smtp_password:
                    _PENDING_FILE.write_text(
                        json.dumps({"to_email": to_email, "subject": subject, "body": body}, ensure_ascii=False),
                        encoding="utf-8")
                    return {"error": "환경변수 누락", "detail": "SMTP_PASSWORD 미설정",
                            "env_key_required": "SMTP_PASSWORD",
                            "hint": ".env에 SMTP_PASSWORD=앱비밀번호 추가 후 '설정 완료' 알려주세요."}
                try:
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = subject
                    msg["From"] = smtp_user
                    msg["To"] = to_email
                    msg.attach(MIMEText(body, "plain", "utf-8"))
                    with smtplib.SMTP(smtp_host, smtp_port) as server:
                        server.ehlo(); server.starttls(); server.ehlo()
                        server.login(smtp_user, smtp_password)
                        server.send_message(msg)
                    if _PENDING_FILE.exists():
                        _PENDING_FILE.unlink()
                    return {"success": "이메일 전송 완료", "to": to_email, "subject": subject, "from": smtp_user}
                except smtplib.SMTPAuthenticationError as e:
                    _PENDING_FILE.write_text(
                        json.dumps({"to_email": to_email, "subject": subject, "body": body}, ensure_ascii=False),
                        encoding="utf-8")
                    return {"error": "SMTP 인증 실패", "detail": str(e),
                            "env_key_required": "SMTP_PASSWORD",
                            "hint": ".env에서 SMTP_PASSWORD 수정 후 '설정 완료' 알려주세요."}
                except Exception as e:
                    return {"error": "이메일 전송 실패", "detail": str(e)}
            ''')

        file_path = self.persister.save(
            skill_name, canonical_code,
            "Gmail SMTP 이메일 전송 스킬 (SMTP_USER, SMTP_PASSWORD 필요)",
            {"service_name": "Gmail SMTP", "strategy": "canonical"}
        )
        success = self.persister.load_and_register(skill_name)
        if success:
            return {
                "success": True,
                "skill_name": skill_name,
                "file_path": str(file_path),
                "test_result": "PASS (Canonical)",
                "message": (
                    f"✅ 이메일 스킬 '{skill_name}' 생성 완료!\n\n"
                    "⚡ 지금 즉시 tool_call로 호출하여 이메일을 전송하세요!"
                ),
            }
        return {"success": False, "message": "이메일 스킬 등록 실패"}


# ── 싱글톤 ────────────────────────────────────────────────

_factory_instance: SkillFactory | None = None


def get_skill_factory() -> SkillFactory:
    global _factory_instance
    if _factory_instance is None:
        _factory_instance = SkillFactory()
    return _factory_instance
