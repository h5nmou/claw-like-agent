"""
proactive_care_api.py — Proactive Care 관련 Tool 등록

- 실시간 웹 검색 도구 (가이드 제작 등에 활용)
- 검색 기반 로컬 가이드 생성 도구
- 케어 자동화 규칙 등록/조회 도구
"""

import json
import logging
import os
from datetime import datetime

import httpx
from core.llm_client import get_llm_client, get_default_model

from core.executor import tool
from core.proactive_care import get_care_engine, save_pending_auto_rule

logger = logging.getLogger("proactive_care_api")

# ── 웹 검색 헬퍼 (skill_factory.py APIDiscovery 재활용) ──

SERPER_ENDPOINT = "https://google.serper.dev/search"
DDGS_ENDPOINT = "https://api.duckduckgo.com/"


async def _search_serper(query: str, num: int = 8) -> list[dict]:
    """Serper (Google) 검색."""
    api_key = os.getenv("SERPER_API_KEY")
    if not api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                SERPER_ENDPOINT,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": query, "num": num, "gl": "kr", "hl": "ko"},
            )
            data = resp.json()
            results = []
            for r in data.get("organic", []):
                results.append({
                    "title": r.get("title", ""),
                    "link": r.get("link", ""),
                    "snippet": r.get("snippet", ""),
                })
            # places 결과 (지역 검색)
            for p in data.get("places", []):
                results.append({
                    "title": p.get("title", ""),
                    "address": p.get("address", ""),
                    "rating": p.get("rating", ""),
                    "link": p.get("link", p.get("cid", "")),
                    "snippet": f"{p.get('category', '')} | 평점 {p.get('rating', 'N/A')} | {p.get('address', '')}",
                })
            return results
    except Exception as e:
        logger.warning(f"Serper 검색 실패: {e}")
        return []


async def _search_duckduckgo(query: str) -> list[dict]:
    """DuckDuckGo 검색 (duckduckgo-search 라이브러리, 무인증 폴백)."""
    try:
        from ddgs import DDGS
        import asyncio

        def _sync_search():
            with DDGS() as ddgs:
                return list(ddgs.text(query, region="kr-kr", max_results=8))

        raw = await asyncio.get_event_loop().run_in_executor(None, _sync_search)
        results = []
        for r in raw:
            results.append({
                "title": r.get("title", ""),
                "link": r.get("href", r.get("link", "")),
                "snippet": r.get("body", r.get("snippet", "")),
            })
        return results
    except Exception as e:
        logger.warning(f"DuckDuckGo 검색 실패: {e}")
        return []


async def _web_search(query: str, num: int = 8) -> list[dict]:
    """Serper 우선, 없으면 DuckDuckGo 폴백."""
    if os.getenv("SERPER_API_KEY"):
        results = await _search_serper(query, num)
        if results:
            return results
    return await _search_duckduckgo(query)


# ── Tool 정의 ────────────────────────────────────────────


@tool
async def web_search(query: str, num_results: int = 8) -> dict:
    """[웹 검색 폴백] 실시간 웹 검색을 수행한다. MCP 기반 도구(create_new_skill)를 먼저 시도한 후, 실패했을 때만 사용하라.

    Args:
        query (str): 검색 쿼리 (예: "제주도 애월읍 실내 관광지 추천 2026")
        num_results (int): 검색 결과 수 (기본 8)

    Returns:
        검색 결과 목록
    """
    results = await _web_search(query, num_results)
    if not results:
        return {"results": [], "count": 0, "message": "검색 결과가 없습니다. 쿼리를 변경하여 다시 시도해 주세요."}
    return {"results": results, "count": len(results), "query": query}


@tool
async def create_local_guide(
    location: str,
    category: str,
    weather_context: str = "",
    guest_name: str = "",
) -> dict:
    """[웹 검색 폴백] 웹 검색 결과 기반 로컬 가이드를 생성한다. 장소/맛집/관광지 요청 시 create_new_skill(MCP Discovery)을 먼저 시도하고, 실패했을 때만 이 도구를 사용하라.

    Args:
        location (str): 숙소 위치 (예: "제주도 애월읍")
        category (str): 가이드 카테고리 (예: "실내 관광지", "카페", "맛집", "공방 체험")
        weather_context (str): 현재 날씨 상황 (예: "맑음에서 비로 변경")
        guest_name (str): 투숙객 이름 (개인화용)

    Returns:
        생성된 가이드 텍스트와 검색 출처
    """
    # 1단계: 다양한 쿼리로 실시간 검색
    queries = [
        f"{location} {category} 추천 {datetime.now().year}",
        f"{location} 근처 {category} 인기 영업중",
    ]
    if "실내" in category or "비" in weather_context:
        queries.append(f"{location} 비올때 갈만한 곳 실내")
    if "공방" in category:
        queries.append(f"{location} 체험 공방 할인 예약")

    all_results = []
    seen_titles = set()
    for q in queries:
        results = await _web_search(q, 6)
        for r in results:
            if r.get("title") and r["title"] not in seen_titles:
                all_results.append(r)
                seen_titles.add(r["title"])

    if not all_results:
        return {
            "success": False,
            "error": f"'{location} {category}' 관련 검색 결과를 찾을 수 없습니다.",
            "guide": "",
        }

    # 2단계: LLM으로 검색 결과를 가이드로 정리
    search_data = json.dumps(all_results[:15], ensure_ascii=False, indent=2)

    prompt = f"""아래 웹 검색 결과를 바탕으로 투숙객을 위한 로컬 가이드를 작성하세요.

## 조건
- 숙소 위치: {location}
- 카테고리: {category}
- 날씨 상황: {weather_context or "정보 없음"}
- 투숙객: {guest_name or "투숙객"}

## 검색 결과
{search_data}

## 가이드 작성 규칙
1. **검색 결과에 실제로 나온 장소/정보만** 포함할 것 (절대 지어내지 말 것)
2. 각 장소에 이름, 간단한 설명, 특징을 포함
3. 가능하면 주소나 연락처, 링크 포함
4. 날씨 상황에 맞는 코멘트 추가
5. 5~8개 장소를 카테고리별로 정리
6. 투숙객에게 보내는 친근한 이메일 형식으로 작성
7. 한국어로 작성

## 출력 형식
이메일 본문에 바로 사용할 수 있는 텍스트로 작성하세요."""

    try:
        client = get_llm_client()
        resp = await client.chat.completions.create(
            model=get_default_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
        )
        guide_text = resp.choices[0].message.content

        sources = [r.get("link", "") for r in all_results[:10] if r.get("link")]

        return {
            "success": True,
            "guide": guide_text,
            "location": location,
            "category": category,
            "sources_count": len(all_results),
            "sources": sources[:5],
        }
    except Exception as e:
        logger.error(f"가이드 생성 실패: {e}")
        return {
            "success": False,
            "error": str(e),
            "raw_search_results": all_results[:8],
        }


@tool
async def propose_care_automation(
    trigger_category: str,
    approved_action: str,
    skill_name: str = "",
    condition_description: str = "",
    skill_sequence: list = None,
) -> dict:
    """케어 액션 완료 후, 사장님에게 자동화 규칙 등록 여부를 텔레그램으로 질문한다. register_care_rule 대신 이 도구를 사용하라.

    Args:
        trigger_category (str): 트리거 카테고리. 반드시 구분: rain(일반 비) / heavy_rain(폭우) / snow / wind / heat / cold / general
        approved_action (str): 승인된 액션 설명 (예: "폭우 시 실내 관광지 가이드 제작 및 이메일 발송")
        skill_name (str): 실행에 사용된 대표 스킬명 (선택, 하위호환용)
        condition_description (str): 자동화 조건 설명 (예: "날씨가 맑음→비로 변경되고 해당 날짜 투숙객이 있는 경우")
        skill_sequence (list): ⭐ **최종적으로 성공한 스킬 호출 순서와 파라미터**.
            [{"skill_name": "generate_indoor_places_guide", "args": {"location": {...}, ...}},
             {"skill_name": "send_email_via_smtp", "args": {"to_email": "...", ...}}] 형태.
            이 목록은 다음 트리거 발생 시 **MCP/웹검색 폴백 없이 그대로 재실행**된다.
            중간에 실패한 스킬(예: 결과 0건으로 폐기된 호출)은 포함시키지 마라.
            사용자 요청을 완료시킨 마지막 성공 호출들만 순서대로 포함하라.

    Returns:
        질문 전송 결과
    """
    from core.telegram_client import telegram_client

    if skill_sequence is None:
        skill_sequence = []

    # ── trigger_category 정규화: 알 수 없는 값은 "general"로 수용 ──
    # "general" 규칙은 저장은 허용하되 get_auto_rules()에서 자동 매칭에서 제외된다.
    # (날씨 webhook이 비→맑음 같은 반대 전환에서도 "general" 규칙을 발동시키는 오동작 방지)
    VALID_CATEGORIES = {"rain", "heavy_rain", "snow", "wind", "heat", "cold", "general"}
    if trigger_category not in VALID_CATEGORIES:
        # 완전히 알 수 없는 값이면 general로 교정
        trigger_category = "general"

    # 대기 중인 규칙 정보 저장
    save_pending_auto_rule(
        trigger_category=trigger_category,
        approved_action=approved_action,
        skill_name=skill_name,
        condition_description=condition_description,
        skill_sequence=skill_sequence,
    )

    # 사장님에게 텔레그램으로 자동화 등록 여부 질문
    condition_text = condition_description or f"{trigger_category} 감지 + 투숙객 존재"
    sequence_text = ""
    if skill_sequence:
        sequence_text = "\n  • 재실행 시퀀스:\n" + "\n".join(
            f"      {i+1}. {s.get('skill_name', '?')}"
            for i, s in enumerate(skill_sequence)
        )
    message = (
        f"📋 자동화 규칙 등록 요청\n\n"
        f"방금 수행한 작업:\n"
        f"  • {approved_action}\n"
        f"  • 사용 스킬: {skill_name or '없음'}"
        f"{sequence_text}\n\n"
        f"다음에 동일한 조건이 발생하면 자동으로 이 작업을 수행할까요?\n"
        f"  조건: {condition_text}"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ 자동화 등록", "callback_data": "auto_rule_yes"},
                {"text": "❌ 등록 안함", "callback_data": "auto_rule_no"},
            ]
        ]
    }

    result = await telegram_client.send_message(
        message, reply_markup=reply_markup
    )

    return {
        "status": "질문 전송 완료",
        "message": "사장님의 승인을 기다리고 있습니다. 승인 시 자동화 규칙이 등록됩니다.",
        "pending_rule": {
            "trigger_category": trigger_category,
            "approved_action": approved_action,
            "skill_name": skill_name,
        },
    }


@tool
async def register_care_rule(
    trigger_category: str,
    approved_action: str,
    skill_name: str = "",
) -> dict:
    """[내부용 — 직접 호출 금지] 자동화 규칙을 등록한다. propose_care_automation을 대신 사용하라.

    Args:
        trigger_category (str): 트리거 카테고리. 반드시 구분: rain(일반 비) / heavy_rain(폭우) / snow / wind / heat / cold / general
        approved_action (str): 승인된 액션 설명 (예: "기상예보 상세 조회")
        skill_name (str): 실행에 사용된 스킬명 (선택)

    Returns:
        등록 결과 dict
    """
    care_engine = get_care_engine()
    result = care_engine.register_rule(
        trigger_category=trigger_category,
        approved_action=approved_action,
        skill_name=skill_name if skill_name else None,
    )
    return result


@tool
async def list_care_rules() -> dict:
    """등록된 모든 Active Rule(자동화 규칙)을 조회한다.

    Returns:
        Active Rule 목록과 통계
    """
    care_engine = get_care_engine()
    rules = care_engine.get_all_rules()
    auto_count = sum(1 for r in rules if r.get("auto_execute"))
    return {
        "rules": rules,
        "total": len(rules),
        "auto_execute_count": auto_count,
        "manual_count": len(rules) - auto_count,
    }
