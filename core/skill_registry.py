"""
skill_registry.py — 스킬 라이브러리 매니저

엔진 시작 시 generated_skills/ 내의 모든 스킬을 자동 로드하고,
skill_index.json 기반으로 캐싱 조회를 제공한다.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

logger = logging.getLogger("skill_registry")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GENERATED_SKILLS_DIR = PROJECT_ROOT / "generated_skills"
SKILL_INDEX_PATH = GENERATED_SKILLS_DIR / "skill_index.json"


class SkillRegistry:
    """
    생성된 스킬들의 라이브러리를 관리.
    엔진 시작 시 한 번 load_all()을 호출하면 이후 모든 생성 스킬이 활성화.
    """

    def __init__(self) -> None:
        self._index: dict = {"skills": [], "last_updated": "", "total_count": 0}
        self._loaded_skills: list[str] = []

    def load_all(self) -> int:
        """
        generated_skills/ 내 모든 .py 파일을 동적 import하여 executor 레지스트리에 등록.

        Returns:
            성공적으로 로드된 스킬 수
        """
        from core.executor import _TOOL_REGISTRY

        GENERATED_SKILLS_DIR.mkdir(exist_ok=True)
        self._ensure_index()

        loaded_count = 0
        skill_files = [
            p for p in sorted(GENERATED_SKILLS_DIR.glob("*.py"))
            if p.name != "__init__.py"
        ]

        if not skill_files:
            logger.info("generated_skills/: 로드할 스킬 없음.")
            return 0

        logger.info(f"스킬 라이브러리 로드 시작: {len(skill_files)}개 파일 발견")

        for skill_file in skill_files:
            skill_name = skill_file.stem
            try:
                spec = importlib.util.spec_from_file_location(
                    f"generated_skills.{skill_name}", skill_file
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # 해당 모듈에서 등록된 함수 확인
                registered = [k for k in _TOOL_REGISTRY if k == skill_name]
                if registered:
                    self._loaded_skills.append(skill_name)
                    loaded_count += 1
                    logger.info(f"  ✅ 스킬 로드: {skill_name}")
                else:
                    # 데코레이터 없는 경우 수동 시도
                    func = getattr(module, skill_name, None)
                    if func and callable(func):
                        _TOOL_REGISTRY[skill_name] = func
                        self._loaded_skills.append(skill_name)
                        loaded_count += 1
                        logger.info(f"  ✅ 스킬 수동 등록: {skill_name}")
                    else:
                        logger.warning(f"  ⚠️ 스킬 함수를 찾을 수 없음: {skill_name}")

            except Exception as e:
                logger.error(f"  ❌ 스킬 로드 실패 {skill_name}: {e}")

        logger.info(f"스킬 라이브러리 로드 완료: {loaded_count}/{len(skill_files)}개")
        return loaded_count

    def get_all_skills(self) -> list[dict]:
        """등록된 모든 스킬 메타데이터 반환."""
        self._ensure_index()
        return self._index.get("skills", [])

    def find_matching_skill(self, query: str) -> dict | None:
        """
        키워드 기반 유사 스킬 캐시 조회.
        완전 일치 우선, 이후 키워드 포함 조회.
        """
        self._ensure_index()
        query_lower = query.lower()
        skills = self._index.get("skills", [])

        # 1순위: 이름 완전 일치
        for skill in skills:
            if skill["name"].lower() == query_lower:
                return skill

        # 2순위: 이름에 키워드 포함
        for skill in skills:
            if query_lower in skill["name"].lower():
                return skill

        # 3순위: 설명에 키워드 포함
        keywords = query_lower.split()
        for skill in skills:
            desc = skill.get("description", "").lower()
            if any(kw in desc for kw in keywords if len(kw) > 2):
                return skill

        return None

    def delete_skill(self, skill_name: str) -> dict:
        """
        스킬을 완전히 삭제.
          1. generated_skills/{skill_name}.py 파일 삭제
          2. executor._TOOL_REGISTRY에서 제거
          3. skill_index.json 업데이트
          4. _loaded_skills 목록에서 제거

        Returns:
            {"success": bool, "message": str}
        """
        from core.executor import _TOOL_REGISTRY
        from datetime import datetime

        skill_file = GENERATED_SKILLS_DIR / f"{skill_name}.py"
        results = []

        # 1. 파일 삭제
        if skill_file.exists():
            skill_file.unlink()
            results.append(f"✅ 파일 삭제: generated_skills/{skill_name}.py")
        else:
            results.append(f"⚠️ 파일 없음: generated_skills/{skill_name}.py")

        # 2. executor 레지스트리에서 제거
        if skill_name in _TOOL_REGISTRY:
            del _TOOL_REGISTRY[skill_name]
            results.append(f"✅ Tool 레지스트리에서 제거: {skill_name}")
        else:
            results.append(f"⚠️ 레지스트리에 없음: {skill_name}")

        # 3. _loaded_skills 목록에서 제거
        if skill_name in self._loaded_skills:
            self._loaded_skills.remove(skill_name)

        # 4. 인덱스 업데이트
        self._ensure_index()
        before_count = len(self._index.get("skills", []))
        self._index["skills"] = [
            s for s in self._index.get("skills", [])
            if s["name"] != skill_name
        ]
        after_count = len(self._index["skills"])
        self._index["total_count"] = after_count
        self._index["last_updated"] = datetime.now().isoformat()
        self._save_index()

        if before_count > after_count:
            results.append(f"✅ 인덱스 업데이트 완료 (남은 스킬: {after_count}개)")
        else:
            results.append(f"⚠️ 인덱스에서 '{skill_name}' 항목을 찾지 못했습니다.")

        logger.info(f"스킬 삭제 완료: {skill_name}")
        return {
            "success": True,
            "skill_name": skill_name,
            "message": "\n".join(results),
        }

    def increment_use_count(self, skill_name: str) -> None:
        """스킬 사용 횟수 증가."""
        self._ensure_index()
        for skill in self._index.get("skills", []):
            if skill["name"] == skill_name:
                skill["use_count"] = skill.get("use_count", 0) + 1
                break
        self._save_index()

    def get_stats(self) -> dict:
        """스킬 라이브러리 통계."""
        self._ensure_index()
        return {
            "total_skills": self._index.get("total_count", 0),
            "loaded_count": len(self._loaded_skills),
            "last_updated": self._index.get("last_updated", "없음"),
            "skill_names": [s["name"] for s in self._index.get("skills", [])],
        }

    def _ensure_index(self) -> None:
        """skill_index.json 존재 확인 및 로드."""
        if SKILL_INDEX_PATH.exists():
            try:
                self._index = json.loads(SKILL_INDEX_PATH.read_text(encoding="utf-8"))
            except Exception:
                self._index = {"skills": [], "last_updated": "", "total_count": 0}
        else:
            self._index = {"skills": [], "last_updated": "", "total_count": 0}
            self._save_index()

    def _save_index(self) -> None:
        GENERATED_SKILLS_DIR.mkdir(exist_ok=True)
        SKILL_INDEX_PATH.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8"
        )


# ── 싱글톤 ────────────────────────────────────────────────

_registry_instance: SkillRegistry | None = None


def get_skill_registry() -> SkillRegistry:
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = SkillRegistry()
    return _registry_instance
