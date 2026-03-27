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
    await broadcaster.emit("webhook", trigger_event, "📩 Webhook 수신")
    await broadcaster.emit(
        "system",
        "LLM에게 전달되는 메시지 구조:\n"
        "  [1] system: Scene 규칙 (역할·목표·제약조건)\n"
        "  [2] tools:  사용 가능한 함수 스키마 4개\n"
        "  [3] user:   트리거 이벤트 (위 Webhook 데이터)",
        "📤 LLM 요청 구성"
    )

    # ── Phase 5: Reason → Act 루프 ──
    await broadcaster.emit("divider", "PHASE 5 — Reason → Act 루프", "")
    final_summary = ""
    for iteration in range(MAX_LOOP_ITERATIONS):
        logger.info(f"── Loop iteration {iteration + 1} ──")
        await broadcaster.emit("loop", f"── 루프 반복 {iteration + 1}/{MAX_LOOP_ITERATIONS} ──", "🔄 Iteration")

        # Brain에게 생각 요청
        await broadcaster.emit("thinking", "LLM에게 판단 요청 중...", "🧠 Brain")
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
            await broadcaster.emit("reasoning", f"LLM 판단: {call_names} 호출 필요", "💡 Reasoning")

            # 각 Tool 호출 실행
            for tool_call in response["content"]:
                tool_name = tool_call["name"]
                tool_args = tool_call["arguments"]
                tool_call_id = tool_call["id"]

                logger.info(f"Executing tool: {tool_name}({json.dumps(tool_args, ensure_ascii=False)})")
                memory.add_event("tool_call", {"name": tool_name, "arguments": tool_args})
                await broadcaster.emit(
                    "tool_call",
                    json.dumps({"function": tool_name, "arguments": tool_args}, ensure_ascii=False, indent=2),
                    f"🔧 Tool 호출: {tool_name}"
                )

                # Tool 실행
                result = await execute(tool_name, tool_args)
                result_str = json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else str(result)
                logger.info(f"Tool result: {result_str}")
                memory.add_event("tool_result", {"name": tool_name, "result": result})
                await broadcaster.emit("tool_result", result_str, f"📋 Tool 결과: {tool_name}")

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
    """실시간 로그 대시보드."""
    html = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agent Engine — 실시간 로그</title>
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
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .header h1 {
            font-size: 1.2rem;
            font-weight: 600;
            background: linear-gradient(90deg, #58a6ff, #bc8cff);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .header .status {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.8rem;
            color: #8b949e;
        }
        .status-dot {
            width: 8px; height: 8px;
            border-radius: 50%;
            background: #3fb950;
            animation: pulse 2s ease-in-out infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.4; }
        }
        .info-bar {
            background: #0d1117;
            border-bottom: 1px solid #21262d;
            padding: 0.6rem 2rem;
            display: flex;
            gap: 2rem;
            font-size: 0.75rem;
            color: #8b949e;
        }
        .info-item { display: flex; align-items: center; gap: 0.4rem; }
        .info-label { color: #58a6ff; }
        .log-container {
            flex: 1;
            overflow-y: auto;
            padding: 1rem 2rem;
        }
        .log-entry {
            display: flex;
            gap: 0.75rem;
            padding: 0.5rem 0.75rem;
            margin-bottom: 0.25rem;
            border-radius: 6px;
            font-size: 0.82rem;
            line-height: 1.5;
            animation: fadeIn 0.3s ease-out;
            border-left: 3px solid transparent;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(-4px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .log-entry:hover { background: rgba(255,255,255,0.03); }
        .log-time {
            color: #484f58;
            white-space: nowrap;
            min-width: 80px;
            flex-shrink: 0;
        }
        .log-meta {
            white-space: nowrap;
            min-width: 160px;
            flex-shrink: 0;
            font-weight: 600;
        }
        .log-content {
            flex: 1;
            white-space: pre-wrap;
            word-break: break-word;
        }
        /* 타입별 색상 */
        .log-entry.webhook    { border-left-color: #d29922; }
        .log-entry.webhook .log-meta { color: #d29922; }

        .log-entry.system     { border-left-color: #58a6ff; }
        .log-entry.system .log-meta { color: #58a6ff; }

        .log-entry.divider {
            border-left: none;
            border-top: 1px solid #30363d;
            margin-top: 1rem;
            margin-bottom: 0.5rem;
            padding-top: 0.75rem;
        }
        .log-entry.divider .log-meta { display: none; }
        .log-entry.divider .log-time { display: none; }
        .log-entry.divider .log-content {
            color: #58a6ff;
            font-weight: 700;
            font-size: 0.8rem;
            letter-spacing: 0.05em;
        }

        .log-entry.scene      { border-left-color: #f0883e; background: rgba(240,136,62,0.06); }
        .log-entry.scene .log-meta { color: #f0883e; }
        .log-entry.scene .log-content { font-size: 0.75rem; color: #8b949e; }

        .log-entry.tool_schema { border-left-color: #39d353; background: rgba(57,211,83,0.05); }
        .log-entry.tool_schema .log-meta { color: #39d353; }

        .log-entry.loop       { border-left-color: #8b949e; background: rgba(139,148,158,0.05); }
        .log-entry.loop .log-meta { color: #8b949e; }

        .log-entry.thinking   { border-left-color: #bc8cff; }
        .log-entry.thinking .log-meta { color: #bc8cff; }

        .log-entry.reasoning  { border-left-color: #d2a8ff; }
        .log-entry.reasoning .log-meta { color: #d2a8ff; }

        .log-entry.tool_call  { border-left-color: #79c0ff; }
        .log-entry.tool_call .log-meta { color: #79c0ff; }

        .log-entry.tool_result { border-left-color: #56d364; }
        .log-entry.tool_result .log-meta { color: #56d364; }

        .log-entry.complete   {
            border-left-color: #3fb950;
            background: rgba(63,185,80,0.08);
        }
        .log-entry.complete .log-meta { color: #3fb950; }

        .log-entry.error      {
            border-left-color: #f85149;
            background: rgba(248,81,73,0.08);
        }
        .log-entry.error .log-meta { color: #f85149; }

        .empty-state {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 60vh;
            color: #484f58;
            gap: 1rem;
        }
        .empty-state .icon { font-size: 3rem; }
        .empty-state p { font-size: 0.9rem; }

        .footer {
            background: #0d1117;
            border-top: 1px solid #21262d;
            padding: 0.5rem 2rem;
            font-size: 0.7rem;
            color: #484f58;
            display: flex;
            justify-content: space-between;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🧠 Universal Agent Engine</h1>
        <div class="status">
            <div class="status-dot" id="statusDot"></div>
            <span id="statusText">대기 중</span>
        </div>
    </div>
    <div class="info-bar">
        <div class="info-item">
            <span class="info-label">Scene:</span> hotel_sync_scene.md
        </div>
        <div class="info-item">
            <span class="info-label">Engine:</span> localhost:8000
        </div>
        <div class="info-item">
            <span class="info-label">Site A:</span> localhost:8001
        </div>
        <div class="info-item">
            <span class="info-label">Site B:</span> localhost:8002
        </div>
        <div class="info-item">
            <span class="info-label">로그:</span> <span id="logCount">0</span>건
        </div>
    </div>

    <div class="log-container" id="logContainer">
        <div class="empty-state" id="emptyState">
            <div class="icon">📡</div>
            <p>Webhook 이벤트 대기 중...</p>
            <p style="font-size:0.75rem;">사이트 A에서 예약을 생성하면 여기에 실시간 로그가 표시됩니다</p>
        </div>
    </div>

    <div class="footer">
        <span>Universal Agent Engine v0.1.0</span>
        <span id="clock"></span>
    </div>

    <script>
        const container = document.getElementById('logContainer');
        const emptyState = document.getElementById('emptyState');
        const logCount = document.getElementById('logCount');
        const statusText = document.getElementById('statusText');
        const statusDot = document.getElementById('statusDot');
        const clock = document.getElementById('clock');
        let count = 0;

        function updateClock() {
            clock.textContent = new Date().toLocaleTimeString('ko-KR');
        }
        setInterval(updateClock, 1000);
        updateClock();

        function addLog(entry) {
            if (emptyState) emptyState.style.display = 'none';
            count++;
            logCount.textContent = count;

            const div = document.createElement('div');
            div.className = 'log-entry ' + entry.type;
            div.innerHTML =
                '<span class="log-time">' + entry.timestamp + '</span>' +
                '<span class="log-meta">' + entry.meta + '</span>' +
                '<span class="log-content">' + escapeHtml(entry.content) + '</span>';

            container.appendChild(div);
            container.scrollTop = container.scrollHeight;

            // 상태 업데이트
            if (entry.type === 'webhook') {
                statusText.textContent = '이벤트 처리 중';
                statusDot.style.background = '#d29922';
            } else if (entry.type === 'thinking') {
                statusText.textContent = 'LLM 판단 중';
                statusDot.style.background = '#bc8cff';
            } else if (entry.type === 'tool_call') {
                statusText.textContent = 'Tool 실행 중';
                statusDot.style.background = '#79c0ff';
            } else if (entry.type === 'complete') {
                statusText.textContent = '대기 중';
                statusDot.style.background = '#3fb950';
            } else if (entry.type === 'error') {
                statusText.textContent = '에러 발생';
                statusDot.style.background = '#f85149';
            }
        }

        function escapeHtml(text) {
            const d = document.createElement('div');
            d.textContent = text;
            return d.innerHTML;
        }

        // SSE 연결
        const es = new EventSource('/logs/stream');
        es.onmessage = (e) => {
            const entry = JSON.parse(e.data);
            addLog(entry);
        };
        es.onerror = () => {
            statusText.textContent = '연결 끊김 — 재연결 중...';
            statusDot.style.background = '#f85149';
        };

        // 기존 로그 로드
        fetch('/logs/history')
            .then(r => r.json())
            .then(logs => logs.forEach(addLog));
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
