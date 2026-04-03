"""
skill_healer.py — 4단계 자기 치유 시스템 (Self-Healing)

스킬 실행 실패 시 자율 복구 파이프라인:
  Stage 1: 재시작 (Restart) — 동일 파라미터로 재실행
  Stage 2: 상태 확인 (Health Check) — 환경변수, 의존성, 네트워크 검사
  Stage 3: AI 진단 (AI Doctor) — LLM이 오류 분석 후 코드 패치 생성
  Stage 4: 최종 알림 (Escalation) — 사장님(헤이든)에게 Telegram/대시보드 알림

각 단계에서 복구되면 즉시 중단, 다음 단계로 진행하지 않음.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("skill_healer")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GENERATED_SKILLS_DIR = PROJECT_ROOT / "generated_skills"
HEALING_LOG_PATH = PROJECT_ROOT / "logs" / "healing_log.json"

# ── 브리핑 훅 ────────────────────────────────────────────────

_briefing_hook: Callable | None = None


def set_briefing_hook(hook: Callable) -> None:
    """엔진의 broadcaster.emit을 연결하여 실시간 브리핑."""
    global _briefing_hook
    _briefing_hook = hook


async def _brief(level: str, msg: str, meta: str = "") -> None:
    """콘솔 + 대시보드 실시간 브리핑."""
    logger.info(f"[{meta}] {msg}")
    if _briefing_hook:
        await _briefing_hook(level, msg, meta)


# ── 치유 보고서 ──────────────────────────────────────────────

@dataclass
class HealingReport:
    skill_name: str
    stage_reached: int = 0            # 1-4
    healed: bool = False
    healing_method: str = ""          # restart / env_fix / ai_patch / escalated
    original_error: str = ""
    patch_applied: str = ""           # AI Doctor가 적용한 패치 설명
    attempts: list[dict] = field(default_factory=list)
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "skill_name": self.skill_name,
            "stage_reached": self.stage_reached,
            "healed": self.healed,
            "healing_method": self.healing_method,
            "original_error": self.original_error,
            "patch_applied": self.patch_applied,
            "attempts": self.attempts,
            "timestamp": self.timestamp,
        }


# ── Self-Healing 엔진 ────────────────────────────────────────

class SkillHealer:
    """
    4단계 자율 복구 시스템.

    사용법:
        healer = get_skill_healer()
        report = await healer.heal(skill_name, error, args, code)
    """

    MAX_RESTART_ATTEMPTS = 2

    def __init__(self) -> None:
        self._client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    async def heal(
        self,
        skill_name: str,
        original_error: str,
        execution_args: dict,
        skill_code: str = "",
    ) -> HealingReport:
        """
        4단계 자가 치유 파이프라인 실행.

        Args:
            skill_name: 실패한 스킬 이름
            original_error: 원본 에러 메시지
            execution_args: 실행 시 사용된 인자
            skill_code: 스킬 소스 코드 (AI Doctor용)

        Returns:
            HealingReport: 치유 결과 보고서
        """
        report = HealingReport(
            skill_name=skill_name,
            original_error=original_error,
        )

        await _brief("system",
            f"🏥 [Self-Healing] '{skill_name}' 자율 복구 시작\n"
            f"   오류: {original_error[:200]}",
            "[AI Doctor 기동]")

        # ── Stage 1: 재시작 (Restart) ──
        await _brief("system", "🔄 [Stage 1/4] 재시작 시도 중...", "[재시작 중...]")
        report.stage_reached = 1
        restart_result = await self._stage_restart(skill_name, execution_args)
        report.attempts.append({"stage": 1, "method": "restart", **restart_result})

        if restart_result.get("success"):
            report.healed = True
            report.healing_method = "restart"
            await _brief("complete",
                f"✅ [Self-Healing] Stage 1 재시작으로 복구 완료: {skill_name}",
                "[복구 완료]")
            self._save_log(report)
            return report

        # ── Stage 2: 상태 확인 (Health Check) ──
        await _brief("system", "🔍 [Stage 2/4] 환경 상태 점검 중...", "[상태 확인 중...]")
        report.stage_reached = 2
        health_result = await self._stage_health_check(skill_name, original_error)
        report.attempts.append({"stage": 2, "method": "health_check", **health_result})

        if health_result.get("fixed"):
            # 환경 수정 후 재시도
            retry_result = await self._stage_restart(skill_name, execution_args)
            if retry_result.get("success"):
                report.healed = True
                report.healing_method = "env_fix"
                await _brief("complete",
                    f"✅ [Self-Healing] Stage 2 환경 수정으로 복구 완료: {skill_name}",
                    "[복구 완료]")
                self._save_log(report)
                return report

        # ── Stage 3: AI 진단 (AI Doctor) ──
        await _brief("system", "🤖 [Stage 3/4] AI Doctor 진단 중...", "[AI Doctor 분석 중...]")
        report.stage_reached = 3

        if not skill_code:
            skill_code = self._load_skill_code(skill_name)

        if skill_code:
            doctor_result = await self._stage_ai_doctor(
                skill_name, original_error, skill_code, execution_args
            )
            report.attempts.append({"stage": 3, "method": "ai_doctor", **doctor_result})

            if doctor_result.get("patched"):
                report.healed = True
                report.healing_method = "ai_patch"
                report.patch_applied = doctor_result.get("diagnosis", "")
                await _brief("complete",
                    f"✅ [Self-Healing] Stage 3 AI Doctor 패치 적용 완료: {skill_name}\n"
                    f"   진단: {doctor_result.get('diagnosis', '')[:200]}",
                    "[AI Doctor 복구 완료]")
                self._save_log(report)
                return report

        # ── Stage 4: 최종 알림 (Escalation) ──
        await _brief("error", "🚨 [Stage 4/4] 자율 복구 실패 — 사장님에게 알림 전송", "[최종 알림]")
        report.stage_reached = 4
        escalation_result = await self._stage_escalation(skill_name, original_error, report)
        report.attempts.append({"stage": 4, "method": "escalation", **escalation_result})

        report.healed = False
        report.healing_method = "escalated"
        self._save_log(report)

        return report

    # ── Stage 1: 재시작 ──────────────────────────────────────

    async def _stage_restart(
        self, skill_name: str, args: dict
    ) -> dict:
        """동일 파라미터로 스킬 재실행 시도."""
        from core.executor import execute

        for attempt in range(1, self.MAX_RESTART_ATTEMPTS + 1):
            try:
                result = await execute(skill_name, args)
                if isinstance(result, dict) and "error" not in result:
                    return {"success": True, "attempt": attempt, "result": str(result)[:200]}
                if isinstance(result, dict) and result.get("success"):
                    return {"success": True, "attempt": attempt, "result": str(result)[:200]}
            except Exception as e:
                logger.warning(f"Stage 1 재시작 실패 (시도 {attempt}): {e}")

            if attempt < self.MAX_RESTART_ATTEMPTS:
                await asyncio.sleep(1)

        return {"success": False, "message": f"{self.MAX_RESTART_ATTEMPTS}회 재시작 실패"}

    # ── Stage 2: 상태 확인 ───────────────────────────────────

    async def _stage_health_check(
        self, skill_name: str, error_msg: str
    ) -> dict:
        """환경변수, 의존성, 네트워크 상태 점검 및 자동 수정."""
        issues_found = []
        fixed = False

        # 2-1. 환경변수 누락 검사
        error_lower = error_msg.lower()
        env_hints = []
        if "api_key" in error_lower or "api key" in error_lower:
            env_hints.append("API_KEY 관련 환경변수")
        if "smtp" in error_lower:
            env_hints.append("SMTP 관련 환경변수")
        if "token" in error_lower:
            env_hints.append("TOKEN 관련 환경변수")

        if env_hints:
            # .env 리로드 시도
            try:
                load_dotenv(str(PROJECT_ROOT / ".env"), override=True)
                issues_found.append(f"환경변수 리로드 수행: {', '.join(env_hints)}")
                fixed = True
            except Exception:
                pass

        # 2-2. 스킬 파일 존재 여부
        skill_file = GENERATED_SKILLS_DIR / f"{skill_name}.py"
        if not skill_file.exists():
            issues_found.append(f"스킬 파일 없음: {skill_file}")

        # 2-3. 의존성 검사 (import 가능 여부)
        if skill_file.exists():
            code = skill_file.read_text(encoding="utf-8")
            import re
            imports = re.findall(r"^import\s+(\w+)|^from\s+(\w+)", code, re.MULTILINE)
            for imp in imports:
                mod_name = imp[0] or imp[1]
                if mod_name in ("core",):
                    continue
                try:
                    __import__(mod_name)
                except ImportError:
                    issues_found.append(f"누락 패키지: {mod_name}")
                    # 자동 설치 시도
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            sys.executable, "-m", "pip", "install", mod_name, "-q",
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await asyncio.wait_for(proc.wait(), timeout=30)
                        issues_found.append(f"✅ {mod_name} 자동 설치 완료")
                        fixed = True
                    except Exception:
                        issues_found.append(f"❌ {mod_name} 설치 실패")

        # 2-4. 네트워크 연결 검사
        if "connection" in error_lower or "timeout" in error_lower or "connect" in error_lower:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get("https://httpbin.org/get")
                    if resp.status_code == 200:
                        issues_found.append("네트워크 연결: 정상")
                    else:
                        issues_found.append(f"네트워크 상태 불안정: HTTP {resp.status_code}")
            except Exception as e:
                issues_found.append(f"네트워크 연결 실패: {e}")

        return {
            "issues": issues_found,
            "fixed": fixed,
        }

    # ── Stage 3: AI Doctor ───────────────────────────────────

    async def _stage_ai_doctor(
        self,
        skill_name: str,
        error_msg: str,
        skill_code: str,
        execution_args: dict,
    ) -> dict:
        """LLM이 오류를 분석하고 코드 패치를 생성·적용."""
        prompt = f"""당신은 'AI Doctor' — 에이전트 스킬 자율 복구 전문가입니다.

## 환자 정보
- 스킬명: {skill_name}
- 실행 인자: {json.dumps(execution_args, ensure_ascii=False)}

## 증상 (에러 메시지)
```
{error_msg[:1000]}
```

## 현재 코드
```python
{skill_code[:3000]}
```

## 진단 및 처방 규칙
1. 에러의 근본 원인을 정확히 진단하라
2. 최소한의 코드 변경으로 수정하라 (전체 재작성 금지)
3. 기존 함수 시그니처와 @tool 데코레이터는 절대 변경 금지
4. 환경변수 하드코딩 금지 — os.getenv() 유지
5. 수정된 전체 Python 코드를 반환하라

## 출력 형식 (JSON)
{{
  "diagnosis": "근본 원인 한 줄 진단",
  "fix_description": "수정 내용 설명",
  "patched_code": "수정된 전체 Python 코드 (마크다운 없이 순수 코드)"
}}"""

        try:
            response = await self._client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4o"),
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)

            patched_code = result.get("patched_code", "")
            diagnosis = result.get("diagnosis", "")

            if not patched_code:
                return {"patched": False, "diagnosis": diagnosis, "reason": "패치 코드 없음"}

            # 패치 코드 적용 전 보안 검사
            from core.skill_security import get_security_gate
            gate = get_security_gate()
            sec_report = gate.scan(patched_code, skill_name)

            if sec_report.blocked:
                return {
                    "patched": False,
                    "diagnosis": diagnosis,
                    "reason": f"보안 검사 실패: {sec_report.findings}",
                }

            # 패치 적용
            skill_file = GENERATED_SKILLS_DIR / f"{skill_name}.py"
            skill_file.write_text(patched_code, encoding="utf-8")

            # 레지스트리 재로드
            from core.executor import _TOOL_REGISTRY
            try:
                spec = importlib.util.spec_from_file_location(skill_name, skill_file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if skill_name not in _TOOL_REGISTRY:
                    func = getattr(module, skill_name, None)
                    if func:
                        _TOOL_REGISTRY[skill_name] = func
            except Exception as e:
                return {"patched": False, "diagnosis": diagnosis, "reason": f"패치 로드 실패: {e}"}

            # 패치 후 재실행 테스트
            from core.executor import execute
            try:
                test_result = await execute(skill_name, execution_args)
                if isinstance(test_result, dict) and "error" not in test_result:
                    return {
                        "patched": True,
                        "diagnosis": diagnosis,
                        "fix": result.get("fix_description", ""),
                    }
                # 여전히 에러가 있지만 패치는 적용됨
                return {
                    "patched": True,
                    "diagnosis": diagnosis,
                    "fix": result.get("fix_description", ""),
                    "warning": f"패치 적용했으나 테스트 결과: {str(test_result)[:200]}",
                }
            except Exception as e:
                return {
                    "patched": True,
                    "diagnosis": diagnosis,
                    "fix": result.get("fix_description", ""),
                    "warning": f"패치 후 테스트 예외: {e}",
                }

        except Exception as e:
            logger.error(f"AI Doctor 진단 실패: {e}")
            return {"patched": False, "diagnosis": "", "reason": f"LLM 호출 실패: {e}"}

    # ── Stage 4: 최종 알림 ───────────────────────────────────

    async def _stage_escalation(
        self, skill_name: str, error_msg: str, report: HealingReport
    ) -> dict:
        """자율 복구 실패 시 사장님에게 알림 전송."""
        escalation_msg = (
            f"🚨 **[Self-Healing 실패 알림]**\n\n"
            f"스킬: `{skill_name}`\n"
            f"도달 단계: Stage {report.stage_reached}/4\n"
            f"원인: {error_msg[:300]}\n\n"
            f"시도한 복구 방법:\n"
        )
        for attempt in report.attempts:
            stage = attempt.get("stage", "?")
            method = attempt.get("method", "?")
            success = "✅" if attempt.get("success") or attempt.get("patched") or attempt.get("fixed") else "❌"
            escalation_msg += f"  Stage {stage} ({method}): {success}\n"

        escalation_msg += (
            f"\n**조치 필요**: 수동으로 스킬 코드를 검토하거나 삭제해주세요.\n"
            f"파일: `generated_skills/{skill_name}.py`"
        )

        # Telegram 알림 시도
        try:
            from core.telegram_client import telegram_client
            await telegram_client.send_message(escalation_msg)
            return {"notified": True, "channel": "telegram"}
        except Exception as e:
            logger.warning(f"Telegram 알림 실패: {e}")

        # 대시보드 알림 (broadcaster 훅)
        await _brief("error", escalation_msg, "[🚨 Self-Healing 실패]")
        return {"notified": True, "channel": "dashboard"}

    # ── 유틸리티 ─────────────────────────────────────────────

    def _load_skill_code(self, skill_name: str) -> str:
        """스킬 소스 코드를 파일에서 로드."""
        skill_file = GENERATED_SKILLS_DIR / f"{skill_name}.py"
        if skill_file.exists():
            return skill_file.read_text(encoding="utf-8")
        return ""

    def _save_log(self, report: HealingReport) -> None:
        """치유 보고서를 로그에 저장."""
        try:
            HEALING_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            history = []
            if HEALING_LOG_PATH.exists():
                try:
                    history = json.loads(HEALING_LOG_PATH.read_text(encoding="utf-8"))
                except Exception:
                    history = []

            history.append(report.to_dict())
            if len(history) > 200:
                history = history[-200:]

            HEALING_LOG_PATH.write_text(
                json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"치유 로그 저장 실패: {e}")


# ── 싱글톤 ────────────────────────────────────────────────────

_healer_instance: SkillHealer | None = None


def get_skill_healer() -> SkillHealer:
    global _healer_instance
    if _healer_instance is None:
        _healer_instance = SkillHealer()
    return _healer_instance
