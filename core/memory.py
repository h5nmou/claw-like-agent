"""
memory.py — 세션 이벤트 로그 관리 (단기 메모리)
"""

from __future__ import annotations

import json
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Event:
    """단일 이벤트 기록."""
    timestamp: str
    role: str          # "trigger" | "assistant" | "tool_call" | "tool_result" | "system"
    content: Any       # 문자열 또는 dict

    def to_dict(self) -> dict:
        return asdict(self)


class Memory:
    """세션별 인메모리 이벤트 로그."""

    def __init__(self) -> None:
        self._events: list[Event] = []

    # ── 기록 ──────────────────────────────────────────

    def add_event(self, role: str, content: Any) -> None:
        event = Event(
            timestamp=datetime.utcnow().isoformat(),
            role=role,
            content=content,
        )
        self._events.append(event)

    # ── 조회 ──────────────────────────────────────────

    def get_history(self) -> list[dict]:
        """전체 이벤트 이력을 dict 리스트로 반환."""
        return [e.to_dict() for e in self._events]

    def get_summary(self) -> str:
        """이벤트 이력을 사람이 읽을 수 있는 요약 문자열로 반환."""
        lines: list[str] = []
        for e in self._events:
            content_str = (
                json.dumps(e.content, ensure_ascii=False)
                if isinstance(e.content, dict)
                else str(e.content)
            )
            lines.append(f"[{e.timestamp}] {e.role}: {content_str}")
        return "\n".join(lines)

    def clear(self) -> None:
        self._events.clear()

    def __len__(self) -> int:
        return len(self._events)
