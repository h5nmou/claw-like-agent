"""
proactive_care.py — Proactive Care Mode (선제적 케어 모드)

사용자 메시지에서 날씨/위험 키워드를 감지하고,
예약 데이터와 결합하여 선제적 케어 제안을 생성한다.

주요 기능:
  1. Simulated Sensing: 키워드 기반 위험 감지 (폭우, 눈, 폭설, 강풍 등)
  2. Contextual Reasoning: 예약/위치 데이터와 결합하여 LLM이 2-3개 케어 제안 생성
  3. Dynamic Skill Synthesis: 필요 시 Skill Factory로 즉석 스킬 생성
  4. Automation Learning: 승인된 케어 패턴을 Active Rule로 등록하여 자동화
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from dotenv import load_dotenv
from core.llm_client import get_llm_client, get_default_model

load_dotenv()

logger = logging.getLogger("proactive_care")

# ── 키워드 패턴 정의 ─────────────────────────────────────

# 날씨/재해 키워드 → 카테고리 매핑
# ⚠️ "rain"(일반 비)과 "heavy_rain"(폭우)는 반드시 구분할 것. 사장님 지시.
_WEATHER_KEYWORDS: dict[str, list[str]] = {
    "rain": ["비", "소나기", "빗줄기", "우산"],
    "heavy_rain": ["폭우", "호우", "집중호우", "폭풍우", "침수", "장마"],
    "snow": ["눈", "폭설", "대설", "적설", "빙판"],
    "wind": ["강풍", "태풍", "돌풍", "폭풍"],
    "heat": ["폭염", "무더위", "열사병", "고온"],
    "cold": ["한파", "혹한", "영하", "체감온도"],
    "general": ["날씨", "기상", "기상특보", "기상청"],
}

# 긴급도 매핑
_URGENCY_MAP: dict[str, str] = {
    "rain": "medium",        # 일반 비 — 야외활동 불편 수준
    "heavy_rain": "high",    # 폭우 — 침수·안전 위험
    "snow": "high",
    "wind": "critical",
    "heat": "medium",
    "cold": "medium",
    "general": "low",
}

# ── Active Rules 저장소 ──────────────────────────────────

_RULES_FILE = Path("generated_skills/active_rules.json")


def _load_rules() -> list[dict]:
    """저장된 자동화 규칙 목록 로드."""
    if _RULES_FILE.exists():
        try:
            return json.loads(_RULES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_rules(rules: list[dict]) -> None:
    """자동화 규칙 목록 저장. 비어있으면 파일 삭제."""
    if not rules:
        if _RULES_FILE.exists():
            _RULES_FILE.unlink()
            logger.info("active_rules.json 삭제 (규칙 0개)")
        return
    _RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _RULES_FILE.write_text(
        json.dumps(rules, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── 로그 훅 ──────────────────────────────────────────────

_log_hook: Optional[Callable] = None

# ── 대기 중인 케어 제안 (텔레그램 콜백 매칭용) ──────────
_pending_care_proposals: Optional[dict] = None


def save_pending_proposals(analysis: dict, affected_bookings: list[dict] | None = None) -> None:
    """케어 제안을 임시 저장 (텔레그램 버튼 클릭 시 매칭용)."""
    global _pending_care_proposals
    _pending_care_proposals = analysis
    if affected_bookings:
        _pending_care_proposals["_affected_bookings"] = affected_bookings


def get_pending_proposal(callback_data: str) -> Optional[dict]:
    """
    텔레그램 콜백 데이터(care_1, care_2, care_all, care_no)에서
    해당하는 제안 내용을 반환한다.
    """
    if _pending_care_proposals is None:
        return None

    proposals = _pending_care_proposals.get("proposals", [])

    if callback_data == "care_all":
        return {
            "type": "approve_all",
            "analysis": _pending_care_proposals,
            "proposals": proposals,
        }
    elif callback_data == "care_no":
        return {
            "type": "dismiss",
            "analysis": _pending_care_proposals,
            "proposals": [],
        }
    elif callback_data.startswith("care_"):
        try:
            idx = int(callback_data.replace("care_", "")) - 1
            if 0 <= idx < len(proposals):
                return {
                    "type": "approve_one",
                    "analysis": _pending_care_proposals,
                    "selected_index": idx + 1,
                    "proposal": proposals[idx],
                }
        except ValueError:
            pass

    return None


def clear_pending_proposals() -> None:
    """대기 중인 케어 제안 초기화."""
    global _pending_care_proposals
    _pending_care_proposals = None


# ── 자동화 규칙 등록 대기 (사장님 승인 후 등록) ──────────
_pending_auto_rule: Optional[dict] = None


def save_pending_auto_rule(
    trigger_category: str,
    approved_action: str,
    skill_name: str = "",
    condition_description: str = "",
    skill_sequence: Optional[list] = None,
) -> None:
    """자동화 규칙 등록 대기 — 사장님 승인 후 register_care_rule 호출.

    skill_sequence: 최종적으로 성공한 스킬 호출 순서와 파라미터.
                    [{"skill_name": "xxx", "args": {...}}, ...] 형태.
                    지정되면 다음 트리거 시 이 시퀀스만 재실행된다(폴백 탐색 생략).
    """
    global _pending_auto_rule
    _pending_auto_rule = {
        "trigger_category": trigger_category,
        "approved_action": approved_action,
        "skill_name": skill_name,
        "condition_description": condition_description,
        "skill_sequence": skill_sequence or [],
    }


def get_pending_auto_rule() -> Optional[dict]:
    """대기 중인 자동화 규칙 반환."""
    return _pending_auto_rule


def clear_pending_auto_rule() -> None:
    """대기 중인 자동화 규칙 초기화."""
    global _pending_auto_rule
    _pending_auto_rule = None


def set_care_log_hook(hook: Callable) -> None:
    """broadcaster.emit을 로그 훅으로 연결."""
    global _log_hook
    _log_hook = hook


async def _emit(log_type: str, content: str, meta: str = "") -> None:
    if _log_hook:
        await _log_hook(log_type, content, meta)


# ── Proactive Care Engine ────────────────────────────────

class ProactiveCareEngine:
    """선제적 케어 엔진: 키워드 감지 → 상황 분석 → 케어 제안 → 자동화 학습."""

    def __init__(self):
        self._client = get_llm_client()
        self._model = get_default_model()

    # ── Stage 1: Simulated Sensing (키워드 감지) ─────────

    def detect_triggers(self, message: str) -> list[dict]:
        """
        메시지에서 날씨/위험 키워드를 감지한다.

        Returns:
            감지된 트리거 목록: [{"category": str, "keyword": str, "urgency": str}]
        """
        triggers = []
        seen_categories = set()

        for category, keywords in _WEATHER_KEYWORDS.items():
            for kw in keywords:
                if kw in message:
                    if category not in seen_categories:
                        triggers.append({
                            "category": category,
                            "keyword": kw,
                            "urgency": _URGENCY_MAP.get(category, "low"),
                        })
                        seen_categories.add(category)

        return triggers

    # ── Stage 2: Contextual Reasoning (상황 분석) ────────

    async def analyze_context(
        self,
        message: str,
        triggers: list[dict],
        booking_data: Optional[list[dict]] = None,
        location: str = "",
        previous_weather: str = "",
        current_weather_condition: str = "",
    ) -> dict:
        """
        감지된 트리거 + 예약 데이터를 결합하여 LLM이 상황을 분석하고
        2~3개의 선제적 케어 제안을 생성한다.
        """
        trigger_summary = ", ".join(
            f"{t['category']}({t['keyword']}, 긴급도:{t['urgency']})"
            for t in triggers
        )

        booking_context = ""
        if booking_data:
            booking_context = f"\n\n현재 숙박 중인 투숙객:\n```json\n{json.dumps(booking_data, ensure_ascii=False, indent=2)}\n```"

        location_context = f"\n\n숙소 위치: {location}" if location else ""

        # 시나리오별 필수 제안 규칙
        scenario_rules = ""
        if previous_weather == "sunny" and current_weather_condition in ("rain", "heavy_rain"):
            _weather_word = "폭우" if current_weather_condition == "heavy_rain" else "비"
            scenario_rules = f"""

## 필수 제안 (반드시 포함할 것)
맑은 날씨에서 {_weather_word}(으)로 변경된 상황이므로, 다음 두 가지 액션을 proposals에 반드시 포함하세요:
1. **실내 핫플레이스 가이드 제작 및 투숙객 전송** — 숙소 주변의 실내 관광지, 카페, 맛집 등을 정리한 가이드를 만들어 투숙객에게 이메일로 전송
2. **주변 공방 할인권 탐색 및 투숙객 전송** — 숙소 인근의 도자기/캔들/가죽 등 체험 공방 할인 프로그램을 찾아 투숙객에게 안내
추가로 상황에 적합한 제안을 1개 더 포함할 수 있습니다.
⚠️ 사용자에게 보내는 모든 텍스트에서 현재 날씨를 "{_weather_word}"로만 표현하세요. {'"비"라고 쓰지 마세요' if _weather_word == '폭우' else '"폭우"라는 단어는 절대 사용하지 마세요'}."""

        prompt = f"""당신은 숙소의 선제적 고객 케어 어시스턴트입니다. 날씨 변화를 감지하고,
현재 숙박 중인 투숙객을 위한 선제적 케어 제안을 생성하세요.
{location_context}

## 날씨 변경
이전: {previous_weather} → 현재: {current_weather_condition}
트리거: {trigger_summary}

## 상황
"{message}"
{booking_context}
{scenario_rules}

## 규칙
1. risk_level: 날씨 심각도에 따라 결정 (태풍/폭설=critical, 폭우(heavy_rain)/강풍=high, 일반 비(rain)/눈=medium, 흐림/맑음=low)
   ⚠️ **"rain"(일반 비)와 "heavy_rain"(폭우)는 반드시 구분**. 현재 날씨가 "rain"이면 "비"로만 표현하고 "폭우"라는 단어를 사용하지 마세요.
2. proposals는 2~3개, 투숙객 경험 향상에 초점을 맞춘 구체적 액션
3. 각 제안은 숙소 위치({location or '미정'}) 주변 맥락을 반영
4. 기존 도구(send_telegram_message, 이메일 등)로 처리 가능한 것은 requires_skill: false
5. 새로운 외부 API/서비스가 필요한 것만 requires_skill: true + skill_request(자연어)와 skill_test_args(dict) 제공
6. priority: 1이 가장 긴급

## 출력 형식 (JSON만 반환)
{{
    "risk_level": "critical|high|medium|low",
    "analysis": "상황 분석 요약 (1~2줄, 숙소 위치와 투숙객 언급)",
    "proposals": [
        {{
            "action": "구체적 액션 설명",
            "reason": "이 액션이 필요한 이유",
            "requires_skill": false,
            "skill_request": null,
            "skill_test_args": null,
            "priority": 1
        }}
    ]
}}"""

        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                response_format={"type": "json_object"},
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            logger.error(f"Contextual reasoning failed: {e}")
            return {
                "risk_level": triggers[0]["urgency"] if triggers else "low",
                "analysis": f"키워드 감지됨: {trigger_summary}. LLM 분석 실패.",
                "proposals": [],
            }

    # ── Stage 3: Care Proposal Formatting ────────────────

    def format_care_message(self, analysis: dict) -> str:
        """케어 제안을 대시보드용 메시지로 포맷."""
        risk_emoji = {
            "critical": "🚨", "high": "⚠️", "medium": "🌤️", "low": "ℹ️"
        }
        risk_level = analysis.get("risk_level", "low")
        emoji = risk_emoji.get(risk_level, "ℹ️")

        lines = [
            f"{emoji} [선제적 케어] 위험도: {risk_level.upper()}",
            f"📋 {analysis.get('analysis', '')}",
            "",
        ]

        for i, p in enumerate(analysis.get("proposals", []), 1):
            lines.append(f"{i}. {p['action']}")
            lines.append(f"   → {p['reason']}")

        return "\n".join(lines)

    def format_care_telegram_message(self, analysis: dict) -> str:
        """케어 제안을 텔레그램 HTML 메시지로 포맷."""
        risk_emoji = {
            "critical": "🚨", "high": "⚠️", "medium": "🌤️", "low": "ℹ️"
        }
        risk_level = analysis.get("risk_level", "low")
        emoji = risk_emoji.get(risk_level, "ℹ️")

        lines = [
            f"{emoji} <b>선제적 케어 알림</b>",
            f"위험도: <b>{risk_level.upper()}</b>",
            "",
            analysis.get("analysis", ""),
            "",
        ]

        for i, p in enumerate(analysis.get("proposals", []), 1):
            lines.append(f"<b>{i}.</b> {p['action']}")

        lines.append("")
        lines.append("아래 버튼으로 승인해주세요.")

        return "\n".join(lines)

    def format_care_telegram_buttons(self, analysis: dict) -> dict:
        """케어 제안을 텔레그램 인라인 버튼으로 포맷.

        버튼 라벨에는 제안의 action 전체 문장을 그대로 담는다.
        텔레그램은 라벨이 길면 자체적으로 줄바꿈/자름 처리하고,
        대시보드에서는 CSS(text-overflow:ellipsis)로 가변 말줄임한다.
        """
        proposals = analysis.get("proposals", [])
        buttons = []

        for i, p in enumerate(proposals, 1):
            label = f"{i}. {p.get('action', '')}".strip()
            buttons.append([{
                "text": label,
                "callback_data": f"care_{i}",
            }])

        # 전체 실행 버튼은 제공하지 않음 (사장님 지시) — 개별 제안만 승인

        buttons.append([{
            "text": "❌ 무시",
            "callback_data": "care_no",
        }])

        return {"inline_keyboard": buttons}

    # ── Stage 4: Automation Learning (Active Rules) ──────

    def register_rule(
        self,
        trigger_category: str,
        approved_action: str,
        skill_name: Optional[str] = None,
        skill_args_template: Optional[dict] = None,
        skill_sequence: Optional[list] = None,
    ) -> dict:
        """
        승인된 케어 패턴을 Active Rule로 등록.
        동일 트리거가 다시 감지되면 자동 실행된다.

        skill_sequence: 최종 성공한 스킬 호출 순서(list[{skill_name, args}]).
                        지정되면 다음 트리거 시 MCP/API/웹검색 폴백 탐색 없이
                        이 시퀀스만 재실행된다.
        """
        rules = _load_rules()

        # 중복 체크
        for r in rules:
            if r["trigger_category"] == trigger_category and r["action"] == approved_action:
                r["approval_count"] = r.get("approval_count", 1) + 1
                r["last_approved"] = datetime.now().isoformat()
                # skill_sequence가 새로 들어오면 갱신(최신 성공 경로로 업데이트)
                if skill_sequence:
                    r["skill_sequence"] = skill_sequence
                _save_rules(rules)
                return {"status": "updated", "rule": r}

        new_rule = {
            "id": f"rule_{len(rules) + 1:03d}",
            "trigger_category": trigger_category,
            "action": approved_action,
            "skill_name": skill_name,
            "skill_args_template": skill_args_template,
            "skill_sequence": skill_sequence or [],
            "approval_count": 1,
            "auto_execute": True,  # 사장님이 명시적으로 승인했으므로 즉시 자동 실행 활성화
            "created_at": datetime.now().isoformat(),
            "last_approved": datetime.now().isoformat(),
        }
        rules.append(new_rule)
        _save_rules(rules)

        return {"status": "created", "rule": new_rule}

    def promote_rules(self) -> list[dict]:
        """
        3회 이상 승인된 규칙을 자동 실행(auto_execute=True)으로 승격.
        """
        rules = _load_rules()
        promoted = []

        for r in rules:
            if r.get("approval_count", 0) >= 3 and not r.get("auto_execute"):
                r["auto_execute"] = True
                r["promoted_at"] = datetime.now().isoformat()
                promoted.append(r)

        if promoted:
            _save_rules(rules)

        return promoted

    def get_auto_rules(self, trigger_category: str, current_condition: str = "") -> list[dict]:
        """특정 트리거에 대해 자동 실행 가능한 규칙 목록 반환.

        매칭 정책:
          1) trigger_category가 정확히 일치해야 한다 (rain, heavy_rain, snow ...).
          2) "general" 카테고리는 더 이상 자동 매칭되지 않는다 (오발동 방지).
          3) current_condition이 명시되면 'sunny'/'cloudy' 같은 평온한 상태에서는
             어떤 위험 카테고리(rain/snow/wind/heat/cold) 규칙도 발동하지 않는다.
        """
        # 평온한 날씨로 전환된 경우 — 위험 카테고리 자동 규칙은 무시
        BENIGN = {"sunny", "cloudy", "clear", "fair"}
        if current_condition and current_condition.lower() in BENIGN:
            return []
        # general 카테고리는 자동 매칭에서 제외 (모든 변화에 발동되는 부작용 차단)
        if not trigger_category or trigger_category == "general":
            return []
        rules = _load_rules()
        return [
            r for r in rules
            if r.get("auto_execute")
            and r["trigger_category"] == trigger_category
            and r["trigger_category"] != "general"
        ]

    def get_all_rules(self) -> list[dict]:
        """전체 Active Rule 목록 반환."""
        return _load_rules()

    def delete_rule(self, rule_id: str) -> bool:
        """규칙 삭제."""
        rules = _load_rules()
        original_len = len(rules)
        rules = [r for r in rules if r["id"] != rule_id]
        if len(rules) < original_len:
            _save_rules(rules)
            return True
        return False

    # ── Weather Webhook 전용 처리 ────────────────────────

    async def process_weather_event(
        self,
        weather_condition: str,
        weather_label: str,
        affected_bookings: list[dict],
        location: str = "",
        previous_weather: str = "",
    ) -> Optional[dict]:
        """
        날씨 변경 webhook에서 직접 호출.
        키워드 감지를 건너뛰고 바로 상황 분석 → 케어 제안 생성.
        """
        # condition → category 매핑
        # "rain"(일반 비)과 "heavy_rain"(폭우)는 별개 카테고리. 혼용 금지.
        _CONDITION_TO_CATEGORY = {
            "sunny": "general",
            "cloudy": "general",
            "rain": "rain",
            "heavy_rain": "heavy_rain",
            "snow": "snow",
            "heavy_snow": "snow",
            "wind": "wind",
            "typhoon": "wind",
            "heat": "heat",
            "cold": "cold",
        }
        category = _CONDITION_TO_CATEGORY.get(weather_condition, "general")
        urgency = _URGENCY_MAP.get(category, "low")

        triggers = [{
            "category": category,
            "keyword": weather_label,
            "urgency": urgency,
        }]

        await _emit(
            "system",
            f"🌦️ [Proactive Care] 날씨 변경 감지: {weather_label} (긴급도: {urgency.upper()})\n"
            f"   영향 받는 투숙객: {len(affected_bookings)}명",
            "[선제적 케어]",
        )

        # 자동 실행 규칙 확인 (현재 weather 카테고리와 정확히 일치하는 것만)
        auto_actions = self.get_auto_rules(category, current_condition=weather_condition)
        if auto_actions:
            await _emit(
                "system",
                f"⚡ [Active Rule] {len(auto_actions)}개 자동 실행 규칙 발견 ({weather_condition})",
                "[자동화]",
            )

        # 상황 분석
        message = f"날씨가 {weather_label}(으)로 변경되었습니다. 현재 숙박 중인 투숙객이 {len(affected_bookings)}명 있습니다."
        await _emit("thinking", "🧠 [Proactive Care] 상황 분석 중...", "[선제적 케어]")
        analysis = await self.analyze_context(
            message, triggers, affected_bookings,
            location=location,
            previous_weather=previous_weather,
            current_weather_condition=weather_condition,
        )

        await _emit(
            "system",
            f"📊 [Proactive Care] 위험도: {analysis.get('risk_level', 'unknown').upper()}\n"
            f"   분석: {analysis.get('analysis', '')}\n"
            f"   제안: {len(analysis.get('proposals', []))}개",
            "[선제적 케어]",
        )

        # 메시지 포맷
        care_message = self.format_care_message(analysis)
        telegram_buttons = self.format_care_telegram_buttons(analysis)

        # 규칙 승격 체크
        promoted = self.promote_rules()
        if promoted:
            await _emit(
                "system",
                f"🎓 [Active Rule 승격] {len(promoted)}개 규칙이 자동 실행으로 승격됨",
                "[자동화 학습]",
            )

        return {
            "triggers": triggers,
            "analysis": analysis,
            "care_message": care_message,
            "telegram_buttons": telegram_buttons,
            "auto_actions": auto_actions,
        }

    # ── 메인 처리 플로우 (키워드 기반) ─────────────────────

    async def process_message(
        self,
        message: str,
        booking_data: Optional[list[dict]] = None,
    ) -> Optional[dict]:
        """
        메시지를 분석하여 선제적 케어가 필요하면 제안을 생성한다.

        Returns:
            None (트리거 없음) 또는
            {
                "triggers": [...],
                "analysis": {...},
                "care_message": str,
                "telegram_buttons": dict,
                "auto_actions": [...]  # 자동 실행 가능한 규칙
            }
        """
        # Stage 1: 키워드 감지
        triggers = self.detect_triggers(message)
        if not triggers:
            return None

        await _emit(
            "system",
            f"🌦️ [Proactive Care] 위험 키워드 감지: "
            + ", ".join(f"{t['keyword']}({t['urgency']})" for t in triggers),
            "[선제적 케어]",
        )

        # 자동 실행 규칙 확인
        auto_actions = []
        for trigger in triggers:
            auto_rules = self.get_auto_rules(trigger["category"])
            auto_actions.extend(auto_rules)

        if auto_actions:
            await _emit(
                "system",
                f"⚡ [Active Rule] {len(auto_actions)}개 자동 실행 규칙 발견 — 자동 처리 준비",
                "[자동화]",
            )

        # Stage 2: 상황 분석
        await _emit("thinking", "🧠 [Proactive Care] 상황 분석 중...", "[선제적 케어]")
        analysis = await self.analyze_context(message, triggers, booking_data)

        await _emit(
            "system",
            f"📊 [Proactive Care] 위험도: {analysis.get('risk_level', 'unknown').upper()}\n"
            f"   분석: {analysis.get('analysis', '')}",
            "[선제적 케어]",
        )

        # Stage 3: 메시지 포맷
        care_message = self.format_care_message(analysis)
        telegram_buttons = self.format_care_telegram_buttons(analysis)

        # 규칙 승격 체크
        promoted = self.promote_rules()
        if promoted:
            await _emit(
                "system",
                f"🎓 [Active Rule 승격] {len(promoted)}개 규칙이 자동 실행으로 승격됨: "
                + ", ".join(r["action"][:30] for r in promoted),
                "[자동화 학습]",
            )

        return {
            "triggers": triggers,
            "analysis": analysis,
            "care_message": care_message,
            "telegram_buttons": telegram_buttons,
            "auto_actions": auto_actions,
        }


# ── 싱글톤 ───────────────────────────────────────────────

_care_engine: Optional[ProactiveCareEngine] = None


def get_care_engine() -> ProactiveCareEngine:
    global _care_engine
    if _care_engine is None:
        _care_engine = ProactiveCareEngine()
    return _care_engine
