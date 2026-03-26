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


def get_registered_tools() -> list[str]:
    """등록된 Tool 이름 목록 반환."""
    return list(_TOOL_REGISTRY.keys())
