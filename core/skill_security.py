"""
skill_security.py — Enterprise Skill Security Gate

다단계 보안 검사 + 프롬프트 주입 방어 + Allowlist 정책:
  1. 악성 패턴 차단 (eval, os.system 등)
  2. import 허용 목록 검사
  3. 프롬프트 주입 방어 (데이터/명령 분리 원칙)
  4. Allowlist 정책 (안전한 명령 세트 검증)
  5. 위험도 평가 (low / medium / high / critical)
  6. critical 즉시 차단 / high → 사장님(헤이든) 승인 요청
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("skill_security")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECURITY_LOG_PATH = PROJECT_ROOT / "logs" / "security_audit.json"

# ── 위험도 ────────────────────────────────────────────────────────────────────

RISK_LEVELS = ("low", "medium", "high", "critical")


@dataclass
class SecurityReport:
    risk_level: str = "low"          # low | medium | high | critical
    blocked: bool = False
    findings: list[str] = field(default_factory=list)
    score: int = 100                  # 보안 점수 0-100
    approved: bool = True             # False → 실행 불가
    approval_required: bool = False   # True → 사장님 승인 필요
    injection_detected: bool = False  # 프롬프트 주입 탐지 여부
    allowlist_violations: list[str] = field(default_factory=list)
    summary: str = ""
    code_hash: str = ""               # 코드 무결성 해시

    def to_dict(self) -> dict:
        return {
            "risk_level": self.risk_level,
            "blocked": self.blocked,
            "findings": self.findings,
            "score": self.score,
            "approved": self.approved,
            "approval_required": self.approval_required,
            "injection_detected": self.injection_detected,
            "allowlist_violations": self.allowlist_violations,
            "summary": self.summary,
            "code_hash": self.code_hash,
        }


# ── 허용/차단 목록 ─────────────────────────────────────────────────────────────

# 즉시 실행 차단 패턴 (critical)
_CRITICAL_PATTERNS: list[tuple[str, str]] = [
    (r"\beval\s*\(", "eval() 함수 사용 — 코드 인젝션 위험"),
    (r"\bexec\s*\(", "exec() 함수 사용 — 임의 코드 실행 위험"),
    (r"\b__import__\s*\(", "__import__() 직접 호출 — 우회 임포트 위험"),
    (r"os\.system\s*\(", "os.system() 호출 — 쉘 명령 실행 위험"),
    (r"subprocess\.call\s*\(", "subprocess.call() — 쉘 명령 실행 위험"),
    (r"subprocess\.run\s*\(.*shell\s*=\s*True", "shell=True subprocess — 쉘 인젝션 위험"),
    (r"rm\s+-rf\s+/", "rm -rf / 패턴 — 파일 삭제 위험"),
    (r"DROP\s+TABLE", "SQL DROP TABLE — 데이터 삭제 위험"),
    (r"shutil\.rmtree\s*\(\s*['\"]?/", "shutil.rmtree(/) — 루트 디렉토리 삭제 위험"),
    (r"compile\s*\(.*\bexec\b", "compile() + exec — 동적 코드 실행 위험"),
    (r"globals\s*\(\s*\)\s*\[", "globals() 직접 접근 — 네임스페이스 조작 위험"),
    (r"setattr\s*\(\s*__builtins__", "builtins 변조 — 빌트인 함수 오버라이드 위험"),
]

# 주의 패턴 (high)
_HIGH_PATTERNS: list[tuple[str, str]] = [
    (r"subprocess\.(Popen|run|call|check_output)", "subprocess 시스템 호출 — 검토 필요"),
    (r"open\s*\(.*['\"]w['\"]", "파일 쓰기 작업 — 검토 필요"),
    (r"socket\.", "소켓 통신 — 검토 필요"),
    (r"ctypes\.", "ctypes 저수준 메모리 접근 — 검토 필요"),
    (r"pickle\.(loads|load)\s*\(", "pickle 역직렬화 — 코드 실행 위험"),
    (r"marshal\.(loads|load)\s*\(", "marshal 역직렬화 — 코드 실행 위험"),
    (r"yaml\.load\s*\((?!.*Loader)", "yaml.load() without SafeLoader — 코드 실행 위험"),
    (r"webbrowser\.open", "webbrowser.open — 외부 브라우저 실행"),
]

# 경고 패턴 (medium)
_MEDIUM_PATTERNS: list[tuple[str, str]] = [
    (r"requests\.get|requests\.post|httpx\.", "외부 HTTP 요청"),
    (r"os\.environ\[", "환경변수 직접 쓰기"),
    (r"sys\.path\.(append|insert)", "sys.path 변경"),
    (r"tempfile\.", "임시 파일 사용"),
    (r"threading\.Thread", "스레드 생성"),
]

# import 허용 목록 (이 목록에 없는 것은 medium으로 플래그)
_ALLOWED_IMPORTS: set[str] = {
    # 표준 라이브러리
    "os", "sys", "json", "re", "ast", "math", "random", "hashlib",
    "datetime", "time", "pathlib", "typing", "dataclasses", "functools",
    "itertools", "collections", "enum", "abc", "io", "copy", "textwrap",
    "logging", "traceback", "inspect", "importlib",
    "smtplib", "email", "email.mime", "email.mime.text", "email.mime.multipart",
    "urllib", "urllib.parse", "urllib.request", "http", "http.client",
    "asyncio", "concurrent", "threading", "queue",
    "csv", "xml", "html", "base64", "struct", "zlib",
    "uuid", "decimal", "fractions", "statistics", "secrets",
    "contextlib", "warnings", "types", "string",
    # 서드파티 (일반적으로 안전)
    "httpx", "requests", "aiohttp",
    "openai", "anthropic",
    "dotenv", "python_dotenv",
    "pydantic",
    "jwt",
    # 프로젝트 내부
    "core", "core.executor", "core.skill_factory",
    # 기타 필요 패키지
    "bs4", "beautifulsoup4",
    "PIL", "pillow",
    "numpy", "pandas",
    "feedparser", "markdown",
}

# ── 프롬프트 주입 방어 패턴 ────────────────────────────────────────────────────

_INJECTION_PATTERNS: list[tuple[str, str]] = [
    # 시스템 프롬프트 탈취 시도
    (r"ignore\s+(previous|all|above)\s+(instructions?|prompts?|rules?)",
     "시스템 프롬프트 무시 시도"),
    (r"(disregard|forget|override)\s+(your|the|all)\s+(instructions?|rules?|constraints?)",
     "명령 오버라이드 시도"),
    (r"you\s+are\s+now\s+(a|an|the)\s+",
     "역할 재정의 시도 (role hijacking)"),
    (r"system\s*:\s*",
     "system: 프리픽스를 사용한 명령 주입"),
    (r"<\|?\s*system\s*\|?>",
     "시스템 태그 주입"),
    (r"```\s*(system|instruction|prompt)",
     "코드블록을 이용한 프롬프트 주입"),
    # 데이터 탈취 시도
    (r"(print|output|show|reveal|display)\s+(the|your|all)\s+(system|secret|api|key|token|password)",
     "민감 정보 탈취 시도"),
    (r"(what|show)\s+(is|are)\s+your\s+(instructions?|prompts?|rules?)",
     "시스템 프롬프트 노출 요청"),
    # 위장 명령
    (r"\\n\s*(system|assistant|user)\s*:",
     "줄바꿈을 이용한 역할 위장"),
    (r"</?(?:system|instruction|admin|root)>",
     "HTML 태그를 이용한 권한 상승 시도"),
]

# ── Allowlist: 안전한 동작 세트 ─────────────────────────────────────────────────

# 스킬이 수행할 수 있는 '안전한 동작' 카테고리
_ALLOWED_OPERATIONS: dict[str, list[str]] = {
    "http_read": [
        "httpx.AsyncClient.get",
        "httpx.AsyncClient.head",
        "requests.get",
        "aiohttp.ClientSession.get",
    ],
    "http_write": [
        "httpx.AsyncClient.post",
        "httpx.AsyncClient.put",
        "httpx.AsyncClient.patch",
        "requests.post",
        "requests.put",
    ],
    "email": [
        "smtplib.SMTP",
        "smtplib.SMTP_SSL",
    ],
    "file_read": [
        "open:r",
        "pathlib.Path.read_text",
        "pathlib.Path.read_bytes",
    ],
    "data_processing": [
        "json.loads",
        "json.dumps",
        "csv.reader",
        "csv.writer",
        "xml.etree",
    ],
    "env_read": [
        "os.getenv",
        "os.environ.get",
    ],
}

# 승인 없이 금지되는 동작 (사장님 승인 필요)
_RESTRICTED_OPERATIONS: list[tuple[str, str]] = [
    (r"\.delete\s*\(", "HTTP DELETE 요청 — 데이터 삭제"),
    (r"shutil\.(copy|move|rmtree)", "파일 시스템 변경"),
    (r"os\.(remove|unlink|rmdir|rename)", "파일 시스템 삭제/이동"),
    (r"sqlite3|psycopg|mysql|pymongo", "데이터베이스 직접 접근"),
]


# ── 보안 검사 로직 ─────────────────────────────────────────────────────────────

class SkillSecurityGate:
    """
    Enterprise-grade 스킬 보안 게이트.

    7단계 검사 파이프라인:
      1. Critical 패턴 스캔
      2. High 패턴 스캔
      3. Medium 패턴 스캔
      4. Import 허용 목록 검사
      5. 프롬프트 주입 방어 스캔
      6. Allowlist/Restricted 동작 검증
      7. AST 구문 유효성 + 무결성 해시
    """

    def scan(self, code: str, skill_name: str = "unknown") -> SecurityReport:
        """
        코드를 전체 스캔하여 SecurityReport 반환.

        Args:
            code: 검사할 Python 코드 문자열
            skill_name: 스킬 이름 (로그용)

        Returns:
            SecurityReport: 위험도, 발견 사항, 승인 여부
        """
        findings: list[str] = []
        allowlist_violations: list[str] = []
        max_risk = "low"
        score = 100
        injection_detected = False
        approval_required = False

        # ─ 1단계: Critical 패턴 검사 ─
        for pattern, desc in _CRITICAL_PATTERNS:
            if re.search(pattern, code, re.IGNORECASE):
                findings.append(f"🚨 [CRITICAL] {desc}")
                max_risk = "critical"
                score = max(0, score - 40)

        # ─ 2단계: High 패턴 검사 ─
        for pattern, desc in _HIGH_PATTERNS:
            if re.search(pattern, code, re.IGNORECASE):
                findings.append(f"⚠️ [HIGH] {desc}")
                if max_risk not in ("critical",):
                    max_risk = "high"
                score = max(0, score - 20)

        # ─ 3단계: Medium 패턴 검사 ─
        for pattern, desc in _MEDIUM_PATTERNS:
            if re.search(pattern, code, re.IGNORECASE):
                findings.append(f"ℹ️ [MEDIUM] {desc}")
                if max_risk not in ("critical", "high"):
                    max_risk = "medium"
                score = max(0, score - 5)

        # ─ 4단계: Import 허용 목록 검사 ─
        import_findings = self._check_imports(code)
        for imp_finding in import_findings:
            findings.append(imp_finding)
            if max_risk not in ("critical", "high"):
                max_risk = "medium"
            score = max(0, score - 5)

        # ─ 5단계: 프롬프트 주입 방어 스캔 ─
        injection_findings = self._scan_prompt_injection(code)
        if injection_findings:
            injection_detected = True
            for inj in injection_findings:
                findings.append(f"🛡️ [INJECTION] {inj}")
            if max_risk not in ("critical",):
                max_risk = "high"
            score = max(0, score - 30)

        # ─ 6단계: Allowlist/Restricted 동작 검증 ─
        restricted_findings = self._check_restricted_operations(code)
        for rf in restricted_findings:
            findings.append(f"🔒 [RESTRICTED] {rf}")
            allowlist_violations.append(rf)
            approval_required = True
            if max_risk not in ("critical",):
                max_risk = "high"
            score = max(0, score - 15)

        # ─ 7단계: AST 구문 유효성 + 무결성 해시 ─
        try:
            ast.parse(code)
        except SyntaxError as e:
            findings.append(f"❌ [SYNTAX] 구문 오류: {e}")
            score = 0

        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]

        # 승인 여부 결정
        blocked = max_risk == "critical"
        approved = not blocked

        # high 위험도 + restricted 동작 → 승인 요청
        if max_risk == "high" and allowlist_violations:
            approval_required = True

        summary_parts = [
            f"스킬: {skill_name}",
            f"위험도: {max_risk.upper()}",
            f"보안점수: {score}/100",
            f"해시: {code_hash}",
        ]
        if findings:
            summary_parts.append(f"발견: {len(findings)}건")
        if injection_detected:
            summary_parts.append("🛡️ 주입 탐지")
        if approval_required:
            summary_parts.append("🔒 승인 필요")

        report = SecurityReport(
            risk_level=max_risk,
            blocked=blocked,
            findings=findings,
            score=score,
            approved=approved,
            approval_required=approval_required,
            injection_detected=injection_detected,
            allowlist_violations=allowlist_violations,
            summary=" | ".join(summary_parts),
            code_hash=code_hash,
        )

        # 보안 감사 로그 저장
        self._save_audit_log(skill_name, report)

        return report

    def scan_user_input(self, user_input: str) -> list[str]:
        """
        사용자 입력에서 프롬프트 주입 시도를 탐지.

        데이터/명령 분리 원칙:
          - 사용자 입력은 '데이터'로만 취급
          - 시스템 명령으로 해석될 수 있는 패턴을 차단

        Args:
            user_input: 사용자 입력 문자열

        Returns:
            탐지된 주입 패턴 설명 리스트 (빈 리스트 = 안전)
        """
        return self._scan_prompt_injection(user_input)

    def verify_integrity(self, code: str, expected_hash: str) -> bool:
        """코드 무결성 검증 (해시 비교)."""
        actual_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
        return actual_hash == expected_hash

    # ── 내부 검사 메서드 ──────────────────────────────────────

    def _check_imports(self, code: str) -> list[str]:
        """허용 목록에 없는 import 탐지."""
        findings = []
        import_pattern = re.compile(
            r"^\s*(?:import\s+([\w.,\s]+)|from\s+([\w.]+)\s+import)",
            re.MULTILINE,
        )
        for match in import_pattern.finditer(code):
            if match.group(1):
                for mod in match.group(1).split(","):
                    mod = mod.strip().split(".")[0]
                    if mod and mod not in _ALLOWED_IMPORTS:
                        findings.append(f"ℹ️ [IMPORT] 미허가 패키지: '{mod}' — 수동 검토 필요")
            elif match.group(2):
                mod = match.group(2).split(".")[0]
                if mod and mod not in _ALLOWED_IMPORTS:
                    findings.append(f"ℹ️ [IMPORT] 미허가 패키지: '{mod}' — 수동 검토 필요")
        return findings

    def _scan_prompt_injection(self, text: str) -> list[str]:
        """프롬프트 주입 패턴 탐지."""
        detections = []
        for pattern, desc in _INJECTION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                detections.append(desc)
        return detections

    def _check_restricted_operations(self, code: str) -> list[str]:
        """제한된 동작 탐지 (사장님 승인 필요)."""
        violations = []
        for pattern, desc in _RESTRICTED_OPERATIONS:
            if re.search(pattern, code, re.IGNORECASE):
                violations.append(desc)
        return violations

    def _save_audit_log(self, skill_name: str, report: SecurityReport) -> None:
        """보안 감사 로그 저장."""
        try:
            SECURITY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            history = []
            if SECURITY_LOG_PATH.exists():
                try:
                    history = json.loads(SECURITY_LOG_PATH.read_text(encoding="utf-8"))
                except Exception:
                    history = []

            history.append({
                "timestamp": datetime.now().isoformat(),
                "skill_name": skill_name,
                **report.to_dict(),
            })
            if len(history) > 300:
                history = history[-300:]

            SECURITY_LOG_PATH.write_text(
                json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"보안 감사 로그 저장 실패: {e}")


# ── 싱글톤 ────────────────────────────────────────────────────────────────────

_gate_instance: SkillSecurityGate | None = None


def get_security_gate() -> SkillSecurityGate:
    global _gate_instance
    if _gate_instance is None:
        _gate_instance = SkillSecurityGate()
    return _gate_instance
