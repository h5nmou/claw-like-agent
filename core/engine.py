"""
engine.py — 메인 에이전트 루프 & Webhook 서버 + 실시간 로그 대시보드

Trigger → Perceive → Reason → Act → Log 사이클을 관리.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
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
from core.mcp_client import MCPClient
from core.telegram_client import telegram_client
from core.proactive_care import get_care_engine, set_care_log_hook, save_pending_proposals, get_pending_proposal, clear_pending_proposals, save_pending_auto_rule, get_pending_auto_rule, clear_pending_auto_rule

# Tool 모듈을 import하여 @tool 데코레이터가 실행되도록 함
import tools.site_a_api  # noqa: F401
import tools.site_b_api  # noqa: F401
import tools.telco_auth_api  # noqa: F401
import tools.proactive_care_api  # noqa: F401

load_dotenv()

# ── 로깅 설정 ────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("engine")

# 반복 로그 억제 — 정상 요청은 숨기고 에러만 표시
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# ── FastAPI 앱 ───────────────────────────────────────

app = FastAPI(title="Universal Agent Engine", version="0.2.0")
onboarding_mgr = OnboardingManager()

# ── 원본 요청 컨텍스트 보존 저장소 ─────────────────────
# 채팅/텔레그램으로 받은 원본 사용자 요청을 임시 보관.
# 예약 확정 등의 Webhook이 발생하면 이 컨텍스트를 자동으로 주입하여
# 새 에이전트 루프에서도 원본 요청(이메일 전송 등)을 이어서 처리한다.
_pending_original_context = None  # type: dict | None

# /chat 세션용 Brain — 대화 히스토리를 유지하여 이전 대화를 기억한다.
# webhook 이벤트는 별도 Brain을 사용하므로 채팅 히스토리에 영향 없음.
_chat_brain: "Brain | None" = None

# Phone-MCP 설정
# PHONE_MCP_ENABLED=false 로 설정하면 연결 시도를 완전히 생략한다.
PHONE_MCP_ENABLED = os.getenv("PHONE_MCP_ENABLED", "true").strip().lower() != "false"
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://192.168.0.20:8080")
mcp_client = MCPClient(MCP_SERVER_URL)



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


@app.on_event("startup")
async def startup_mcp_tools():
    """Phone-MCP 서버에서 원격 Tool 목록 동기화."""
    await broadcaster.emit("divider", "PHASE 0.5 — Phone-MCP 연동", "")

    if not PHONE_MCP_ENABLED:
        logger.info("Phone-MCP disabled via PHONE_MCP_ENABLED=false. Skipping connection.")
        await broadcaster.emit(
            "system",
            "⏭️ Phone-MCP 연동 건너뜀 (PHONE_MCP_ENABLED=false)",
            "⚙️ MCP 연동",
        )
        return

    await broadcaster.emit("system", f"📱 Phone-MCP ({MCP_SERVER_URL}) 연결 시도 중...", "⚙️ MCP 연동")

    # Tool 목록 가져오기 및 등록
    await mcp_client.fetch_and_register_tools()

    # 등록된 도구 확인을 위해 잠시 대기
    from core.executor import get_registered_tools
    all_tools = get_registered_tools()
    logger.info(f"Final tool registry: {all_tools}")
    await broadcaster.emit("system", f"✅ Phone-MCP 연동 완료 — 전체 도구 수: {len(all_tools)}개", "⚙️ MCP 연동")


@app.on_event("startup")
async def startup_skill_registry():
    """generated_skills/ 내 저장된 모든 스킬을 자동 로드하고 Enterprise 서브시스템 초기화."""
    from core.skill_registry import get_skill_registry
    from core.skill_factory import set_log_hook
    from core.skill_healer import set_briefing_hook

    await broadcaster.emit("divider", "PHASE 0.6 — Enterprise Skill Ecosystem 초기화", "")

    # ── 이전 세션의 임시 파일 정리 ──
    _temp_files = [
        Path("generated_skills/pending_care_task.json"),
        Path("generated_skills/pending_intent.json"),
        Path("generated_skills/pending_email_task.json"),
    ]
    _cleaned = []
    for _tf in _temp_files:
        if _tf.exists():
            _tf.unlink()
            _cleaned.append(_tf.name)
    if _cleaned:
        await broadcaster.emit(
            "system",
            f"🧹 이전 세션 임시 파일 정리: {', '.join(_cleaned)}",
            "⚙️ Startup Cleanup"
        )

    await broadcaster.emit("system", "📚 [로그 분석 중...] 스킬 라이브러리 초기화...", "⚙️ Skill Registry")

    # broadcaster.emit을 skill_factory, self-healer, proactive care의 로그 훅으로 연결
    set_log_hook(broadcaster.emit)
    set_briefing_hook(broadcaster.emit)
    set_care_log_hook(broadcaster.emit)

    registry = get_skill_registry()
    loaded_count = registry.load_all()
    stats = registry.get_stats()

    if loaded_count > 0:
        skill_names = ", ".join(stats["skill_names"])
        await broadcaster.emit(
            "system",
            f"✅ 스킬 라이브러리 로드 완료 — {loaded_count}개 스킬 활성화\n"
            f"  등록 스킬: {skill_names}\n"
            f"  Progressive Loading: {'활성' if stats.get('progressive_loading') else '비활성'}",
            "📚 Skill Library"
        )
    else:
        await broadcaster.emit(
            "system",
            "📭 저장된 스킬 없음 — 새 요청 시 Enterprise Skill Factory 2.0이 자동 생성합니다.",
            "📚 Skill Library"
        )

    # 품질 평가기 초기화 + 오래된 로그 정리
    from core.skill_quality import get_quality_evaluator
    evaluator = get_quality_evaluator()
    stale_removed = evaluator.cleanup_stale_entries()
    if stale_removed:
        await broadcaster.emit(
            "system",
            f"🧹 quality_log.json 정리: 삭제된 스킬 로그 {stale_removed}건 제거",
            "⚙️ Quality Monitor"
        )
    quality_stats = evaluator.get_dashboard_stats()
    if quality_stats:
        critical_skills = [name for name, s in quality_stats.items() if s["status"] == "critical"]
        if critical_skills:
            await broadcaster.emit(
                "error",
                f"🚨 [품질 경고] 다음 스킬이 연속 실패 상태: {', '.join(critical_skills)}",
                "⚙️ Quality Monitor"
            )
        else:
            await broadcaster.emit(
                "system",
                f"✅ [보안 취약점 스캔 완료] 품질 모니터링 활성 — {len(quality_stats)}개 스킬 추적 중",
                "⚙️ Quality Monitor"
            )
    else:
        await broadcaster.emit(
            "system",
            "✅ 품질 평가 엔진 초기화 완료 — 스킬 실행 시 자동 추적 시작",
            "⚙️ Quality Monitor"
        )


# ── Telegram 봇 연동 ────────────────────────────────
async def handle_telegram_message(text: str, chat_id: str):
    """텔레그램에서 수신된 메시지를 에이전트 루프로 전달."""
    global _pending_original_context
    logger.info(f"Processing telegram message from {chat_id}: {text}")
    await broadcaster.emit("webhook", {"source": "Telegram", "msg": text}, "📲 Telegram 수신")
    
    event = {
        "event": "user_command",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "telegram",
        "chat_id": chat_id,
        "message": text
    }

    # ── Proactive Care 콜백 처리 (텔레그램 버튼 클릭) ──
    if text.startswith("care_"):
        proposal_info = get_pending_proposal(text)
        if proposal_info:
            clear_pending_proposals()

            if proposal_info["type"] == "dismiss":
                await broadcaster.emit("system", "❌ 사장님이 케어 제안을 무시했습니다.", "[선제적 케어]")
                await telegram_client.send_message("케어 제안을 무시합니다.", user_id=chat_id)
                return

            # 승인된 제안 내용을 이벤트에 주입
            analysis = proposal_info["analysis"]

            _proposals_for_event = []  # 이벤트에 주입할 제안 목록

            if proposal_info["type"] == "approve_one":
                _proposals_for_event = [proposal_info["proposal"]]
            elif proposal_info["type"] == "approve_all":
                _proposals_for_event = proposal_info["proposals"]

            # 각 제안의 상세 실행 단계 생성
            _steps_text = ""
            for i, p in enumerate(_proposals_for_event, 1):
                _steps_text += f"\n--- 제안 {i} ---\n"
                _steps_text += f"액션: {p['action']}\n"
                _steps_text += f"사유: {p['reason']}\n"
                if p.get("requires_skill") and p.get("skill_request"):
                    _steps_text += f"필요 스킬: {p['skill_request']}\n"
                    if p.get("skill_test_args"):
                        _steps_text += f"스킬 테스트 인자: {json.dumps(p['skill_test_args'], ensure_ascii=False)}\n"

            event["message"] = (
                f"사장님이 선제적 케어 제안을 승인했습니다.\n"
                f"\n== 승인된 제안 ==\n{_steps_text}\n"
                f"\n== 실행 지침 ==\n"
                f"각 제안에 대해 아래 단계를 순서대로 실행하세요:\n\n"
                f"  STEP 1 [MCP 기반 스킬 생성 — 최우선]:\n"
                f"    ⛔ create_local_guide, web_search는 폴백 도구이므로 이 단계에서 절대 사용 금지!\n"
                f"    → 반드시 create_new_skill 을 호출하세요.\n"
                f"    → Skill Factory가 Smithery.ai에서 특화 MCP 서버를 자동 탐색합니다.\n"
                f"       예: 장소/맛집 → Google Maps MCP, 날씨 → Weather MCP\n"
                f"    → create_new_skill이 실패를 반환한 경우에만 STEP 1-B로 진행\n\n"
                f"  STEP 1-B [웹 검색 폴백 — create_new_skill 실패 시에만]:\n"
                f"    → create_new_skill이 실패/mcp_install_required를 반환한 경우에만\n"
                f"    → create_local_guide 또는 web_search 사용 가능\n\n"
                f"  STEP 2 [액션 실행]: 생성된 스킬 또는 폴백 결과로 실제 작업 수행\n"
                f"    ⚠️ LLM 학습 데이터만으로 장소를 지어내는 것은 금지! 반드시 외부 데이터 소스 사용.\n\n"
                f"  STEP 3 [투숙객 전달]: 결과물을 투숙객 이메일로 전송 (send_email_via_smtp 또는 스킬 생성)\n\n"
                f"  STEP 4 [보고]: send_telegram_message로 사장님에게 실행 결과 + 데이터 소스 보고\n\n"
                f"  STEP 5 [자동화 등록 질문]: propose_care_automation 도구를 호출하세요.\n"
                f"    → 이 도구가 사장님에게 텔레그램으로 자동화 등록 여부를 질문합니다.\n"
                f"    → 사장님이 승인하면 시스템이 자동으로 규칙을 등록합니다.\n"
                f"    ⛔ register_care_rule을 직접 호출하지 마세요!\n"
                f"    ⛔ send_telegram_message로 '자동화 등록' 버튼을 직접 만들지 마세요! propose_care_automation만 사용하세요.\n"
                f"\n⚠️ 규칙 등록만 하고 끝내지 마세요! STEP 1~4를 반드시 먼저 수행하세요.\n"
                f"⚠️ 환경변수가 여러 개 필요하면 한꺼번에 요구하지 말고, 현재 STEP에 필요한 것만 안내 후 멈추세요.\n"
            )

            # 투숙객 정보도 주입
            if analysis.get("analysis"):
                event["message"] += f"\n상황 분석: {analysis['analysis']}"

            # 영향 받는 투숙객의 이메일 정보 명시 (LLM이 추측하지 않도록)
            _affected = analysis.get("_affected_bookings", [])
            if _affected:
                _guest_lines = "\n".join(
                    f"  - {b.get('guest_name', '?')}: {b.get('guest_email', '이메일 없음')} "
                    f"(체크인: {b.get('check_in', '?')}, 체크아웃: {b.get('check_out', '?')})"
                    for b in _affected
                )
                event["message"] += (
                    f"\n\n== 투숙객 정보 (이메일 전송 시 반드시 이 주소를 사용하세요) ==\n{_guest_lines}"
                )

            # proposals 원본도 포함 (투숙객 이메일 등 참조용)
            if analysis.get("proposals"):
                event["proactive_care_approved_proposals"] = _proposals_for_event

            # 케어 작업 pending 파일 저장 (env_key_required 발생 시 복구용)
            try:
                Path("generated_skills/pending_care_task.json").write_text(
                    json.dumps({
                        "proposals": _proposals_for_event,
                        "analysis": analysis,
                        "affected_bookings": analysis.get("_affected_bookings", []),
                        "event_message": event["message"],
                        "saved_at": datetime.now().isoformat(),
                    }, ensure_ascii=False),
                    encoding="utf-8"
                )
            except Exception:
                pass

            await broadcaster.emit("system",
                f"✅ 사장님이 케어 제안을 승인: {text}",
                "[선제적 케어]")
        else:
            # 중복 버튼 클릭 — 첫 클릭에서 이미 proposal이 소비됨.
            # LLM을 호출하지 않고 조용히 종료(불필요한 "대기 중인 케어 제안이 없습니다" 응답 방지).
            await broadcaster.emit("system",
                f"ℹ️ 중복 케어 버튼 클릭 무시: {text} (이미 처리된 제안)",
                "[선제적 케어]")
            return

    # ── 자동화 규칙 등록 승인/거부 콜백 ──
    elif text == "auto_rule_yes":
        pending_rule = get_pending_auto_rule()
        if pending_rule:
            clear_pending_auto_rule()
            care_engine = get_care_engine()
            _seq = pending_rule.get("skill_sequence") or []
            result = care_engine.register_rule(
                trigger_category=pending_rule["trigger_category"],
                approved_action=pending_rule["approved_action"],
                skill_name=pending_rule.get("skill_name"),
                skill_sequence=_seq,
            )
            await broadcaster.emit(
                "system",
                f"✅ 자동화 규칙 등록 완료: {pending_rule['approved_action']}"
                + (f" (재실행 스킬 {len(_seq)}개)" if _seq else ""),
                "[선제적 케어]",
            )
            _seq_text = ""
            if _seq:
                _seq_text = "\n- 재실행 시퀀스:\n" + "\n".join(
                    f"    {i+1}. {s.get('skill_name', '?')}" for i, s in enumerate(_seq)
                )
            await telegram_client.send_message(
                f"✅ 자동화 규칙이 등록되었습니다.\n"
                f"- 트리거: {pending_rule['trigger_category']}\n"
                f"- 액션: {pending_rule['approved_action']}\n"
                f"- 스킬: {pending_rule.get('skill_name', '없음')}"
                f"{_seq_text}\n\n"
                f"다음에 동일 조건 발생 시 위 시퀀스를 그대로 재실행합니다.",
                user_id=chat_id,
            )
        else:
            await telegram_client.send_message(
                "대기 중인 자동화 규칙이 없습니다.", user_id=chat_id
            )
        return

    elif text == "auto_rule_no" or text.strip().lstrip("✅❌📋🔧").strip().strip("[]").strip() in ("등록 안함", "자동화 안함", "No"):
        pending_rule = get_pending_auto_rule()
        clear_pending_auto_rule()
        await broadcaster.emit(
            "system", "❌ 사장님이 자동화 규칙 등록을 거부했습니다.", "[선제적 케어]"
        )
        await telegram_client.send_message(
            "자동화 규칙 등록을 건너뜁니다. 다음에 동일 조건 발생 시 다시 제안드리겠습니다.",
            user_id=chat_id,
        )
        return

    # ── 자동화 등록 텍스트 버튼 fallback ──
    # 대괄호([자동화 등록]) / 이모지(✅ 자동화 등록) prefix 모두 허용
    elif text.strip().lstrip("✅❌📋🔧").strip().strip("[]").strip() in ("자동화 등록", "Yes", "등록"):
        pending_rule = get_pending_auto_rule()
        if pending_rule:
            clear_pending_auto_rule()
            care_engine = get_care_engine()
            care_engine.register_rule(
                trigger_category=pending_rule["trigger_category"],
                approved_action=pending_rule["approved_action"],
                skill_name=pending_rule.get("skill_name"),
                skill_sequence=pending_rule.get("skill_sequence") or [],
            )
            await broadcaster.emit(
                "system",
                f"✅ 자동화 규칙 등록 완료 (텍스트 버튼 fallback): {pending_rule['approved_action']}",
                "[선제적 케어]",
            )
            await telegram_client.send_message(
                f"✅ 자동화 규칙이 등록되었습니다.\n"
                f"- 트리거: {pending_rule['trigger_category']}\n"
                f"- 액션: {pending_rule['approved_action']}\n"
                f"다음에 동일 조건 발생 시 자동으로 제안됩니다.",
                user_id=chat_id,
            )
        else:
            # pending_rule 없음 → LLM이 propose_care_automation을 우회한 경우
            # agent loop에서 최근 수행한 케어 액션을 register_care_rule로 등록하도록 위임
            await broadcaster.emit(
                "system",
                "⚠️ [자동화 등록 버튼] pending_rule 없음 — LLM이 propose_care_automation 우회한 케이스. Agent에게 위임",
                "[선제적 케어]",
            )
            event = {
                "event": "auto_rule_register_request",
                "message": text,
                "chat_id": chat_id,
                "instruction": (
                    "사장님이 방금 수행한 케어 액션(날씨 변화 감지 후 실내 장소 가이드 생성 + 이메일 전송)을 자동화 규칙으로 등록해주세요. "
                    "register_care_rule을 호출하여 trigger_category(예: weather_rain), "
                    "approved_action(방금 수행한 액션 요약), skill_name(사용한 스킬명)을 등록하세요. "
                    "등록 후 사장님에게 텔레그램으로 완료 보고하세요."
                ),
            }
            asyncio.create_task(run_agent_loop(event))
        return

    # ── Proactive Care: 키워드 감지 (일반 메시지) ──
    elif not text.startswith("care_") and not text.startswith("auto_rule_") and text.strip("[]") not in ("자동화 등록", "등록 안함", "자동화 안함", "Yes", "No", "등록"):
        care_engine = get_care_engine()
        care_result = await care_engine.process_message(text)
        if care_result:
            tg_msg = care_engine.format_care_telegram_message(care_result["analysis"])
            await telegram_client.send_message(
                tg_msg,
                user_id=chat_id,
                parse_mode="HTML",
                reply_markup=care_result["telegram_buttons"],
            )
            if care_result["auto_actions"]:
                event["proactive_care"] = {
                    "auto_actions": care_result["auto_actions"],
                    "analysis": care_result["analysis"],
                }
            event["proactive_care_context"] = care_result["analysis"]
            event["_skip_dashboard_echo"] = True  # 대시보드에 케어 카드 이미 푸시됨

    # 원본 사용자 요청을 전역 컨텍스트에 보관 (Webhook이 발생하면 주입됨)
    _pending_original_context = {
        "source": "telegram",
        "chat_id": chat_id,
        "original_message": text,
    }

    # 에이전트 루프 실행
    result = await run_agent_loop(event)

    # 루프 완료 후 컨텍스트 초기화
    _pending_original_context = None
    
    # 에이전트가 처리한 최종 요약본을 텔레그램으로 답장 발송
    # 단, 에이전트가 직접 send_telegram_message 추가 툴을 호출하여 이미 버튼이나 메세지를 보냈다면 중복 발송 생략
    history = result.get("history", [])
    used_tg_tool = any(
        item.get("role") == "tool_call"
        and isinstance(item.get("content"), dict)
        and item["content"].get("name") == "send_telegram_message"
        for item in history
    )
    
    if not used_tg_tool:
        final_reply = result.get("summary", "명령을 수행했습니다.")
        await telegram_client.send_message(final_reply, user_id=chat_id)


@app.on_event("startup")
async def startup_telegram_bot():
    """엔진 시작 시 텔레그램 메세지 수신 시작 (백그라운드)"""
    # 텔레그램 토큰이 설정되어 있으면 폴링 Task 생성
    asyncio.create_task(telegram_client.start_polling(handle_telegram_message))


@app.on_event("shutdown")
async def shutdown_telegram_bot():
    """엔진 종료 시 텔레그램 폴링 중지"""
    telegram_client.stop_polling()


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
            "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3],
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

    def clear_history(self) -> None:
        self._history.clear()


broadcaster = LogBroadcaster()

# ── Scene 로드 ───────────────────────────────────────

SCENE_DIR = os.getenv(
    "SCENE_DIR",
    str(Path(__file__).resolve().parent.parent / "scenes")
)


def load_all_scenes(directory: str) -> str:
    """scenes/ 디렉토리 내의 모든 .md 파일을 읽어 하나의 강력한 지침으로 병합."""
    all_content = []
    logger.info(f"Scanning for scenes in {directory}...")
    
    # 디렉토리 내의 모든 .md 파일 로드
    scene_files = list(Path(directory).glob("*.md"))
    if not scene_files:
        logger.warning(f"No scene files found in {directory}!")
        return "You are a helpful AI assistant."

    for scene_file in sorted(scene_files):  # 정렬하여 일관된 순서 유지
        logger.info(f"Loading scene: {scene_file.name}")
        with open(scene_file, "r", encoding="utf-8") as f:
            content = f.read()
            # 메타데이터 구분선 추가 (LLM이 각 씬의 맥락을 이해하는 데 도움)
            all_content.append(f"\n--- Scene: {scene_file.stem} ---\n{content}")
    
    return "\n".join(all_content)


# ── 메인 에이전트 루프 ───────────────────────────────

MAX_LOOP_ITERATIONS = 10  # 무한 루프 방지


async def run_agent_loop(trigger_event: dict, session_brain: "Brain | None" = None) -> dict:
    """
    하나의 트리거 이벤트에 대해 Perceive → Reason → Act 루프를 실행.

    session_brain: 외부에서 주입된 세션 Brain (대화 히스토리 유지용).
                   None이면 매번 새 Brain 생성 (webhook 등 독립 이벤트).
    """
    # ── Phase 1: Scene 로드 ──
    await broadcaster.emit("divider", "PHASE 1 — Scene 로드", "")
    await broadcaster.emit("system", f"📂 Scene 디렉토리({SCENE_DIR}) 로드 중...", "⚙️ 초기화")
    system_prompt = load_all_scenes(SCENE_DIR)

    scene_list = [p.name for p in Path(SCENE_DIR).glob("*.md")]
    await broadcaster.emit("system", f"✅ 로드된 시나리오: {', '.join(scene_list)}", "⚙️ Scene 로드")

    scene_preview = system_prompt[:500] + ("..." if len(system_prompt) > 500 else "")
    await broadcaster.emit("scene", scene_preview, "📜 Composite System Prompt")

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

    # ── Phase 3: Brain 초기화 (세션 Brain이 있으면 재사용) ──
    if session_brain is not None:
        brain = session_brain
        brain.update_system_prompt(system_prompt)
        brain.update_tools(tool_schemas)
        brain.trim_history()
        await broadcaster.emit(
            "system",
            f"Brain 세션 유지 (히스토리 {brain.get_message_count()}건)\n• Tools: {len(tool_schemas)}개",
            "🧠 Brain 세션"
        )
    else:
        await broadcaster.emit("divider", "PHASE 3 — Brain(LLM) 초기화", "")
        await broadcaster.emit(
            "system",
            f"Brain 초기화 완료\n• System Prompt: Scene 규칙 ({len(system_prompt)}자)\n• Tools: {tool_names} ({len(tool_schemas)}개)\n• Model: {os.getenv('OPENAI_MODEL', 'gemini-2.5-flash')}",
            "🧠 Brain 초기화"
        )
        brain = Brain(system_prompt=system_prompt, tools_schema=tool_schemas)
    memory = Memory()

    logger.info(f"Agent loop started. Tools: {tool_names}")

    # ── Phase 4: Webhook 이벤트 수신 ──
    await broadcaster.emit("divider", "PHASE 4 — Webhook 이벤트 처리", "")

    _trigger_msg = trigger_event.get("message", "") or ""
    # 이메일 "전송" 의도가 명확한 키워드만 매칭 (단순히 @가 포함된 것은 이메일 의도가 아님)
    _email_send_keywords = {"이메일 보내", "이메일 전송", "메일 보내", "메일 전송", "email send", "send email", "send mail"}
    _done_keywords  = {"설정완료", "설정 완료", "설정했어", "완료", "입력했어", "비번 설정"}

    _has_email_intent = any(kw in _trigger_msg.lower() for kw in _email_send_keywords)
    _is_done_trigger  = any(kw in _trigger_msg for kw in _done_keywords)

    _pending_email_file  = Path("generated_skills/pending_email_task.json")   # 스킬 실패 시 {to,subj,body}
    _pending_intent_file = Path("generated_skills/pending_intent.json")         # 원본 요청 전체
    _pending_care_file   = Path("generated_skills/pending_care_task.json")      # 케어 제안 승인 후 중단

    # ── (A) 이메일 의도를 가진 원본 요청 저장 ──────────────────────────────
    # "설정완료" 류가 아닌 일반 이메일 요청일 때 원본 메시지를 파일로 보존
    if _has_email_intent and not _is_done_trigger:
        try:
            _pending_intent_file.write_text(
                json.dumps({
                    "original_message": _trigger_msg,
                    "trigger_event": trigger_event,
                    "saved_at": datetime.now().isoformat(),
                }, ensure_ascii=False),
                encoding="utf-8"
            )
            await broadcaster.emit("system",
                f"💾 이메일 요청 저장 → pending_intent.json",
                "[Pending 저장]")
        except Exception:
            pass

    # ── (B) 미완료 작업 컨텍스트 주입 ─────────────────────────────────────
    # "설정완료" 등 완료 트리거일 때만 pending 파일을 복구한다.
    # 일반 메시지(예약 요청 등)에서는 pending을 무시하고 사용자 요청만 처리.
    pending_context = ""

    if _is_done_trigger and _pending_care_file.exists():
        # 우선순위 0: 케어 제안 승인 후 환경변수 부족으로 중단된 작업 (설정완료 시에만 복구)
        try:
            _pc = json.loads(_pending_care_file.read_text(encoding="utf-8"))
            _care_msg = _pc.get("event_message", "")
            _care_proposals = _pc.get("proposals", [])

            pending_context = (
                f"\n\n⚠️ **[선제적 케어 미완료 작업 — 자동 복구]**\n"
                f"환경변수 부족으로 케어 작업이 중단되었습니다. 사장님이 설정을 완료했으므로 즉시 재개하세요.\n\n"
                f"== 이전에 승인된 케어 제안 ==\n{_care_msg}\n\n"
                f"지금 즉시 아래 순서로 작업을 완료하세요:\n"
                f"1. reload_env 호출하여 새 환경변수 로드\n"
                f"2. 이전에 중단된 STEP부터 재개 (Priority Ladder 순서 준수)\n"
                f"3. 모든 STEP 완료 후 결과를 send_telegram_message로 사장님에게 보고\n"
                f"4. ⚠️ **반드시** propose_care_automation을 호출하여 사장님에게 자동화 등록 여부를 질문하세요 (STEP 5 필수)\n"
                f"사용자에게 정보를 다시 요청하지 마세요. 위의 제안 정보에 모든 내용이 있습니다."
            )
            # proposals 원본도 이벤트에 추가
            if _care_proposals:
                trigger_event["proactive_care_approved_proposals"] = _care_proposals

            await broadcaster.emit("system",
                f"📌 pending_care_task 복구: 케어 제안 {len(_care_proposals)}개",
                "[Pending 복구]")
        except Exception:
            pending_context = ""

    elif _is_done_trigger and _pending_email_file.exists():
        # 우선순위 1: 스킬 실패 시 저장한 정확한 이메일 파라미터
        try:
            _pe = json.loads(_pending_email_file.read_text(encoding="utf-8"))
            # pending_intent도 있으면 원본 요청 로드
            _orig_req = ""
            if _pending_intent_file.exists():
                try:
                    _pi2 = json.loads(_pending_intent_file.read_text(encoding="utf-8"))
                    _orig_req = f"\n원본 요청: \"{_pi2.get('original_message', '')}\""
                except Exception:
                    pass
            pending_context = (
                f"\n\n⚠️ **[이전 세션 미완료 작업 — 자동 복구]**\n"
                f"SMTP 비밀번호 부재로 이메일 전송이 중단되었습니다.{_orig_req}\n\n"
                f"지금 즉시 아래 순서로 **모든 작업**을 완료하세요:\n"
                f"1. reload_env 호출하여 SMTP 설정 확인\n"
                f"2. 원본 요청에 예약 생성이 포함된 경우 → get_site_a_bookings로 현재 상태 확인 후 미등록이면 create_site_a_booking 수행\n"
                f"3. send_email_via_smtp 호출:\n"
                f"   - to_email: {_pe.get('to_email')}\n"
                f"   - subject: {_pe.get('subject')}\n"
                f"   - body: {str(_pe.get('body', ''))[:200]}\n"
                f"4. ⚠️ 이메일 전송 완료 후 **반드시** propose_care_automation을 호출하여 사장님에게 자동화 등록 여부를 질문하세요 (STEP 5 필수)\n"
                f"사용자에게 정보를 다시 요청하지 마세요."
            )
            await broadcaster.emit("system",
                f"📌 pending_email_task 복구: {_pe.get('to_email')} / {_pe.get('subject')}",
                "[Pending 복구]")
        except Exception:
            pending_context = ""

    elif _is_done_trigger and _pending_intent_file.exists():
        # 우선순위 2: 원본 요청 메시지 (스킬이 호출되기 전에 중단된 경우, 설정완료 시에만 복구)
        try:
            _pi = json.loads(_pending_intent_file.read_text(encoding="utf-8"))
            _orig = _pi.get("original_message", "")
            pending_context = (
                f"\n\n⚠️ **[이전 세션 미완료 작업 — 자동 복구]**\n"
                f"이전 세션에서 SMTP 비밀번호 부재로 중단된 작업이 있습니다.\n"
                f"원본 요청: \"{_orig}\"\n\n"
                f"아래 순서로 원본 요청의 **모든 작업**을 완료하세요:\n"
                f"1. reload_env 호출하여 SMTP 설정 확인\n"
                f"2. 원본 요청에 예약 생성이 포함된 경우 → get_site_a_bookings로 현재 상태 확인 후 미등록이면 create_site_a_booking 수행\n"
                f"3. 원본 요청에서 수신자·제목·내용을 파악하여 send_email_via_smtp 즉시 호출\n"
                f"사용자에게 정보를 다시 요청하지 마세요. 원본 요청에 모든 정보가 있습니다."
            )
            await broadcaster.emit("system",
                f"📌 pending_intent 복구: {_orig[:60]}...",
                "[Pending 복구]")
        except Exception:
            pending_context = ""

    elif _is_done_trigger:
        # 우선순위 3: "설정완료/입력했어" 등이지만 pending 파일이 없는 경우
        # → reload_env 후 이전 작업 확인을 안내
        pending_context = (
            f"\n\n⚠️ **[환경변수 설정 완료 알림]**\n"
            f"사장님이 환경변수(SMTP 비밀번호 등) 설정을 완료했다고 합니다.\n"
            f"1. 먼저 `reload_env`를 호출하여 새 환경변수를 로드하세요.\n"
            f"2. 로드 완료 후, 사장님에게 '환경변수가 정상적으로 로드되었습니다. 이전에 중단된 작업이 있으면 다시 요청해 주세요.'라고 안내하세요.\n"
        )

    # ── 숙소 위경도 컨텍스트 주입 (장소 검색 스킬에 직접 전달용) ──
    _prop_lat = trigger_event.get("lat") or trigger_event.get("property_lat")
    _prop_lng = trigger_event.get("lng") or trigger_event.get("property_lng")
    # booking 이벤트의 경우 booking 객체 안에 있을 수 있음
    if not _prop_lat:
        _booking = trigger_event.get("booking", {})
        _prop_lat = _booking.get("property_lat")
        _prop_lng = _booking.get("property_lng")
    _coords_context = ""
    if _prop_lat and _prop_lng:
        _coords_context = (
            f"\n\n📍 **[숙소 위경도 — 장소 검색 시 반드시 이 값을 사용하세요]**\n"
            f"latitude: {_prop_lat}\n"
            f"longitude: {_prop_lng}\n"
            f"⚠️ 주소 문자열(예: '제주도 애월읍')을 위경도로 변환하려 하지 마세요. "
            f"위 좌표를 그대로 스킬 파라미터에 넣으세요.\n"
        )

    # ── Proactive Care 컨텍스트 주입 ──
    care_context = ""
    _care_data = trigger_event.get("proactive_care_context")
    if _care_data:
        care_context = (
            f"\n\n🌦️ **[선제적 케어 분석 결과]**\n"
            f"위험도: {_care_data.get('risk_level', 'unknown').upper()}\n"
            f"분석: {_care_data.get('analysis', '')}\n"
            f"제안 수: {len(_care_data.get('proposals', []))}개\n"
        )
        _auto = trigger_event.get("proactive_care", {}).get("auto_actions", [])
        if _auto:
            _registered_skills = [a.get("skill_name") for a in _auto if a.get("skill_name")]
            _skill_list_txt = ", ".join(f"`{s}`" for s in _registered_skills) if _registered_skills else "(스킬 지정 없음)"

            # ⭐ skill_sequence가 기록된 규칙이 있으면 정확한 재실행 지시를 생성
            _sequence_blocks = []
            for a in _auto:
                seq = a.get("skill_sequence") or []
                if seq:
                    lines = []
                    for i, s in enumerate(seq, 1):
                        sname = s.get("skill_name", "?")
                        args = s.get("args") or {}
                        try:
                            args_json = json.dumps(args, ensure_ascii=False)
                        except Exception:
                            args_json = str(args)
                        lines.append(f"    {i}. `{sname}` — 기본 파라미터: {args_json}")
                    _sequence_blocks.append(
                        f"  • [{a.get('action', '액션')}] 정확한 재실행 시퀀스:\n"
                        + "\n".join(lines)
                    )
            _sequence_text = ""
            if _sequence_blocks:
                _sequence_text = (
                    "\n\n🎯 **저장된 정확한 재실행 시퀀스 (이 순서·파라미터로만 호출)**:\n"
                    + "\n".join(_sequence_blocks)
                    + "\n\n※ 위 args의 동적 값(예: 투숙객 이메일, 좌표)은 현재 이벤트에 포함된 실제 값으로 치환하세요. "
                      "나머지(반경, 카테고리 등)는 그대로 사용하세요."
                )

            care_context += (
                f"\n⚡ **자동 실행 규칙 {len(_auto)}개 감지** — 즉시 실행하세요:\n"
                + "\n".join(
                    f"  - {a['action']}"
                    + (f" (스킬: {a['skill_name']})" if a.get("skill_name") else "")
                    for a in _auto
                )
                + _sequence_text
                + "\n"
                "\n**[자동 실행 모드]** 사장님 승인 없이 즉시 위 규칙들을 실행하세요."
                "\n승인 절차가 이미 자동화되어 있습니다 (3회 이상 승인 이력)."
                "\n실행 완료 후 send_telegram_message로 사장님에게 **결과만 보고**하세요."
                "\npropose_care_automation 호출은 불필요합니다 (이미 등록된 규칙).\n"
                f"\n⛔ **CRITICAL — 스킬 재생성·탐색 절대 금지**:\n"
                f"  - 이 Active Rule은 이미 검증된 스킬 [{_skill_list_txt}]을(를) 사용합니다.\n"
                f"  - **`create_new_skill` 호출 금지.** MCP 재탐색·웹 검색 폴백도 금지.\n"
                f"  - 저장된 재실행 시퀀스가 있으면 **그 순서·파라미터 그대로 순차 호출**하세요.\n"
                f"  - 스킬이 `결과없음`을 반환해도 **새 스킬을 만들지 말고**, "
                f"그 사실을 그대로 사장님에게 텔레그램으로 보고하세요 "
                f"(예: '비 오는 날 실내 가이드 — 결과 0건: 좌표 X, 키워드 Y, 반경 Z').\n"
                f"  - 필요시 **같은 스킬을 다른 파라미터**(더 넓은 반경 등)로 한 번만 재시도하세요. "
                f"그래도 0건이면 사장님께 상황만 보고하고 종료하세요.\n"
            )
        else:
            care_context += (
                "\n케어 제안은 이미 텔레그램으로 사장님에게 전송 완료되었습니다."
                "\n**텔레그램으로 중복 발송하지 마세요.** 대시보드에 결과만 보고하세요."
                "\n사장님이 승인하면 해당 액션을 실행하세요.\n"
            )

    # ── Webhook 루프 전용: 텔레그램 보고 억제 ──
    _suppress_tg = ""
    if trigger_event.get("suppress_telegram_report"):
        _suppress_tg = (
            "\n\n🚫 **[텔레그램 보고 금지]**\n"
            "이 루프는 booking_confirmed Webhook에 의한 Site B 동기화 전용입니다.\n"
            "send_telegram_message를 호출하지 마세요. 사장님에 대한 보고는 원본 텔레그램 루프가 담당합니다.\n"
            "Site B 날짜 차단(block_site_b_dates)만 수행하고 종료하세요."
        )

    trigger_text = (
        f"다음 이벤트가 발생했습니다. 적절한 조치를 취해주세요.\n\n"
        f"```json\n{json.dumps(trigger_event, ensure_ascii=False, indent=2)}\n```"
        f"{_coords_context}"
        f"{pending_context}"
        f"{care_context}"
        f"{_suppress_tg}"
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
            # ── 자가 점검 유틸: tool_calls 히스토리에서 특정 도구 호출 여부 확인 ──
            def _tool_was_called(tool_name: str) -> bool:
                for _m in brain._messages:
                    if _m.get("role") == "assistant":
                        for _tc in (_m.get("tool_calls") or []):
                            if (_tc.get("function") or {}).get("name") == tool_name:
                                return True
                return False

            def _tool_last_succeeded(tool_name: str) -> bool:
                """가장 최근 `tool_name` 호출 결과가 성공(error/env_key_required/결과없음 없음)인지."""
                last_call_idx = -1
                last_call_id = None
                for _i, _m in enumerate(brain._messages):
                    if _m.get("role") == "assistant":
                        for _tc in (_m.get("tool_calls") or []):
                            if (_tc.get("function") or {}).get("name") == tool_name:
                                last_call_idx = _i
                                last_call_id = _tc.get("id")
                if last_call_idx < 0:
                    return False
                for _m in brain._messages[last_call_idx + 1:]:
                    if _m.get("role") == "tool" and _m.get("tool_call_id") == last_call_id:
                        _content = _m.get("content", "")
                        try:
                            _parsed = json.loads(_content) if isinstance(_content, str) else _content
                        except Exception:
                            return True  # 파싱 실패는 단순 텍스트 결과 → 성공으로 간주
                        if isinstance(_parsed, dict):
                            if (_parsed.get("error") or _parsed.get("env_key_required")
                                    or _parsed.get("결과없음") or _parsed.get("status") == "rejected"):
                                return False
                            # send_email_via_smtp의 성공 응답은 status/response에 담김
                            return True
                        return True
                return False  # tool result 아직 없음 → 성공으로 단정 안 함

            _intent_text = " ".join([
                str(trigger_event.get("message") or ""),
                str((trigger_event.get("original_context") or {}).get("original_message") or ""),
                str(trigger_event.get("event_message") or ""),
                json.dumps(trigger_event.get("proactive_care") or {}, ensure_ascii=False),
            ])

            # ── ① 이메일 누락 보정 ──
            _email_intent = any(k in _intent_text for k in [
                "이메일", "메일 전송", "메일 발송", "이메일로", "메일로", "send email", "email"
            ])
            _email_called = _tool_was_called("send_email_via_smtp")

            # env_key_required(SMTP_PASSWORD 미설정) 감지 → 사용자에게 설정 안내 자동 푸시
            _smtp_nudged = getattr(brain, "_smtp_setup_nudged", False)
            if _email_called and not _tool_last_succeeded("send_email_via_smtp") and not _smtp_nudged:
                # 가장 최근 이메일 tool 결과가 env_key_required인지 확인
                for _m in reversed(brain._messages):
                    if _m.get("role") == "tool":
                        try:
                            _r = json.loads(_m.get("content") or "{}")
                        except Exception:
                            _r = {}
                        if isinstance(_r, dict) and _r.get("env_key_required") == "SMTP_PASSWORD":
                            brain._smtp_setup_nudged = True
                            await broadcaster.emit(
                                "external_result",
                                (
                                    "🔑 이메일 전송에 필요한 설정이 누락되었습니다.\n\n"
                                    "Gmail 앱 비밀번호를 발급받아 `.env` 파일에 추가해주세요:\n"
                                    "  SMTP_PASSWORD=발급받은_16자리_앱비밀번호\n\n"
                                    "설정 후 채팅창에 '설정 완료'라고 입력하시면 즉시 이메일을 재전송합니다."
                                ),
                                "[SMTP 설정 필요]",
                            )
                            break
                # 이메일이 실패한 상태 → propose nudge로 넘어가지 않도록 그대로 종료 허용
                final_summary = response["content"] or ""
                memory.add_event("assistant", final_summary)
                logger.info(f"Agent completed (SMTP 설정 대기): {final_summary[:100]}...")
                await broadcaster.emit("complete", final_summary, "✅ 작업 완료")
                break

            _already_nudged = getattr(brain, "_email_nudged", False)
            if _email_intent and not _email_called and not _already_nudged:
                brain._email_nudged = True
                logger.info("[Email-Nudge] 이메일 의도 감지되었으나 send_email_via_smtp 호출 누락 — 강제 재촉")
                await broadcaster.emit(
                    "system",
                    "📨 이메일 전송 단계가 누락된 것 같습니다. 자동으로 한 번 더 안내합니다.",
                    "[이메일 누락 보정]",
                )
                brain.add_user_message(
                    "⛔ 작업이 아직 끝나지 않았습니다. 사용자 요청에 '이메일 전송'이 포함되어 있는데 "
                    "지금까지 send_email_via_smtp 도구를 호출하지 않았습니다. "
                    "지금 즉시 send_email_via_smtp(to_email, subject, body)를 tool_call로 호출하여 "
                    "방금 만든 가이드/결과를 투숙객에게 발송하세요. 텍스트 응답하지 말고 곧바로 도구를 호출하세요."
                )
                continue

            # ── ② 자동화 제안 누락 보정 ──
            # 순서: 사용자 요청 작업(이메일 포함)이 **성공**해야만 자동화 등록 질문 재촉.
            # - 이메일 의도가 있으면 send_email_via_smtp가 성공한 경우에만 허용
            #   (env_key_required 등 실패 상태에서는 절대 propose 재촉 금지 — SMTP_PASSWORD 안내가 먼저 가야 함)
            # - 이메일 의도가 없으면 생성/검색 스킬이라도 성공했는지 확인
            _propose_called = _tool_was_called("propose_care_automation")
            _email_ok = _tool_last_succeeded("send_email_via_smtp") if _email_intent else True

            _other_action_succeeded = False
            if not _email_intent:
                for _m in brain._messages:
                    if _m.get("role") == "assistant":
                        for _tc in (_m.get("tool_calls") or []):
                            _name = (_tc.get("function") or {}).get("name", "")
                            if _name.startswith(("generate_", "create_", "recommend_", "search_", "find_")):
                                if _tool_last_succeeded(_name):
                                    _other_action_succeeded = True
                                    break
                    if _other_action_succeeded:
                        break

            _real_action_ok = _email_ok if _email_intent else _other_action_succeeded

            # ⛔ 핵심 스킬 생성 실패 시 전체 작업 실패로 간주 — propose 재촉 금지
            # create_new_skill이 error/실패를 반환했으면 사용자 요청이 완료되지 않은 것
            _create_skill_failed = False
            for _m in brain._messages:
                if _m.get("role") == "assistant":
                    for _tc in (_m.get("tool_calls") or []):
                        if (_tc.get("function") or {}).get("name") == "create_new_skill":
                            _cid = _tc.get("id")
                            for _rm in brain._messages:
                                if _rm.get("role") == "tool" and _rm.get("tool_call_id") == _cid:
                                    try:
                                        _cr = json.loads(_rm.get("content") or "{}")
                                    except Exception:
                                        _cr = {}
                                    if isinstance(_cr, dict) and (_cr.get("error") or not _cr.get("success")):
                                        _create_skill_failed = True
                                    break
            if _create_skill_failed:
                _real_action_ok = False

            _propose_nudged = getattr(brain, "_propose_nudged", False)
            # ⛔ 자동 실행 모드(이미 등록된 Active Rule 발동)에서는 propose 재촉 금지
            #    — 이미 등록된 규칙을 수행 중인데 또 등록 질문 띄우면 무한 반복
            _is_auto_execute = bool((trigger_event.get("proactive_care") or {}).get("auto_actions"))
            if _real_action_ok and not _propose_called and not _propose_nudged and not _is_auto_execute:
                brain._propose_nudged = True
                logger.info("[Propose-Nudge] 작업 완료 후 propose_care_automation 호출 누락 — 강제 재촉")
                await broadcaster.emit(
                    "system",
                    "📋 자동화 등록 질문이 누락된 것 같습니다. 자동으로 한 번 더 안내합니다.",
                    "[자동화 제안 보정]",
                )
                # trigger_category 힌트: 케어 webhook 이벤트면 해당 카테고리, 아니면 general
                _care_ctx = trigger_event.get("proactive_care") or {}
                _cat_hint = ""
                if isinstance(_care_ctx, dict):
                    _aa = _care_ctx.get("auto_actions") or []
                    if _aa and isinstance(_aa[0], dict):
                        _cat_hint = _aa[0].get("trigger_category", "")
                if not _cat_hint:
                    _weather = (trigger_event.get("weather") or {}).get("current", "")
                    _cat_hint = {"rain": "rain", "heavy_rain": "heavy_rain", "snow": "snow"}.get(_weather, "general")
                brain.add_user_message(
                    "⛔ 작업이 아직 끝나지 않았습니다. 방금 투숙객 관련 작업(가이드 제작/이메일 발송 등)을 완료했지만 "
                    "사장님에게 '이 작업을 자동화 규칙으로 등록할지' 질문하지 않았습니다. "
                    "지금 즉시 propose_care_automation 도구를 tool_call로 호출하세요. "
                    f"trigger_category는 '{_cat_hint}'를 사용하세요 (날씨 이벤트면 해당 카테고리, 일반 채팅 요청이면 'general'). "
                    "approved_action에는 방금 수행한 작업의 요약을, skill_sequence에는 실제로 성공했던 스킬 호출만 순서대로 넣으세요. "
                    "텍스트 응답하지 말고 곧바로 도구를 호출하세요."
                )
                continue

            # LLM이 텍스트로 응답 → 루프 종료
            final_summary = response["content"]
            memory.add_event("assistant", final_summary)
            logger.info(f"Agent completed: {final_summary[:100]}...")
            await broadcaster.emit("complete", final_summary, "✅ 작업 완료")
            # 대시보드 채팅창 푸시 — /chat 경로가 아닌 외부 트리거(site_a/webhook/telegram)의 결과를
            # 대시보드 채팅창에도 표시한다. /chat 경로는 이미 response로 반환되므로 제외.
            # hotel_sync_scene 규칙상 Site A 예약 동기화는 "조용히 수행" — LLM이 "..." 같은
            # placeholder만 응답하는 경우가 많으므로 의미 있는 메시지만 푸시한다.
            _src = trigger_event.get("source") or ""
            _summary_clean = (final_summary or "").strip().strip(".").strip()
            _is_meaningful = len(_summary_clean) >= 10
            # 이미 케어 제안 메시지가 대시보드에 푸시된 상태면 최종 요약 중복 전송 방지
            _already_pushed = bool(trigger_event.get("_skip_dashboard_echo"))
            if _src and _src != "dashboard_chat" and _is_meaningful and not _already_pushed:
                _label = {
                    "site_a": "🏨 Site A",
                    "telegram": "📲 Telegram",
                    "webhook": "🔔 Webhook",
                }.get(_src, f"🔔 {_src}")
                await broadcaster.emit(
                    "external_result",
                    f"[{_label}] {final_summary}",
                    "대시보드 알림",
                )
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
                if "site_b" in tool_name:
                    role_label = "[SITE B]"
                elif "site_a" in tool_name:
                    role_label = "[SITE A]"
                elif "mcp" in tool_name or tool_name in ["camera", "gps", "sms", "phone"]:
                    role_label = "[PHONE]"
                else:
                    role_label = "[AGENT C]"

                logger.info(f"Executing tool: {tool_name}({json.dumps(tool_args, ensure_ascii=False)})")
                memory.add_event("tool_call", {"name": tool_name, "arguments": tool_args})
                await broadcaster.emit(
                    "tool_call",
                    json.dumps({"function": tool_name, "arguments": tool_args}, ensure_ascii=False, indent=2),
                    f"🔧 {role_label} Tool 호출: {tool_name}"
                )

                # ── 순서 가드: 이메일 전송 성공 전에 propose_care_automation 호출 금지 ──
                # 사용자 요청에 이메일 의도가 있으면 send_email_via_smtp가 성공한 뒤에만 자동화 질문을 띄운다.
                _intent_text = " ".join([
                    str(trigger_event.get("message") or ""),
                    str((trigger_event.get("original_context") or {}).get("original_message") or ""),
                    str(trigger_event.get("event_message") or ""),
                    json.dumps(trigger_event.get("proactive_care") or {}, ensure_ascii=False),
                ])
                _has_email_intent = any(k in _intent_text for k in [
                    "이메일", "메일 전송", "메일 발송", "이메일로", "메일로", "send email", "email"
                ])
                _has_guide_intent = any(k in _intent_text for k in [
                    "가이드", "추천", "맛집", "관광", "카페", "핫플", "장소 검색", "실내"
                ])

                # ── Order-Guard 보조: 이미 호출된 도구 분석 ──
                _called_names = [
                    (_tc.get("function") or {}).get("name", "")
                    for _m in brain._messages if _m.get("role") == "assistant"
                    for _tc in (_m.get("tool_calls") or [])
                ]
                _guide_skill_called = any(
                    n.startswith(("generate_", "create_", "recommend_", "search_", "find_"))
                    and n != "create_new_skill"
                    for n in _called_names
                )
                _is_auto_run = bool((trigger_event.get("proactive_care") or {}).get("auto_actions"))

                # Order-Guard 0: create_new_skill 호출 시 — 가이드 의도가 있는데 아직 가이드 스킬이
                # 호출되지 않았으면 이메일 스킬을 먼저 만들지 못하게 한다.
                if tool_name == "create_new_skill" and _has_guide_intent and not _guide_skill_called and not _is_auto_run:
                    _req = (tool_args.get("user_request") or "").lower()
                    _is_email_skill_request = any(k in _req for k in [
                        "smtp", "send_email", "이메일", "메일 전송", "메일 발송", "gmail"
                    ])
                    if _is_email_skill_request:
                        logger.warning(
                            "[Order-Guard] create_new_skill(이메일 스킬) 차단 — 가이드 스킬 먼저 필요"
                        )
                        await broadcaster.emit(
                            "system",
                            "⛔ 이메일 스킬 생성을 차단했습니다. 먼저 MCP 기반 가이드 스킬을 생성·호출하세요.",
                            "[순서 가드]",
                        )
                        result = {
                            "status": "blocked",
                            "error": "가이드 스킬을 먼저 생성·실행한 뒤에 이메일 스킬을 만드세요.",
                            "hint": (
                                "사용자 요청 순서: ① MCP 기반 가이드 스킬 생성 → ② 그 스킬 호출로 데이터 수집 "
                                "→ ③ 이메일 스킬 생성(없으면) → ④ 이메일 전송. "
                                "지금은 ①~②가 끝나야 합니다. "
                                "create_new_skill을 다시 호출하되 user_request에 "
                                "'제주 애월읍 주변 실내 핫플레이스(카페·맛집·관광지) 검색 가이드 생성' 같은 "
                                "MCP 기반 가이드 스킬을 요청하세요."
                            ),
                        }
                        result_str = json.dumps(result, ensure_ascii=False, indent=2)
                        brain.add_tool_result(tool_call["id"], result)
                        logger.info(f"Tool result: {result_str}")
                        memory.add_event("tool_result", {"name": tool_name, "result": result})
                        continue

                # Order-Guard A: 가이드 의도가 있는데 MCP 기반 스킬이 한 번도 실행되지 않았다면
                # send_email_via_smtp 호출 차단 — 고정된 텍스트로 이메일 보내는 것 방지
                if tool_name == "send_email_via_smtp" and _has_guide_intent:
                    # 자동 실행 모드면 스킵 (이미 skill_sequence에 따라 실행 중)
                    if not _is_auto_run and not _guide_skill_called:
                        logger.warning(
                            "[Order-Guard] send_email_via_smtp 차단 — MCP 가이드 스킬 호출 없음"
                        )
                        await broadcaster.emit(
                            "system",
                            "⛔ 이메일 전송을 차단했습니다. 먼저 MCP 기반 가이드/검색 스킬을 생성·호출하세요.",
                            "[순서 가드]",
                        )
                        result = {
                            "status": "blocked",
                            "error": "이메일 본문 데이터가 준비되지 않았습니다.",
                            "hint": (
                                "사용자 요청에 '가이드/추천/검색/맛집/관광지/실내' 의도가 있습니다. "
                                "이메일 전송 전에 반드시 MCP 기반 스킬(create_new_skill 후 해당 스킬 호출)로 "
                                "실시간 데이터를 수집해야 합니다. "
                                "지금 create_new_skill을 호출하여 가이드 스킬을 생성한 뒤, "
                                "그 스킬로 실제 데이터를 조회하고, 그 결과를 이메일 body에 담아 "
                                "send_email_via_smtp를 다시 호출하세요."
                            ),
                        }
                        result_str = json.dumps(result, ensure_ascii=False, indent=2)
                        brain.add_tool_result(tool_call["id"], result)
                        logger.info(f"Tool result: {result_str}")
                        memory.add_event("tool_result", {"name": tool_name, "result": result})
                        continue

                if tool_name == "propose_care_automation":
                    # Order-Guard B-0: create_new_skill이 실패했으면 propose 차단
                    # 핵심 스킬이 만들어지지 않았는데 자동화 등록을 질문하면 안 됨
                    _skill_creation_ok = True
                    for _m in brain._messages:
                        if _m.get("role") == "assistant":
                            for _tc in (_m.get("tool_calls") or []):
                                if (_tc.get("function") or {}).get("name") == "create_new_skill":
                                    _cid = _tc.get("id")
                                    for _rm in brain._messages:
                                        if _rm.get("role") == "tool" and _rm.get("tool_call_id") == _cid:
                                            try:
                                                _cr = json.loads(_rm.get("content") or "{}")
                                            except Exception:
                                                _cr = {}
                                            if isinstance(_cr, dict) and (_cr.get("error") or not _cr.get("success")):
                                                _skill_creation_ok = False
                                            break
                    if not _skill_creation_ok:
                        logger.warning("[Order-Guard] propose_care_automation 차단 — create_new_skill 실패")
                        result = {
                            "status": "blocked",
                            "error": "스킬 생성이 실패한 상태에서는 자동화 등록을 할 수 없습니다.",
                            "hint": (
                                "사용자 요청을 아직 완료하지 못했습니다. "
                                "사장님께 실패 사유(API 키 필요 등)를 보고하고 안내만 하세요. "
                                "propose_care_automation은 모든 작업이 성공한 뒤에만 호출하세요."
                            ),
                        }
                        brain.add_tool_result(tool_call["id"], result)
                        logger.info(f"Tool result: {json.dumps(result, ensure_ascii=False)}")
                        memory.add_event("tool_result", {"name": tool_name, "result": result})
                        continue

                    if _has_email_intent:
                        # 최근 send_email_via_smtp 호출 결과가 성공인지 확인
                        _email_ok = False
                        _email_call_id = None
                        _last_call_idx = -1
                        for _i, _m in enumerate(brain._messages):
                            if _m.get("role") == "assistant":
                                for _tc in (_m.get("tool_calls") or []):
                                    if (_tc.get("function") or {}).get("name") == "send_email_via_smtp":
                                        _last_call_idx = _i
                                        _email_call_id = _tc.get("id")
                        if _last_call_idx >= 0:
                            for _m in brain._messages[_last_call_idx + 1:]:
                                if _m.get("role") == "tool" and _m.get("tool_call_id") == _email_call_id:
                                    try:
                                        _r = json.loads(_m.get("content") or "{}")
                                    except Exception:
                                        _r = {}
                                    if isinstance(_r, dict) and not _r.get("error") \
                                            and not _r.get("env_key_required"):
                                        _email_ok = True
                                    break
                        if not _email_ok:
                            logger.warning(
                                "[Order-Guard] propose_care_automation 호출 차단 — send_email_via_smtp 성공 결과 없음"
                            )
                            await broadcaster.emit(
                                "system",
                                "⛔ 자동화 등록 질문을 차단했습니다. 이메일 전송을 먼저 성공시켜야 합니다.",
                                "[순서 가드]",
                            )
                            # propose_care_automation 호출을 실제 실행하지 않고 LLM에게 에러 응답 주입
                            result = {
                                "status": "blocked",
                                "error": "이메일 전송이 먼저 성공해야 합니다.",
                                "hint": (
                                    "지금 send_email_via_smtp를 호출하여 이메일을 전송하세요. "
                                    "SMTP_PASSWORD 미설정이면 사장님께 안내 후 '설정 완료' 응답을 기다리세요. "
                                    "이메일 전송이 성공한 다음에만 propose_care_automation을 호출할 수 있습니다."
                                ),
                            }
                            result_str = json.dumps(result, ensure_ascii=False, indent=2)
                            brain.add_tool_result(tool_call["id"], result)
                            logger.info(f"Tool result: {result_str}")
                            memory.add_event("tool_result", {"name": tool_name, "result": result})
                            continue

                # Tool 실행
                result = await execute(tool_name, tool_args)
                result_str = json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else str(result)
                logger.info(f"Tool result: {result_str}")
                memory.add_event("tool_result", {"name": tool_name, "result": result})

                # ── Self-Healing: 생성된 스킬 실행 실패 시 자율 복구 ──
                _is_generated_skill = tool_name not in {
                    "list_all_available_tools", "send_telegram_message",
                    "create_new_skill", "reload_env",
                    "block_site_b_dates", "unblock_site_b_dates",
                    "get_site_b_availability",
                    "get_site_a_bookings", "create_site_a_booking",
                    "register_care_rule", "list_care_rules",
                }
                _has_error = isinstance(result, dict) and "error" in result

                if _is_generated_skill and _has_error:
                    await broadcaster.emit("system",
                        f"⚠️ 스킬 실행 오류 감지: {tool_name} — 자가 치유 판단 중...",
                        "[오류 감지]")

                    from core.skill_quality import get_quality_evaluator
                    evaluator = get_quality_evaluator()
                    if evaluator.needs_healing(tool_name):
                        await broadcaster.emit("system",
                            f"🏥 [AI Doctor 복구 시도 중] 연속 실패 감지 → Self-Healing 가동",
                            "[Self-Healing]")

                        from core.skill_healer import get_skill_healer
                        healer = get_skill_healer()
                        healing_report = await healer.heal(
                            skill_name=tool_name,
                            original_error=str(result.get("error", "")),
                            execution_args=tool_args,
                        )

                        if healing_report.healed:
                            # 복구 후 재실행
                            await broadcaster.emit("complete",
                                f"✅ [Self-Healing 복구 완료] {tool_name} — 방법: {healing_report.healing_method}",
                                "[복구 완료]")
                            result = await execute(tool_name, tool_args)
                            result_str = json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, dict) else str(result)
                        else:
                            await broadcaster.emit("error",
                                f"🚨 [Self-Healing 실패] {tool_name} — Stage {healing_report.stage_reached}/4 도달",
                                "[복구 실패]")

                    # 품질 게이트: 품질 미달 시 자동 롤백
                    if evaluator.needs_rollback(tool_name):
                        from core.skill_versioning import get_version_manager
                        vm = get_version_manager()
                        rec = evaluator.get_record(tool_name)
                        if rec:
                            gate_result = vm.quality_gate_check(tool_name, rec.avg_quality)
                            if gate_result["action"] == "rollback":
                                await broadcaster.emit("system",
                                    f"⚠️ [품질 게이트 롤백] {gate_result['message']}",
                                    "[Quality Gate]")
                                # 롤백 후 Tool 스키마 갱신
                                updated_schemas = build_function_schemas()
                                brain.update_tools(updated_schemas)

                await broadcaster.emit("tool_result", result_str, f"📋 {role_label} Tool 결과: {tool_name}")

                # 결과를 Brain에 반환
                brain.add_tool_result(tool_call_id, result)

                # 신규 스킬 등록 시 → Brain의 Tool 목록 즉시 갱신
                if tool_name == "create_new_skill" and isinstance(result, dict) and result.get("success"):
                    new_skill_name = result.get("skill_name", "")
                    updated_schemas = build_function_schemas()
                    brain.update_tools(updated_schemas)
                    await broadcaster.emit(
                        "system",
                        f"🔄 Tool 목록 갱신 완료 ({len(updated_schemas)}개)\n"
                        f"✅ '{new_skill_name}' 이 Tool 목록에 추가됨\n"
                        f"⚡ 다음 루프에서 '{new_skill_name}'을 직접 호출하세요!",
                        "[Tool 목록 갱신]"
                    )

                # 스킬 생성 시 env_key_required → pending_email_task 저장 (SMTP 비번 등 환경변수 부족)
                if tool_name == "create_new_skill" and isinstance(result, dict) and result.get("env_key_required"):
                    _email_args = tool_args.get("test_args", {})
                    if _email_args.get("to_email"):
                        try:
                            Path("generated_skills/pending_email_task.json").write_text(
                                json.dumps(_email_args, ensure_ascii=False),
                                encoding="utf-8"
                            )
                            await broadcaster.emit("system",
                                f"💾 이메일 파라미터 저장 → pending_email_task.json ({_email_args.get('to_email')})",
                                "[Pending 저장]")
                        except Exception:
                            pass

                # 스킬 생성 시 mcp_install_required → pending_care_task에 MCP 설치 정보 추가
                if tool_name == "create_new_skill" and isinstance(result, dict) and result.get("mcp_install_required"):
                    _pct = Path("generated_skills/pending_care_task.json")
                    if _pct.exists():
                        try:
                            _pc_data = json.loads(_pct.read_text(encoding="utf-8"))
                            _pc_data["mcp_install_required"] = True
                            _pc_data["install_command"] = result.get("install_command", "")
                            _pc_data["mcp_service"] = result.get("service", "")
                            _pct.write_text(json.dumps(_pc_data, ensure_ascii=False), encoding="utf-8")
                        except Exception:
                            pass

                # 이메일 전송 성공 시 → pending 파일 정리
                if tool_name == "send_email_via_smtp" and isinstance(result, dict) and result.get("success"):
                    for _pf in [
                        Path("generated_skills/pending_email_task.json"),
                        Path("generated_skills/pending_intent.json"),
                    ]:
                        if _pf.exists():
                            _pf.unlink()
                    await broadcaster.emit("system", "🗑️ pending 파일 정리 완료 (이메일 전송 성공)", "[Pending 정리]")

                # 케어 규칙 등록 성공 시 → pending_care_task 정리
                if tool_name == "register_care_rule" and isinstance(result, dict) and result.get("rule_id"):
                    _pct = Path("generated_skills/pending_care_task.json")
                    if _pct.exists():
                        _pct.unlink()
                        await broadcaster.emit("system", "🗑️ pending_care_task 정리 완료 (케어 규칙 등록 성공)", "[Pending 정리]")


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


@app.post("/chat")
async def chat(request: Request):
    """대시보드 채팅창에서 보낸 명령 처리."""
    global _pending_original_context
    data = await request.json()
    user_message = data.get("message", "")

    if not user_message:
        return {"error": "No message provided"}

    # ── 프롬프트 주입 방어 (데이터/명령 분리 원칙) ──
    from core.skill_security import get_security_gate
    gate = get_security_gate()
    injection_findings = gate.scan_user_input(user_message)
    if injection_findings:
        await broadcaster.emit(
            "error",
            f"🛡️ [프롬프트 주입 탐지] 의심스러운 입력 감지:\n"
            + "\n".join(f"  - {f}" for f in injection_findings),
            "[보안 게이트]"
        )
        logger.warning(f"Prompt injection attempt detected: {injection_findings}")
        return {"response": "⚠️ 보안 정책에 위반되는 입력이 감지되었습니다. 일반적인 요청을 입력해 주세요."}

    logger.info(f"Chat command received: {user_message}")

    # ── 대시보드 버튼 callback → handle_telegram_message로 위임 ──
    # 텔레그램 인라인 버튼과 대시보드 버튼은 동일한 텍스트(callback_data 또는 라벨)를 보냄.
    # care_*/auto_rule_*/"자동화 등록"/"등록 안함" 등은 handle_telegram_message에서 처리 로직이 있다.
    _norm = user_message.strip().lstrip("✅❌📋🔧").strip().strip("[]").strip()
    _is_callback = (
        user_message.startswith("care_")
        or user_message.startswith("auto_rule_")
        or _norm in ("자동화 등록", "등록 안함", "자동화 안함", "Yes", "No", "등록")
    )
    if _is_callback:
        await handle_telegram_message(user_message, chat_id="dashboard")
        return {"response": ""}

    # 에이전트 루프 실행 (트리거 이벤트를 채팅 메시지로 설정)
    event = {
        "event": "user_command",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "dashboard_chat",
        "message": user_message
    }

    # ── 투숙객/예약 관련 키워드 감지 시 현재 예약 정보를 자동 주입 ──
    # Agent가 guest_email을 몰라서 "이메일 주소를 알려주세요"라고 되묻는 문제 방지
    _guest_keywords = [
        "투숙객", "게스트", "손님", "이메일", "메일", "예약", "booking", "guest",
        "날씨", "별보기", "주변", "관광", "맛집", "카페", "가이드", "핫플",
        "숙소", "위치", "좌표",
    ]
    if any(kw in user_message.lower() for kw in _guest_keywords):
        try:
            import httpx as _httpx
            _site_a_url = os.getenv("SITE_A_URL", "http://localhost:8001")
            async with _httpx.AsyncClient(timeout=3.0) as _client:
                _resp = await _client.get(f"{_site_a_url}/bookings")
                if _resp.status_code == 200:
                    _data = _resp.json()
                    _bookings = _data.get("bookings", []) or []
                    if _bookings:
                        event["current_bookings"] = _bookings
                        # 숙소 좌표도 이벤트에 주입 — run_agent_loop의 _coords_context 로직이
                        # 이 값을 프롬프트에 넣어서 LLM/Skill Factory가 위치를 정확히 사용하게 됨
                        _plat = _data.get("property_lat")
                        _plng = _data.get("property_lng")
                        _ploc = _data.get("property_location", "")
                        if _plat and _plng:
                            event["lat"] = _plat
                            event["lng"] = _plng
                            if _ploc:
                                event["location"] = _ploc
                        await broadcaster.emit(
                            "system",
                            f"📋 현재 예약 {len(_bookings)}건 + 숙소 좌표({_plat},{_plng})를 컨텍스트에 주입했습니다.",
                            "[예약 컨텍스트]",
                        )
        except Exception as _e:
            logger.debug(f"Booking context inject skipped: {_e}")

    # ── Proactive Care: /chat(대시보드 채팅)에서는 비활성화 ──
    # 사장님이 직접 채팅으로 보낸 메시지는 "일반 명령"이지 선제적 케어 트리거가 아니다.
    # 선제적 케어는 weather_changed webhook 등 자동 이벤트에서만 발동한다.
    # 여기서 care_engine.process_message()를 호출하면 "날씨" 같은 키워드만으로도
    # LLM이 "선제적 케어 분석 결과가 감지되었습니다"로 응답하는 오동작이 발생한다.

    # 원본 사용자 요청을 전역 컨텍스트에 보관
    # → 예약 확정(booking_confirmed) 등의 Webhook이 이 루프 실행 중에 발생하면,
    #   /webhook 핸들러가 original_context를 Webhook 이벤트에 자동으로 주입하여
    #   새 에이전트 루프도 원본 요청(이메일 전송 등)을 인지하고 처리한다.
    _pending_original_context = {
        "source": "dashboard_chat",
        "original_message": user_message,
    }

    # ── 세션 Brain 유지: 이전 대화 히스토리를 기억하며 연속 대화 가능 ──
    global _chat_brain
    if _chat_brain is None:
        _sys = load_all_scenes(SCENE_DIR)
        _tools = build_function_schemas()
        _chat_brain = Brain(system_prompt=_sys, tools_schema=_tools)
    result = await run_agent_loop(event, session_brain=_chat_brain)

    # 루프 완료 후 컨텍스트 초기화
    _pending_original_context = None
    
    return {"response": result.get("summary", "작업을 완료했습니다.")}



# ── API 엔드포인트 ───────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Carrier-Grade Trust 실시간 로그 대시보드."""
    html = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>숙박 사업자용 Agent Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'JetBrains Mono', 'Fira Code', 'SF Mono', monospace;
            background: #ffffff;
            color: #1f2937;
            height: 100vh;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }
        .header {
            background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
            border-bottom: 1px solid #e2e8f0;
            padding: 1rem 2rem;
            display: flex; align-items: center; justify-content: space-between;
        }
        .header h1 {
            font-size: 1.2rem; font-weight: 600;
            background: linear-gradient(90deg, #f59e0b, #ef4444, #8b5cf6);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }
        .header .status { display: flex; align-items: center; gap: 0.5rem; font-size: 0.8rem; color: #64748b; }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; animation: pulse 2s ease-in-out infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

        /* ── Network Topology ── */
        .topology {
            background: #f8fafc; border-bottom: 1px solid #e2e8f0;
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
            background: rgba(239,68,68,0.05); border: 1px dashed rgba(239,68,68,0.3);
        }
        .zone-public .zone-label { color: #dc2626; }
        .zone-public .topo-node { background: rgba(239,68,68,0.08); color: #dc2626; border: 1px solid rgba(239,68,68,0.2); }

        .topo-arrow {
            display: flex; align-items: center; color: #94a3b8; font-size: 0.65rem;
            padding: 0 0.4rem; flex-direction: column; gap: 0.15rem;
        }
        .arrow-line { color: #16a34a; font-weight: 700; font-size: 0.8rem; }
        .arrow-label { font-size: 0.55rem; color: #64748b; }

        .zone-private {
            background: rgba(22,163,74,0.04); border: 1px solid rgba(22,163,74,0.2);
            flex: 1;
        }
        .zone-private .zone-label { color: #16a34a; }
        .zone-private .topo-node { background: rgba(22,163,74,0.08); color: #16a34a; border: 1px solid rgba(22,163,74,0.2); }
        .zone-private .inner-row { display: flex; gap: 0.5rem; align-items: center; }
        .topo-vpal {
            display: flex; align-items: center; gap: 0.3rem;
            padding: 0.15rem 0.5rem; border-radius: 10px;
            background: rgba(14,165,233,0.08); border: 1px solid rgba(14,165,233,0.25);
            color: #0284c7; font-size: 0.55rem; font-weight: 600;
        }
        @keyframes dataFlow {
            0% { opacity: 0.3; } 50% { opacity: 1; } 100% { opacity: 0.3; }
        }
        .flow-dot {
            width: 4px; height: 4px; border-radius: 50%; background: #16a34a;
            animation: dataFlow 1.5s ease-in-out infinite;
        }
        .flow-dot:nth-child(2) { animation-delay: 0.3s; }
        .flow-dot:nth-child(3) { animation-delay: 0.6s; }

        /* ── Main Layout ── */
        .main-content { display: flex; flex: 1; overflow: hidden; position: relative; }
        .log-panel { flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 200px; }

        /* ── Resizer ── */
        .panel-resizer {
            width: 5px; cursor: col-resize; background: #e2e8f0;
            transition: background 0.2s; flex-shrink: 0;
        }
        .panel-resizer:hover, .panel-resizer.active { background: #8b5cf6; }

        /* ── Chat Panel ── */
        .chat-panel {
            flex: 1; min-width: 250px; background: #ffffff; display: flex; flex-direction: column;
            border-right: 1px solid #e2e8f0;
        }
        .chat-header {
            padding: 0.6rem 1.2rem; background: rgba(139, 92, 246, 0.04);
            border-bottom: 1px solid #e2e8f0; font-size: 0.8rem; font-weight: 700;
            color: #7c3aed; display: flex; align-items: center; gap: 0.5rem;
        }
        .chat-body { flex: 1; overflow-y: auto; padding: 1rem; display: flex; flex-direction: column; gap: 1rem; background: #fafafa; }
        .chat-input-area {
            padding: 1rem; background: #f8fafc; border-top: 1px solid #e2e8f0;
            display: flex; flex-direction: column; gap: 0.6rem;
        }
        .chat-input {
            width: 100%; background: #ffffff; border: 1px solid #d1d5db; border-radius: 6px;
            padding: 0.6rem 0.8rem; color: #1f2937; font-size: 0.8rem; resize: none;
            outline: none; transition: border-color 0.2s;
        }
        .chat-input:focus { border-color: #8b5cf6; }
        .chat-btn {
            background: #7c3aed; color: #ffffff; border: none; border-radius: 6px;
            padding: 0.5rem; font-weight: 700; font-size: 0.75rem; cursor: pointer;
            transition: opacity 0.2s, transform 0.1s;
        }
        .chat-btn:hover { opacity: 0.9; }
        .chat-btn:active { transform: scale(0.98); }
        .chat-msg {
            padding: 0.6rem 0.8rem; border-radius: 12px; font-size: 0.75rem; max-width: 85%;
            animation: slideIn 0.3s ease-out;
        }
        @keyframes slideIn { from { opacity: 0; transform: translateX(10px); } to { opacity: 1; transform: translateX(0); } }
        .msg-user { align-self: flex-end; background: #f1f5f9; color: #1e293b; border-bottom-right-radius: 2px; border: 1px solid #e2e8f0; }
        .msg-agent { align-self: flex-start; background: rgba(139, 92, 246, 0.08); color: #6d28d9; border-bottom-left-radius: 2px; border: 1px solid rgba(139, 92, 246, 0.2); }

        .log-header {
            padding: 0.5rem 1.5rem; background: #f8fafc; border-bottom: 1px solid #e2e8f0;
            display: flex; justify-content: space-between; align-items: center;
            font-size: 0.75rem; color: #64748b; cursor: pointer; user-select: none;
            transition: background 0.2s;
        }
        .log-header:hover { background: #f1f5f9; }
        .log-header-title { display: flex; align-items: center; gap: 0.4rem; }
        .toggle-icon { display: inline-block; transition: transform 0.2s; font-size: 0.6rem; }
        .log-container { flex: 1; overflow-y: auto; padding: 0.5rem 1.5rem; }
        .log-container.collapsed { display: none; }

        .log-entry {
            display: flex; gap: 0.5rem; padding: 0.35rem 0.6rem; margin-bottom: 0.15rem;
            border-radius: 6px; font-size: 0.78rem; line-height: 1.5;
            animation: fadeIn 0.3s ease-out; border-left: 3px solid transparent;
        }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; } }
        .log-entry:hover { background: rgba(0,0,0,0.02); }
        .log-time { color: #94a3b8; white-space: nowrap; min-width: 72px; flex-shrink: 0; font-size: 0.7rem; }
        .log-meta { white-space: nowrap; min-width: 220px; flex-shrink: 0; font-weight: 600; font-size: 0.75rem; }
        .log-content { flex: 1; white-space: pre-wrap; word-break: break-word; color: #374151; }

        /* Role colors */
        .log-entry.webhook    { border-left-color: #d97706; } .log-entry.webhook .log-meta { color: #d97706; }
        .log-entry.system     { border-left-color: #2563eb; } .log-entry.system .log-meta { color: #2563eb; }
        .log-entry.thinking   { border-left-color: #7c3aed; } .log-entry.thinking .log-meta { color: #7c3aed; }
        .log-entry.reasoning  { border-left-color: #8b5cf6; } .log-entry.reasoning .log-meta { color: #8b5cf6; }
        .log-entry.tool_call  { border-left-color: #0284c7; } .log-entry.tool_call .log-meta { color: #0284c7; }
        .log-entry.tool_result { border-left-color: #16a34a; } .log-entry.tool_result .log-meta { color: #16a34a; }
        .log-entry.complete   { border-left-color: #22c55e; background: rgba(34,197,94,0.06); } .log-entry.complete .log-meta { color: #16a34a; }
        .log-entry.error      { border-left-color: #dc2626; background: rgba(220,38,38,0.05); } .log-entry.error .log-meta { color: #dc2626; }
        .log-entry.loop       { border-left-color: #94a3b8; background: rgba(148,163,184,0.05); } .log-entry.loop .log-meta { color: #94a3b8; }
        .log-entry.scene      { border-left-color: #ea580c; background: rgba(234,88,12,0.04); } .log-entry.scene .log-meta { color: #ea580c; } .log-entry.scene .log-content { font-size: 0.7rem; color: #94a3b8; }
        .log-entry.tool_schema { border-left-color: #16a34a; background: rgba(22,163,74,0.04); } .log-entry.tool_schema .log-meta { color: #16a34a; }

        /* Telco highlight events */
        .log-entry.policy {
            border-left-color: #d97706; background: rgba(217,119,6,0.06);
            border: 1px solid rgba(217,119,6,0.15); border-left: 3px solid #d97706;
        }
        .log-entry.policy .log-meta { color: #b45309; font-weight: 700; }
        .log-entry.policy .log-content { color: #92400e; font-weight: 600; }

        .log-entry.vpal {
            border-left-color: #0284c7; background: rgba(2,132,199,0.05);
            border: 1px solid rgba(2,132,199,0.12); border-left: 3px solid #0284c7;
        }
        .log-entry.vpal .log-meta { color: #0284c7; font-weight: 700; }
        .log-entry.vpal .log-content { color: #0369a1; }

        .log-entry.signature {
            border-left-color: #7c3aed; background: rgba(124,58,237,0.05);
            border: 1px solid rgba(124,58,237,0.12); border-left: 3px solid #7c3aed;
        }
        .log-entry.signature .log-meta { color: #7c3aed; font-weight: 700; }
        .log-entry.signature .log-content { color: #6d28d9; }

        .log-entry.divider {
            border-left: none; border-top: 1px solid #e2e8f0;
            margin-top: 0.8rem; margin-bottom: 0.3rem; padding-top: 0.6rem;
        }
        .log-entry.divider .log-meta, .log-entry.divider .log-time { display: none; }
        .log-entry.divider .log-content { color: #2563eb; font-weight: 700; font-size: 0.8rem; letter-spacing: 0.05em; }

        .empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 60vh; color: #94a3b8; gap: 1rem; }
        .empty-state .icon { font-size: 3rem; }

        /* ── Left Sidebar ── */
        .left-sidebar {
            width: 240px; flex-shrink: 0;
            display: flex; flex-direction: column;
            background: #ffffff; border-right: 1px solid #e2e8f0;
            overflow: hidden;
        }

        /* ── Skill Library Panel ── */
        .skill-library-panel {
            background: #ffffff; display: flex; flex-direction: column;
            flex: 1 1 50%; min-height: 0; overflow: hidden;
            border-bottom: 1px solid #e2e8f0;
        }
        .skill-library-header {
            padding: 0.5rem 1rem; font-size: 0.78rem; font-weight: 700;
            color: #16a34a; display: flex; align-items: center; justify-content: space-between;
            border-bottom: 1px solid #e2e8f0; background: rgba(22,163,74,0.03);
            cursor: pointer; user-select: none; transition: background 0.2s; flex-shrink: 0;
        }
        .skill-library-header:hover { background: rgba(22,163,74,0.06); }
        .skill-cards {
            display: flex; flex-direction: column; gap: 0.4rem;
            padding: 0.5rem 0.8rem; overflow-y: auto; flex: 1; min-height: 0;
        }
        .skill-card {
            background: rgba(22,163,74,0.05); border: 1px solid rgba(22,163,74,0.2);
            border-radius: 8px; padding: 0.35rem 0.6rem; font-size: 0.7rem;
            display: flex; flex-direction: column; gap: 0.1rem;
            animation: fadeIn 0.4s ease-out; width: 100%; box-sizing: border-box;
        }
        .skill-card-name { color: #16a34a; font-weight: 700; }
        .skill-card-desc { color: #64748b; font-size: 0.62rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .skill-card-service { color: #22c55e; font-size: 0.58rem; }
        .skill-card-delete {
            background: none; border: none; cursor: pointer; color: #94a3b8;
            font-size: 0.7rem; padding: 0; margin-top: 0.1rem; text-align: right;
            transition: color 0.2s;
        }
        .skill-card-delete:hover { color: #dc2626; }
        .skill-empty { color: #94a3b8; font-size: 0.72rem; padding: 0.5rem 1.5rem; }

        /* ── Active Rules Panel ── */
        .rules-panel {
            background: #ffffff; display: flex; flex-direction: column;
            flex: 1 1 50%; min-height: 0; overflow: hidden;
        }
        .rules-header {
            padding: 0.5rem 1rem; font-size: 0.78rem; font-weight: 700;
            color: #d97706; display: flex; align-items: center; justify-content: space-between;
            border-bottom: 1px solid #e2e8f0; background: rgba(217,119,6,0.03);
            cursor: pointer; user-select: none; transition: background 0.2s; flex-shrink: 0;
        }
        .rules-header:hover { background: rgba(217,119,6,0.06); }
        .rules-cards {
            display: flex; flex-direction: column; gap: 0.4rem;
            padding: 0.5rem 0.8rem; overflow-y: auto; flex: 1; min-height: 0;
        }
        .rule-card {
            background: rgba(217,119,6,0.04); border: 1px solid rgba(217,119,6,0.2);
            border-radius: 8px; padding: 0.4rem 0.6rem; font-size: 0.7rem;
            display: flex; flex-direction: column; gap: 0.15rem;
            animation: fadeIn 0.4s ease-out; width: 100%; box-sizing: border-box;
        }
        .rule-card-action {
            color: #92400e; font-weight: 700; font-size: 0.72rem;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .rule-card-trigger { color: #64748b; font-size: 0.62rem; }
        .rule-card-meta { color: #94a3b8; font-size: 0.58rem; display: flex; gap: 0.5rem; align-items: center; }
        .rule-badge-auto { background: rgba(22,163,74,0.12); color: #16a34a; padding: 0.05rem 0.35rem; border-radius: 6px; font-size: 0.55rem; font-weight: 700; }
        .rule-badge-manual { background: rgba(100,116,139,0.12); color: #64748b; padding: 0.05rem 0.35rem; border-radius: 6px; font-size: 0.55rem; font-weight: 700; }
        .rule-card-delete {
            background: none; border: none; cursor: pointer; color: #94a3b8;
            font-size: 0.7rem; padding: 0; text-align: right; transition: color 0.2s;
        }
        .rule-card-delete:hover { color: #dc2626; }
        .rules-empty { color: #94a3b8; font-size: 0.72rem; padding: 0.5rem 1.5rem; }

        /* ── Notary Table ── */
        .notary-panel {
            background: #ffffff; border-top: 1px solid #e2e8f0;
            max-height: 200px; overflow-y: auto; padding: 0;
        }
        .notary-header {
            padding: 0.6rem 1.5rem; font-size: 0.8rem; font-weight: 700;
            color: #d97706; display: flex; align-items: center; gap: 0.5rem;
            border-bottom: 1px solid #e2e8f0; background: rgba(217,119,6,0.03);
            position: sticky; top: 0; z-index: 2;
        }
        .notary-table { width: 100%; border-collapse: collapse; }
        .notary-table th {
            padding: 0.4rem 0.8rem; text-align: left; font-size: 0.65rem;
            color: #64748b; text-transform: uppercase; font-weight: 600;
            border-bottom: 1px solid #e2e8f0; background: #ffffff;
            position: sticky; top: 34px; z-index: 1;
        }
        .notary-table td {
            padding: 0.35rem 0.8rem; font-size: 0.72rem;
            border-bottom: 1px solid #f1f5f9;
        }
        .notary-table tr:hover { background: rgba(0,0,0,0.02); }
        .notary-badge {
            padding: 0.1rem 0.4rem; border-radius: 8px; font-size: 0.6rem; font-weight: 600;
        }
        .badge-issued { background: rgba(34,197,94,0.12); color: #16a34a; }
        .badge-registered { background: rgba(37,99,235,0.12); color: #2563eb; }
        .badge-denied { background: rgba(220,38,38,0.12); color: #dc2626; }
        .badge-isolated { background: rgba(220,38,38,0.15); color: #dc2626; }
        .badge-reactivated { background: rgba(34,197,94,0.15); color: #16a34a; }

        .footer {
            background: #f8fafc; border-top: 1px solid #e2e8f0;
            padding: 0.4rem 2rem; font-size: 0.65rem; color: #94a3b8;
            display: flex; justify-content: space-between;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🏨 숙박 사업자용 Agent Dashboard</h1>
        <div class="status">
            <div class="status-dot" id="statusDot"></div>
            <span id="statusText">대기 중</span>
        </div>
    </div>

    <div class="main-content">
        <!-- Left Sidebar: Skill Library + Active Rules -->
        <aside class="left-sidebar">
            <div class="skill-library-panel" id="skillLibraryPanel">
                <div class="skill-library-header" onclick="toggleSkillPanel()">
                    <span>🧩 Skills <span id="skillCount">0</span></span>
                    <span class="toggle-icon" id="skillToggleIcon" style="font-size:0.6rem;">▼</span>
                </div>
                <div class="skill-cards" id="skillCards" style="display:flex;"></div>
            </div>
            <div class="rules-panel" id="rulesPanel">
                <div class="rules-header" onclick="toggleRulesPanel()">
                    <span>⚡ Rules <span id="rulesCount">0</span></span>
                    <span class="toggle-icon" id="rulesToggleIcon" style="font-size:0.6rem;">▼</span>
                </div>
                <div class="rules-cards" id="rulesCards" style="display:flex;"></div>
            </div>
        </aside>

        <!-- Chat Main -->
        <div class="chat-panel" id="chatPanel">
            <div class="chat-header">💬 Agent Command Center</div>
            <div class="chat-body" id="chatBody">
                <div class="chat-msg msg-agent">반갑습니다. 당신의 AI 에이전트 입니다. 어떤 작업을 도와드릴까요?</div>
            </div>
            <div class="chat-input-area">
                <textarea class="chat-input" id="chatInput" placeholder="에이전트에게 명령을 입력하세요... (Shift+Enter로 줄바꿈)" rows="2"></textarea>
                <button class="chat-btn" onclick="sendChatMessage()">전송 (Enter)</button>
                <button class="chat-btn" style="background:transparent; border:1px solid #d1d5db; color:#64748b; font-size:0.6rem;" onclick="clearLogs()">로그 초기화</button>
            </div>
        </div>
    </div>

    <div class="footer">
        <span>숙박 사업자용 Agent Dashboard v0.2.0</span>
        <span id="clock"></span>
    </div>

    <script>
        const statusText = document.getElementById('statusText');
        const statusDot = document.getElementById('statusDot');
        const clock = document.getElementById('clock');

        function updateClock() { clock.textContent = new Date().toLocaleTimeString('ko-KR'); }
        setInterval(updateClock, 1000); updateClock();

        const chatBody = document.getElementById('chatBody');
        const chatInput = document.getElementById('chatInput');

        async function sendChatMessage() {
            const text = chatInput.value.trim();
            if (!text) return;
            
            chatInput.value = '';
            appendChatMessage('user', text);
            statusText.textContent = '처리 중...';
            statusDot.style.background = '#f59e0b';

            try {
                const response = await fetch('/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message: text })
                });
                const data = await response.json();
                appendChatMessage('agent', data.response || '작업을 수행했습니다.');
            } catch (err) {
                appendChatMessage('agent', '에러: 서버와 통신할 수 없습니다.');
            } finally {
                statusText.textContent = '대기 중';
                statusDot.style.background = '#3fb950';
            }
        }

        function appendChatMessage(role, text) {
            const div = document.createElement('div');
            div.className = 'chat-msg msg-' + role;
            // newline to <br>, preserve double spaces
            div.innerHTML = escapeHtml(text)
                .replace(/\\n/g, '<br>')
                .replace(/  /g, '&nbsp; ');
            chatBody.appendChild(div);
            chatBody.scrollTop = chatBody.scrollHeight;
        }

        chatInput.addEventListener('keydown', (e) => {
            // 한글 조합 중 엔터 키 중복 발생 방지
            if (e.isComposing || e.keyCode === 229) return;

            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendChatMessage();
            }
        });

        function escapeHtml(t) { const d = document.createElement('div'); d.textContent = t; return d.innerHTML; }

        // ── 에이전트 → 대시보드 버튼 프롬프트 렌더링 ──
        function appendAgentPrompt(message, buttons) {
            const wrap = document.createElement('div');
            wrap.className = 'chat-msg msg-agent';
            const text = document.createElement('div');
            text.innerHTML = escapeHtml(message || '')
                .replace(/\\n/g, '<br>')
                .replace(/  /g, '&nbsp; ');
            wrap.appendChild(text);
            if (buttons && buttons.length) {
                const btnRow = document.createElement('div');
                btnRow.style.cssText =
                    'display:flex;flex-direction:column;gap:0.4rem;margin-top:0.6rem;align-items:stretch;';
                buttons.forEach(label => {
                    const b = document.createElement('button');
                    b.textContent = label;
                    b.title = label;   // 말줄임 시 마우스 호버로 전체 텍스트 확인
                    b.style.cssText =
                        'display:block;width:100%;min-width:0;max-width:100%;'
                      + 'padding:0.55rem 0.9rem;font-size:0.82rem;'
                      + 'border:1px solid #c4b5fd;background:#f5f3ff;color:#6d28d9;'
                      + 'border-radius:10px;cursor:pointer;text-align:left;'
                      + 'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;'
                      + 'box-sizing:border-box;';
                    b.onclick = () => {
                        // 버튼 클릭 → 채팅창에 사용자 메시지로 반영 + /chat 전송
                        appendChatMessage('user', label);
                        statusText.textContent = '처리 중...';
                        statusDot.style.background = '#f59e0b';
                        Array.from(btnRow.children).forEach(el => el.disabled = true);
                        fetch('/chat', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ message: label })
                        })
                        .then(r => r.json())
                        .then(data => {
                            if (data.response) appendChatMessage('agent', data.response);
                        })
                        .catch(() => appendChatMessage('agent', '에러: 서버와 통신할 수 없습니다.'))
                        .finally(() => {
                            statusText.textContent = '대기 중';
                            statusDot.style.background = '#3fb950';
                        });
                    };
                    btnRow.appendChild(b);
                });
                wrap.appendChild(btnRow);
            }
            chatBody.appendChild(wrap);
            chatBody.scrollTop = chatBody.scrollHeight;
        }

        // ── SSE 구독: 외부 트리거 결과 + 에이전트 프롬프트 ──
        try {
            const eventSource = new EventSource('/logs/stream');
            eventSource.onmessage = (e) => {
                try {
                    const entry = JSON.parse(e.data);
                    if (entry.type === 'external_result') {
                        appendChatMessage('agent', entry.content || '');
                        statusText.textContent = '대기 중';
                        statusDot.style.background = '#3fb950';
                    } else if (entry.type === 'dashboard_prompt') {
                        const payload = JSON.parse(entry.content || '{}');
                        appendAgentPrompt(payload.message || '', payload.buttons || []);
                    }
                } catch (err) { /* ignore malformed events */ }
            };
            eventSource.onerror = () => { /* auto reconnect */ };
        } catch (err) { console.warn('SSE subscription failed', err); }

        // Skill Library
        function toggleSkillPanel() {
            const cards = document.getElementById('skillCards');
            const icon = document.getElementById('skillToggleIcon');
            const isHidden = cards.style.display === 'none';
            cards.style.display = isHidden ? 'flex' : 'none';
            icon.textContent = isHidden ? '▼' : '▶';
            if (isHidden) loadSkills();
        }

        // ── 기술 용어 → 사장님이 이해하기 쉬운 한글 라벨 매핑 ──
        const SKILL_LABEL_MAP = {
            send_email_via_smtp:              { icon: '✉️', name: '이메일 발송' },
            generate_local_guide:             { icon: '📍', name: '지역 가이드 만들기' },
            recommend_indoor_places:          { icon: '🏠', name: '실내 명소 추천' },
            generate_indoor_places_guide:     { icon: '🏠', name: '실내 명소 가이드' },
            generate_personalized_travel_guidebook: { icon: '📖', name: '맞춤 여행 가이드' },
            create_indoor_activity_guide:     { icon: '🏠', name: '실내 체험 가이드' },
            search_indoor_hotplaces:          { icon: '🔍', name: '실내 핫플 검색' },
            create_travel_guide:              { icon: '📖', name: '여행 가이드 제작' },
        };
        const SKILL_VERB_HINT = [
            { re: /email|mail|smtp/i,           icon: '✉️', hint: '이메일' },
            { re: /guide|guidebook/i,           icon: '📖', hint: '가이드' },
            { re: /indoor|실내/i,               icon: '🏠', hint: '실내 장소' },
            { re: /place|location|poi|map/i,    icon: '📍', hint: '장소 검색' },
            { re: /weather|rain|snow/i,         icon: '🌦️', hint: '날씨' },
            { re: /search|recommend/i,          icon: '🔍', hint: '검색·추천' },
        ];
        function prettifySkill(name) {
            if (SKILL_LABEL_MAP[name]) return SKILL_LABEL_MAP[name];
            for (const h of SKILL_VERB_HINT) {
                if (h.re.test(name)) {
                    return { icon: h.icon, name: h.hint };
                }
            }
            // 폴백: snake_case → 공백 구분 + 단어 첫글자 대문자
            const pretty = name.replace(/_/g, ' ').replace(/\\b\\w/g, c => c.toUpperCase());
            return { icon: '⚡', name: pretty };
        }
        function prettifyService(service) {
            if (!service) return '';
            if (/google-maps|google_maps|maps/i.test(service)) return '🗺️ Google 지도';
            if (/gmail|smtp/i.test(service))                   return '✉️ 이메일 (Gmail)';
            if (/modelcontextprotocol\\/server-(\\w+)/i.test(service)) return '🔌 MCP 서버';
            if (/smithery/i.test(service))                     return '🔌 Smithery';
            return '🔌 외부 서비스';
        }

        function loadSkills() {
            fetch('/skills')
                .then(r => r.json())
                .then(data => {
                    const skills = data.skills || [];
                    document.getElementById('skillCount').textContent = skills.length;
                    const cards = document.getElementById('skillCards');
                    if (skills.length === 0) {
                        cards.innerHTML = '<span class="skill-empty">아직 만들어진 기능이 없습니다. 대화창에 필요한 작업을 말씀하시면 자동으로 준비됩니다.</span>';
                        return;
                    }
                    cards.innerHTML = skills.map(s => {
                        const pretty = prettifySkill(s.name);
                        const serviceLabel = prettifyService(s.service);
                        return `<div class="skill-card" title="${escapeHtml(s.name)}">
                            <span class="skill-card-name">${pretty.icon} ${escapeHtml(pretty.name)}</span>
                            ${serviceLabel ? `<span class="skill-card-service">${serviceLabel}</span>` : ''}
                            <button class="skill-card-delete" onclick="deleteSkill('${escapeHtml(s.name)}')" title="기능 삭제">🗑️ 삭제</button>
                        </div>`;
                    }).join('');
                })
                .catch(() => {});
        }

        async function deleteSkill(skillName) {
            if (!confirm(`'${skillName}' 스킬을 삭제하시겠습니까?\n삭제하면 파일과 레지스트리에서 즉시 제거됩니다.`)) return;
            try {
                const resp = await fetch(`/skills/${encodeURIComponent(skillName)}`, { method: 'DELETE' });
                const data = await resp.json();
                if (resp.ok) {
                    loadSkills();
                } else {
                    alert(`삭제 실패: ${data.error || '알 수 없는 오류'}`);
                }
            } catch(e) {
                alert(`통신 오류: ${e}`);
            }
        }

        loadSkills();
        setInterval(loadSkills, 5000);

        // ── Active Rules ──
        function toggleRulesPanel() {
            const cards = document.getElementById('rulesCards');
            const icon = document.getElementById('rulesToggleIcon');
            const isHidden = cards.style.display === 'none';
            cards.style.display = isHidden ? 'flex' : 'none';
            icon.textContent = isHidden ? '▼' : '▶';
            if (isHidden) loadRules();
        }

        const TRIGGER_LABEL = {
            rain:       '🌧️ 비 올 때',
            heavy_rain: '⛈️ 폭우 올 때',
            snow:       '❄️ 눈 올 때',
            wind:       '🌬️ 강풍 불 때',
            heat:       '🔥 폭염 일 때',
            cold:       '🥶 한파 일 때',
            general:    '🌦️ 날씨 변할 때',
        };
        function prettifyTrigger(cat) { return TRIGGER_LABEL[cat] || ('🌦️ ' + (cat || '이벤트')); }

        function loadRules() {
            fetch('/care/rules')
                .then(r => r.json())
                .then(data => {
                    const rules = data.rules || [];
                    document.getElementById('rulesCount').textContent = rules.length;
                    const cards = document.getElementById('rulesCards');
                    if (rules.length === 0) {
                        cards.innerHTML = '<span class="rules-empty">등록된 자동화가 없습니다. 케어 제안을 승인하면 여기에 추가됩니다.</span>';
                        return;
                    }
                    cards.innerHTML = rules.map(r => {
                        const badge = r.auto_execute
                            ? '<span class="rule-badge-auto">자동</span>'
                            : '<span class="rule-badge-manual">승인</span>';
                        const trigger = prettifyTrigger(r.trigger_category);
                        const skillPretty = r.skill_name ? prettifySkill(r.skill_name) : null;
                        const skillLine = skillPretty
                            ? `사용 기능: ${skillPretty.icon} ${escapeHtml(skillPretty.name)}`
                            : '';
                        return `<div class="rule-card" title="${escapeHtml(r.action || '')}">
                            <span class="rule-card-action">${badge} ${escapeHtml(r.action || '')}</span>
                            <span class="rule-card-trigger">${trigger} · 승인 ${r.approval_count || 0}회</span>
                            ${skillLine ? `<span class="rule-card-meta">${skillLine}</span>` : ''}
                            <button class="rule-card-delete" onclick="deleteRule('${escapeHtml(r.id)}')" title="자동화 삭제">🗑️ 삭제</button>
                        </div>`;
                    }).join('');
                })
                .catch(() => {});
        }

        async function deleteRule(ruleId) {
            if (!confirm('이 자동화 규칙을 삭제하시겠습니까?')) return;
            try {
                const resp = await fetch('/care/rules/' + encodeURIComponent(ruleId) + '/delete', { method: 'POST' });
                const data = await resp.json();
                if (resp.ok) {
                    loadRules();
                } else {
                    alert('삭제 실패: ' + (data.error || '알 수 없는 오류'));
                }
            } catch(e) {
                alert('통신 오류: ' + e);
            }
        }

        loadRules();
        setInterval(loadRules, 5000);
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
async def get_log_history():
    """지금까지의 로그 히스토리 반환."""
    return broadcaster.get_history()

@app.post("/logs/clear")
async def clear_logs():
    """로그 내역을 완전 초기화합니다."""
    broadcaster.clear_history()
    return {"status": "success"}


@app.post("/webhook")
async def webhook(request: Request):
    """사이트 A의 예약/날씨 이벤트를 수신하여 에이전트 루프를 트리거."""
    try:
        event = await request.json()
        logger.info(f"Webhook received: {json.dumps(event, ensure_ascii=False)}")

        # ── 새 예약 Webhook → 대시보드에 간단 알림 ──
        if event.get("event") == "booking_confirmed":
            _b = event.get("booking", {}) or {}
            _guest = _b.get("guest_name") or "?"
            _email = _b.get("guest_email") or ""
            _room = _b.get("room_id") or "?"
            _ci = _b.get("check_in") or "?"
            _co = _b.get("check_out") or "?"
            _lines = [
                f"[🏨 Site A] 새 예약 접수",
                f"• 투숙객: {_guest}" + (f" ({_email})" if _email else ""),
                f"• 객실: {_room}",
                f"• 기간: {_ci} ~ {_co}",
            ]
            await broadcaster.emit(
                "external_result",
                "\n".join(_lines),
                "🏨 예약 접수",
            )
            # 이후 agent loop는 Site B 동기화를 조용히 수행 — 최종 요약 중복 방지
            event["_skip_dashboard_echo"] = True

        # ── 날씨 변경 Webhook → Proactive Care 트리거 ──
        elif event.get("event") == "weather_changed":
            weather_info = event.get("weather", {})
            affected = event.get("affected_bookings", [])
            new_condition = weather_info.get("current", "")
            weather_label = weather_info.get("label", new_condition)

            await broadcaster.emit(
                "webhook",
                f"🌦️ 날씨 변경 감지: {weather_info.get('previous')} → {new_condition}\n"
                f"   영향 받는 예약: {len(affected)}건",
                "🌦️ [SITE A] Weather Webhook"
            )

            # Proactive Care 분석 (키워드 감지 우회, 직접 weather condition 전달)
            care_engine = get_care_engine()
            care_result = await care_engine.process_weather_event(
                weather_condition=new_condition,
                weather_label=weather_label,
                affected_bookings=affected,
                location=event.get("location", ""),
                previous_weather=weather_info.get("previous", ""),
            )

            if care_result:
                event["proactive_care_context"] = care_result["analysis"]

                if care_result["auto_actions"]:
                    # ── Auto-execute path: 제안 버튼 없이 즉시 실행 ──
                    event["proactive_care"] = {
                        "auto_actions": care_result["auto_actions"],
                        "analysis": care_result["analysis"],
                    }
                    # save_pending_proposals 호출 안함 (버튼 없으므로 불필요)
                    auto_names = "\n".join(
                        f"  • {a['action']}" for a in care_result["auto_actions"]
                    )
                    await telegram_client.send_message(
                        f"⚡ <b>자동 실행 규칙 감지</b>\n{auto_names}\n\n"
                        f"자동으로 실행 중입니다. 완료 후 결과를 보고드리겠습니다.",
                        parse_mode="HTML",
                    )
                    event["_skip_dashboard_echo"] = True
                else:
                    # ── Manual path: 기존 제안 + 승인 버튼 플로우 ──
                    save_pending_proposals(care_result["analysis"], affected_bookings=affected)
                    tg_msg = care_engine.format_care_telegram_message(care_result["analysis"])
                    # 텔레그램 token/user_id와 무관하게 전송 시도 — 대시보드 브로드캐스트는 항상 수행됨
                    await telegram_client.send_message(
                        tg_msg,
                        parse_mode="HTML",
                        reply_markup=care_result["telegram_buttons"],
                    )
                    # 이미 대시보드에 케어 제안 카드가 갔으니 run_agent_loop의 최종 요약은 중복 전송 방지
                    event["_skip_dashboard_echo"] = True

            result = await run_agent_loop(event)
            return JSONResponse(content=result)

        # ── 원본 컨텍스트 자동 주입 ──────────────────────────────
        # 사용자 명령(chat/telegram)이 처리 중일 때 Webhook이 도착하면,
        # 원본 요청 메시지를 Webhook 이벤트에 주입하여 새 루프에 전달한다.
        # 이를 통해 "이메일 보내줘" 등의 후속 작업이 컨텍스트 소실 없이 계속된다.
        if _pending_original_context and "original_context" not in event:
            event["original_context"] = _pending_original_context
            # Webhook agent loop는 Site B 동기화만 수행. 텔레그램 보고는 원본 텔레그램 루프가 담당.
            event["suppress_telegram_report"] = True
            logger.info(f"Injected original_context into webhook event: {_pending_original_context}")
            await broadcaster.emit(
                "system",
                f"🔗 원본 컨텍스트 주입: '{_pending_original_context.get('original_message', '')[:60]}...'\n"
                f"   → Webhook 루프가 Site B 동기화만 처리합니다. (텔레그램 보고 생략)",
                "🔗 Context Bridge"
            )

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


# ── Skill Library API ────────────────────────────────────

@app.get("/skills")
async def get_skills():
    """등록된 생성 스킬 목록 반환 (카테고리별 그룹핑 포함)."""
    from core.skill_registry import get_skill_registry
    registry = get_skill_registry()
    all_skills = registry.get_all_skills()

    # 카테고리별 그룹핑
    by_category: dict[str, list] = {}
    for skill in all_skills:
        cat = skill.get("category", "Uncategorized")
        by_category.setdefault(cat, []).append(skill)

    # skill_index.json에서 categories 메타데이터 로드
    categories_meta = {}
    try:
        idx_path = Path(__file__).resolve().parent.parent / "generated_skills" / "skill_index.json"
        if idx_path.exists():
            idx = json.loads(idx_path.read_text(encoding="utf-8"))
            categories_meta = idx.get("categories", {})
    except Exception:
        pass

    return {
        "skills": all_skills,
        "by_category": by_category,
        "categories": categories_meta,
        "stats": registry.get_stats(),
    }


@app.get("/skills/{skill_name}/code")
async def get_skill_code(skill_name: str):
    """특정 스킬의 소스 코드 반환 (감사 목적)."""
    from pathlib import Path
    skill_path = Path("generated_skills") / f"{skill_name}.py"
    if not skill_path.exists():
        return JSONResponse(status_code=404, content={"error": "Skill not found"})
    return {"skill_name": skill_name, "code": skill_path.read_text(encoding="utf-8")}


@app.delete("/skills/{skill_name}")
async def delete_skill(skill_name: str):
    """생성된 스킬을 완전히 삭제 (파일 + 레지스트리 + 인덱스)."""
    from core.skill_registry import get_skill_registry
    registry = get_skill_registry()

    # 존재 여부 확인
    all_skills = registry.get_all_skills()
    skill_names = [s["name"] for s in all_skills]
    if skill_name not in skill_names:
        # 파일이 있을 수도 있으니 파일도 확인
        from pathlib import Path
        if not (Path("generated_skills") / f"{skill_name}.py").exists():
            return JSONResponse(
                status_code=404,
                content={"error": f"스킬 '{skill_name}'을 찾을 수 없습니다."}
            )

    result = registry.delete_skill(skill_name)

    await broadcaster.emit(
        "system",
        f"🗑️ 스킬 삭제 완료: {skill_name}\n{result['message']}",
        "🗑️ Skill 삭제"
    )

    return result


# ── Enterprise API 엔드포인트 ─────────────────────────────────

@app.get("/quality")
async def get_quality_dashboard():
    """스킬 품질 대시보드 — 오류율, 품질 등급, 자가 치유 상태."""
    from core.skill_quality import get_quality_evaluator
    evaluator = get_quality_evaluator()
    stats = evaluator.get_dashboard_stats()

    summary = {
        "total_tracked": len(stats),
        "healthy": sum(1 for s in stats.values() if s["status"] == "healthy"),
        "warning": sum(1 for s in stats.values() if s["status"] == "warning"),
        "critical": sum(1 for s in stats.values() if s["status"] == "critical"),
        "skills": stats,
    }
    return summary


@app.get("/quality/{skill_name}")
async def get_skill_quality(skill_name: str):
    """특정 스킬의 품질 상세 정보."""
    from core.skill_quality import get_quality_evaluator
    evaluator = get_quality_evaluator()
    rec = evaluator.get_record(skill_name)
    if not rec:
        return JSONResponse(status_code=404, content={"error": f"품질 기록 없음: {skill_name}"})
    return {
        "skill_name": rec.skill_name,
        "total_runs": rec.total_runs,
        "error_rate": f"{rec.error_rate:.1%}",
        "avg_quality": f"{rec.avg_quality:.1f}",
        "avg_time_ms": f"{rec.avg_time_ms:.0f}",
        "consecutive_failures": rec.consecutive_failures,
        "needs_healing": evaluator.needs_healing(skill_name),
        "needs_rollback": evaluator.needs_rollback(skill_name),
    }


@app.post("/skills/{skill_name}/heal")
async def heal_skill(skill_name: str):
    """특정 스킬에 대해 수동으로 자가 치유를 트리거."""
    from core.skill_healer import get_skill_healer
    healer = get_skill_healer()

    await broadcaster.emit("system",
        f"🏥 [AI Doctor 복구 시도 중] 수동 트리거: {skill_name}",
        "[Self-Healing]")

    report = await healer.heal(
        skill_name=skill_name,
        original_error="수동 치유 트리거",
        execution_args={},
    )
    return report.to_dict()


@app.get("/skills/{skill_name}/versions")
async def get_skill_versions(skill_name: str):
    """특정 스킬의 버전 이력 조회."""
    from core.skill_versioning import get_version_manager
    vm = get_version_manager()
    return {
        "skill_name": skill_name,
        "current_version": vm.get_current_version(skill_name),
        "versions": vm.list_versions(skill_name),
        "history": vm.get_version_history(skill_name),
    }


@app.post("/skills/{skill_name}/rollback/{version}")
async def rollback_skill(skill_name: str, version: str):
    """특정 버전으로 스킬 롤백."""
    from core.skill_versioning import get_version_manager
    vm = get_version_manager()
    result = vm.rollback(skill_name, version)

    if result["success"]:
        await broadcaster.emit("system",
            f"↩️ 스킬 롤백 완료: {skill_name} → {version}",
            "[버전 롤백]")
        # Brain Tool 스키마 갱신
        from core.executor import build_function_schemas
        updated = build_function_schemas()
        logger.info(f"롤백 후 Tool 스키마 갱신: {len(updated)}개")

    return result


@app.get("/security/audit")
async def get_security_audit():
    """보안 감사 로그 최근 50건 조회."""
    audit_path = Path(__file__).resolve().parent.parent / "logs" / "security_audit.json"
    if not audit_path.exists():
        return {"entries": [], "total": 0}
    try:
        import json as _json
        history = _json.loads(audit_path.read_text(encoding="utf-8"))
        return {
            "entries": history[-50:],
            "total": len(history),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/healing/log")
async def get_healing_log():
    """자가 치유 로그 최근 20건 조회."""
    healing_path = Path(__file__).resolve().parent.parent / "logs" / "healing_log.json"
    if not healing_path.exists():
        return {"entries": [], "total": 0}
    try:
        import json as _json
        history = _json.loads(healing_path.read_text(encoding="utf-8"))
        return {
            "entries": history[-20:],
            "total": len(history),
            "healed_count": sum(1 for e in history if e.get("healed")),
            "escalated_count": sum(1 for e in history if not e.get("healed")),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── Proactive Care API 엔드포인트 ────────────────────────

@app.get("/care/rules")
async def get_care_rules():
    """등록된 Active Rule(자동화 규칙) 전체 목록 조회."""
    care_engine = get_care_engine()
    rules = care_engine.get_all_rules()
    auto_count = sum(1 for r in rules if r.get("auto_execute"))
    return {
        "rules": rules,
        "total": len(rules),
        "auto_execute_count": auto_count,
        "manual_count": len(rules) - auto_count,
    }


@app.post("/care/rules/{rule_id}/delete")
async def delete_care_rule(rule_id: str):
    """특정 Active Rule 삭제."""
    care_engine = get_care_engine()
    if care_engine.delete_rule(rule_id):
        return {"status": "deleted", "rule_id": rule_id}
    return JSONResponse(status_code=404, content={"error": f"Rule '{rule_id}' not found"})


@app.get("/care/status")
async def get_care_status():
    """Proactive Care 상태 조회 (규칙 수, 키워드 카테고리 등)."""
    care_engine = get_care_engine()
    rules = care_engine.get_all_rules()
    from core.proactive_care import _WEATHER_KEYWORDS, _URGENCY_MAP
    return {
        "enabled": True,
        "keyword_categories": {
            cat: {"keywords": kws, "urgency": _URGENCY_MAP[cat]}
            for cat, kws in _WEATHER_KEYWORDS.items()
        },
        "rules_total": len(rules),
        "rules_auto_execute": sum(1 for r in rules if r.get("auto_execute")),
    }
