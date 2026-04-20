"""
llm_client.py — LLM 클라이언트 팩토리 (Gemini / Claude / OpenAI 호환)

기본(일반용): Google Gemini OpenAI 호환 엔드포인트.
Maker(코드 합성): Claude 또는 별도 고성능 모델 (MAKER_API_KEY + MAKER_BASE_URL).

환경변수:
  ── 일반용 (Brain, Proactive Care 등) ──
  GEMINI_API_KEY        Gemini API 키 (https://aistudio.google.com/apikey)
  OPENAI_API_KEY        폴백용. GEMINI_API_KEY가 없을 때 사용.
  OPENAI_BASE_URL       기본: Gemini 호환 URL. 비우면 OpenAI 본가.
  OPENAI_MODEL          기본 모델명 (default: gemini-2.5-flash)

  ── Maker 전용 (Skill Factory 코드 합성·피어 리뷰) ──
  MAKER_API_KEY         Maker 전용 API 키. 미설정 시 일반용 키 사용.
  MAKER_BASE_URL        Maker 전용 엔드포인트 URL. 미설정 시 일반용 URL 사용.
                        Claude 사용 시: https://openrouter.ai/api/v1 (OpenRouter 프록시)
                        ⚠️ Anthropic 직접 URL(api.anthropic.com)은 OpenAI SDK와 호환 안 됨.
  SKILL_MAKER_MODEL     Maker 모델명 (default: claude-sonnet-4-5)
  SKILL_USER_MODEL      경량 분류 모델명 (default: gemini-2.5-flash)
"""
from __future__ import annotations
import os
from openai import AsyncOpenAI


_GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def get_llm_client() -> AsyncOpenAI:
    """일반용 AsyncOpenAI 클라이언트 (Brain, Proactive Care 등)."""
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    base_url = os.getenv("OPENAI_BASE_URL", _GEMINI_OPENAI_BASE_URL).strip() or None
    if base_url:
        return AsyncOpenAI(api_key=api_key, base_url=base_url)
    return AsyncOpenAI(api_key=api_key)


def get_maker_client() -> AsyncOpenAI:
    """Maker 전용 AsyncOpenAI 클라이언트 (Skill Factory 코드 합성·피어 리뷰).
    현재는 일반용 클라이언트와 동일 (Gemini).
    """
    return get_llm_client()


def get_default_model() -> str:
    """기본 모델(일반용)."""
    return os.getenv("OPENAI_MODEL", "gemini-2.5-flash")


def get_maker_model() -> str:
    """Skill Factory Maker 모델(코드 합성·피어 리뷰용 고성능).

    Claude 사용 시 MAKER_BASE_URL을 OpenRouter로 설정하고
    SKILL_MAKER_MODEL=anthropic/claude-sonnet-4-5 로 지정.
    """
    return os.getenv("SKILL_MAKER_MODEL", os.getenv("OPENAI_MODEL", "gemini-2.5-pro"))


def get_user_model() -> str:
    """Skill Factory User 모델(경량 분류·매칭용)."""
    return os.getenv("SKILL_USER_MODEL", "gemini-2.5-flash")
