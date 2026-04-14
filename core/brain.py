"""
brain.py — LLM 인터페이스 (Gemini OpenAI 호환 엔드포인트 + Function Calling)
"""

from __future__ import annotations

import json
from typing import Any

from dotenv import load_dotenv
from core.llm_client import get_llm_client, get_default_model

load_dotenv()


class Brain:
    """LLM과의 대화를 관리하는 모듈."""

    def __init__(self, system_prompt: str, tools_schema: list[dict]) -> None:
        self._client = get_llm_client()
        self._model = get_default_model()
        self._system_prompt = system_prompt
        self._tools_schema = tools_schema
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

    # ── 메시지 추가 ──────────────────────────────────

    def add_user_message(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_tool_result(self, tool_call_id: str, result: Any) -> None:
        if isinstance(result, str):
            result_str = result
        else:
            try:
                result_str = json.dumps(result, ensure_ascii=False)
            except (TypeError, ValueError):
                # 비직렬화 객체(httpx.AsyncClient 등) → 오류 메시지로 변환
                result_str = json.dumps({
                    "error": "TOOL_RETURNED_NON_SERIALIZABLE",
                    "type": type(result).__name__,
                    "detail": f"스킬이 JSON으로 직렬화할 수 없는 객체({type(result).__name__})를 반환했습니다. 스킬 코드를 확인하세요.",
                }, ensure_ascii=False)
        self._messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result_str,
        })

    # ── LLM 호출 ─────────────────────────────────────

    async def think(self) -> dict[str, Any]:
        """
        현재 메시지 히스토리를 기반으로 LLM에 요청.

        Returns:
            {
                "type": "text" | "tool_calls",
                "content": str (text일 때) | list[dict] (tool_calls일 때),
            }
        """
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": self._messages,
        }
        if self._tools_schema:
            kwargs["tools"] = self._tools_schema
            kwargs["tool_choice"] = "auto"

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message

        # 메시지를 히스토리에 추가
        # Gemini OpenAI 호환 엔드포인트는 null 값을 거부하므로 필요한 필드만 추출.
        # (OpenAI SDK의 model_dump()는 content=null, function_call=null, refusal=null 등을
        #  포함하는데 Gemini가 "Value is not a struct: null"로 400 에러를 반환함)
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        assistant_msg["content"] = message.content or ""
        if message.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ]
        self._messages.append(assistant_msg)

        # Tool 호출이 있는 경우
        if message.tool_calls:
            tool_calls = []
            for tc in message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                })
            return {"type": "tool_calls", "content": tool_calls}

        # 텍스트 응답
        return {"type": "text", "content": message.content or ""}

    # ── 유틸리티 ─────────────────────────────────────

    def get_message_count(self) -> int:
        return len(self._messages)

    def update_tools(self, new_tools_schema: list[dict]) -> None:
        """루프 중 새 스킬이 등록되면 Tool 목록을 즉시 갱신.
        
        다음 think() 호출 시 LLM이 업데이트된 도구 목록을 받게 됩니다.
        """
        self._tools_schema = new_tools_schema

    def reset(self, keep_system: bool = True) -> None:
        if keep_system:
            self._messages = [self._messages[0]]
        else:
            self._messages = []
