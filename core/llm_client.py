"""
llm_client.py — LLM 클라이언트 팩토리 (Gemini / OpenAI 호환)

Google Gemini의 OpenAI 호환 엔드포인트를 기본으로 사용한다.
환경변수로 OpenAI 본가 또는 타 호환 엔드포인트로 쉽게 전환 가능.

환경변수:
  GEMINI_API_KEY        Gemini API 키 (https://aistudio.google.com/apikey)
  OPENAI_API_KEY        폴백용. GEMINI_API_KEY가 없을 때 사용.
  OPENAI_BASE_URL       기본: https://generativelanguage.googleapis.com/v1beta/openai/
                        OpenAI 본가로 돌아가려면 이 값을 비우거나 openai URL로 설정.
"""
from __future__ import annotations
import os
from openai import AsyncOpenAI


_GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def get_llm_client() -> AsyncOpenAI:
    """현재 환경변수 설정에 맞는 AsyncOpenAI 클라이언트를 반환."""
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    base_url = os.getenv("OPENAI_BASE_URL", _GEMINI_OPENAI_BASE_URL).strip() or None
    if base_url:
        return AsyncOpenAI(api_key=api_key, base_url=base_url)
    return AsyncOpenAI(api_key=api_key)


def get_default_model() -> str:
    """기본 모델(일반용)."""
    return os.getenv("OPENAI_MODEL", "gemini-2.5-flash")


def get_maker_model() -> str:
    """Skill Factory Maker 모델(코드 합성·피어 리뷰용 고성능)."""
    return os.getenv("SKILL_MAKER_MODEL", os.getenv("OPENAI_MODEL", "gemini-2.5-pro"))


def get_user_model() -> str:
    """Skill Factory User 모델(경량 분류·매칭용)."""
    return os.getenv("SKILL_USER_MODEL", "gemini-2.5-flash")
