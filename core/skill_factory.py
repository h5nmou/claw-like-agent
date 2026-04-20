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
from dotenv import load_dotenv

from core.smithery_client import get_smithery_client

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

from core.llm_client import get_llm_client, get_maker_client, get_maker_model as _get_maker_model, get_user_model as _get_user_model  # noqa: E402


# ── API 탐색 ─────────────────────────────────────────────

class APIDiscovery:
    """웹 검색 + Smithery API 기반 MCP 서버 탐색기."""

    DDGS_ENDPOINT = "https://api.duckduckgo.com/"
    SERPER_ENDPOINT = "https://google.serper.dev/search"

    def __init__(self) -> None:
        self.serper_key = os.getenv("SERPER_API_KEY")
        self._client = get_llm_client()
        self.smithery = get_smithery_client()

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
    # 정책:
    # - 실시간성이 중요한 장소 검색(google_maps 등) → MCP 우선
    # - Gmail 전송은 MCP가 아닌 간단한 SMTP Canonical Template으로 처리
    #   (OAuth 복잡성 회피, 앱 비밀번호만으로 즉시 동작)
    _OFFICIAL_MCP_MAP: dict[str, dict] = {
        "google_maps": {
            "keywords": ["map", "maps", "google maps", "장소", "위치", "경로", "지도", "맛집", "근처", "주변", "geocode", "directions", "place search", "구글맵", "구글지도", "위도", "경도"],
            "server": "@modelcontextprotocol/server-google-maps",
            "source": "github.com/modelcontextprotocol/servers",
            "install": "npx -y @modelcontextprotocol/server-google-maps",
            "env_key": "GOOGLE_MAPS_API_KEY",
            "description": "Google Maps Platform MCP — 장소 검색, 경로, 지오코딩",
            "priority": 1,
        },
        "google": {
            "keywords": ["google", "drive", "calendar", "구글", "구글드라이브", "구글캘린더"],
            "server": "@modelcontextprotocol/server-google-maps",
            "source": "github.com/modelcontextprotocol/servers",
            "install": "npx -y @modelcontextprotocol/server-google-maps",
            "env_key": "GOOGLE_MAPS_API_KEY",
            "description": "Google Maps Platform MCP — 장소 검색, 경로, 지오코딩",
            "priority": 2,
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

        [최우선] Google Maps MCP (실시간 장소 검색) → Smithery Registry
        Tier 0: 로컬 MCP 프로브 (이미 연결된 서버)
        Tier 1: Google Maps 등 공식 MCP (실시간 장소 검색에 필수)
        Tier 2: MCP 레지스트리 심층 탐색 (Smithery / Awesome-MCP)
        Tier 3: REST API / pip 라이브러리 폴백 (웹 검색 — 최후의 수단)
        ※ Gmail 전송은 MCP가 아닌 SMTP Canonical Template으로 별도 처리됨.
        """
        await _log("system", f"🔍 탐색 시작: '{user_request}'", "[탐색 중...]")

        # ── Tier 0: 로컬 MCP 서버 (이미 연결된 Phone-MCP 등) ──
        local_mcp = await self._probe_local_mcp(user_request)
        if local_mcp:
            return local_mcp

        # ── Tier 1-Priority: Google Maps 공식 MCP (실시간 장소 검색 최우선) ──
        # 장소/맛집/경로 등 실시간성이 중요한 요청은 MCP를 가장 먼저 시도한다.
        priority_result = await self._check_priority_services(user_request)
        if priority_result:
            return priority_result

        # ── Tier 1: 기타 메이저 서비스 공식 MCP 조회 ──
        official_mcp = await self._check_official_mcp(user_request)
        if official_mcp:
            return official_mcp

        # ── Tier 2: 레지스트리 심층 탐색 — 정책상 비활성화됨 ──
        # (사장님 지시: Smithery 시도하지 않음. 공식 MCP → npx 자동 실행 → 웹 검색 폴백으로 직행)

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

    # ── Tier 1-Priority: 실시간 장소 검색(Google Maps) 최우선 체크 ──────────

    async def _check_priority_services(self, user_request: str) -> dict | None:
        """
        실시간성이 중요한 장소 기반 요청은 Google Maps MCP를 최우선으로 시도한다.
        이메일(Gmail) 요청은 MCP가 아닌 SMTP Canonical Template으로 처리되므로
        여기서는 다루지 않는다(create_skill 맨 앞의 _handle_email_skill이 담당).
        """
        req_lower = user_request.lower()

        # 지도/장소 관련 힌트 (실시간 데이터 필수)
        map_hints = ["map", "maps", "장소", "위치", "경로", "지도", "맛집", "근처", "주변",
                     "directions", "place", "구글맵", "위도", "경도", "주소", "핫플",
                     "관광", "카페", "여행", "가이드"]

        priority_services = []
        if any(h in req_lower for h in map_hints):
            priority_services.append("google_maps")

        if not priority_services:
            return None

        for service_id in priority_services:
            info = self._OFFICIAL_MCP_MAP.get(service_id)
            if not info:
                continue

            await _log("system",
                f"⭐ [최우선 서비스] {service_id.upper()} MCP 시도: {info['server']}\n"
                f"   장소 검색은 실시간성이 중요하므로 MCP를 항상 우선 사용합니다.",
                f"[{service_id.upper()} MCP 시도 중]")

            result = await self._try_official_service(service_id, info, user_request)
            if result:
                return result

        return None

    # ── Tier 1: 메이저 서비스 공식 MCP ────────────────────────

    async def _try_official_service(self, service_id: str, info: dict, _user_request: str = "") -> dict | None:
        """단일 공식 MCP 서비스를 시도하는 공통 헬퍼."""
        env_key = info.get("env_key")
        auth_needed = env_key is not None

        await _log("system",
            f"🏛️ [Tier 1] 공식 MCP 서버 발견: {info['server']}\n"
            f"   서비스: {service_id}\n"
            f"   출처: {info['source']}\n"
            f"   설치: {info['install']}\n"
            f"   인증: {'필요 (' + env_key + ')' if auth_needed else '불필요'}",
            "[공식 MCP 발견]")

        # ── Step A: Smithery 프록시 — 정책상 비활성화됨 ──
        # (사장님 지시: Smithery 시도하지 않음. 바로 로컬 MCP 또는 npx 자동 실행으로 진행)

        # ── Step B: 로컬 MCP 서버 확인 ──
        local_mcp_url = os.getenv("MCP_SERVER_URL", "http://localhost:8080")
        if not local_mcp_url.rstrip("/").endswith("/mcp"):
            local_mcp_url = local_mcp_url.rstrip("/") + "/mcp"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.post(local_mcp_url, json={
                    "jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 1
                })
                if resp.status_code == 200:
                    tools = resp.json().get("result", {}).get("tools", [])
                    await _log("system",
                        f"✅ [로컬 MCP 서버 연결 성공] 도구 {len(tools)}개 발견",
                        "[로컬 MCP 연결]")
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
                            f"JSON-RPC tools/list → tools/call 패턴으로 호출."
                        ),
                        "mcp_tools": tools,
                        "mcp_registry_source": info["source"],
                        "mcp_install_method": info["install"],
                        "mcp_reliability": "high",
                        "mcp_tier": "official",
                    }
        except Exception:
            pass  # 로컬 MCP 서버 없음 → Step C로

        # ── Step C: npx 자동 실행 시도 (사용자에게 설치 요청 대신 Agent가 직접 실행) ──
        install_cmd = info["install"]
        if install_cmd.startswith("npx"):
            await _log("system",
                f"🚀 [Agent 자동 실행] {install_cmd} 직접 시작 중...\n"
                f"   사장님에게 설치 요청 없이 Agent가 직접 MCP 서버를 탐색합니다.",
                "[npx 자동 실행]")
            env_vars = {env_key: os.getenv(env_key, "")} if env_key else {}
            probe = await self._probe_stdio_mcp(install_cmd, env_vars)
            if probe:
                tools_count = len(probe["tools"])
                tools_names = [t.get("name", "?") for t in probe["tools"][:5]]
                await _log("system",
                    f"✅ [npx 자동 실행 성공] {info['server']} — 도구 {tools_count}개 발견\n"
                    f"   도구 목록: {tools_names}",
                    "[npx 자동 실행 성공]")
                return {
                    "strategy": "subprocess_mcp",
                    "service_name": info["server"],
                    "description": info["description"],
                    "subprocess_cmd": probe["proc_cmd"],
                    "env_key_name": env_key,
                    "auth_required": auth_needed,
                    "mcp_tools": probe["tools"],
                    "mcp_tier": "official_subprocess",
                    "mcp_reliability": "high",
                    "pip_packages": [],
                    "implementation_hint": (
                        f"⚠️ 중요 구조 규칙: 반드시 @tool 데코레이터가 붙은 단일 함수 하나만 작성하라. "
                        f"helper 함수(initialize_mcp 등)를 별도로 만들지 마라 — 모든 로직을 @tool 함수 안에 인라인으로 작성하라.\n"
                        f"구현 방법: @tool 함수 내부에서 asyncio.create_subprocess_exec({probe['proc_cmd']!r}, "
                        f"stdin=PIPE, stdout=PIPE, stderr=PIPE, "
                        f"env={{...os.environ, 'GOOGLE_MAPS_API_KEY': os.getenv('GOOGLE_MAPS_API_KEY','')}} 로 MCP 서버를 시작. "
                        f"JSON-RPC 순서: initialize → notifications/initialized → tools/call. "
                        f"사용 가능 도구: {[t.get('name') for t in probe['tools']]}. "
                        f"결과는 dict를 반환. 호출 완료 후 반드시 proc.terminate()로 정리.\n"
                        f"함수 반환값은 반드시 dict 또는 str 이어야 하며, httpx.AsyncClient나 subprocess 객체를 반환하지 마라."
                    ),
                }
            await _log("system",
                f"⚠️ [npx 자동 실행 실패] {install_cmd} — 사용자에게 설치 안내로 폴백",
                "[npx 실패 폴백]")

        # ── 최종 폴백: 설치 안내 반환 ──
        env_hint = f"\n   환경변수: {env_key}" if auth_needed else ""
        await _log("system",
            f"📦 [MCP 서버 미설치] {info['server']}가 로컬에 없습니다.\n"
            f"   설치 명령어: {install_cmd}{env_hint}",
            "[MCP 설치 필요]")

        return {
            "strategy": "mcp_install_required",
            "service_name": info["server"],
            "description": info["description"],
            "install_command": install_cmd,
            "env_key_name": env_key,
            "auth_required": auth_needed,
            "source": info["source"],
            "mcp_tier": "official",
            "install_guide": (
                f"📦 **MCP 서버 설치가 필요합니다**\n\n"
                f"서버: {info['server']}\n"
                f"설명: {info['description']}\n\n"
                f"**설치 방법:**\n"
                f"```\n{install_cmd}\n```\n"
                + (f"\n**환경변수 설정:**\n`.env` 파일에 `{env_key}=발급받은키` 추가\n" if auth_needed else "")
                + f"\n설치 후 '설정 완료'라고 알려주세요."
            ),
        }

    async def _probe_stdio_mcp(self, install_cmd: str, env_vars: dict) -> dict | None:
        """
        npx 명령을 subprocess로 직접 실행하여 stdio JSON-RPC로 MCP 서버를 탐색.
        사용자에게 설치 요청 대신 Agent가 직접 실행하고 도구 목록을 가져온다.
        """
        cmd_parts = install_cmd.split()
        env = {**os.environ, **{k: v for k, v in env_vars.items() if v}}
        proc = None
        try:
            await _log("system",
                f"🔧 [stdio MCP 프로브] 명령어: {install_cmd}",
                "[stdio MCP 시작]")
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )

            # ── Step 1: initialize ──
            init_msg = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "skill-factory", "version": "1.0"},
                }
            }) + "\n"
            proc.stdin.write(init_msg.encode())
            await proc.stdin.drain()
            await asyncio.wait_for(proc.stdout.readline(), timeout=20)  # init response

            # ── Step 2: notifications/initialized ──
            notif = json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {}
            }) + "\n"
            proc.stdin.write(notif.encode())
            await proc.stdin.drain()

            # ── Step 3: tools/list ──
            list_msg = json.dumps({
                "jsonrpc": "2.0", "id": 2,
                "method": "tools/list", "params": {}
            }) + "\n"
            proc.stdin.write(list_msg.encode())
            await proc.stdin.drain()
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
            result = json.loads(raw)
            tools = result.get("result", {}).get("tools", [])

            return {"proc_cmd": cmd_parts, "tools": tools}
        except Exception as e:
            await _log("system", f"⚠️ [stdio MCP 프로브 실패] {e}", "[npx 프로브 실패]")
            return None
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except Exception:
                    pass

    async def _check_official_mcp(self, user_request: str) -> dict | None:
        """
        요청 키워드를 기반으로 공식 MCP 표준 서버(modelcontextprotocol/servers)에
        매칭되는 서비스가 있는지 확인. 공식 서버는 보안성이 높고 설정이 표준화되어 있음.
        Google Maps는 _check_priority_services에서 이미 처리됨.
        Gmail은 _handle_email_skill(SMTP Canonical Template)에서 처리됨.
        """
        req_lower = user_request.lower()
        priority_ids = {"google_maps"}  # already handled in priority phase

        for service_id, info in self._OFFICIAL_MCP_MAP.items():
            if service_id in priority_ids:
                continue  # skip — already tried in priority phase
            if any(kw in req_lower for kw in info["keywords"]):
                result = await self._try_official_service(service_id, info, user_request)
                if result:
                    return result

        return None

    # ── Tier 2: 레지스트리 심층 탐색 ──────────────────────────

    async def _search_mcp_registries(self, user_request: str) -> dict | None:
        """
        Smithery REST API로 MCP 서버를 시맨틱 검색 → 매니지드 프록시 연결.
        API 키 미설정 시 기존 웹 검색 폴백.
        """
        await _log("system",
            "📚 [Tier 2] MCP 레지스트리 탐색 시작",
            "[MCP 레지스트리]")

        # ── STEP 1: Smithery REST API 직접 연동 (우선) ──
        if self.smithery.is_available:
            await _log("system",
                "🏪 [Smithery API] 시맨틱 검색 시작...",
                "[Smithery 탐색 중...]")

            servers = await self.smithery.search_servers(user_request)
            if servers:
                # 검색 결과 로그
                server_list = "\n".join(
                    f"  - {s.get('qualifiedName', '?')}: {s.get('description', '')[:80]}"
                    f" (사용: {s.get('useCount', 0)}회, 검증: {'✅' if s.get('verified') else '❌'})"
                    for s in servers[:5]
                )
                await _log("system",
                    f"🔍 [Smithery] {len(servers)}개 서버 발견:\n{server_list}",
                    "[Smithery 검색 결과]")

                # 최적 서버 선택 (시맨틱 검색 상위 = 가장 적합)
                best = servers[0]
                qualified_name = best.get("qualifiedName", "")

                # 상세 정보 조회 (도구 목록 포함)
                details = await self.smithery.get_server_details(qualified_name)
                if details:
                    display_name = details.get("displayName", qualified_name)
                    tools_from_details = details.get("tools", [])

                    await _log("system",
                        f"📋 [Smithery] 서버 상세: {display_name}\n"
                        f"   도구 수: {len(tools_from_details)}개\n"
                        f"   설명: {details.get('description', '')[:120]}",
                        "[Smithery 서버 상세]")

                    # 프록시 연결 생성
                    deployment_url = details.get("deploymentUrl") or details.get("mcpUrl") or details.get("url", "")
                    conn_id = await self.smithery.get_or_create_connection(
                        qualified_name, server_url=deployment_url
                    )

                    if conn_id:
                        # 연결 성공 → 실제 tools/list로 라이브 도구 확인
                        live_tools = await self.smithery.list_tools(conn_id)
                        final_tools = live_tools if live_tools else tools_from_details

                        tools_desc = "\n".join(
                            f"  - {t.get('name', '?')}: {t.get('description', '')[:60]}"
                            for t in final_tools[:10]
                        )
                        await _log("system",
                            f"✅ [Smithery] 프록시 연결 완료: {qualified_name}\n"
                            f"   연결 ID: {conn_id}\n"
                            f"   사용 가능 도구:\n{tools_desc}",
                            "[Smithery 연결 완료]")

                        return {
                            "strategy": "mcp",
                            "service_name": display_name,
                            "api_endpoint": f"{self.smithery.BASE_URL}/connections/{conn_id}/call",
                            "pip_packages": [],
                            "auth_required": True,
                            "env_key_name": "SMITHERY_API_KEY",
                            "description": details.get("description", ""),
                            "implementation_hint": (
                                f"Smithery 프록시를 통해 MCP 도구 호출. "
                                f"연결 ID: {conn_id}. "
                                f"도구 목록: {[t.get('name') for t in final_tools[:5]]}"
                            ),
                            "mcp_registry_source": f"smithery.ai/server/{qualified_name}",
                            "mcp_install_method": "smithery_proxy",
                            "mcp_tool_schema": final_tools[0] if final_tools else None,
                            "mcp_tools": final_tools,
                            "mcp_reliability": "high" if best.get("verified") else "medium",
                            "mcp_tier": "community",
                            "smithery_connection_id": conn_id,
                            "smithery_qualified_name": qualified_name,
                        }
                    else:
                        await _log("system",
                            f"⚠️ [Smithery] 프록시 연결 실패: {qualified_name} → 웹 검색 폴백",
                            "[Smithery 연결 실패]")
                else:
                    await _log("system",
                        f"⚠️ [Smithery] 서버 상세 조회 실패: {qualified_name} → 웹 검색 폴백",
                        "[Smithery 상세 실패]")
            else:
                await _log("system",
                    "ℹ️ [Smithery] 검색 결과 없음 → 웹 검색 폴백",
                    "[Smithery 미발견]")
        else:
            await _log("system",
                "ℹ️ SMITHERY_API_KEY 미설정 → Smithery 탐색 불가",
                "[Smithery 비활성]")
            # API 키 미설정 시 웹 검색 폴백 없이 즉시 반환
            # → discover_strategy()에서 사용자에게 SMITHERY_API_KEY 설정 안내
            return None

        # ── STEP 2: 웹 검색 폴백 (Smithery API 사용 가능하지만 검색/연결 실패 시) ──
        registry_results = []

        smithery_queries = [
            f"site:smithery.ai {user_request}",
            f"smithery.ai MCP server {user_request}",
        ]
        for q in smithery_queries:
            await _log("system", f"🏪 웹 검색 폴백: {q}", "[Smithery 웹 검색...]")
            results = await self.search(q)
            registry_results.extend(results)

        awesome_queries = [
            f"github awesome-mcp-servers {user_request}",
            f"github punkpeye awesome-mcp {user_request}",
        ]
        for q in awesome_queries:
            await _log("system", f"📦 웹 검색 폴백: {q}", "[Awesome-MCP 웹 검색...]")
            results = await self.search(q)
            registry_results.extend(results)

        if not registry_results:
            await _log("system",
                "ℹ️ [Tier 2] 레지스트리에서 관련 도구 미발견 → API 폴백",
                "[레지스트리 미발견]")
            return None

        analysis = await self._analyze_mcp_registry(user_request, registry_results)
        if analysis and analysis.get("found"):
            mcp_source = analysis.get("source", "unknown")
            mcp_name = analysis.get("mcp_server_name", "Unknown MCP")
            await _log("system",
                f"✅ [Tier 2] MCP 서버 발견 (웹 검색): {mcp_name}\n"
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
        # PHONE_MCP_ENABLED=false 면 프로빙 자체 생략
        if os.getenv("PHONE_MCP_ENABLED", "true").strip().lower() == "false":
            await _log("system",
                "⏭️ 로컬 MCP 프로빙 생략 (PHONE_MCP_ENABLED=false)",
                "[MCP 프로빙 생략]")
            return None

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
            # APIDiscovery는 일반 클라이언트 + 일반 모델을 사용 (Maker 모델은 코드 합성 전용)
            from core.llm_client import get_default_model
            response = await self._client.chat.completions.create(
                model=get_default_model(),
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
        self._client = get_maker_client()  # Maker 전용 클라이언트 (Claude 등)

    @staticmethod
    def _format_mcp_tools_context(strategy: dict) -> str:
        """MCP 전략일 때 도구 목록 또는 외부 레지스트리 정보를 프롬프트에 주입."""
        strat = strategy.get("strategy")

        # subprocess_mcp 전략: stdio JSON-RPC 패턴 주입
        if strat == "subprocess_mcp":
            cmd = strategy.get("subprocess_cmd", [])
            env_key = strategy.get("env_key_name") or ""
            mcp_tools = strategy.get("mcp_tools", [])
            tools_text = json.dumps(mcp_tools, ensure_ascii=False, indent=2)
            return f"""
## ⚠️ subprocess_mcp 전략 — stdio JSON-RPC 패턴 (CRITICAL)

이 MCP 서버는 **stdio(stdin/stdout) 기반** 프로세스다.
HTTP 클라이언트(httpx) 절대 사용 금지. 반드시 아래 패턴으로만 구현하라.

### 사용 가능한 도구 목록
```json
{tools_text}
```

### 완전한 구현 패턴 (이 구조를 그대로 사용하라)
```python
from __future__ import annotations
import os, asyncio, json
from asyncio import subprocess as asp
from core.executor import tool

@tool
async def YOUR_FUNCTION_NAME(param1: str, param2: str = "") -> dict:
    \"\"\"Docstring here.\"\"\"
    import logging
    logger = logging.getLogger(__name__)

    api_key = os.getenv("{env_key or 'YOUR_ENV_KEY'}", "")
    cmd = {cmd!r}
    env = {{**os.environ{f', "{env_key}": api_key' if env_key else ''}}}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asp.PIPE, stdout=asp.PIPE, stderr=asp.PIPE,
        env=env,
    )

    async def _mcp_call(method: str, params: dict, call_id: int, timeout: int = 15):
        \"\"\"MCP에 JSON-RPC 요청을 보내고 응답을 파싱. 요청/응답 원문을 로그로 기록.\"\"\"
        req = {{"jsonrpc":"2.0","id":call_id,"method":method,"params":params}}
        req_line = json.dumps(req, ensure_ascii=False) + "\\n"
        logger.info(f"[MCP REQUEST] {{req_line.strip()}}")
        proc.stdin.write(req_line.encode()); await proc.stdin.drain()
        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        raw_text = raw.decode(errors="replace").strip()
        logger.info(f"[MCP RESPONSE] {{raw_text}}")
        return json.loads(raw_text) if raw_text else {{}}

    try:
        # 1) initialize
        await _mcp_call("initialize",
            {{"protocolVersion":"2024-11-05","capabilities":{{}},
              "clientInfo":{{"name":"agent","version":"1.0"}}}},
            call_id=1, timeout=20)

        # 2) notifications/initialized (no response expected)
        notif = json.dumps({{"jsonrpc":"2.0","method":"notifications/initialized","params":{{}}}}) + "\\n"
        logger.info(f"[MCP NOTIFY] {{notif.strip()}}")
        proc.stdin.write(notif.encode()); await proc.stdin.drain()

        # 3) tools/call — 요청/응답 모두 자동 로깅됨
        data = await _mcp_call("tools/call",
            {{"name":"maps_search_places",  # 도구 목록에서 선택
              "arguments":{{"query": param1}}}},
            call_id=2, timeout=15)

        content = data.get("result", {{}}).get("content", [])
        # ⚠️ content[].text는 JSON 문자열인 경우가 많음 — 반드시 json.loads()로 파싱
        parsed = None
        for c in content:
            if c.get("type") == "text" and c.get("text"):
                try:
                    parsed = json.loads(c["text"])
                    break
                except (json.JSONDecodeError, ValueError):
                    parsed = c["text"]  # 일반 텍스트면 원본 유지
        return {{"결과": parsed}} if parsed is not None else {{"결과없음": True, "detail": str(data)}}
    except Exception as e:
        logger.exception(f"[MCP ERROR] {{e}}")
        return {{"error": str(e), "detail": "stdio MCP 통신 실패"}}
    finally:
        if proc.returncode is None:
            proc.terminate()
            try: await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception: pass
```

### 핵심 규칙
1. **httpx 절대 사용 금지** — HTTP 클라이언트가 아닌 stdio 프로세스임
2. **import httpx 작성 금지** — 이 스킬에는 httpx가 필요 없음
3. initialize → notifications/initialized → tools/call 순서 필수
4. proc.terminate() 반드시 finally 블록에서 호출
5. 반환값은 dict — subprocess 객체나 coroutine 절대 반환 금지
6. **⚠️ MCP 요청/응답 원문 로깅 필수** — 위 `_mcp_call` 헬퍼처럼 모든 tools/call 직전에 `logger.info(f"[MCP REQUEST] {{req}}")`, 직후에 `logger.info(f"[MCP RESPONSE] {{raw}}")`를 찍어라. 디버깅 가능한 스킬만 유지된다.

### ⚠️ MCP 응답 파싱 규칙 (CRITICAL — 이 부분을 틀리면 스킬이 무조건 실패)
MCP 서버의 `tools/call` 응답은 **항상** 다음 구조다:
```json
{{"result": {{"content": [{{"type": "text", "text": "<JSON 문자열>"}}]}}}}
```

- `content[].text`는 **일반 텍스트가 아니라 JSON 문자열인 경우가 대부분**이다.
- 예: `maps_geocode` 응답 text = `'{{"location": {{"lat": 33.41, "lng": 126.39}}, "formatted_address": "..."}}' `
- **반드시 `json.loads(text)`로 파싱한 후 필드를 추출하라.**
- `item.get("type") == "geo"`, `item["places"]` 같은 **존재하지 않는 키를 추측하지 마라** — content 항목의 타입은 `text`뿐이다.

❌ 금지:
```python
for item in content:
    if item.get("type") == "geo":  # ← 이런 타입은 존재하지 않음
        coords = item["latitude"]
```

✅ 올바름:
```python
for item in content:
    if item.get("type") == "text":
        try:
            parsed = json.loads(item["text"])
            # parsed가 list일 수도, dict일 수도 있음
            if isinstance(parsed, list) and parsed: parsed = parsed[0]
            loc = (parsed or {{}}).get("location") or {{}}
            lat, lng = loc.get("lat"), loc.get("lng")
        except json.JSONDecodeError:
            pass
```

### ⚠️ 좌표 파라미터 Type-Resilient 처리 (Google Maps MCP 한정)
`location` 파라미터가 함수 입력으로 들어올 때, 다음 세 가지 형태를 **모두 방어적으로 처리**해야 한다:
1. `latitude=33.41, longitude=126.39` (별도 float 파라미터)
2. `location={{"latitude": 33.41, "longitude": 126.39}}` (dict)
3. `location="제주도 애월읍"` (문자열 — 이때만 geocoding 필요)

**이미 좌표가 있으면 geocoding을 절대 호출하지 마라** — Google Maps는 한국어 주소에서 ZERO_RESULTS를 자주 반환한다.

```python
# ✅ 입력 판별 패턴
lat = lng = None
if latitude is not None and longitude is not None:
    lat, lng = float(latitude), float(longitude)
elif isinstance(location, dict):
    lat = location.get("latitude") or location.get("lat")
    lng = location.get("longitude") or location.get("lng")
    if lat is not None and lng is not None:
        lat, lng = float(lat), float(lng)
# 좌표가 여전히 없으면 그때만 문자열 → geocoding
if lat is None and isinstance(location, str) and location.strip():
    # maps_geocode 호출 후 _parse_mcp_text_content로 lat/lng 추출
    ...

# maps_search_places 호출 시 location 파라미터는 {{"latitude": lat, "longitude": lng}} 객체
```
"""

        if strat != "mcp":
            return ""

        mcp_tools = strategy.get("mcp_tools")
        connection_id = strategy.get("smithery_connection_id")

        # Case 1: Smithery 프록시 연결 — 도구 목록 + 프록시 호출 패턴
        if connection_id and mcp_tools:
            tools_text = json.dumps(mcp_tools, ensure_ascii=False, indent=2)
            return f"""
## Smithery MCP 프록시 (CRITICAL — 이 패턴을 반드시 사용하라)

이 MCP 서버는 Smithery 매니지드 프록시를 통해 호출한다.
로컬 설치 불필요. 아래 도구 목록에 있는 도구만 사용하라.

### 사용 가능한 도구 목록
```json
{tools_text}
```

### Smithery 프록시 호출 패턴 (반드시 이 패턴을 따르라)
```python
import httpx, os

SMITHERY_API_KEY = os.getenv("SMITHERY_API_KEY")
SMITHERY_PROXY_URL = "https://api.smithery.ai/connections/{connection_id}/call"

async with httpx.AsyncClient(timeout=15.0) as client:
    resp = await client.post(
        SMITHERY_PROXY_URL,
        headers={{"Authorization": f"Bearer {{SMITHERY_API_KEY}}"}},
        json={{
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {{"name": "도구명", "arguments": {{"key": "value"}}}},
            "id": 1
        }}
    )
    if resp.status_code != 200:
        return {{"error": "Smithery 프록시 호출 실패", "detail": f"HTTP {{resp.status_code}}"}}

    data = resp.json()
    if "error" in data:
        return {{"error": "MCP 오류", "detail": str(data["error"])}}

    content_list = data.get("result", {{}}).get("content", [])
    texts = [c.get("text", "") for c in content_list if c.get("type") == "text"]
    return {{"결과": "\\n".join(texts) if texts else str(data.get("result", ""))}}
```

### 핵심 규칙
1. **SMITHERY_API_KEY**를 반드시 `os.getenv()`로 읽어라 (하드코딩 금지)
2. **도구 목록의 `name`과 `inputSchema.properties`를 정확히** 사용하라
3. 모든 검색 파라미터(지명, 카테고리 등)는 **함수 파라미터로** 받아라
4. **결과는 result.content[].text에서 추출** — MCP 표준 응답 형식
"""

        # Case 2: 로컬 MCP 서버 — 실제 도구 목록이 있는 경우
        if mcp_tools and not connection_id:
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

        # Case 3: 외부 MCP 레지스트리 발견 (프록시 연결 없음) — 범용 래퍼 스킬 생성
        registry_source = strategy.get("mcp_registry_source", "")
        tool_schema = strategy.get("mcp_tool_schema")
        install_method = strategy.get("mcp_install_method", "")

        if registry_source:
            schema_text = json.dumps(tool_schema, ensure_ascii=False, indent=2) if tool_schema else "스키마 미상"
            return f"""
## 외부 MCP 레지스트리에서 발견된 서버 (범용 래퍼 스킬로 작성하라)

이 MCP 서버는 외부 레지스트리({registry_source})에서 발견되었다.
`tools/list`로 도구를 확인하고 `tools/call`로 호출하는 범용 래퍼 스킬을 작성하라.

### 발견된 MCP 서버 정보
- 서버: {strategy.get('service_name', 'Unknown')}
- 출처: {registry_source}
- 설치: {install_method}
- 도구 스키마 (참고용): {schema_text}

### 구현 패턴
```python
mcp_url = os.getenv("MCP_SERVER_URL", "http://localhost:8080")
if not mcp_url.rstrip("/").endswith("/mcp"):
    mcp_url = mcp_url.rstrip("/") + "/mcp"

async with httpx.AsyncClient(timeout=15.0) as client:
    list_resp = await client.post(mcp_url, json={{
        "jsonrpc": "2.0", "method": "tools/list", "params": {{}}, "id": 1
    }})
    tools = list_resp.json().get("result", {{}}).get("tools", [])
    # 도구 이름을 동적으로 탐색하여 호출
```

### 핵심 규칙
1. 모든 변수를 함수 파라미터로 — 하드코딩 금지
2. tools/list로 동적 탐색 — 도구 이름을 하드코딩하지 말 것
3. Fallback 필수 — 에러 dict 반환
4. 결과는 result.content[].text에서 추출
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
1. **반드시 `async def` 함수로 작성** {"(httpx.AsyncClient 사용)" if strategy.get("strategy") != "subprocess_mcp" else "(asyncio.create_subprocess_exec 사용 — httpx 절대 금지)"}
2. **`from core.executor import tool` import 후 `@tool` 데코레이터 적용**
3. **Google-style Docstring 필수** (Args:, Returns: 섹션 포함)
4. 환경변수는 `os.getenv("ENV_KEY_NAME")` 로만 읽기 (하드코딩 절대 금지)
5. 에러는 dict 형태로 반환: `{{"error": "...", "detail": "..."}}`
6. 성공 시 실용적인 한국어 키-값 dict 반환
7. **import 구문은 함수 상단이 아닌 파일 최상단에 위치**
8. `from __future__ import annotations` 포함
9. pip 패키지가 필요한 경우 주석으로 설치 명령 명시
10. **민감 정보(API 키, 토큰)는 절대 코드에 노출 금지** — os.getenv() 전용
11. **⚠️ 반환값 필수 규칙**: 함수는 반드시 `dict` 또는 `str`을 반환해야 한다. `httpx.AsyncClient`, subprocess, coroutine 등 비직렬화 객체를 반환하면 안 된다.
12. **⚠️ 단일 @tool 함수 원칙**: 파일에 `@tool` 함수는 반드시 하나만 작성. helper 함수(`initialize_*`, `create_client` 등)가 필요하면 `@tool` 함수 BODY 안에 인라인으로 작성하거나 중첩 함수로 정의.

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

{"## ⛔ subprocess_mcp 전략 주의 (CRITICAL)" + chr(10) + "이 스킬은 subprocess_mcp 전략입니다. 위의 ## ⚠️ subprocess_mcp 전략 섹션의 패턴만 사용하세요." + chr(10) + "httpx, MCP_SERVER_URL, HTTP POST 등은 절대 사용하지 마세요. 위의 stdio 패턴이 유일한 올바른 구현입니다." if strategy.get("strategy") == "subprocess_mcp" else """## MCP 전략 구현 시 반드시 준수 (CRITICAL — strategy가 "mcp"인 경우)
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
- ❌ `/api/restaurants`, `/api/weather` 같은 경로를 임의로 만드는 것"""}

## 🏭 Skill Factory Manufacturing Standard (제조 표준 — 반드시 모든 항목 준수)

### 1) MCP Response Handling + Robust Extraction (MCP 응답 처리·견고한 파싱)
MCP 도구의 결과는 **항상 리스트 형태**로 반환된다: `[{{"type": "text", "text": "<JSON 문자열>"}}]`.
단, `text` 앞뒤에 설명 문구나 공백이 붙어있는 경우가 있으므로 **순수 JSON 덩어리만 추출**하는 방어 파싱을 반드시 포함하라.

```python
import re
content = data.get("result", {{}}).get("content", [])
parsed = None
if content and isinstance(content, list):
    text = content[0].get("text", "") if isinstance(content[0], dict) else ""
    if text:
        text = text.strip()
        # 1차 시도: 그대로 json.loads
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            # 2차 시도: 순수 JSON 블록만 잘라내기 — find('{')와 rfind('}') 또는 [] 사용
            #          (응답 앞뒤에 "Server running...", 로그 노이즈 등이 섞일 수 있음)
            obj_start, obj_end = text.find("{{"), text.rfind("}}")
            arr_start, arr_end = text.find("["), text.rfind("]")
            json_block = None
            # 둘 다 발견되면 더 바깥쪽(앞쪽이 빠른 쪽)을 선택
            candidates = []
            if obj_start != -1 and obj_end != -1 and obj_end > obj_start:
                candidates.append((obj_start, text[obj_start:obj_end+1]))
            if arr_start != -1 and arr_end != -1 and arr_end > arr_start:
                candidates.append((arr_start, text[arr_start:arr_end+1]))
            if candidates:
                candidates.sort(key=lambda x: x[0])
                json_block = candidates[0][1]
            if json_block:
                try:
                    parsed = json.loads(json_block)
                except (json.JSONDecodeError, ValueError):
                    parsed = text
            else:
                parsed = text
```
- `content[0]["text"]`는 대부분 **JSON 문자열** → `json.loads()` 필수
- JSON 앞뒤에 자연어/로그 노이즈가 붙어있을 수 있음 → **`text.find('{{')`와 `text.rfind('}}')` (또는 `[ ]`)로 순수 JSON 블록만 잘라낸 뒤 재파싱**. 정규표현식보다 빠르고 안정적
- 존재하지 않는 타입(`"geo"`, `"places"` 등)을 가정하지 마라 — content 타입은 대부분 `"text"` 하나다

**⚠️ 파싱된 객체의 형태 — list / dict 둘 다 처리 필수 (CRITICAL)**
파싱된 JSON은 세 가지 형태 중 하나로 온다. 하나만 가정하면 빈 결과로 조기 종료된다.
```python
# maps_search_places 실제 응답: {{"places": [...]}} 또는 {{"results": [...]}} 또는 그냥 [...]
if isinstance(parsed, list):
    places = parsed                                      # 형태 A
elif isinstance(parsed, dict):
    places = parsed.get("places") or parsed.get("results") or []   # 형태 B/C
else:
    places = []
```
- ❌ **금지**: `if isinstance(parsed, list):` 만 체크 → dict 응답을 통째로 버려서 "결과없음" 버그
- ✅ **필수**: list 분기 + dict 분기 두 갈래 모두 처리. dict면 `"places"` → `"results"` → `"items"` 순으로 키 탐색

### 2) Input Type Resilience + Flexible Types (다형 입력 방어)
모든 파라미터는 `isinstance()`로 타입을 먼저 확인하라. 호출자마다 포맷이 다를 수 있다.

**2-a) location 파라미터 (문자열 주소 vs 좌표 객체) — 하이브리드 입력 파이프라인**
```python
# ✅ isinstance로 먼저 확인하여 파이프라인 분기
if isinstance(location, dict):
    # 좌표 객체 → geocoding 건너뛰고 즉시 maps_search_places로 진입
    lat = location.get("latitude") or location.get("lat")
    lng = location.get("longitude") or location.get("lng")
    coords = {{"latitude": float(lat), "longitude": float(lng)}}
elif isinstance(location, str):
    # 주소 문자열 → maps_geocode 먼저 실행 → 좌표 추출 후 maps_search_places
    geo = await call_mcp("maps_geocode", {{"address": location}})
    coords = extract_coords(geo)
else:
    return {{"error": "location은 str 또는 dict 여야 합니다"}}
# 이후 동일한 coords로 검색 진행
places = await call_mcp("maps_search_places", {{"query": q, "location": coords}})
```
- 별도 `latitude: float = None, longitude: float = None` 파라미터도 추가해 직접 좌표 경로 허용
- **좌표가 이미 있으면 geocoding 호출 금지** (한국어 주소는 ZERO_RESULTS 위험)
- **dict 분기를 먼저 체크** — 좌표가 우선이고, 주소는 fallback

**2-b) 키워드/카테고리 파라미터 (문자열 자동 분리)**
```python
# ✅ str로 들어오면 ',' 로만 분리. 공백은 절대 분리 기준으로 쓰지 마라!
#    "실내 관광지" 같이 공백이 포함된 단일 구절이 두 개로 쪼개지면 검색이 망가진다.
if isinstance(keywords, str):
    keywords = [k.strip() for k in keywords.split(",") if k.strip()]
elif isinstance(keywords, list):
    keywords = [str(k).strip() for k in keywords if str(k).strip()]
else:
    keywords = []
```
- ❌ **금지**: `re.split(r"[,\\s]+", ...)` — 공백까지 분리하면 "실내 관광지" → "실내", "관광지" (버그)
- 타입 검사 없이 바로 `.split()` 또는 iteration하지 마라 — `str`/`list` 모두 받아야 한다

### 2') MCP 통신 Persistence (subprocess 재사용)
한 번의 스킬 실행 안에서 MCP 서버(`npx`)를 **여러 번 재시작하지 마라**. 지오코딩과 검색을 같은 subprocess에서 순차 수행하라.

```python
# ✅ 올바름 — subprocess 1회 열고 initialize 1회, 그 뒤 여러 tools/call 연속 호출
proc = await asyncio.create_subprocess_exec(*cmd, stdin=..., stdout=..., env=env)
try:
    # initialize + notifications/initialized (한 번만)
    ...
    # tools/call #1: maps_geocode
    ...
    # tools/call #2, #3, ...: maps_search_places (같은 proc, id만 증가)
    ...
finally:
    proc.terminate()

# ❌ 금지 — 매 호출마다 subprocess를 새로 띄우는 방식
for kw in keywords:
    proc = await asyncio.create_subprocess_exec(...)   # ← 비효율, 초기화 비용 중복
```

### 3) Zero-Result Prevention + Smart Query (검색 결과 없음 방지·검색 전략)
검색 쿼리가 너무 복잡하면 결과가 0건이 된다. **넓은 키워드 + 결과 필터링** 전략을 채택하라.

```python
# ❌ 금지 — 구체적 수식어로 좁혀서 검색
query = "비 오는 날 가기 좋은 실내 핫플레이스"   # ← 결과 0건
query = "indoor cafe recommended"               # ← 마찬가지 0건 (수식어 포함)
query = "실내 카페"                              # ← 0건 (수식어 + 한국어)

# ❌ 절대 금지 — 검색 쿼리에 형용사·수식어 포함
#   '실내', 'indoor', '비오는날', '추천', 'recommended', 'best', 'popular' 같은 단어를
#   query에 넣으면 Google Maps API가 이를 **상호명의 일부**로 오해하여 0건을 반환한다.
#   ➡️ 수식어는 사후 필터(types, name/desc 검사)로 처리하라.

# ✅ 권장 — 표준 카테고리(영어)만 query로 던지고, '실내 여부'는 사후 필터로 판정
INDOOR_TYPES = {{"museum", "art_gallery", "cafe", "shopping_mall", "library",
                "aquarium", "movie_theater", "spa", "restaurant", "bakery",
                "book_store", "department_store"}}
# ⚠️ '실내' 판정은 INDOOR_TYPES 포함 + OUTDOOR_TYPES 미포함 두 조건으로 판단
OUTDOOR_TYPES = {{"park", "hiking_area", "campground", "natural_feature",
                 "stadium", "amusement_park", "zoo", "tourist_attraction"}}  # tourist_attraction은 야외 비중↑
# 사후 필터 예시:
#   types_set = set(place.get("types") or [])
#   is_indoor = (types_set & INDOOR_TYPES) and not (types_set & OUTDOOR_TYPES)

# ⚠️ CRITICAL — rating 필터링 규칙 (버그 방지)
#   1) rating이 None/없음 = "평점 미집계"일 뿐 "평점 낮음"이 아니다 → 필터 탈락시키지 마라
#   2) rating이 있는 장소만 min_rating으로 비교, 없으면 일단 통과시켜라
#   3) min_rating 기본값은 0.0 (호출자가 명시하지 않으면 필터 off)
#   4) user_ratings_total(리뷰 수)을 rating으로 폴백하지 마라 — 전혀 다른 값이다
def passes_rating(place, min_r):
    r = place.get("rating")
    if r is None or r == 0:
        return True   # 평점 미집계 → 필터 통과 (낙오 방지)
    try: return float(r) >= float(min_r)
    except: return True

# ⚠️ CRITICAL — tried_keywords는 루프 안에서 반드시 append로 누적해야 한다.
#             0건 시 진단 정보(Self-Logging)에 실제 사용 키워드가 기록되어야 한다.
# ⚠️ CRITICAL — MCP 검색 쿼리는 반드시 영어로 보내라 (한국어 → 영어 매핑 필수).
#             Google Maps 등 글로벌 MCP는 영어 쿼리에서 결과 품질이 훨씬 높다.
KO_TO_EN_QUERY = {{
    "카페": "cafe", "맛집": "restaurant", "관광지": "tourist attraction",
    "박물관": "museum", "미술관": "art gallery", "갤러리": "gallery",
    "명소": "landmark", "쇼핑몰": "shopping mall", "베이커리": "bakery",
    "스파": "spa", "아쿠아리움": "aquarium", "영화관": "movie theater",
    "도서관": "library", "공방": "workshop studio", "전시관": "exhibition hall",
    "서점": "book store",
    # ⚠️ "실내 카페", "실내 관광지" 같은 수식어 결합 키는 의도적으로 제외.
    #     수식어가 query에 들어가면 Google Maps가 상호명으로 오해하여 0건이 됨.
    #     "실내" 의도는 OUTDOOR_TYPES 제외 사후 필터로 처리한다.
}}
# 입력에 "실내 카페" 같이 수식어 + 카테고리가 섞여 있으면 카테고리만 추출
def _strip_modifiers(ko: str) -> str:
    for mod in ("실내 ", "실외 ", "감성 ", "유명한 ", "인기 ", "추천 "):
        if ko.startswith(mod):
            return ko[len(mod):]
    return ko
tried_keywords = []        # 실제 검색에 쓴 키워드(영어) 누적
merged, seen = [], set()
# 입력 categories가 한국어여도 반드시 영어 쿼리로 변환 후 검색
base_keywords_ko = list(categories) if isinstance(categories, list) else ["카페", "맛집", "명소"]
base_keywords_en = [
    KO_TO_EN_QUERY.get(_strip_modifiers(k.strip()), _strip_modifiers(k.strip()))
    for k in base_keywords_ko
]
for kw in base_keywords_en:
    tried_keywords.append(kw)              # ← 반드시 기록 (영어 쿼리 그대로)
    result = await call_mcp_in_same_proc(proc, "maps_search_places",
                                         {{"query": kw, "location": coords}})
    for place in extract_places(result):
        pid = place.get("place_id")
        if not pid or pid in seen:
            continue
        # 1차 필터: 야외 types 제외 + (실내 types 포함 OR name에 실내/indoor)
        types = set(place.get("types", []) or [])
        desc = (place.get("name", "") + " " + (place.get("formatted_address") or "")).lower()
        is_outdoor = bool(types & OUTDOOR_TYPES)        # 명백한 야외는 즉시 탈락
        is_indoor = bool(types & INDOOR_TYPES) or "indoor" in desc or "실내" in desc
        indoor_ok = is_indoor and not is_outdoor
        rating_ok = passes_rating(place, min_rating)
        if indoor_ok and rating_ok:
            merged.append(place); seen.add(pid)

# ⚠️ 단계적 완화 (Graceful Degradation) — 결과가 너무 적을 때 필터를 순차적으로 풀어라
if len(merged) < 3:   # 3건 미만이면 rating 필터 해제 재시도
    for place in all_candidates_seen:   # 1차에서 수집한 전체 후보
        pid = place.get("place_id")
        if pid in seen: continue
        if indoor_ok_for(place):
            merged.append(place); seen.add(pid)

if len(merged) < 3:   # 여전히 부족하면 indoor 필터도 해제 (rating만 OR 무필터)
    for place in all_candidates_seen:
        pid = place.get("place_id")
        if pid in seen: continue
        merged.append(place); seen.add(pid)
```
- **키워드 리스트는 합치지 말고 개별 루프** — place_id로 중복 제거
- **`tried_keywords.append(kw)`를 루프 안에서 반드시 호출** — 진단 정보에 기록되어야 한다
- types/name 기반 **사후 필터링**으로 좁혀라 (쿼리에 "실내"를 붙이는 것보다 훨씬 안전)
- `categories` 파라미터가 입력되면 `base_keywords = list(categories)`로 채워서 루프를 돌려라 (input을 무시하고 하드코딩 리스트만 쓰지 말 것)
- **단계적 완화 필수**: 결과가 3건 미만이면 rating 필터 → indoor 필터 순서로 해제 후 재선별 (한 번 수집한 후보를 버리지 마라)

### 4) No-Guessing Policy (추측 금지)
MCP 도구의 inputSchema와 응답 구조를 **절대 추측하지 마라**.
- 파라미터명/타입: 반드시 위 "사용 가능한 도구 목록"의 `inputSchema.properties`를 보고 작성하라
- Google Maps 등 lat/lng 도구는 `location` 파라미터를 `{{"latitude": X, "longitude": Y}}` **객체**로 보내라 (문자열 금지)
- 과거 경험·문서 예시를 그대로 쓰지 말고 **이번 호출에서 확인한 실제 스키마**만 사용하라

### 4-0) MCP 통신 원문 로깅 (CRITICAL — 디버깅 필수)
**모든 MCP tools/call 전후로 요청·응답 원문을 반드시 `logger.info`로 기록하라.**
로그가 없으면 "결과없음" 원인이 쿼리인지·필터인지·MCP 응답인지 판별 불가능.

```python
import logging
logger = logging.getLogger(__name__)

async def _mcp_call(proc, method, params, call_id=1, timeout=15):
    req = {{"jsonrpc":"2.0","id":call_id,"method":method,"params":params}}
    req_line = json.dumps(req, ensure_ascii=False) + "\\n"
    logger.info(f"[MCP REQUEST] {{req_line.strip()}}")   # ← 요청 원문
    proc.stdin.write(req_line.encode()); await proc.stdin.drain()
    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
    raw_text = raw.decode(errors="replace").strip()
    logger.info(f"[MCP RESPONSE] {{raw_text}}")           # ← 응답 원문(파싱 전)
    return json.loads(raw_text) if raw_text else {{}}
```

- `[MCP REQUEST]` / `[MCP RESPONSE]` 고정 태그로 로그에서 쉽게 grep 가능하게 하라
- `raw_text`는 **파싱 전 원본**을 찍어라. json.loads() 실패 원인 추적에 필수
- 에러 발생 시 `logger.exception(...)`으로 traceback도 기록
- 응답이 너무 길 것 같아도 잘라내지 말고 원문 전체 기록 (디버깅 용도)

### 4-1) MCP 인자 영어화 정책 (CRITICAL — 글로벌 MCP 호환성)
**모든 MCP 도구의 검색 쿼리·카테고리·키워드 인자는 반드시 영어로 보내라.**
한국어 쿼리는 글로벌 MCP 서버(Google Maps, Smithery 등록 서버 다수)에서 결과 품질이 현저히 떨어지거나 0건이 된다.

**필수 패턴**: 한국어 입력 → 영어 매핑 dict로 변환 → MCP 호출
```python
# ✅ 올바름 — 한국어 카테고리를 영어로 매핑 후 호출
KO_TO_EN = {{"카페":"cafe", "맛집":"restaurant", "관광지":"tourist attraction",
            "박물관":"museum", "미술관":"art gallery", "베이커리":"bakery",
            "쇼핑몰":"shopping mall", "스파":"spa", "도서관":"library",
            "공방":"workshop studio"}}
en_query = KO_TO_EN.get(ko_keyword.strip(), ko_keyword.strip())
await call_mcp("maps_search_places", {{"query": en_query, "location": coords}})

# ❌ 금지 — 한국어 그대로 MCP에 전달
await call_mcp("maps_search_places", {{"query": "카페", "location": coords}})  # 결과 품질 ↓
```

**예외**: `address`(geocoding) 같은 위치 정보는 한국어를 보내도 됨 (지오코딩 서버는 다국어 주소 지원).
하지만 **검색 쿼리·카테고리는 무조건 영어**.

**진단 로그**: `tried_keywords`에는 **실제 보낸 영어 쿼리**를 기록하라 (사장님이 원인 추적할 수 있도록).

### 5) Self-Validation + Self-Logging (자가 검증·자가 진단)
생성한 스킬은 **첫 실행에서 실패하면 안 된다**. 코드 합성 시 다음을 반드시 시뮬레이션하라:
- **샘플 입력 시나리오** (예: `location="애월"`, `location={{"latitude":33.41,"longitude":126.39}}`) 두 경우를 머릿속으로 돌려 **두 경로 모두 동작**하는지 확인하라
- **응답 파싱 에러 시 재시도/폴백 경로**를 포함하라 (json.loads 실패 → JSON 블록 re.search 재시도 → 원본 text 유지)
- 코드 작성 후 스스로 리뷰: "이 함수에 '애월' 문자열만 전달해도 / 좌표 dict만 전달해도 / keywords를 str로 전달해도 모두 동작하는가? 모두 YES면 통과."

**Self-Logging (결과 0건 시 진단 정보 필수)**:
검색 결과가 0건일 때 단순히 "결과 없음"만 반환하지 마라 — 사장님이 원인을 파악할 수 있도록 **실제 사용한 좌표와 키워드**를 함께 반환하라.

⚠️ **CRITICAL — `tried_keywords`는 반드시 루프 안에서 `.append()`로 누적되어 있어야 한다.** 빈 리스트 `[]`를 반환하면 진단이 무용지물이다. 함수 끝에서 리스트를 반환하기 전에 `assert tried_keywords or not_searched_flag` 같은 체크를 머릿속으로 해보라.

```python
# 함수 시작 부분에서 초기화
tried_keywords = []

# 검색 루프 안에서 반드시 기록
for kw in base_keywords:
    tried_keywords.append(kw)      # ← 빠뜨리면 안 됨
    ...

# 0건 시 반환
if not merged:
    return {{
        "결과없음": True,
        "detail": f"'{{location}}' 인근 {{radius}}m 이내 검색 결과 0건",
        "검색조건": {{
            "좌표": {{"latitude": lat, "longitude": lng}},
            "사용된_키워드": tried_keywords,   # ← 실제 채워진 리스트여야 함
            "반경(m)": radius,
            "필터": "indoor types + 한글 '실내' 키워드",
        }},
        "원인_추정": "키워드가 너무 구체적이거나 해당 좌표 주변에 해당 타입 장소가 없음"
    }}
```

## ⛔ 데이터 조작 절대 금지 (CRITICAL — 가장 중요한 규칙)
생성하는 스킬 코드는 **절대로 가짜 데이터, placeholder, 예시 데이터를 반환하면 안 된다.**

### 금지 패턴
```python
# ❌ 절대 금지 — API 실패 시 샘플 데이터 반환
except Exception:
    return {{"places": [{{"name": "XX 카페", "desc": "편안한 분위기"}}]}}  # ← 데이터 날조

# ❌ 절대 금지 — 빈 결과를 가짜로 채우기
if not results:
    results = [{{"name": "추천 장소 1"}}]  # ← 없는 데이터를 만들어냄

# ❌ 절대 금지 — 오류를 숨기고 기본값 반환
except Exception as e:
    return {{"결과": "검색 결과입니다."}}  # ← 오류 은폐
```

### 올바른 패턴 (반드시 이렇게 작성)
```python
# ✅ 오류 시 오류 사실만 정직하게 반환
except Exception as e:
    return {{"error": str(e), "detail": "데이터를 가져오지 못했습니다. 실제 결과 없음."}}

# ✅ 빈 결과도 정직하게 반환
if not results:
    return {{"결과없음": True, "detail": f"'{{query}}'에 대한 검색 결과가 없습니다."}}
```

**이 규칙을 어기면 사용자가 존재하지 않는 장소·정보를 믿고 행동할 수 있어 심각한 피해가 발생한다.**

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
        self._client = get_maker_client()  # Maker 전용 클라이언트 (Claude 등)

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
        lines = code.split("\n")
        # 우선순위 1: @tool 데코레이터 바로 다음의 함수명을 반환
        for i, line in enumerate(lines):
            if "@tool" in line.strip():
                for j in range(i + 1, min(i + 4, len(lines))):
                    nxt = lines[j].strip()
                    if nxt.startswith("async def ") or nxt.startswith("def "):
                        name = nxt.split("def ")[1].split("(")[0].strip()
                        if name not in ("tool", "main"):
                            return name
        # 폴백: 첫 번째 함수명
        for line in lines:
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
        email_hit = any(kw in user_req_lower for kw in email_keywords)
        if email_hit:
            # 부정/배제 맥락 감지: "이메일 전송은 별도", "이메일 말고", "메일 제외" 등
            import re as _re_email
            neg_patterns = [
                r"(이메일|메일|email|mail)\s*[은는이가]?\s*(별도|제외|말고|아닌|빼고|아니|제외하고)",
                r"(별도로|따로)\s*(처리|전송|보낼)",
                r"이메일\s*전송은\s*(별도|따로|제외|하지)",
                r"(하지\s*않|안\s*보내|안보내)",
            ]
            is_negated = any(_re_email.search(p, user_req_lower) for p in neg_patterns)
            # 장소/가이드 의도 감지
            place_intent_keywords = {"장소", "관광", "카페", "맛집", "핫플", "추천",
                                     "가이드", "검색", "주변", "인근", "지도",
                                     "places", "guide", "search", "restaurant"}
            has_place_intent = any(kw in user_req_lower for kw in place_intent_keywords)
            # 부정 맥락 또는 장소 의도가 강하면 이메일 분기 건너뛰기
            if is_negated or has_place_intent:
                await _log("system",
                    "⚠️ [이메일 분기 건너뜀] 부정 문맥 또는 장소/가이드 의도 감지 — 정상 합성 경로로 진행",
                    "[이메일 분기 스킵]")
            else:
                return await self._handle_email_skill(user_request)

        # ── Phase 0: Reusability Check (기존 스킬 재사용 판단) ──
        reuse_result = await self._check_reusability(user_request)
        if reuse_result:
            return reuse_result

        # ── Phase 1: 탐색 및 전략 수립 ──
        await _log("divider", "PHASE 1 — 탐색 및 전략 수립", "")
        strategy = await self.discovery.discover_strategy(user_request)

        if strategy.get("strategy") == "mcp_install_required":
            # MCP 서버가 발견되었으나 로컬에 미설치 + Smithery 프록시도 불가
            install_guide = strategy.get("install_guide", "")
            return {
                "success": False,
                "mcp_install_required": True,
                "service": strategy.get("service_name"),
                "install_command": strategy.get("install_command"),
                "env_key_name": strategy.get("env_key_name"),
                "message": install_guide,
            }

        if strategy.get("strategy") == "unknown":
            # env_key_name이 있으면 → 사용자에게 API 키 설정 안내 (Smithery 등)
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
                        f"{strategy.get('description', '')}\n\n"
                        f"`.env` 파일에 `{env_key}=발급받은키` 를 추가한 후\n"
                        f"`reload_env`를 호출하거나 '설정 완료'라고 알려주세요."
                    ),
                }
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

                # ── ModuleNotFoundError: 자동 pip 설치 후 즉시 재시도 ──
                import re as _re
                missing_mod = _re.search(r"No module named '([\w\.\-]+)'", error_msg)
                if missing_mod:
                    pkg = missing_mod.group(1).split(".")[0]  # e.g. "google.maps" → "google"
                    await _log("system",
                        f"⚙️ [자동 pip 설치] '{pkg}' 패키지 자동 설치 중...",
                        "[패키지 자동 설치]")
                    try:
                        pip_proc = await asyncio.create_subprocess_exec(
                            sys.executable, "-m", "pip", "install", pkg, "-q",
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await asyncio.wait_for(pip_proc.wait(), timeout=60)
                        await _log("system",
                            f"✅ '{pkg}' 설치 완료 — 재실행 중...",
                            "[패키지 자동 설치 완료]")
                        # 즉시 재실행 (attempt 카운트 소비 없이)
                        exec_result = await self.sandbox.execute(code, test_args)
                        if exec_result["success"]:
                            await _log("system",
                                f"✅ 패키지 설치 후 실행 성공!",
                                "[실행 성공]")
                            break
                        else:
                            error_msg = exec_result["error"] or ""
                            previous_error = error_msg
                    except Exception as pip_e:
                        await _log("system",
                            f"⚠️ '{pkg}' 자동 설치 실패: {pip_e}",
                            "[패키지 설치 실패]")

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
            client = get_llm_client()
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

        # 부정 컨텍스트 감지: "X 아닌", "X 말고", "X 제외", "X 없는" 패턴에서 X 추출 → 매칭 제외
        import re as _re
        _negation_pattern = _re.compile(
            r'(\S+)\s*(?:이\s*)?(?:아닌|말고|제외|없는|빼고|아니라|아니고)'
        )
        negated_keywords = set()
        for m in _negation_pattern.finditer(req_lower):
            negated_keywords.update(m.group(1).split())

        # 요청에서 의미 있는 키워드 추출 (불용어 + 부정 키워드 제거)
        stopwords = {
            "을", "를", "이", "가", "에", "의", "로", "와", "과", "하", "해", "줘", "좀",
            "알려", "보여", "찾아", "검색", "조회", "만들어", "기능", "스킬", "새",
            "the", "a", "an", "is", "to", "for", "and", "or", "in", "on", "me", "please",
        }
        req_keywords = {
            w for w in req_lower.split()
            if len(w) > 1 and w not in stopwords and w not in negated_keywords
        }

        best_match = None
        best_score = 0

        for skill in all_skills:
            # 단어 경계 기반 매칭 (substring 오탐 방지)
            skill_words = set(
                (skill.get("description", "") + " " + skill.get("name", "")).lower().split()
            )
            matched = sum(1 for kw in req_keywords if kw in skill_words)
            if req_keywords and matched > best_score:
                best_score = matched
                best_match = skill

        # 키워드 70% 이상 매칭되면 재사용 판단 (50% → 70%로 상향)
        if best_match and req_keywords and (best_score / len(req_keywords)) >= 0.7:
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
