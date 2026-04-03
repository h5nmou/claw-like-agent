"""
skill_quality.py — Enterprise Skill Quality Evaluator

다중 신호 기반 스킬 품질 보증 시스템:
  1. 실행 오류율 (Execution Error Rate)
  2. 의미적 일치도 (Semantic Alignment) — 요청 의도 vs 실제 결과
  3. 성능 지표 (응답 시간, 리소스 사용)
  4. 종합 품질 점수 (0-100) 산출
  5. 품질 미달 시 자동 롤백 트리거
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("skill_quality")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
QUALITY_LOG_PATH = PROJECT_ROOT / "generated_skills" / "quality_log.json"


# ── 품질 보고서 ──────────────────────────────────────────────

@dataclass
class QualityReport:
    """스킬 실행에 대한 종합 품질 보고서."""
    skill_name: str
    version: str = "unknown"
    execution_success: bool = True
    execution_time_ms: float = 0.0
    error_message: str = ""
    semantic_score: float = 1.0        # 0.0 ~ 1.0 (의미적 일치도)
    output_validity: bool = True       # 출력 형식 유효성
    security_score: int = 100          # 보안 점수 (0-100)
    total_score: float = 100.0         # 종합 점수 (0-100)
    grade: str = "A"                   # A/B/C/D/F
    recommendations: list[str] = field(default_factory=list)
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "skill_name": self.skill_name,
            "version": self.version,
            "execution_success": self.execution_success,
            "execution_time_ms": self.execution_time_ms,
            "error_message": self.error_message,
            "semantic_score": self.semantic_score,
            "output_validity": self.output_validity,
            "security_score": self.security_score,
            "total_score": self.total_score,
            "grade": self.grade,
            "recommendations": self.recommendations,
            "timestamp": self.timestamp,
        }


# ── 실행 이력 추적 ───────────────────────────────────────────

@dataclass
class SkillExecutionRecord:
    """개별 스킬의 실행 이력 집계."""
    skill_name: str
    total_runs: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_time_ms: float = 0.0
    last_error: str = ""
    quality_scores: list[float] = field(default_factory=list)
    consecutive_failures: int = 0

    @property
    def error_rate(self) -> float:
        if self.total_runs == 0:
            return 0.0
        return self.failure_count / self.total_runs

    @property
    def avg_time_ms(self) -> float:
        if self.total_runs == 0:
            return 0.0
        return self.total_time_ms / self.total_runs

    @property
    def avg_quality(self) -> float:
        if not self.quality_scores:
            return 100.0
        return sum(self.quality_scores[-10:]) / len(self.quality_scores[-10:])


# ── 품질 평가 엔진 ───────────────────────────────────────────

class SkillQualityEvaluator:
    """
    다중 신호 기반 스킬 품질 평가 엔진.

    평가 기준:
      - 실행 성공 여부 (40%)
      - 의미적 일치도 (25%)
      - 보안 점수 (20%)
      - 성능 (15%)
    """

    # 가중치 설정
    WEIGHT_EXECUTION = 0.40
    WEIGHT_SEMANTIC  = 0.25
    WEIGHT_SECURITY  = 0.20
    WEIGHT_PERF      = 0.15

    # 등급 기준
    GRADE_THRESHOLDS = {
        "A": 85,
        "B": 70,
        "C": 55,
        "D": 40,
        "F": 0,
    }

    # 품질 미달 임계값
    QUALITY_THRESHOLD = 40.0          # 이 점수 미만이면 롤백 트리거
    MAX_CONSECUTIVE_FAILURES = 3      # 연속 실패 시 자가 치유 트리거
    SLOW_EXECUTION_MS = 10000         # 10초 이상이면 성능 경고

    def __init__(self) -> None:
        self._records: dict[str, SkillExecutionRecord] = {}
        self._load_history()

    # ── 핵심 평가 메서드 ──────────────────────────────────────

    def evaluate(
        self,
        skill_name: str,
        user_request: str,
        execution_result: Any,
        execution_time_ms: float,
        security_score: int = 100,
        version: str = "unknown",
    ) -> QualityReport:
        """
        스킬 실행 결과를 종합 평가하여 QualityReport 반환.

        Args:
            skill_name: 스킬 이름
            user_request: 원본 사용자 요청
            execution_result: 스킬 실행 결과 (dict 또는 str)
            execution_time_ms: 실행 시간 (밀리초)
            security_score: 보안 검사 점수 (0-100)
            version: 스킬 버전

        Returns:
            QualityReport: 종합 품질 보고서
        """
        recommendations = []

        # ── 1. 실행 성공 여부 평가 (40%) ──
        exec_success = True
        error_msg = ""
        if isinstance(execution_result, dict):
            if "error" in execution_result:
                exec_success = False
                error_msg = str(execution_result.get("error", ""))
        elif isinstance(execution_result, str) and "error" in execution_result.lower():
            exec_success = False
            error_msg = execution_result

        exec_score = 100.0 if exec_success else 0.0

        # ── 2. 의미적 일치도 평가 (25%) ──
        semantic_score = self._evaluate_semantic_alignment(
            user_request, execution_result, exec_success
        )
        if semantic_score < 0.5:
            recommendations.append("의미적 일치도 낮음 — 스킬 로직 재검토 필요")

        # ── 3. 보안 점수 (20%) ──
        if security_score < 60:
            recommendations.append(f"보안 점수 {security_score}/100 — 보안 검토 필요")

        # ── 4. 성능 평가 (15%) ──
        perf_score = self._evaluate_performance(execution_time_ms)
        if execution_time_ms > self.SLOW_EXECUTION_MS:
            recommendations.append(
                f"실행 시간 {execution_time_ms:.0f}ms — 성능 최적화 권장"
            )

        # ── 출력 유효성 ──
        output_valid = self._validate_output(execution_result)
        if not output_valid:
            recommendations.append("출력 형식이 표준(dict)이 아님 — 반환값 형식 점검 필요")

        # ── 종합 점수 산출 ──
        total_score = (
            exec_score * self.WEIGHT_EXECUTION
            + (semantic_score * 100) * self.WEIGHT_SEMANTIC
            + security_score * self.WEIGHT_SECURITY
            + perf_score * self.WEIGHT_PERF
        )
        total_score = round(min(100.0, max(0.0, total_score)), 1)

        # ── 등급 결정 ──
        grade = "F"
        for g, threshold in self.GRADE_THRESHOLDS.items():
            if total_score >= threshold:
                grade = g
                break

        # ── 실행 이력 기록 ──
        self._record_execution(
            skill_name, exec_success, execution_time_ms, error_msg, total_score
        )

        report = QualityReport(
            skill_name=skill_name,
            version=version,
            execution_success=exec_success,
            execution_time_ms=execution_time_ms,
            error_message=error_msg,
            semantic_score=semantic_score,
            output_validity=output_valid,
            security_score=security_score,
            total_score=total_score,
            grade=grade,
            recommendations=recommendations,
        )

        # 품질 로그 저장
        self._save_report(report)

        return report

    # ── 의미적 일치도 ─────────────────────────────────────────

    def _evaluate_semantic_alignment(
        self,
        user_request: str,
        result: Any,
        exec_success: bool,
    ) -> float:
        """
        사용자 요청과 실행 결과 간의 의미적 일치도를 0.0~1.0으로 평가.

        경량 키워드 기반 평가 (LLM 호출 없이 로컬 처리):
          - 실행 실패 시 기본 0.2
          - 결과에 요청 관련 키워드가 포함되어 있으면 가산
          - 결과가 의미 있는 데이터를 포함하면 가산
        """
        if not exec_success:
            return 0.2

        score = 0.5  # 기본 (실행 성공)

        result_text = ""
        if isinstance(result, dict):
            result_text = json.dumps(result, ensure_ascii=False).lower()
        elif isinstance(result, str):
            result_text = result.lower()

        if not result_text:
            return 0.3

        # 요청 키워드 매칭
        request_keywords = set(user_request.lower().split())
        # 불용어 제거
        stopwords = {"을", "를", "이", "가", "에", "의", "로", "와", "과", "하", "해", "줘", "좀",
                      "the", "a", "an", "is", "to", "for", "and", "or", "in", "on"}
        meaningful_keywords = {kw for kw in request_keywords if len(kw) > 1 and kw not in stopwords}

        if meaningful_keywords:
            matched = sum(1 for kw in meaningful_keywords if kw in result_text)
            keyword_ratio = matched / len(meaningful_keywords)
            score += keyword_ratio * 0.3

        # 결과 풍부도 (데이터 필드 수)
        if isinstance(result, dict):
            data_keys = {k for k in result if k not in ("error", "detail", "status")}
            if len(data_keys) >= 3:
                score += 0.15
            elif len(data_keys) >= 1:
                score += 0.05

        # success 명시적 표시
        if isinstance(result, dict) and result.get("success"):
            score += 0.05

        return min(1.0, score)

    # ── 성능 평가 ─────────────────────────────────────────────

    def _evaluate_performance(self, execution_time_ms: float) -> float:
        """실행 시간 기반 성능 점수 (0-100)."""
        if execution_time_ms <= 1000:
            return 100.0
        elif execution_time_ms <= 3000:
            return 85.0
        elif execution_time_ms <= 5000:
            return 70.0
        elif execution_time_ms <= 10000:
            return 50.0
        else:
            return max(10.0, 100.0 - (execution_time_ms / 200))

    # ── 출력 유효성 ───────────────────────────────────────────

    def _validate_output(self, result: Any) -> bool:
        """출력이 표준 형식(dict 또는 non-empty str)인지 검증."""
        if isinstance(result, dict):
            return True
        if isinstance(result, str) and len(result.strip()) > 0:
            return True
        return False

    # ── 실행 이력 관리 ────────────────────────────────────────

    def _record_execution(
        self,
        skill_name: str,
        success: bool,
        time_ms: float,
        error: str,
        quality_score: float,
    ) -> None:
        """실행 결과를 이력에 기록."""
        if skill_name not in self._records:
            self._records[skill_name] = SkillExecutionRecord(skill_name=skill_name)

        rec = self._records[skill_name]
        rec.total_runs += 1
        rec.total_time_ms += time_ms
        rec.quality_scores.append(quality_score)

        if success:
            rec.success_count += 1
            rec.consecutive_failures = 0
        else:
            rec.failure_count += 1
            rec.consecutive_failures += 1
            rec.last_error = error

    def get_record(self, skill_name: str) -> Optional[SkillExecutionRecord]:
        """스킬의 실행 이력 반환."""
        return self._records.get(skill_name)

    def needs_healing(self, skill_name: str) -> bool:
        """자가 치유가 필요한 상태인지 판단."""
        rec = self._records.get(skill_name)
        if not rec:
            return False
        return rec.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES

    def needs_rollback(self, skill_name: str) -> bool:
        """롤백이 필요한 상태인지 판단."""
        rec = self._records.get(skill_name)
        if not rec:
            return False
        return rec.avg_quality < self.QUALITY_THRESHOLD

    def get_dashboard_stats(self) -> dict:
        """전체 스킬 품질 대시보드 데이터."""
        stats = {}
        for name, rec in self._records.items():
            stats[name] = {
                "total_runs": rec.total_runs,
                "error_rate": f"{rec.error_rate:.1%}",
                "avg_quality": f"{rec.avg_quality:.1f}",
                "avg_time_ms": f"{rec.avg_time_ms:.0f}",
                "consecutive_failures": rec.consecutive_failures,
                "status": "critical" if rec.consecutive_failures >= 3
                          else "warning" if rec.error_rate > 0.3
                          else "healthy",
            }
        return stats

    # ── 영속화 ────────────────────────────────────────────────

    def _save_report(self, report: QualityReport) -> None:
        """품질 보고서를 로그 파일에 추가."""
        try:
            QUALITY_LOG_PATH.parent.mkdir(exist_ok=True)
            history = []
            if QUALITY_LOG_PATH.exists():
                try:
                    history = json.loads(QUALITY_LOG_PATH.read_text(encoding="utf-8"))
                except Exception:
                    history = []

            history.append(report.to_dict())
            # 최근 500건만 보관
            if len(history) > 500:
                history = history[-500:]

            QUALITY_LOG_PATH.write_text(
                json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"품질 로그 저장 실패: {e}")

    def _load_history(self) -> None:
        """이전 품질 로그에서 이력 복원."""
        if not QUALITY_LOG_PATH.exists():
            return
        try:
            history = json.loads(QUALITY_LOG_PATH.read_text(encoding="utf-8"))
            for entry in history:
                name = entry.get("skill_name", "")
                if not name:
                    continue
                if name not in self._records:
                    self._records[name] = SkillExecutionRecord(skill_name=name)
                rec = self._records[name]
                rec.total_runs += 1
                if entry.get("execution_success"):
                    rec.success_count += 1
                else:
                    rec.failure_count += 1
                rec.total_time_ms += entry.get("execution_time_ms", 0)
                rec.quality_scores.append(entry.get("total_score", 50))
        except Exception as e:
            logger.warning(f"품질 이력 복원 실패: {e}")


# ── 싱글톤 ────────────────────────────────────────────────────

_evaluator_instance: SkillQualityEvaluator | None = None


def get_quality_evaluator() -> SkillQualityEvaluator:
    global _evaluator_instance
    if _evaluator_instance is None:
        _evaluator_instance = SkillQualityEvaluator()
    return _evaluator_instance
