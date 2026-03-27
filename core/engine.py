"""
engine.py — 메인 에이전트 루프 & Webhook 서버 + 실시간 로그 대시보드

Trigger → Perceive → Reason → Act → Log 사이클을 관리.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse
from dotenv import load_dotenv

from core.brain import Brain
from core.executor import execute, build_function_schemas
from core.memory import Memory
from core.onboarding_manager import OnboardingManager

# Tool 모듈을 import하여 @tool 데코레이터가 실행되도록 함
import tools.site_a_api  # noqa: F401
import tools.site_b_api  # noqa: F401
import tools.telco_auth_api  # noqa: F401

load_dotenv()

# ── 로깅 설정 ────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("engine")

# ── FastAPI 앱 ───────────────────────────────────────

app = FastAPI(title="Universal Agent Engine", version="0.2.0")
onboarding_mgr = OnboardingManager()


@app.on_event("startup")
async def startup_onboarding():
    """엔진 시작 시 온보딩 상태 확인."""
    if onboarding_mgr.needs_onboarding():
        logger.info("온보딩 필요 — RSA Keypair 생성 및 통신사 앱에 승인 요청")
        public_key_pem = onboarding_mgr.generate_keypair()
        # 통신사 앱(Mock)에 대기 정보 전송
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    "http://localhost:8004/pending",
                    json={
                        "public_key": public_key_pem,
                        "requested_policies": ["booking_sync"],
                    },
                    timeout=5.0,
                )
            logger.info("통신사 앱에 승인 요청 전송 완료 — http://localhost:8004 에서 승인 대기 중")
        except Exception as e:
            logger.warning(f"통신사 앱 연결 실패: {e} — 수동 온보딩 필요")
    else:
        agent_id = onboarding_mgr.get_agent_id()
        logger.info(f"온보딩 완료 상태 — Agent ID: {agent_id}")

# ── SSE 로그 브로드캐스터 ─────────────────────────────

class LogBroadcaster:
    """연결된 모든 SSE 클라이언트에 로그를 브로드캐스트."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []
        self._history: list[dict] = []  # 최근 로그 보관

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.remove(q)

    async def emit(self, log_type: str, content: Any, meta: str = "") -> None:
        entry = {
            "timestamp": datetime.utcnow().strftime("%H:%M:%S.%f")[:-3],
            "type": log_type,
            "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=2),
            "meta": meta,
        }
        self._history.append(entry)
        # 최대 200개 보관
        if len(self._history) > 200:
            self._history = self._history[-200:]
        for q in self._subscribers:
            await q.put(entry)

    def get_history(self) -> list[dict]:
        return list(self._history)


broadcaster = LogBroadcaster()

# ── Scene 로드 ───────────────────────────────────────

SCENE_PATH = os.getenv(
    "SCENE_PATH",
    str(Path(__file__).resolve().parent.parent / "scenes" / "hotel_sync_scene.md"),
)


def load_scene(path: str) -> str:
    """Scene(.md) 파일을 읽어 system prompt 문자열로 반환."""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ── 메인 에이전트 루프 ───────────────────────────────

MAX_LOOP_ITERATIONS = 10  # 무한 루프 방지


async def run_agent_loop(trigger_event: dict) -> dict:
    """
    하나의 트리거 이벤트에 대해 Perceive → Reason → Act 루프를 실행.
    """
    # ── Phase 1: Scene 로드 ──
    await broadcaster.emit("divider", "PHASE 1 — Scene 로드", "")
    await broadcaster.emit("system", "📄 Scene 파일 로드 중...", "⚙️ 초기화")
    system_prompt = load_scene(SCENE_PATH)
    scene_preview = system_prompt[:500] + ("..." if len(system_prompt) > 500 else "")
    await broadcaster.emit("scene", scene_preview, "📜 Scene → System Prompt")

    # ── Phase 2: Tool 스캐닝 ──
    await broadcaster.emit("divider", "PHASE 2 — Tool 등록", "")
    await broadcaster.emit("system", "🔧 Tool 모듈 스캐닝 중...", "⚙️ 초기화")
    tool_schemas = build_function_schemas()
    tool_names = [s['function']['name'] for s in tool_schemas]

    for schema in tool_schemas:
        func_info = schema["function"]
        params = func_info.get("parameters", {}).get("properties", {})
        param_list = ", ".join(f"{k}: {v.get('type', '?')}" for k, v in params.items())
        await broadcaster.emit(
            "tool_schema",
            f"{func_info['name']}({param_list})\n→ {func_info.get('description', '')}",
            "🔧 Tool 등록"
        )

    # ── Phase 3: Brain 초기화 ──
    await broadcaster.emit("divider", "PHASE 3 — Brain(LLM) 초기화", "")
    await broadcaster.emit(
        "system",
        f"Brain 초기화 완료\n• System Prompt: Scene 규칙 ({len(system_prompt)}자)\n• Tools: {tool_names} ({len(tool_schemas)}개)\n• Model: {os.getenv('OPENAI_MODEL', 'gpt-4o')}",
        "🧠 Brain 초기화"
    )
    brain = Brain(system_prompt=system_prompt, tools_schema=tool_schemas)
    memory = Memory()

    logger.info(f"Agent loop started. Tools: {tool_names}")

    # ── Phase 4: Webhook 이벤트 수신 ──
    await broadcaster.emit("divider", "PHASE 4 — Webhook 이벤트 처리", "")
    trigger_text = (
        f"다음 이벤트가 발생했습니다. 적절한 조치를 취해주세요.\n\n"
        f"```json\n{json.dumps(trigger_event, ensure_ascii=False, indent=2)}\n```"
    )
    brain.add_user_message(trigger_text)
    memory.add_event("trigger", trigger_event)
    await broadcaster.emit("webhook", trigger_event, "📩 [SITE A] Webhook 수신")
    await broadcaster.emit(
        "system",
        "LLM에게 전달되는 메시지 구조:\n"
        "  [1] system: Scene 규칙 (역할·목표·제약조건)\n"
        "  [2] tools:  사용 가능한 함수 스키마 7개\n"
        "  [3] user:   트리거 이벤트 (Webhook 데이터)",
        "📤 [AGENT C] LLM 요청 구성"
    )

    # ── Phase 5: Reason → Act 루프 ──
    await broadcaster.emit("divider", "PHASE 5 — Reason → Act 루프", "")
    final_summary = ""
    for iteration in range(MAX_LOOP_ITERATIONS):
        logger.info(f"── Loop iteration {iteration + 1} ──")
        await broadcaster.emit("loop", f"── 루프 반복 {iteration + 1}/{MAX_LOOP_ITERATIONS} ──", "🔄 Iteration")

        # Brain에게 생각 요청
        await broadcaster.emit("thinking", "LLM에게 판단 요청 중...", "🧠 [AGENT C] Brain")
        response = await brain.think()
        logger.info(f"Brain response type: {response['type']}")

        if response["type"] == "text":
            # LLM이 텍스트로 응답 → 루프 종료
            final_summary = response["content"]
            memory.add_event("assistant", final_summary)
            logger.info(f"Agent completed: {final_summary[:100]}...")
            await broadcaster.emit("complete", final_summary, "✅ 작업 완료")
            break

        elif response["type"] == "tool_calls":
            # LLM 판단 결과 로그
            call_names = [tc["name"] for tc in response["content"]]
            await broadcaster.emit("reasoning", f"LLM 판단: {call_names} 호출 필요", "💡 [AGENT C] Reasoning")

            # 각 Tool 호출 실행
            for tool_call in response["content"]:
                tool_name = tool_call["name"]
                tool_args = tool_call["arguments"]
                tool_call_id = tool_call["id"]

                # 역할 라벨 결정
                if "telco" in tool_name or "auth" in tool_name:
                    role_label = "[TELCO D]"
                elif "site_b" in tool_name:
                    role_label = "[SITE B]"
                elif "site_a" in tool_name:
                    role_label = "[SITE A]"
                else:
                    role_label = "[AGENT C]"

                logger.info(f"Executing tool: {tool_name}({json.dumps(tool_args, ensure_ascii=False)})")
                memory.add_event("tool_call", {"name": tool_name, "arguments": tool_args})
                await broadcaster.emit(
                    "tool_call",
                    json.dumps({"function": tool_name, "arguments": tool_args}, ensure_ascii=False, indent=2),
                    f"🔧 {role_label} Tool 호출: {tool_name}"
                )

                # Tool 실행
                result = await execute(tool_name, tool_args)
                result_str = json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else str(result)
                logger.info(f"Tool result: {result_str}")
                memory.add_event("tool_result", {"name": tool_name, "result": result})

                # 토큰 발급 시 정책 하이라이트
                if isinstance(result, dict) and result.get("policy_matched"):
                    await broadcaster.emit(
                        "policy",
                        f"✅ 자동 승인 사유: 사장님 사전 설정 정책 [{result['policy_matched']}] 적용됨",
                        f"📜 [TELCO D] Policy Check"
                    )
                if isinstance(result, dict) and result.get("vpal_session_id"):
                    await broadcaster.emit(
                        "vpal",
                        f"🔒 VPAL 세션 할당: {result['vpal_session_id'][:12]}... (Private Slice 터널 활성화)",
                        f"🌐 [TELCO D] Network Scan"
                    )
                if isinstance(result, dict) and result.get("token"):
                    await broadcaster.emit(
                        "signature",
                        f"✍️ RS256 디지털 서명 토큰 발행 (5분 유효)",
                        f"🔐 [TELCO D] Signature Issuance"
                    )

                await broadcaster.emit("tool_result", result_str, f"📋 {role_label} Tool 결과: {tool_name}")

                # 결과를 Brain에 반환
                brain.add_tool_result(tool_call_id, result)
    else:
        final_summary = "최대 반복 횟수에 도달하여 루프가 종료되었습니다."
        memory.add_event("system", final_summary)
        logger.warning(final_summary)
        await broadcaster.emit("error", final_summary, "⚠️ 타임아웃")

    return {
        "status": "completed",
        "summary": final_summary,
        "history": memory.get_history(),
    }


# ── API 엔드포인트 ───────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Carrier-Grade Trust 실시간 로그 대시보드."""
    html = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Carrier-Grade Trust Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'JetBrains Mono', 'Fira Code', 'SF Mono', monospace;
            background: #0a0e17;
            color: #c9d1d9;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        .header {
            background: linear-gradient(135deg, #0d1117 0%, #161b22 100%);
            border-bottom: 1px solid #21262d;
            padding: 1rem 2rem;
            display: flex; align-items: center; justify-content: space-between;
        }
        .header h1 {
            font-size: 1.2rem; font-weight: 600;
            background: linear-gradient(90deg, #f59e0b, #ef4444, #bc8cff);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }
        .header .status { display: flex; align-items: center; gap: 0.5rem; font-size: 0.8rem; color: #8b949e; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; background: #3fb950; animation: pulse 2s ease-in-out infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

        /* ── Network Topology ── */
        .topology {
            background: #0d1117; border-bottom: 1px solid #21262d;
            padding: 1rem 2rem; font-size: 0.7rem;
        }
        .topo-container { display: flex; align-items: stretch; gap: 0; max-width: 100%; }
        .topo-zone {
            padding: 0.6rem 0.8rem; border-radius: 8px; position: relative;
            display: flex; flex-direction: column; align-items: center; gap: 0.3rem;
        }
        .topo-zone .zone-label {
            font-size: 0.6rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em;
            margin-bottom: 0.2rem;
        }
        .topo-node {
            padding: 0.3rem 0.6rem; border-radius: 6px; font-weight: 600; font-size: 0.7rem;
            display: flex; align-items: center; gap: 0.3rem;
        }

        .zone-public {
            background: rgba(248,81,73,0.06); border: 1px dashed rgba(248,81,73,0.2);
        }
        .zone-public .zone-label { color: #f85149; }
        .zone-public .topo-node { background: rgba(248,81,73,0.1); color: #f85149; border: 1px solid rgba(248,81,73,0.2); }

        .topo-arrow {
            display: flex; align-items: center; color: #484f58; font-size: 0.65rem;
            padding: 0 0.4rem; flex-direction: column; gap: 0.15rem;
        }
        .arrow-line { color: #3fb950; font-weight: 700; font-size: 0.8rem; }
        .arrow-label { font-size: 0.55rem; color: #8b949e; }

        .zone-private {
            background: rgba(34,197,94,0.04); border: 1px solid rgba(34,197,94,0.15);
            flex: 1;
        }
        .zone-private .zone-label { color: #3fb950; }
        .zone-private .topo-node { background: rgba(34,197,94,0.08); color: #3fb950; border: 1px solid rgba(34,197,94,0.15); }
        .zone-private .inner-row { display: flex; gap: 0.5rem; align-items: center; }
        .topo-vpal {
            display: flex; align-items: center; gap: 0.3rem;
            padding: 0.15rem 0.5rem; border-radius: 10px;
            background: rgba(56,189,248,0.08); border: 1px solid rgba(56,189,248,0.2);
            color: #38bdf8; font-size: 0.55rem; font-weight: 600;
        }
        @keyframes dataFlow {
            0% { opacity: 0.3; } 50% { opacity: 1; } 100% { opacity: 0.3; }
        }
        .flow-dot {
            width: 4px; height: 4px; border-radius: 50%; background: #3fb950;
            animation: dataFlow 1.5s ease-in-out infinite;
        }
        .flow-dot:nth-child(2) { animation-delay: 0.3s; }
        .flow-dot:nth-child(3) { animation-delay: 0.6s; }

        /* ── Main Layout ── */
        .main-content { display: flex; flex: 1; overflow: hidden; }
        .log-panel { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
        .log-header {
            padding: 0.5rem 1.5rem; background: #0d1117; border-bottom: 1px solid #21262d;
            display: flex; justify-content: space-between; align-items: center;
            font-size: 0.75rem; color: #8b949e;
        }
        .log-container { flex: 1; overflow-y: auto; padding: 0.5rem 1.5rem; }

        .log-entry {
            display: flex; gap: 0.5rem; padding: 0.35rem 0.6rem; margin-bottom: 0.15rem;
            border-radius: 6px; font-size: 0.78rem; line-height: 1.5;
            animation: fadeIn 0.3s ease-out; border-left: 3px solid transparent;
        }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; } }
        .log-entry:hover { background: rgba(255,255,255,0.03); }
        .log-time { color: #484f58; white-space: nowrap; min-width: 72px; flex-shrink: 0; font-size: 0.7rem; }
        .log-meta { white-space: nowrap; min-width: 220px; flex-shrink: 0; font-weight: 600; font-size: 0.75rem; }
        .log-content { flex: 1; white-space: pre-wrap; word-break: break-word; }

        /* Role colors */
        .log-entry.webhook    { border-left-color: #d29922; } .log-entry.webhook .log-meta { color: #d29922; }
        .log-entry.system     { border-left-color: #58a6ff; } .log-entry.system .log-meta { color: #58a6ff; }
        .log-entry.thinking   { border-left-color: #bc8cff; } .log-entry.thinking .log-meta { color: #bc8cff; }
        .log-entry.reasoning  { border-left-color: #d2a8ff; } .log-entry.reasoning .log-meta { color: #d2a8ff; }
        .log-entry.tool_call  { border-left-color: #79c0ff; } .log-entry.tool_call .log-meta { color: #79c0ff; }
        .log-entry.tool_result { border-left-color: #56d364; } .log-entry.tool_result .log-meta { color: #56d364; }
        .log-entry.complete   { border-left-color: #3fb950; background: rgba(63,185,80,0.08); } .log-entry.complete .log-meta { color: #3fb950; }
        .log-entry.error      { border-left-color: #f85149; background: rgba(248,81,73,0.08); } .log-entry.error .log-meta { color: #f85149; }
        .log-entry.loop       { border-left-color: #8b949e; background: rgba(139,148,158,0.03); } .log-entry.loop .log-meta { color: #8b949e; }
        .log-entry.scene      { border-left-color: #f0883e; background: rgba(240,136,62,0.04); } .log-entry.scene .log-meta { color: #f0883e; } .log-entry.scene .log-content { font-size: 0.7rem; color: #8b949e; }
        .log-entry.tool_schema { border-left-color: #39d353; background: rgba(57,211,83,0.03); } .log-entry.tool_schema .log-meta { color: #39d353; }

        /* Telco highlight events */
        .log-entry.policy {
            border-left-color: #f59e0b; background: rgba(245,158,11,0.1);
            border: 1px solid rgba(245,158,11,0.15); border-left: 3px solid #f59e0b;
        }
        .log-entry.policy .log-meta { color: #f59e0b; font-weight: 700; }
        .log-entry.policy .log-content { color: #fbbf24; font-weight: 600; }

        .log-entry.vpal {
            border-left-color: #38bdf8; background: rgba(56,189,248,0.08);
            border: 1px solid rgba(56,189,248,0.12); border-left: 3px solid #38bdf8;
        }
        .log-entry.vpal .log-meta { color: #38bdf8; font-weight: 700; }
        .log-entry.vpal .log-content { color: #7dd3fc; }

        .log-entry.signature {
            border-left-color: #a78bfa; background: rgba(167,139,250,0.08);
            border: 1px solid rgba(167,139,250,0.12); border-left: 3px solid #a78bfa;
        }
        .log-entry.signature .log-meta { color: #a78bfa; font-weight: 700; }
        .log-entry.signature .log-content { color: #c4b5fd; }

        .log-entry.divider {
            border-left: none; border-top: 1px solid #30363d;
            margin-top: 0.8rem; margin-bottom: 0.3rem; padding-top: 0.6rem;
        }
        .log-entry.divider .log-meta, .log-entry.divider .log-time { display: none; }
        .log-entry.divider .log-content { color: #58a6ff; font-weight: 700; font-size: 0.8rem; letter-spacing: 0.05em; }

        .empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 60vh; color: #484f58; gap: 1rem; }
        .empty-state .icon { font-size: 3rem; }

        /* ── Notary Table ── */
        .notary-panel {
            background: #0d1117; border-top: 1px solid #21262d;
            max-height: 240px; overflow-y: auto; padding: 0;
        }
        .notary-header {
            padding: 0.6rem 1.5rem; font-size: 0.8rem; font-weight: 700;
            color: #f59e0b; display: flex; align-items: center; gap: 0.5rem;
            border-bottom: 1px solid #21262d; background: rgba(245,158,11,0.03);
            position: sticky; top: 0; z-index: 2;
        }
        .notary-table { width: 100%; border-collapse: collapse; }
        .notary-table th {
            padding: 0.4rem 0.8rem; text-align: left; font-size: 0.65rem;
            color: #8b949e; text-transform: uppercase; font-weight: 600;
            border-bottom: 1px solid #21262d; background: #0d1117;
            position: sticky; top: 34px; z-index: 1;
        }
        .notary-table td {
            padding: 0.35rem 0.8rem; font-size: 0.72rem;
            border-bottom: 1px solid rgba(33,38,45,0.5);
        }
        .notary-table tr:hover { background: rgba(255,255,255,0.02); }
        .notary-badge {
            padding: 0.1rem 0.4rem; border-radius: 8px; font-size: 0.6rem; font-weight: 600;
        }
        .badge-issued { background: rgba(34,197,94,0.15); color: #22c55e; }
        .badge-registered { background: rgba(88,166,255,0.15); color: #58a6ff; }
        .badge-denied { background: rgba(248,81,73,0.15); color: #f85149; }
        .badge-isolated { background: rgba(248,81,73,0.2); color: #f85149; }
        .badge-reactivated { background: rgba(34,197,94,0.2); color: #22c55e; }

        .footer {
            background: #0d1117; border-top: 1px solid #21262d;
            padding: 0.4rem 2rem; font-size: 0.65rem; color: #484f58;
            display: flex; justify-content: space-between;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🔐 Carrier-Grade Trust Dashboard</h1>
        <div class="status">
            <div class="status-dot" id="statusDot"></div>
            <span id="statusText">대기 중</span>
        </div>
    </div>

    <!-- Network Topology -->
    <div class="topology">
        <div class="topo-container">
            <div class="topo-zone zone-public">
                <span class="zone-label">☁️ Public Internet</span>
                <div class="topo-node">🏨 Site A :8001</div>
                <div class="topo-node">🧠 Agent C :8000</div>
            </div>
            <div class="topo-arrow">
                <span class="arrow-label">VPAL</span>
                <span class="arrow-line">═══►</span>
                <div style="display:flex;gap:2px;">
                    <div class="flow-dot"></div>
                    <div class="flow-dot"></div>
                    <div class="flow-dot"></div>
                </div>
            </div>
            <div class="topo-zone zone-private">
                <span class="zone-label">🔒 SKT Telco Private Slice (MEC Edge Zone)</span>
                <div class="inner-row">
                    <div class="topo-node">🔐 Telco D :8003</div>
                    <div class="topo-vpal">
                        <span>VPAL Tunnel</span>
                        <div style="display:flex;gap:2px;">
                            <div class="flow-dot"></div><div class="flow-dot"></div><div class="flow-dot"></div>
                        </div>
                    </div>
                    <div class="topo-node">📅 Site B :8002</div>
                </div>
            </div>
        </div>
    </div>

    <!-- Log Panel -->
    <div class="main-content">
        <div class="log-panel">
            <div class="log-header">
                <span>🔍 실시간 Semantic Log</span>
                <span><span id="logCount">0</span>건</span>
            </div>
            <div class="log-container" id="logContainer">
                <div class="empty-state" id="emptyState">
                    <div class="icon">📡</div>
                    <p>Webhook 이벤트 대기 중...</p>
                    <p style="font-size:0.72rem;">Site A에서 예약을 생성하면 [SITE A] → [AGENT C] → [TELCO D] → [SITE B] 흐름이 표시됩니다</p>
                </div>
            </div>
        </div>
    </div>

    <!-- Carrier Notary Table -->
    <div class="notary-panel" id="notaryPanel">
        <div class="notary-header">📜 Carrier Notary — 공증 기록 (실시간)</div>
        <table class="notary-table">
            <thead>
                <tr>
                    <th>타임스탬프</th>
                    <th>이벤트</th>
                    <th>인증 방식</th>
                    <th>Agent</th>
                    <th>정책/사유</th>
                    <th>서명 유효성</th>
                    <th>결과</th>
                </tr>
            </thead>
            <tbody id="notaryBody">
                <tr><td colspan="7" style="text-align:center;color:#484f58;padding:1rem;">공증 기록 없음</td></tr>
            </tbody>
        </table>
    </div>

    <div class="footer">
        <span>Carrier-Grade Trust v0.2.0 · RS256 + VPAL + Kill-switch</span>
        <span id="clock"></span>
    </div>

    <script>
        const container = document.getElementById('logContainer');
        const emptyState = document.getElementById('emptyState');
        const logCount = document.getElementById('logCount');
        const statusText = document.getElementById('statusText');
        const statusDot = document.getElementById('statusDot');
        const clock = document.getElementById('clock');
        const notaryBody = document.getElementById('notaryBody');
        let count = 0;

        function updateClock() { clock.textContent = new Date().toLocaleTimeString('ko-KR'); }
        setInterval(updateClock, 1000); updateClock();

        function addLog(entry) {
            if (emptyState) emptyState.style.display = 'none';
            count++; logCount.textContent = count;
            const div = document.createElement('div');
            div.className = 'log-entry ' + entry.type;
            div.innerHTML =
                '<span class="log-time">' + entry.timestamp + '</span>' +
                '<span class="log-meta">' + escapeHtml(entry.meta) + '</span>' +
                '<span class="log-content">' + escapeHtml(entry.content) + '</span>';
            container.appendChild(div);
            container.scrollTop = container.scrollHeight;

            if (entry.type === 'webhook') { statusText.textContent = '[SITE A] 이벤트 수신'; statusDot.style.background = '#d29922'; }
            else if (entry.type === 'thinking') { statusText.textContent = '[AGENT C] LLM 판단 중'; statusDot.style.background = '#bc8cff'; }
            else if (entry.type === 'policy') { statusText.textContent = '[TELCO D] Policy Check'; statusDot.style.background = '#f59e0b'; }
            else if (entry.type === 'vpal') { statusText.textContent = '[TELCO D] VPAL 세션 할당'; statusDot.style.background = '#38bdf8'; }
            else if (entry.type === 'signature') { statusText.textContent = '[TELCO D] 서명 발행'; statusDot.style.background = '#a78bfa'; }
            else if (entry.type === 'tool_call') { statusText.textContent = 'Tool 실행 중'; statusDot.style.background = '#79c0ff'; }
            else if (entry.type === 'complete') { statusText.textContent = '대기 중'; statusDot.style.background = '#3fb950'; }
            else if (entry.type === 'error') { statusText.textContent = '에러 발생'; statusDot.style.background = '#f85149'; }
        }

        function escapeHtml(t) { const d = document.createElement('div'); d.textContent = t; return d.innerHTML; }

        // SSE
        const es = new EventSource('/logs/stream');
        es.onmessage = (e) => { addLog(JSON.parse(e.data)); };
        es.onerror = () => { statusText.textContent = '연결 끊김 — 재연결 중...'; statusDot.style.background = '#f85149'; };
        fetch('/logs/history').then(r => r.json()).then(logs => logs.forEach(addLog));

        // Notary polling
        function loadNotary() {
            fetch('http://localhost:8003/notary')
                .then(r => r.json())
                .then(data => {
                    const records = data.records || [];
                    if (records.length === 0) return;
                    let html = '';
                    records.slice().reverse().forEach(r => {
                        const ts = (r.timestamp || '').substring(0, 19).replace('T', ' ');
                        const evt = r.event || '';
                        let badgeClass = 'badge-issued';
                        if (evt.includes('denied') || evt.includes('isolated')) badgeClass = 'badge-denied';
                        else if (evt.includes('registered')) badgeClass = 'badge-registered';
                        else if (evt.includes('reactivated')) badgeClass = 'badge-reactivated';

                        const authMethod = evt === 'agent_registered' ? 'USIM PIN' : 'Policy 자동';
                        const policy = r.policy_matched || r.policies || r.reason || '-';
                        const sigValid = (evt === 'token_issued' || evt === 'agent_registered') ? '✅ Valid' : (evt.includes('denied') ? '❌ N/A' : '–');
                        const result = (evt === 'token_issued') ? '🟢 발급' :
                                       (evt === 'agent_registered') ? '🔵 등록' :
                                       (evt.includes('isolated')) ? '🔴 격리' :
                                       (evt.includes('reactivated')) ? '🟢 복구' :
                                       (evt.includes('denied')) ? '🔴 거부' : '–';

                        html += '<tr>' +
                            '<td style="color:#64748b;">' + ts + '</td>' +
                            '<td><span class="notary-badge ' + badgeClass + '">' + evt + '</span></td>' +
                            '<td>' + authMethod + '</td>' +
                            '<td>' + (r.agent_id || '-') + '</td>' +
                            '<td style="color:#94a3b8;">' + policy + '</td>' +
                            '<td>' + sigValid + '</td>' +
                            '<td>' + result + '</td></tr>';
                    });
                    notaryBody.innerHTML = html;
                })
                .catch(() => {});
        }
        loadNotary();
        setInterval(loadNotary, 3000);
    </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/logs/stream")
async def log_stream(request: Request):
    """SSE로 실시간 로그를 스트리밍."""
    queue = broadcaster.subscribe()

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {"data": json.dumps(entry, ensure_ascii=False)}
                except asyncio.TimeoutError:
                    # 연결 유지용 keep-alive
                    yield {"comment": "keep-alive"}
        finally:
            broadcaster.unsubscribe(queue)

    return EventSourceResponse(event_generator())


@app.get("/logs/history")
async def log_history():
    """기존 로그 히스토리 반환."""
    return JSONResponse(content=broadcaster.get_history())


@app.post("/webhook")
async def webhook(request: Request):
    """사이트 A의 예약 이벤트를 수신하여 에이전트 루프를 트리거."""
    try:
        event = await request.json()
        logger.info(f"Webhook received: {json.dumps(event, ensure_ascii=False)}")

        result = await run_agent_loop(event)
        return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"Webhook processing failed: {e}", exc_info=True)
        await broadcaster.emit("error", str(e), "❌ 에러")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": str(e)},
        )


@app.post("/run")
async def manual_run(request: Request):
    """수동으로 이벤트를 전달하여 에이전트 루프 실행 (디버깅용)."""
    event = await request.json()
    result = await run_agent_loop(event)
    return JSONResponse(content=result)


@app.post("/onboarding/complete")
async def onboarding_complete(request: Request):
    """통신사 앱에서 승인 완료 후 위임장 수신."""
    data = await request.json()
    cert = data.get("delegation_certificate", {})
    telco_pub = data.get("telco_public_key", "")

    onboarding_mgr.save_certificate(cert, telco_pub)
    agent_id = cert.get("agent_id", "N/A")
    logger.info(f"온보딩 완료: Agent ID = {agent_id}")
    await broadcaster.emit("system", f"✅ 온보딩 완료! Agent ID: {agent_id}\n정책: {cert.get('policies', [])}", "🎉 온보딩")

    return {"status": "onboarded", "agent_id": agent_id}


@app.post("/onboarding/reset")
async def onboarding_reset():
    """온보딩 초기화 (디버깅용)."""
    onboarding_mgr.reset()
    return {"status": "reset"}
