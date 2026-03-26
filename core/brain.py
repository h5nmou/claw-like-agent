"""
brain.py — LLM 인터페이스 (OpenAI Chat Completions + Function Calling)
"""

from __future__ import annotations

import json
import os
from typing import Any

from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()


class Brain:
    """LLM과의 대화를 관리하는 모듈."""

    def __init__(self, system_prompt: str, tools_schema: list[dict]) -> None:
        self._client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self._model = os.getenv("OPENAI_MODEL", "gpt-4o")
        self._system_prompt = system_prompt
        self._tools_schema = tools_schema
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

    # ── 메시지 추가 ──────────────────────────────────

    def add_user_message(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_tool_result(self, tool_call_id: str, result: Any) -> None:
        result_str = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
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
        self._messages.append(message.model_dump())

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

    def reset(self, keep_system: bool = True) -> None:
        if keep_system:
            self._messages = [self._messages[0]]
        else:
            self._messages = []
