"""
executor.py — Tool Registry & Executor (Enterprise Edition)

@tool 데코레이터로 함수를 등록하고,
docstring + type hints를 파싱하여 OpenAI function schema(JSON)를 자동 생성.

Progressive Disclosure:
  - build_function_schemas(): 활성화된 도구만 스키마 생성
  - build_metadata_schemas(): 메타데이터만 포함한 경량 스키마 (점진적 로딩)
  - 품질 평가 및 자가 치유 통합
"""

from __future__ import annotations

import inspect
import json
import time
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
    """등록된 모든 Tool 함수를 OpenAI function calling 스키마 리스트로 변환."""
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


def build_metadata_schemas() -> list[dict]:
    """
    Progressive Disclosure용 경량 스키마.
    이름과 설명만 포함, 파라미터 상세 정보는 제외.
    수천 개의 도구가 있어도 토큰 사용 최소화.
    """
    schemas: list[dict] = []
    for name, func in _TOOL_REGISTRY.items():
        description, _ = _parse_docstring(func.__doc__)
        schemas.append({
            "name": name,
            "description": description[:200],
        })
    return schemas


# ── 실행 (품질 평가 통합) ────────────────────────────────

async def execute(tool_name: str, arguments: dict[str, Any]) -> Any:
    """
    등록된 Tool 함수를 이름으로 찾아 실행.
    async 함수와 sync 함수 모두 지원.
    실행 시간을 측정하여 품질 평가에 활용.
    """
    func = _TOOL_REGISTRY.get(tool_name)
    if func is None:
        return {"error": f"Unknown tool: {tool_name}"}

    start_time = time.time()
    try:
        result = func(**arguments)
        if inspect.isawaitable(result):
            result = await result

        execution_time_ms = (time.time() - start_time) * 1000

        # 생성된 스킬의 품질 추적 (코어 도구는 제외)
        _track_quality(tool_name, result, execution_time_ms)

        return result
    except Exception as e:
        execution_time_ms = (time.time() - start_time) * 1000
        error_result = {"error": f"Tool execution failed: {str(e)}"}

        # 실패 추적
        _track_quality(tool_name, error_result, execution_time_ms, error=str(e))

        return error_result


def _track_quality(
    tool_name: str,
    _result: Any,
    _execution_time_ms: float,
    error: str = "",
) -> None:
    """생성된 스킬의 실행 품질을 비동기적으로 추적."""
    # 코어 도구는 추적하지 않음
    core_tools = {
        "list_all_available_tools", "send_telegram_message",
        "create_new_skill", "reload_env",
    }
    if tool_name in core_tools:
        return

    try:
        from core.skill_registry import get_skill_registry
        registry = get_skill_registry()
        if registry.get_metadata(tool_name) is None:
            return  # 생성된 스킬이 아님

        # 사용 횟수 증가
        registry.increment_use_count(tool_name)
    except Exception:
        pass


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
        "activated_skills": stats.get("activated_skills", []),
        "progressive_loading": stats.get("progressive_loading", False),
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
        if isinstance(buttons, str):
            try:
                buttons = json.loads(buttons)
            except Exception:
                buttons = [buttons]

        if isinstance(buttons, list):
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


# ── 환경변수 동적 리로드 ──────────────────────────────────

@tool
async def reload_env() -> dict:
    """
    .env 파일을 다시 읽어 현재 프로세스에 반영합니다.
    사용자가 .env 파일을 직접 수정한 후 이 도구를 호출하면 서버 재시작 없이 변경사항이 적용됩니다.

    Returns:
        dict: {"success": true, "loaded_keys": [...], "message": "..."}
    """
    import os
    from pathlib import Path
    try:
        from dotenv import load_dotenv, dotenv_values
    except ImportError:
        import subprocess, sys
        subprocess.run([sys.executable, "-m", "pip", "install", "python-dotenv", "-q"])
        from dotenv import load_dotenv, dotenv_values

    try:
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if not env_path.exists():
            return {"error": ".env 파일이 존재하지 않습니다."}

        vals = dotenv_values(str(env_path))
        load_dotenv(str(env_path), override=True)
        loaded_keys = list(vals.keys())

        smtp_ok = "SMTP_PASSWORD" in loaded_keys and bool(vals.get("SMTP_PASSWORD"))

        pending_file = env_path.parent / "generated_skills" / "pending_email_task.json"
        pending_email = None
        if smtp_ok and pending_file.exists():
            try:
                pending_email = json.loads(pending_file.read_text(encoding="utf-8"))
            except Exception:
                pending_email = None

        result = {
            "success": True,
            "loaded_keys": loaded_keys,
            "smtp_ready": smtp_ok,
            "message": (
                f"✅ .env 재로드 완료.\n"
                + ("✅ SMTP_PASSWORD 확인됨!" if smtp_ok
                   else "⚠️ SMTP_PASSWORD 미설정 상태입니다.")
            ),
        }

        if smtp_ok and pending_email:
            result["pending_email"] = pending_email
            result["message"] += (
                f"\n\n⚡ **대기 중인 이메일 작업이 있습니다. 즉시 send_email_via_smtp를 호출하세요!**\n"
                f"- to_email: {pending_email.get('to_email')}\n"
                f"- subject: {pending_email.get('subject')}\n"
                f"- body: {str(pending_email.get('body', ''))[:80]}...\n\n"
                f"지금 즉시 위 파라미터로 send_email_via_smtp tool_call을 수행해야 합니다."
            )
        elif smtp_ok and not pending_email:
            result["message"] += (
                "\n\n📬 SMTP 준비 완료. 이전에 보내려던 이메일이 있었다면 수신자와 내용을 알려주세요.\n"
                "예: 'h5nmou@gmail.com에 A사이트 예약 완료 안내 메일 보내줘'"
            )

        return result
    except Exception as e:
        return {"error": f".env 재로드 실패: {str(e)}"}
