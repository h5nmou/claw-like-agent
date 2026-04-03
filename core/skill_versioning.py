"""
skill_versioning.py — Enterprise Semantic Versioning

스킬별 버전 이력 관리 + 품질 게이트 + 자동 롤백:
  - versions/v1.0.0.py 형태로 저장
  - 자동 patch/minor/major 버전 증분
  - 품질 미달 시 자동 롤백 트리거
  - 특정 버전 수동 롤백 지원
  - SKILL.md front-matter 동기화 (OpenClaw 규격)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skill_versioning")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GENERATED_SKILLS_DIR = PROJECT_ROOT / "generated_skills"


class SkillVersionManager:
    """
    엔터프라이즈 시맨틱 버전 관리.

    폴더 구조 (OpenClaw 규격):
        generated_skills/
        ├── {skill_name}.py        ← flat 파일 (하위 호환)
        └── {skill_name}/
            ├── SKILL.md           ← YAML 프런트매터 + 실행 지침
            ├── skill.py           ← 현재 활성 버전
            └── versions/
                ├── v1.0.0.py
                ├── v1.1.0.py
                └── v1.1.0.meta.json  ← 버전별 품질/보안 메타데이터
    """

    # 품질 게이트 임계값
    QUALITY_GATE_THRESHOLD = 40.0  # 이 점수 미만이면 자동 롤백

    # ── 버전 저장 ────────────────────────────────────────────

    def save_version(
        self,
        skill_name: str,
        code: str,
        bump: str = "patch",
        description: str = "",
        env_required: Optional[list[str]] = None,
        tags: Optional[list[str]] = None,
        allowlist: Optional[list[str]] = None,
        risk_level: str = "low",
        quality_score: Optional[float] = None,
        security_score: Optional[int] = None,
        review_score: Optional[int] = None,
    ) -> str:
        """
        스킬 코드를 새 버전으로 저장하고 skill.py를 갱신.

        Args:
            skill_name: 스킬 이름
            code: 저장할 Python 코드
            bump: 버전 증분 방식 (major/minor/patch)
            description: 스킬 설명
            env_required: 필요한 환경변수 목록
            tags: 태그 목록
            allowlist: 허용된 import 목록
            risk_level: 위험도
            quality_score: 품질 점수 (0-100)
            security_score: 보안 점수 (0-100)
            review_score: 피어 리뷰 점수 (0-100)

        Returns:
            새 버전 문자열 (예: "v1.1.0")
        """
        skill_dir = GENERATED_SKILLS_DIR / skill_name
        versions_dir = skill_dir / "versions"
        skill_dir.mkdir(parents=True, exist_ok=True)
        versions_dir.mkdir(exist_ok=True)

        current = self._latest_version(versions_dir)
        new_version = self._bump_version(current, bump)

        # 버전 파일 저장
        version_file = versions_dir / f"{new_version}.py"
        version_file.write_text(code, encoding="utf-8")

        # 버전 메타데이터 저장
        meta = {
            "version": new_version,
            "created_at": datetime.now().isoformat(),
            "description": description,
            "risk_level": risk_level,
            "quality_score": quality_score,
            "security_score": security_score,
            "review_score": review_score,
            "bump_type": bump,
        }
        meta_file = versions_dir / f"{new_version}.meta.json"
        meta_file.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # skill.py (active) 갱신
        (skill_dir / "skill.py").write_text(code, encoding="utf-8")

        # SKILL.md 생성 또는 갱신
        self._write_skill_md(
            skill_dir,
            skill_name=skill_name,
            version=new_version,
            description=description,
            env_required=env_required or [],
            tags=tags or [],
            allowlist=allowlist or [],
            risk_level=risk_level,
            quality_score=quality_score,
            security_score=security_score,
        )

        logger.info(f"버전 저장: {skill_name} {new_version} → {version_file}")
        return new_version

    # ── 품질 게이트 자동 롤백 ────────────────────────────────

    def quality_gate_check(
        self,
        skill_name: str,
        quality_score: float,
    ) -> dict:
        """
        품질 점수가 임계값 미만이면 이전 안정 버전으로 자동 롤백.

        Args:
            skill_name: 스킬 이름
            quality_score: 현재 버전의 품질 점수

        Returns:
            {"action": "pass" | "rollback", "version": str, "message": str}
        """
        if quality_score >= self.QUALITY_GATE_THRESHOLD:
            return {
                "action": "pass",
                "version": self.get_current_version(skill_name) or "unknown",
                "message": f"품질 게이트 통과: {quality_score:.1f} >= {self.QUALITY_GATE_THRESHOLD}",
            }

        # 이전 안정 버전 탐색
        stable_version = self._find_stable_version(skill_name)
        if not stable_version:
            return {
                "action": "pass",  # 롤백 대상 없음
                "version": self.get_current_version(skill_name) or "unknown",
                "message": f"품질 미달({quality_score:.1f})이지만 롤백 대상 없음",
            }

        # 자동 롤백 실행
        rollback_result = self.rollback(skill_name, stable_version)
        if rollback_result["success"]:
            logger.warning(
                f"품질 게이트 자동 롤백: {skill_name} → {stable_version} "
                f"(점수: {quality_score:.1f} < {self.QUALITY_GATE_THRESHOLD})"
            )
            return {
                "action": "rollback",
                "version": stable_version,
                "message": (
                    f"⚠️ 품질 게이트 자동 롤백: {skill_name}\n"
                    f"현재 점수: {quality_score:.1f} < 임계값: {self.QUALITY_GATE_THRESHOLD}\n"
                    f"안정 버전 {stable_version}으로 복원 완료"
                ),
            }

        return {
            "action": "pass",
            "version": self.get_current_version(skill_name) or "unknown",
            "message": f"품질 미달이나 롤백 실패: {rollback_result.get('message')}",
        }

    def _find_stable_version(self, skill_name: str) -> Optional[str]:
        """품질 점수가 임계값 이상인 가장 최근 버전을 찾음."""
        versions_dir = GENERATED_SKILLS_DIR / skill_name / "versions"
        if not versions_dir.exists():
            return None

        versions = sorted(f.stem for f in versions_dir.glob("v*.py"))
        # 최신 버전부터 역순으로 검색 (현재 버전 제외)
        for ver in reversed(versions[:-1]) if len(versions) > 1 else []:
            meta_file = versions_dir / f"{ver}.meta.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    q_score = meta.get("quality_score")
                    if q_score is not None and q_score >= self.QUALITY_GATE_THRESHOLD:
                        return ver
                except Exception:
                    pass
            else:
                # 메타데이터 없는 이전 버전은 안정적이라고 가정
                return ver

        return None

    # ── 롤백 ─────────────────────────────────────────────────

    def rollback(self, skill_name: str, version: str) -> dict:
        """특정 버전으로 롤백."""
        versions_dir = GENERATED_SKILLS_DIR / skill_name / "versions"
        version_file = versions_dir / f"{version}.py"

        if not version_file.exists():
            available = self.list_versions(skill_name)
            return {
                "success": False,
                "version": version,
                "message": f"버전 '{version}'을 찾을 수 없습니다. 사용 가능: {available}",
            }

        skill_dir = GENERATED_SKILLS_DIR / skill_name
        code = version_file.read_text(encoding="utf-8")
        (skill_dir / "skill.py").write_text(code, encoding="utf-8")

        # flat 파일도 동기화
        flat_file = GENERATED_SKILLS_DIR / f"{skill_name}.py"
        if flat_file.exists():
            flat_file.write_text(code, encoding="utf-8")

        # executor 재등록
        try:
            from core.executor import _TOOL_REGISTRY
            import importlib.util
            if flat_file.exists():
                spec = importlib.util.spec_from_file_location(skill_name, flat_file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if skill_name not in _TOOL_REGISTRY:
                    func = getattr(module, skill_name, None)
                    if func:
                        _TOOL_REGISTRY[skill_name] = func
        except Exception as e:
            logger.warning(f"롤백 후 재등록 실패: {e}")

        logger.info(f"롤백 완료: {skill_name} → {version}")
        return {
            "success": True,
            "version": version,
            "message": f"✅ {skill_name}을 {version}으로 롤백 완료",
        }

    # ── 버전 조회 ─────────────────────────────────────────────

    def list_versions(self, skill_name: str) -> list[str]:
        """저장된 버전 목록 반환 (정렬됨)."""
        versions_dir = GENERATED_SKILLS_DIR / skill_name / "versions"
        if not versions_dir.exists():
            return []
        return sorted(f.stem for f in versions_dir.glob("v*.py"))

    def get_current_version(self, skill_name: str) -> Optional[str]:
        """현재 SKILL.md에 기록된 버전 반환."""
        skill_md = GENERATED_SKILLS_DIR / skill_name / "SKILL.md"
        if not skill_md.exists():
            return None
        content = skill_md.read_text(encoding="utf-8")
        match = re.search(r"^version:\s*(.+)$", content, re.MULTILINE)
        return match.group(1).strip() if match else None

    def get_version_history(self, skill_name: str) -> list[dict]:
        """버전별 메타데이터 이력 반환."""
        versions_dir = GENERATED_SKILLS_DIR / skill_name / "versions"
        if not versions_dir.exists():
            return []

        history = []
        for ver in sorted(f.stem for f in versions_dir.glob("v*.py")):
            entry = {"version": ver}
            meta_file = versions_dir / f"{ver}.meta.json"
            if meta_file.exists():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    entry.update(meta)
                except Exception:
                    pass
            history.append(entry)
        return history

    # ── 내부 헬퍼 ─────────────────────────────────────────────

    def _latest_version(self, versions_dir: Path) -> str:
        if not versions_dir.exists():
            return "v0.0.0"
        versions = sorted(f.stem for f in versions_dir.glob("v*.py"))
        return versions[-1] if versions else "v0.0.0"

    def _bump_version(self, current: str, bump: str) -> str:
        match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", current)
        if not match:
            return "v1.0.0"
        major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if bump == "major":
            return f"v{major + 1}.0.0"
        elif bump == "minor":
            return f"v{major}.{minor + 1}.0"
        else:
            return f"v{major}.{minor}.{patch + 1}"

    def _write_skill_md(
        self,
        skill_dir: Path,
        skill_name: str,
        version: str,
        description: str,
        env_required: list[str],
        tags: list[str],
        allowlist: list[str],
        risk_level: str,
        quality_score: Optional[float] = None,
        security_score: Optional[int] = None,
    ) -> None:
        """SKILL.md 파일 생성/갱신 (OpenClaw 규격)."""
        env_str = "[" + ", ".join(env_required) + "]" if env_required else "[]"
        tags_str = "[" + ", ".join(tags) + "]" if tags else "[]"
        allow_str = "[" + ", ".join(allowlist) + "]" if allowlist else "[]"
        quality_str = f"{quality_score:.1f}" if quality_score is not None else "N/A"
        security_str = str(security_score) if security_score is not None else "N/A"

        content = f"""---
name: {skill_name}
version: {version}
description: {description or skill_name + " 스킬"}
tags: {tags_str}
env_required: {env_str}
allowlist: {allow_str}
risk_level: {risk_level}
quality_score: {quality_str}
security_score: {security_str}
created_at: {datetime.now().strftime("%Y-%m-%d")}
author: enterprise-skill-factory-2.0
---

## 실행 지침

이 스킬은 Enterprise Skill Factory 2.0에 의해 자동 생성되었습니다.

- **환경변수 필요**: {env_str}
- **위험도**: {risk_level.upper()}
- **품질 점수**: {quality_str}/100
- **보안 점수**: {security_str}/100
- **버전**: {version}

## 실행 스크립트

`skill.py` 파일을 참고하세요.

## 버전 이력

자세한 버전 이력은 `versions/` 폴더를 참고하세요.
각 버전별 메타데이터는 `v*.meta.json` 파일에 기록됩니다.
"""
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


# ── 싱글톤 ────────────────────────────────────────────────────

_versioner_instance: SkillVersionManager | None = None


def get_version_manager() -> SkillVersionManager:
    global _versioner_instance
    if _versioner_instance is None:
        _versioner_instance = SkillVersionManager()
    return _versioner_instance
