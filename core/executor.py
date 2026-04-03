"""
executor.py — Tool Registry & Executor

@tool 데코레이터로 함수를 등록하고,
docstring + type hints를 파싱하여 OpenAI function schema(JSON)를 자동 생성.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable, get_type_hints


# ── 글로벌 레지스트리 ────────────────────────────────

_TOOL_REGISTRY: dict[str, Callable] = {}


def tool(func: Callable) -> Callable:
    """함수 등록용 데코레이터. @tool을 붙이면 자동으로 레지스트리에 등록."""
    _TOOL_REGISTRY[func.__name__] = func
    return func


# ── Python 타입 → JSON Schema 타입 변환 ──────────────

_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _python_type_to_json(py_type: type) -> str:
    return _TYPE_MAP.get(py_type, "string")


# ── Docstring 파싱 ───────────────────────────────────

def _parse_docstring(docstring: str | None) -> tuple[str, dict[str, str]]:
    """
    Google-style docstring에서 함수 설명과 파라미터 설명을 추출.

    Returns:
        (description, {param_name: param_description})
    """
    if not docstring:
        return ("", {})

    lines = docstring.strip().split("\n")
    description_lines: list[str] = []
    param_docs: dict[str, str] = {}
    in_args = False

    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("args:"):
            in_args = True
            continue
        if stripped.lower().startswith("returns:"):
            in_args = False
            continue

        if in_args:
            # "param_name: description" 또는 "param_name (type): description"
            if ":" in stripped:
                parts = stripped.split(":", 1)
                pname = parts[0].strip().split("(")[0].strip()
                pdesc = parts[1].strip()
                param_docs[pname] = pdesc
        else:
            if stripped:
                description_lines.append(stripped)

    return (" ".join(description_lines), param_docs)


# ── Schema 생성 ──────────────────────────────────────

def build_function_schemas() -> list[dict]:
    """
    등록된 모든 Tool 함수를 OpenAI function calling 스키마 리스트로 변환.
    """
    schemas: list[dict] = []

    for name, func in _TOOL_REGISTRY.items():
        hints = get_type_hints(func)
        sig = inspect.signature(func)
        description, param_docs = _parse_docstring(func.__doc__)

        properties: dict[str, dict] = {}
        required: list[str] = []

        for pname, param in sig.parameters.items():
            ptype = hints.get(pname, str)
            prop: dict[str, str] = {
                "type": _python_type_to_json(ptype),
            }
            if pname in param_docs:
                prop["description"] = param_docs[pname]

            properties[pname] = prop

            # 기본값이 없으면 required
            if param.default is inspect.Parameter.empty:
                required.append(pname)

        schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }
        schemas.append(schema)

    return schemas


# ── 실행 ─────────────────────────────────────────────

async def execute(tool_name: str, arguments: dict[str, Any]) -> Any:
    """
    등록된 Tool 함수를 이름으로 찾아 실행.
    async 함수와 sync 함수 모두 지원.
    """
    func = _TOOL_REGISTRY.get(tool_name)
    if func is None:
        return {"error": f"Unknown tool: {tool_name}"}

    try:
        result = func(**arguments)
        # async 함수라면 await
        if inspect.isawaitable(result):
            result = await result
        return result
    except Exception as e:
        return {"error": f"Tool execution failed: {str(e)}"}


@tool
def list_all_available_tools() -> dict:
    """
    현재 시스템에 등록된 모든 도구(로컬 및 Phone-MCP 원격 도구, 자동 생성 스킬 포함)의 이름과 설명을 반환합니다.
    사용자가 '어떤 기능이 있어?', '도구 목록 보여줘'라고 물어볼 때 이 도구를 사용하여 목록을 확인하세요.
    """
    from core.skill_registry import get_skill_registry
    registry = get_skill_registry()
    generated_skill_names = {s["name"] for s in registry.get_all_skills()}

    tools_list = []
    for name, func in _TOOL_REGISTRY.items():
        doc = func.__doc__
        desc = doc.strip().split("\n")[0] if doc else "설명 없음"
        tag = ""
        if "[PHONE-MCP]" in desc:
            tag = "[PHONE-MCP] "
            desc = desc.replace("[PHONE-MCP] ", "")
        elif name in generated_skill_names:
            tag = "[생성된 스킬] "
        tools_list.append({"name": name, "tag": tag, "description": tag + desc})

    stats = registry.get_stats()
    return {
        "registered_tools": tools_list,
        "total": len(tools_list),
        "generated_skill_count": stats["total_skills"],
    }


from core.telegram_client import telegram_client

@tool
async def send_telegram_message(message: str, buttons: list[str] = None) -> dict:
    """
    사장님의 텔레그램으로 메시지를 전송합니다 (버튼 추가 가능).
    버튼을 추가하면 사장님이 클릭을 통해 쉽게 명령을 내릴 수 있습니다 (예: ["주소록 확인", "예약 현황", "승인"]).
    버튼 클릭 시 해당 텍스트가 당신(에이전트)에게 채팅으로 직접 전달됩니다.

    Args:
        message (str): 텔레그램으로 전송할 본문 메시지 텍스트
        buttons (list): (선택 사항) 사용자가 클릭할 수 있는 버튼의 라벨 배열. 예: ["버튼1", "버튼2"]
    """
    reply_markup = None
    if buttons:
        # LLM이 리스트가 아닌 단일 문자열로 통째로 보낼 경우를 대비하여 방어 처리
        if isinstance(buttons, str):
            try:
                import json
                buttons = json.loads(buttons)
            except Exception:
                buttons = [buttons]
                
        # 리스트가 확실하게 보장된 상태에서 버튼 생성
        if isinstance(buttons, list):
            # Telegram API의 callback_data 64바이트 제한을 피하기 위해 일반 키보드(Reply Keyboard) 사용
            keyboard = [[{"text": str(btn)}] for btn in buttons]
            reply_markup = {
                "keyboard": keyboard,
                "resize_keyboard": True,
                "one_time_keyboard": True
            }

    result = await telegram_client.send_message(message, reply_markup=reply_markup)
    return {"status": "success", "response": result} if "error" not in result else result


def get_registered_tools() -> list[str]:
    """등록된 Tool 이름 목록 반환."""
    return list(_TOOL_REGISTRY.keys())


# ── Skill Factory 진입점 ──────────────────────────────────

@tool
async def create_new_skill(user_request: str, test_args: dict = None) -> dict:
    """
    기존 Tool로 처리 불가능한 요청을 받으면 이 도구를 호출하여 새 스킬을 자율 생성합니다.
    [3단계: API 탐색 → 코드 합성/검증 → 레지스트리 등록]을 자동으로 수행하며, 성공 시 즉시 사용 가능한 신규 Tool이 등록됩니다.

    Args:
        user_request (str): 사용자의 요청 자연어 설명 (예: "서울 현재 날씨 알려줘", "환율 조회 기능 만들어줘")
        test_args (dict): 생성된 함수 테스트용 인자 dict (예: {"city": "Seoul"}). 없으면 빈 dict 전달.

    Returns:
        성공 시: {"success": true, "skill_name": ..., "description": ..., "test_result": ..., "message": ...}
        실패 시: {"success": false, "message": ..., "env_key_required": ... (선택)}
    """
    from core.skill_factory import get_skill_factory
    factory = get_skill_factory()
    return await factory.create_skill(
        user_request=user_request,
        test_args=test_args or {},
    )
