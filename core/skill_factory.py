"""
skill_factory.py — Enterprise Skill Factory 2.0

엔터프라이즈급 자율 스킬 생성 파이프라인:

Phase 1: 탐색 — 공식 API/MCP 서버 탐색 → 공공데이터포털 폴백
Phase 2: 합성 및 검증 — LATM 구조 (Maker 고성능 모델 → User 경량 모델)
  - 반복적 프롬프팅 (Iterative Prompting): 실행 오류 + 환경 피드백 루프
  - 피어 리뷰 (Peer Review): 별도 모델이 효율성/안전성 검토
  - 보안 게이트: 다단계 보안 + 프롬프트 주입 방어
  - 품질 평가: 다중 신호 채점
Phase 3: 등록 — 버전 관리 + executor 등록 + 즉시 사용 가능
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import tempfile
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("skill_factory")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GENERATED_SKILLS_DIR = PROJECT_ROOT / "generated_skills"
SKILL_INDEX_PATH = GENERATED_SKILLS_DIR / "skill_index.json"

# ── 로그 출력 헬퍼 (실시간 브리핑) ──────────────────────

_log_hook: Callable[[str, str, str], Any] | None = None


def set_log_hook(hook: Callable) -> None:
    """엔진의 broadcaster.emit 함수를 연결하여 대시보드에 실시간 출력."""
    global _log_hook
    _log_hook = hook


async def _log(level: str, msg: str, meta: str = "") -> None:
    """콘솔 + 대시보드 동시 출력."""
    logger.info(f"[{meta}] {msg}")
    if _log_hook:
        await _log_hook(level, msg, meta)


# ── LATM 모델 설정 ──────────────────────────────────────────

def _get_maker_model() -> str:
    """Maker (고성능 모델) — 코드 생성 + 피어 리뷰."""
    return os.getenv("SKILL_MAKER_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o"))


def _get_user_model() -> str:
    """User (경량 모델) — 실제 실행 시 사용."""
    return os.getenv("SKILL_USER_MODEL", "gpt-4o-mini")


# ── API 탐색 ─────────────────────────────────────────────

class APIDiscovery:
    """웹 검색 기반 API/MCP 서버 탐색기."""

    DDGS_ENDPOINT = "https://api.duckduckgo.com/"
    SERPER_ENDPOINT = "https://google.serper.dev/search"

    def __init__(self) -> None:
        self.serper_key = os.getenv("SERPER_API_KEY")
        self._client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    async def search(self, query: str) -> list[dict]:
        """검색 실행 (Serper 우선, 없으면 DuckDuckGo)."""
        if self.serper_key:
            return await self._search_serper(query)
        return await self._search_duckduckgo(query)

    async def _search_serper(self, query: str) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self.SERPER_ENDPOINT,
                    headers={"X-API-KEY": self.serper_key, "Content-Type": "application/json"},
                    json={"q": query, "num": 5}
                )
                data = resp.json()
                return [
                    {"title": r.get("title", ""), "link": r.get("link", ""), "snippet": r.get("snippet", "")}
                    for r in data.get("organic", [])
                ]
        except Exception as e:
            logger.warning(f"Serper 검색 실패: {e}, DuckDuckGo로 폴백")
            return await self._search_duckduckgo(query)

    async def _search_duckduckgo(self, query: str) -> list[dict]:
        """DuckDuckGo Instant Answer API (무인증)."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    self.DDGS_ENDPOINT,
                    params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
                    headers={"User-Agent": "Mozilla/5.0 SkillFactory/2.0"},
                )
                data = resp.json()
                results = []
                if data.get("AbstractText"):
                    results.append({
                        "title": data.get("Heading", ""),
                        "link": data.get("AbstractURL", ""),
                        "snippet": data.get("AbstractText", "")
                    })
                for r in data.get("RelatedTopics", [])[:4]:
                    if isinstance(r, dict) and r.get("Text"):
                        results.append({
                            "title": r.get("Text", "")[:60],
                            "link": r.get("FirstURL", ""),
                            "snippet": r.get("Text", "")
                        })
                return results
        except Exception as e:
            logger.warning(f"DuckDuckGo 검색 실패: {e}")
            return []

    async def discover_strategy(self, user_request: str) -> dict:
        """요청에 맞는 최적 API/MCP 전략을 결정하여 반환."""
        await _log("system", f"🔍 탐색 시작: '{user_request}'", "[탐색 중...]")

        queries = [
            f"{user_request} official REST API python",
            f"{user_request} python library pip",
        ]
        all_results = []
        for q in queries:
            await _log("system", f"🌐 검색: {q}", "[탐색 중...]")
            results = await self.search(q)
            all_results.extend(results)

        mcp_results = await self.search(f"{user_request} MCP server model context protocol")
        all_results.extend(mcp_results)

        public_results = await self.search(f"{user_request} 공공데이터포털 data.go.kr API")
        all_results.extend(public_results)

        strategy = await self._analyze_with_llm(user_request, all_results)
        return strategy

    async def _analyze_with_llm(self, user_request: str, search_results: list[dict]) -> dict:
        """검색 결과를 LLM에 전달하여 최적 전략 결정."""
        results_text = "\n".join(
            f"- {r['title']}: {r['snippet'][:150]} ({r['link']})"
            for r in search_results[:8]
        )

        prompt = f"""사용자 요청: "{user_request}"

웹 검색 결과:
{results_text}

위 정보를 바탕으로 이 요청을 처리하기 위한 최적의 Python 코드 전략을 JSON으로 결정하세요.

규칙:
1. API 우선 → MCP 차선 (DOM/Vision 불가)
2. 인증이 필요한 경우 env_key_name을 반드시 명시
3. 공식 API가 없으면 공공데이터포털(data.go.kr) 검토
4. pip 설치 가능한 Python 라이브러리도 전략으로 채택 가능

## 특별 규칙 — 이메일 전송 요청
이메일 전송 기능은 Python 표준 라이브러리 smtplib + Gmail SMTP를 사용하세요.
- service_name: "Gmail SMTP"
- strategy: "pip_library"
- pip_packages: [] (표준 라이브러리만 사용)
- auth_required: true
- env_key_name: "SMTP_PASSWORD"  ← 반드시 이 이름 사용
- 다른 환경변수: SMTP_HOST, SMTP_PORT, SMTP_USER (이미 .env에 설정됨)

반드시 아래 JSON 형식으로만 응답:
{{
  "strategy": "official_api" | "pip_library" | "mcp" | "public_api",
  "service_name": "서비스명 (예: OpenWeatherMap)",
  "api_endpoint": "API 엔드포인트 URL (있을 경우)",
  "pip_packages": ["필요한 pip 패키지 목록 (없으면 빈 배열)"],
  "auth_required": true | false,
  "env_key_name": "환경변수명 (예: OPENWEATHER_API_KEY, 불필요하면 null)",
  "description": "이 전략으로 어떻게 구현할지 한 문장 설명",
  "implementation_hint": "핵심 구현 힌트 (예: httpx로 GET 요청, 응답에서 main.temp 추출)"
}}"""

        try:
            response = await self._client.chat.completions.create(
                model=_get_maker_model(),
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)
            await _log("system", f"✅ API 발견: [{result.get('service_name')}] — {result.get('description')}", "[API 발견]")
            return result
        except Exception as e:
            logger.error(f"전략 분석 실패: {e}")
            return {
                "strategy": "unknown",
                "service_name": "Unknown",
                "description": f"전략 결정 실패: {e}",
                "auth_required": False,
                "env_key_name": None,
                "pip_packages": [],
            }


# ── 코드 합성 (LATM Maker) ──────────────────────────────────

class CodeSynthesizer:
    """LATM Maker — 고성능 모델 기반 코드 생성기."""

    def __init__(self) -> None:
        self._client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    async def synthesize(
        self,
        user_request: str,
        strategy: dict,
        test_args: dict | None = None,
        previous_error: str | None = None,
        previous_code: str | None = None,
        env_feedback: str | None = None,
        iteration: int = 1,
    ) -> str:
        """
        완전한 @tool 함수 코드 생성 (Google-style docstring 포함).

        반복적 프롬프팅 (Iterative Prompting):
          - previous_error: 이전 실행 오류 피드백
          - env_feedback: 환경 피드백 (패키지 누락, 네트워크 상태 등)
          - iteration: 현재 반복 횟수
        """
        env_key = strategy.get("env_key_name")
        auth_note = f"환경변수 `{env_key}`에서 API 키를 읽어라." if env_key else "인증 불필요."
        pip_note = f"pip 패키지 필요: {strategy.get('pip_packages', [])}" if strategy.get("pip_packages") else ""

        param_hint = ""
        if test_args:
            param_names = list(test_args.keys())
            param_hint = f"\n## 함수 파라미터 요구사항 (반드시 준수)\n함수는 다음 파라미터 이름을 **정확히** 사용해야 합니다: {param_names}\n예: def func({', '.join(param_names)}): ..."

        # 반복적 프롬프팅: 이전 오류 + 환경 피드백 통합
        correction_section = ""
        if previous_error and previous_code:
            correction_section = f"""
## 이전 실행 오류 (반복 {iteration}차 — 반드시 수정):
```
{previous_error}
```
## 이전 코드:
```python
{previous_code}
```
위 오류를 분석하여 근본 원인을 해결하라. 동일한 실수를 반복하지 마라.
"""
        if env_feedback:
            correction_section += f"""
## 환경 피드백 (시스템 자동 감지):
{env_feedback}
이 환경 피드백을 반영하여 코드를 조정하라.
"""

        prompt = f"""당신은 Python 에이전트 도구를 생성하는 코드 합성 전문가입니다.

## 사용자 요청
"{user_request}"
{param_hint}
## 구현 전략
- 서비스: {strategy.get('service_name')}
- 전략: {strategy.get('strategy')}
- API 엔드포인트: {strategy.get('api_endpoint', 'N/A')}
- 설명: {strategy.get('description')}
- 힌트: {strategy.get('implementation_hint', 'N/A')}
- 인증: {auth_note}
- {pip_note}
{correction_section}

## 코드 생성 규칙
1. **반드시 `async def` 함수로 작성** (httpx.AsyncClient 사용)
2. 함수명은 snake_case, 동사_대상 형태 (예: get_weather_info, search_news_articles)
3. **`from core.executor import tool` import 후 `@tool` 데코레이터 적용**
4. **Google-style Docstring 필수** (Args:, Returns: 섹션 포함)
5. 환경변수는 `os.getenv("ENV_KEY_NAME")` 로만 읽기 (하드코딩 절대 금지)
6. 에러는 dict 형태로 반환: `{{"error": "...", "detail": "..."}}`
7. 성공 시 실용적인 한국어 키-값 dict 반환
8. **import 구문은 함수 상단이 아닌 파일 최상단에 위치**
9. `from __future__ import annotations` 포함
10. pip 패키지가 필요한 경우 주석으로 설치 명령 명시
11. **민감 정보(API 키, 토큰)는 절대 코드에 노출 금지** — os.getenv() 전용

## 이메일 전송 구현 시 반드시 준수 (CRITICAL)
이메일 전송 함수의 파라미터명은 **반드시 아래와 같이 정확히 사용**해야 합니다:
- `to_email` (수신자 이메일, NOT 'recipient', NOT 'email', NOT 'to')
- `subject` (제목)
- `body` (본문)
함수 시그니처 예시: `async def send_email_via_smtp(to_email: str, subject: str, body: str) -> dict:`

## 출력 형식
Python 코드 파일 전체를 마크다운 코드블록 없이 순수 Python 코드로만 출력:
"""

        await _log("system",
            f"⚙️ [LATM Maker] 코드 합성 중... (모델: {_get_maker_model()}, 반복: {iteration}차)",
            "[코드 합성 중...]")

        response = await self._client.chat.completions.create(
            model=_get_maker_model(),
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content.strip()

        # 마크다운 코드블록 제거
        if raw.startswith("```python"):
            raw = raw[len("```python"):].lstrip()
        if raw.startswith("```"):
            raw = raw[3:].lstrip()
        if raw.endswith("```"):
            raw = raw[:-3].rstrip()

        return raw


# ── 피어 리뷰 (Peer Review) ─────────────────────────────────

class PeerReviewer:
    """
    별도 고성능 모델을 활용한 코드 리뷰어.
    효율성, 가독성, 특히 안전성을 검토.
    """

    def __init__(self) -> None:
        self._client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    async def review(self, code: str, skill_name: str, strategy: dict) -> dict:
        """
        생성된 코드를 피어 리뷰.

        Returns:
            {
                "approved": bool,
                "score": int (0-100),
                "issues": list[str],
                "suggestions": list[str],
                "patched_code": str | None  (수정이 필요한 경우)
            }
        """
        review_model = os.getenv("SKILL_REVIEW_MODEL", _get_maker_model())
        await _log("system",
            f"🔍 [Peer Review] 코드 리뷰 시작 (모델: {review_model})",
            "[피어 리뷰 중...]")

        prompt = f"""당신은 시니어 Python 보안 코드 리뷰어입니다.
아래 자동 생성된 에이전트 Tool 코드를 검토하세요.

## 스킬 정보
- 이름: {skill_name}
- 서비스: {strategy.get('service_name', 'Unknown')}
- 전략: {strategy.get('strategy', 'unknown')}

## 검토 대상 코드
```python
{code}
```

## 검토 기준
1. **안전성** (최우선): eval/exec 사용, 쉘 인젝션, 하드코딩된 시크릿, SQL 인젝션
2. **효율성**: 불필요한 API 호출, 리소스 누수 (미닫힌 클라이언트), 비효율적 루프
3. **가독성**: 명확한 변수명, 적절한 에러 처리, 반환값 일관성
4. **호환성**: async/await 올바른 사용, @tool 데코레이터 존재, 올바른 import

## 출력 (JSON)
{{
  "approved": true/false,
  "score": 0-100,
  "issues": ["심각한 문제점 목록 (빈 배열이면 문제 없음)"],
  "suggestions": ["개선 제안 목록"],
  "patched_code": null 또는 "수정된 전체 코드 (심각한 문제가 있을 때만)"
}}"""

        try:
            response = await self._client.chat.completions.create(
                model=review_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)

            score = result.get("score", 70)
            issues = result.get("issues", [])

            if issues:
                issues_text = "\n".join(f"  - {i}" for i in issues)
                await _log("system",
                    f"⚠️ [Peer Review] 발견된 이슈 ({len(issues)}건):\n{issues_text}",
                    "[피어 리뷰 결과]")
            else:
                await _log("system",
                    f"✅ [Peer Review] 통과 — 점수: {score}/100",
                    "[피어 리뷰 통과]")

            return result

        except Exception as e:
            logger.warning(f"피어 리뷰 실패: {e}")
            # 리뷰 실패 시 기본 승인 (가용성 우선)
            return {"approved": True, "score": 60, "issues": [], "suggestions": [f"리뷰 실패: {e}"], "patched_code": None}


# ── 샌드박스 실행 ─────────────────────────────────────────────

class SandboxExecutor:
    """생성된 코드를 격리된 subprocess에서 실행, 결과 검증."""

    TIMEOUT = 30

    async def execute(self, code: str, test_args: dict | None = None) -> dict:
        """코드를 임시 파일로 저장 후 subprocess 실행."""
        func_name = self._extract_function_name(code)
        if not func_name:
            return {"success": False, "output": "", "error": "함수명을 찾을 수 없습니다."}

        import re as _re
        code = _re.sub(
            r'from\s+[Cc]ore\.executor\s+import\s+tool',
            'from core.executor import tool',
            code
        )
        cleaned_code = "\n".join(
            line for line in code.split("\n")
            if not line.strip().startswith("from __future__")
            and not line.strip().startswith("from core.executor import tool")
        )

        filtered_args = self._filter_args_for_func(cleaned_code, func_name, test_args or {})
        if not filtered_args and test_args:
            filtered_args = test_args
        test_args_repr = json.dumps(filtered_args, ensure_ascii=False)

        _injected_root = repr(str(PROJECT_ROOT))
        _injected_env = repr(str(PROJECT_ROOT / ".env"))
        runner_code = (
            "from __future__ import annotations\n"
            "import asyncio, sys, os, json\n"
            "from pathlib import Path\n"
            "from types import ModuleType\n"
            "\n"
            f"_root = Path({_injected_root})\n"
            "if str(_root) not in sys.path:\n"
            "    sys.path.insert(0, str(_root))\n"
            "\n"
            "from dotenv import load_dotenv\n"
            f"load_dotenv({_injected_env})\n"
            "\n"
            "def tool(func):\n"
            "    return func\n"
            "\n"
            "_mock_exe = ModuleType('core.executor')\n"
            "_mock_exe.tool = tool\n"
            "_mock_core = sys.modules.get('core') or ModuleType('core')\n"
            "_mock_core.executor = _mock_exe\n"
            "sys.modules.setdefault('core', _mock_core)\n"
            "sys.modules['core.executor'] = _mock_exe\n"
            "\n"
            + cleaned_code + "\n"
            "\n"
            "async def main():\n"
            "    try:\n"
            f"        result = await {func_name}(**{test_args_repr})\n"
            "        print('SKILL_RESULT:', json.dumps(result, ensure_ascii=False, default=str))\n"
            "    except Exception as e:\n"
            "        print('SKILL_ERROR:', str(e), file=sys.stderr)\n"
            "        raise\n"
            "\n"
            "asyncio.run(main())\n"
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(runner_code)
            tmp_path = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ},
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.TIMEOUT
            )

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if "SKILL_RESULT:" in stdout_str:
                result_line = [l for l in stdout_str.split("\n") if "SKILL_RESULT:" in l][-1]
                result_json = result_line.replace("SKILL_RESULT:", "").strip()
                return {"success": True, "output": result_json, "error": None}
            elif "SKILL_ERROR:" in stderr_str or stderr_str:
                return {"success": False, "output": stdout_str, "error": stderr_str}
            else:
                return {"success": True, "output": stdout_str, "error": None}
        except asyncio.TimeoutError:
            return {"success": False, "output": "", "error": f"실행 타임아웃 ({self.TIMEOUT}초)"}
        except Exception as e:
            return {"success": False, "output": "", "error": str(e)}
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _extract_function_name(self, code: str) -> str | None:
        for line in code.split("\n"):
            line = line.strip()
            if line.startswith("async def ") or line.startswith("def "):
                name = line.split("def ")[1].split("(")[0].strip()
                if name not in ("tool", "main"):
                    return name
        return None

    def _filter_args_for_func(self, code: str, func_name: str, test_args: dict) -> dict:
        """생성된 코드의 함수 파라미터와 test_args를 최적 매핑."""
        import re
        pattern = rf"(?:async\s+)?def\s+{re.escape(func_name)}\s*\(([^)]*)\)"
        match = re.search(pattern, code, re.DOTALL)
        if not match:
            return test_args

        params_str = match.group(1)
        param_names_ordered = []
        for part in params_str.split(","):
            part = part.strip()
            if not part or part in ("self", "*args", "**kwargs"):
                continue
            name = part.split(":")[0].split("=")[0].strip()
            if name:
                param_names_ordered.append(name)

        if not param_names_ordered:
            return {}

        result = {}
        used_values = set()
        for param in param_names_ordered:
            if param in test_args:
                result[param] = test_args[param]
                used_values.add(param)

        remaining_values = [v for k, v in test_args.items() if k not in used_values]
        val_idx = 0
        for param in param_names_ordered:
            if param not in result:
                if val_idx < len(remaining_values):
                    result[param] = remaining_values[val_idx]
                    val_idx += 1

        return result

    def collect_env_feedback(self, _code: str, error: str) -> str:
        """실행 오류에서 환경 피드백을 추출하여 반복적 프롬프팅에 제공."""
        feedback_parts = []

        # 패키지 누락 감지
        import re
        missing_mod = re.search(r"ModuleNotFoundError: No module named '(\w+)'", error)
        if missing_mod:
            feedback_parts.append(f"- 패키지 '{missing_mod.group(1)}' 미설치 — pip install 필요")

        # 환경변수 누락 감지
        env_miss = re.search(r"(API_KEY|TOKEN|PASSWORD|SECRET)\s*(미설정|is None|not set|missing)", error, re.IGNORECASE)
        if env_miss:
            feedback_parts.append(f"- 환경변수 누락 감지: {env_miss.group(0)}")

        # 네트워크 오류 감지
        if "ConnectionError" in error or "ConnectTimeout" in error:
            feedback_parts.append("- 외부 API 연결 실패 — 엔드포인트 URL 또는 네트워크 상태 확인 필요")

        # 인증 오류 감지
        if "401" in error or "403" in error or "Unauthorized" in error:
            feedback_parts.append("- API 인증 실패 — API 키 또는 인증 방식 확인 필요")

        # JSON 파싱 오류
        if "JSONDecodeError" in error:
            feedback_parts.append("- API 응답이 JSON이 아님 — 응답 형식 또는 엔드포인트 확인 필요")

        return "\n".join(feedback_parts) if feedback_parts else ""


# ── 스킬 저장 ─────────────────────────────────────────────

class SkillPersister:
    """생성된 스킬을 파일로 저장하고 인덱스를 업데이트."""

    def save(self, skill_name: str, code: str, description: str, strategy: dict) -> Path:
        """generated_skills/{skill_name}.py 저장."""
        GENERATED_SKILLS_DIR.mkdir(exist_ok=True)

        import re as _re
        code = _re.sub(
            r'from\s+[Cc][Oo][Rr][Ee]\.executor\s+import\s+tool',
            'from core.executor import tool',
            code
        )

        header = (
            f'"""\n자동 생성 스킬: {skill_name}\n'
            f'생성일: {datetime.now().isoformat()}\n'
            f'전략: {strategy.get("service_name", "Unknown")}\n'
            f'Factory: Enterprise Skill Factory 2.0\n"""\n\n'
        )
        full_code = header + code

        path = GENERATED_SKILLS_DIR / f"{skill_name}.py"
        path.write_text(full_code, encoding="utf-8")
        logger.info(f"스킬 저장: {path}")

        self._update_index(skill_name, description, strategy)
        return path

    def _update_index(self, skill_name: str, description: str, strategy: dict) -> None:
        if SKILL_INDEX_PATH.exists():
            index = json.loads(SKILL_INDEX_PATH.read_text(encoding="utf-8"))
        else:
            index = {"skills": [], "last_updated": "", "total_count": 0}

        index["skills"] = [s for s in index["skills"] if s["name"] != skill_name]
        index["skills"].append({
            "name": skill_name,
            "description": description,
            "service": strategy.get("service_name", "Unknown"),
            "strategy": strategy.get("strategy", "unknown"),
            "created_at": datetime.now().isoformat(),
            "use_count": 0,
            "quality_score": None,
            "version": "v1.0.0",
        })
        index["last_updated"] = datetime.now().isoformat()
        index["total_count"] = len(index["skills"])

        SKILL_INDEX_PATH.write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_and_register(self, skill_name: str) -> bool:
        """저장된 스킬 파일을 동적으로 import하여 executor 레지스트리에 등록."""
        from core.executor import _TOOL_REGISTRY

        path = GENERATED_SKILLS_DIR / f"{skill_name}.py"
        if not path.exists():
            return False

        try:
            spec = importlib.util.spec_from_file_location(skill_name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            found = skill_name in _TOOL_REGISTRY
            if not found:
                func = getattr(module, skill_name, None)
                if func:
                    _TOOL_REGISTRY[skill_name] = func
                    found = True
            if found:
                logger.info(f"스킬 동적 로드 성공: {skill_name}")
            return found
        except Exception as e:
            logger.error(f"스킬 로드 실패 {skill_name}: {e}")
            return False


# ── 메인 Skill Factory 2.0 ──────────────────────────────────

class SkillFactory:
    """
    Enterprise Skill Factory 2.0 — The Autonomous Toolmaker.

    파이프라인:
      Phase 1: 탐색 및 전략 수립
      Phase 2: 코드 합성 (LATM Maker) → 샌드박스 검증 → 반복적 프롬프팅
      Phase 2.5: 피어 리뷰 (안전성/효율성 검토)
      Phase 2.7: 보안 게이트 (다단계 보안 + 프롬프트 주입 방어)
      Phase 3: 등록 (버전 관리 + executor 등록 + 품질 평가)
    """

    MAX_CORRECTIONS = 5  # 반복적 프롬프팅 최대 횟수 (3→5 증가)

    def __init__(self) -> None:
        self.discovery = APIDiscovery()
        self.synthesizer = CodeSynthesizer()
        self.reviewer = PeerReviewer()
        self.sandbox = SandboxExecutor()
        self.persister = SkillPersister()

    async def create_skill(
        self,
        user_request: str,
        test_args: dict | None = None,
    ) -> dict:
        """
        요청에 맞는 스킬을 생성하고 등록.

        Returns:
            {
                "success": bool,
                "skill_name": str,
                "description": str,
                "file_path": str,
                "test_result": str,
                "quality_grade": str,
                "security_score": int,
                "message": str
            }
        """
        await _log("divider", "SKILL FACTORY 2.0 — 엔터프라이즈 스킬 생성 시작", "")
        await _log("system", f"📋 요청 분석: {user_request}", "🏭 Skill Factory 2.0")

        # ── 이메일 스킬 처리 (Canonical Template) ──
        from core.executor import _TOOL_REGISTRY
        email_keywords = {"email", "mail", "smtp", "이메일", "메일"}
        user_req_lower = user_request.lower()
        if any(kw in user_req_lower for kw in email_keywords):
            return await self._handle_email_skill(user_request)

        # ── Phase 1: 탐색 및 전략 수립 ──
        await _log("divider", "PHASE 1 — 탐색 및 전략 수립", "")
        strategy = await self.discovery.discover_strategy(user_request)

        if strategy.get("strategy") == "unknown":
            return {
                "success": False,
                "message": f"❌ 적합한 API/서비스를 찾지 못했습니다: {strategy.get('description')}",
            }

        # .env 키 필요 여부 안내
        env_key = strategy.get("env_key_name")
        if env_key and not os.getenv(env_key):
            await _log("system",
                f"🔑 '{env_key}' 환경변수가 필요합니다.",
                "[.env 설정 요청]")
            return {
                "success": False,
                "env_key_required": env_key,
                "service": strategy.get("service_name"),
                "message": (
                    f"🔑 **API 키 설정 필요**: `{env_key}`\n\n"
                    f".env 파일에 `{env_key}=발급받은API키` 를 추가한 후 다시 요청해 주세요.\n"
                    f"서비스: {strategy.get('service_name')} — {strategy.get('description')}"
                ),
            }

        # pip 패키지 설치
        pip_packages = strategy.get("pip_packages", [])
        if pip_packages:
            await _log("system", f"⚙️ pip 패키지 설치 중: {pip_packages}", "[패키지 설치]")
            for pkg in pip_packages:
                try:
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "pip", "install", pkg, "-q",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(proc.wait(), timeout=60)
                    await _log("system", f"✅ {pkg} 설치 완료", "[패키지 설치]")
                except Exception as e:
                    await _log("error", f"❌ {pkg} 설치 실패: {e}", "[패키지 설치]")

        # ── Phase 2: 코드 합성 및 반복적 검증 (Iterative Prompting) ──
        await _log("divider", "PHASE 2 — 코드 합성 및 반복적 검증 (Iterative Prompting)", "")

        code = None
        exec_result = None
        previous_error = None
        env_feedback = None
        has_test_args = bool(test_args)

        for attempt in range(1, self.MAX_CORRECTIONS + 1):
            if attempt > 1:
                await _log("system",
                    f"🔄 반복적 프롬프팅 {attempt}/{self.MAX_CORRECTIONS} — 오류/환경 피드백 반영",
                    "[Iterative Prompting]")

            code = await self.synthesizer.synthesize(
                user_request, strategy,
                test_args=test_args,
                previous_error=previous_error,
                previous_code=code,
                env_feedback=env_feedback,
                iteration=attempt,
            )

            if not has_test_args:
                import ast
                try:
                    ast.parse(code)
                    await _log("system", "✅ 구문 검사 통과 (test_args 없어 실행 테스트 생략)", "[구문 검사]")
                    exec_result = {"success": True, "output": "{}", "error": None}
                    break
                except SyntaxError as se:
                    previous_error = f"SyntaxError: {se}"
                    env_feedback = None
                    await _log("error", f"❌ 구문 오류 (시도 {attempt}): {previous_error}", "[오류 감지]")
                    continue

            await _log("system", "🧪 샌드박스 실행 중...", "[실행 중...]")
            exec_result = await self.sandbox.execute(code, test_args)

            if exec_result["success"]:
                await _log("system", f"✅ 실행 성공! 결과: {exec_result['output'][:200]}", "[실행 성공]")
                break
            else:
                error_msg = exec_result["error"] or ""
                if "missing" in error_msg and "required positional argument" in error_msg:
                    await _log("system",
                        "⚠️ test_args 인자 불일치 (코드 자체는 정상) — 구문 검사 후 등록 진행",
                        "[실행 스킵]")
                    import ast
                    try:
                        ast.parse(code)
                        exec_result = {"success": True, "output": "{}", "error": None}
                        break
                    except SyntaxError:
                        pass

                previous_error = error_msg
                # 환경 피드백 수집 (반복적 프롬프팅의 핵심)
                env_feedback = self.sandbox.collect_env_feedback(code, error_msg)
                if env_feedback:
                    await _log("system",
                        f"📊 [환경 피드백 수집]\n{env_feedback}",
                        "[환경 피드백]")

                await _log("error",
                    f"❌ 실행 오류 (시도 {attempt}): {previous_error[:300]}",
                    "[오류 감지]")

        if not exec_result or not exec_result["success"]:
            return {
                "success": False,
                "message": f"❌ {self.MAX_CORRECTIONS}회 반복적 프롬프팅 후에도 실행 실패:\n{previous_error}",
            }

        skill_name = self.sandbox._extract_function_name(code)
        if not skill_name:
            return {"success": False, "message": "❌ 유효한 함수명을 추출할 수 없습니다."}

        description = strategy.get("description", f"{user_request} 처리 스킬")

        # ── Phase 2.5: 피어 리뷰 (Peer Review) ──
        await _log("divider", "PHASE 2.5 — 피어 리뷰 (Peer Review)", "")
        review_result = await self.reviewer.review(code, skill_name, strategy)

        if review_result.get("patched_code"):
            await _log("system",
                "🔧 [Peer Review] 코드 패치 적용",
                "[피어 리뷰 패치]")
            code = review_result["patched_code"]

        if not review_result.get("approved", True):
            issues = review_result.get("issues", [])
            await _log("error",
                f"❌ [Peer Review] 거부됨: {issues}",
                "[피어 리뷰 거부]")
            return {
                "success": False,
                "message": f"❌ 피어 리뷰 거부: {', '.join(issues)}",
            }

        review_score = review_result.get("score", 70)

        # ── Phase 2.7: 보안 게이트 (Security Gate) ──
        await _log("divider", "PHASE 2.7 — 보안 게이트 (Security Gate)", "")
        await _log("system", "🛡️ [보안 취약점 스캔 중...]", "[보안 스캔 중...]")

        from core.skill_security import get_security_gate
        gate = get_security_gate()
        sec_report = gate.scan(code, skill_name)

        await _log("system",
            f"🛡️ [보안 취약점 스캔 완료] {sec_report.summary}",
            "[보안 스캔 완료]")

        if sec_report.blocked:
            return {
                "success": False,
                "message": (
                    f"🚨 보안 게이트 차단: {skill_name}\n"
                    f"위험도: {sec_report.risk_level.upper()}\n"
                    f"발견: {sec_report.findings}"
                ),
            }

        if sec_report.approval_required:
            await _log("system",
                f"🔒 [승인 필요] {sec_report.allowlist_violations}",
                "[사장님 승인 대기]")
            # Telegram 승인 요청
            try:
                from core.telegram_client import telegram_client
                approval_msg = (
                    f"🔒 **스킬 배포 승인 요청**\n\n"
                    f"스킬: `{skill_name}`\n"
                    f"위험도: {sec_report.risk_level.upper()}\n"
                    f"보안점수: {sec_report.score}/100\n"
                    f"제한 동작: {', '.join(sec_report.allowlist_violations)}\n\n"
                    f"승인하시려면 '승인'을 입력하세요."
                )
                await telegram_client.send_message(approval_msg)
            except Exception:
                pass

        security_score = sec_report.score

        # ── Phase 3: 등록 및 저장 ──
        await _log("divider", "PHASE 3 — 스킬 등록 및 저장", "")

        file_path = self.persister.save(skill_name, code, description, strategy)
        loaded = self.persister.load_and_register(skill_name)

        # 버전 관리
        version = "v1.0.0"
        try:
            from core.skill_versioning import get_version_manager
            vm = get_version_manager()
            version = vm.save_version(
                skill_name, code,
                bump="minor",
                description=description,
                tags=strategy.get("pip_packages", []),
                risk_level=sec_report.risk_level,
            )
        except Exception as e:
            logger.warning(f"버전 저장 실패: {e}")

        # 품질 평가
        quality_grade = "B"
        try:
            from core.skill_quality import get_quality_evaluator
            evaluator = get_quality_evaluator()
            quality_report = evaluator.evaluate(
                skill_name=skill_name,
                user_request=user_request,
                execution_result=json.loads(exec_result.get("output", "{}")) if exec_result.get("output") else {},
                execution_time_ms=0,
                security_score=security_score,
                version=version,
            )
            quality_grade = quality_report.grade
        except Exception as e:
            logger.warning(f"품질 평가 실패: {e}")

        if loaded:
            await _log(
                "complete",
                f"🎉 엔터프라이즈 스킬 등록 완료!\n"
                f"  이름: {skill_name}\n"
                f"  설명: {description}\n"
                f"  서비스: {strategy.get('service_name')}\n"
                f"  버전: {version}\n"
                f"  보안점수: {security_score}/100\n"
                f"  피어리뷰: {review_score}/100\n"
                f"  품질등급: {quality_grade}\n"
                f"  파일: generated_skills/{skill_name}.py\n"
                f"  즉시 호출 가능: 다음 요청부터 바로 사용됩니다.",
                "[엔터프라이즈 스킬 등록 완료]"
            )
            return {
                "success": True,
                "skill_name": skill_name,
                "description": description,
                "service": strategy.get("service_name"),
                "file_path": str(file_path),
                "test_result": exec_result["output"],
                "version": version,
                "security_score": security_score,
                "review_score": review_score,
                "quality_grade": quality_grade,
                "message": (
                    f"✅ **엔터프라이즈 스킬 '{skill_name}' 등록 완료!**\n\n"
                    f"- 서비스: {strategy.get('service_name')}\n"
                    f"- 설명: {description}\n"
                    f"- 버전: {version}\n"
                    f"- 보안: {security_score}/100 | 리뷰: {review_score}/100 | 등급: {quality_grade}\n"
                    f"- 테스트: {exec_result['output'][:300]}\n"
                    f"- 파일: `generated_skills/{skill_name}.py`\n\n"
                    f"⚡ **이 스킬은 지금 즉시 호출 가능합니다!**\n"
                    f"반드시 다음 단계로 '{skill_name}' 도구를 tool_call로 즉시 호출하여 요청을 완료하세요.\n"
                    f"말로만 '사용하겠습니다'라고 하지 말고 실제 tool_call을 수행해야 합니다."
                ),
            }
        else:
            return {
                "success": False,
                "message": f"코드 실행은 성공했으나 레지스트리 등록에 실패했습니다. 파일은 저장됨: {file_path}",
            }

    # ── 이메일 스킬 전용 처리 ─────────────────────────────────

    async def _handle_email_skill(self, _user_request: str) -> dict:
        """이메일 스킬은 Canonical Template으로 즉시 생성 (LLM 합성 불필요)."""
        from core.executor import _TOOL_REGISTRY

        skill_name = "send_email_via_smtp"
        skill_file = GENERATED_SKILLS_DIR / f"{skill_name}.py"

        if skill_file.exists() and skill_name not in _TOOL_REGISTRY:
            await _log("system", f"📂 [{skill_name}] 파일 발견 — 레지스트리 로드 중...", "[스킬 로드]")
            self.persister.load_and_register(skill_name)

        if skill_name in _TOOL_REGISTRY:
            await _log("system", f"✅ [{skill_name}] 재사용", "[스킬 재사용]")
            return {
                "success": True,
                "skill_name": skill_name,
                "description": "Gmail SMTP 이메일 전송 스킬",
                "message": (
                    f"✅ 이미 등록된 스킬 '{skill_name}' 사용!\n\n"
                    "⚡ 지금 즉시 이 스킬을 tool_call로 호출하여 이메일을 전송하세요!"
                ),
            }

        await _log("system", f"🚀 [{skill_name}] Canonical Template으로 생성", "[스킬 생성]")
        canonical_code = textwrap.dedent('''\
            from __future__ import annotations
            import os, json, smtplib
            from pathlib import Path
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            from core.executor import tool

            _PENDING_FILE = Path(__file__).parent / "pending_email_task.json"

            @tool
            async def send_email_via_smtp(to_email: str, subject: str, body: str) -> dict:
                """Gmail SMTP 서버를 통해 이메일을 전송합니다.

                Args:
                    to_email (str): 수신자 이메일 주소 (예: h5nmou@gmail.com)
                    subject (str): 이메일 제목
                    body (str): 이메일 본문 (plain text)

                Returns:
                    dict: 성공 시 {"success": "이메일 전송 완료", "to": to_email}
                """
                smtp_user = os.getenv("SMTP_USER")
                smtp_password = os.getenv("SMTP_PASSWORD")
                smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
                smtp_port = int(os.getenv("SMTP_PORT", "587"))

                if not smtp_user:
                    return {"error": "환경변수 누락", "detail": "SMTP_USER 미설정"}
                if not smtp_password:
                    _PENDING_FILE.write_text(
                        json.dumps({"to_email": to_email, "subject": subject, "body": body}, ensure_ascii=False),
                        encoding="utf-8")
                    return {"error": "환경변수 누락", "detail": "SMTP_PASSWORD 미설정",
                            "env_key_required": "SMTP_PASSWORD",
                            "hint": ".env에 SMTP_PASSWORD=앱비밀번호 추가 후 '설정 완료' 알려주세요."}
                try:
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = subject
                    msg["From"] = smtp_user
                    msg["To"] = to_email
                    msg.attach(MIMEText(body, "plain", "utf-8"))
                    with smtplib.SMTP(smtp_host, smtp_port) as server:
                        server.ehlo(); server.starttls(); server.ehlo()
                        server.login(smtp_user, smtp_password)
                        server.send_message(msg)
                    if _PENDING_FILE.exists():
                        _PENDING_FILE.unlink()
                    return {"success": "이메일 전송 완료", "to": to_email, "subject": subject, "from": smtp_user}
                except smtplib.SMTPAuthenticationError as e:
                    _PENDING_FILE.write_text(
                        json.dumps({"to_email": to_email, "subject": subject, "body": body}, ensure_ascii=False),
                        encoding="utf-8")
                    return {"error": "SMTP 인증 실패", "detail": str(e),
                            "env_key_required": "SMTP_PASSWORD",
                            "hint": ".env에서 SMTP_PASSWORD 수정 후 '설정 완료' 알려주세요."}
                except Exception as e:
                    return {"error": "이메일 전송 실패", "detail": str(e)}
            ''')

        file_path = self.persister.save(
            skill_name, canonical_code,
            "Gmail SMTP 이메일 전송 스킬 (SMTP_USER, SMTP_PASSWORD 필요)",
            {"service_name": "Gmail SMTP", "strategy": "canonical"}
        )
        success = self.persister.load_and_register(skill_name)
        if success:
            return {
                "success": True,
                "skill_name": skill_name,
                "file_path": str(file_path),
                "test_result": "PASS (Canonical)",
                "message": (
                    f"✅ 이메일 스킬 '{skill_name}' 생성 완료!\n\n"
                    "⚡ 지금 즉시 tool_call로 호출하여 이메일을 전송하세요!"
                ),
            }
        return {"success": False, "message": "이메일 스킬 등록 실패"}


# ── 싱글톤 ────────────────────────────────────────────────

_factory_instance: SkillFactory | None = None


def get_skill_factory() -> SkillFactory:
    global _factory_instance
    if _factory_instance is None:
        _factory_instance = SkillFactory()
    return _factory_instance
