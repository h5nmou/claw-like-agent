"""
skill_registry.py — Enterprise Skill Library Manager

점진적 로딩 (Progressive Disclosure) 아키텍처:
  1. 시작 시 메타데이터(이름, 설명)만 로드 → 가벼운 초기화
  2. 실제 호출 시에만 전체 코드를 활성화 (Lazy Activation)
  3. 수천 개의 스킬도 메모리 효율적으로 관리 가능

하이브리드 저장:
  - 정적 스킬: 로컬 generated_skills/ 에 저장
  - 동적 API 레퍼런스: 실행 시점에 외부에서 Fetch
"""

from __future__ import annotations

import importlib.util
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skill_registry")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GENERATED_SKILLS_DIR = PROJECT_ROOT / "generated_skills"
SKILL_INDEX_PATH = GENERATED_SKILLS_DIR / "skill_index.json"


class SkillRegistry:
    """
    엔터프라이즈 스킬 라이브러리 매니저.

    Progressive Disclosure:
      - load_metadata(): 인덱스만 로드 (이름 + 설명)
      - activate(skill_name): 실제 코드를 import하여 executor에 등록
      - load_all(): 전체 활성화 (소규모 라이브러리용)
    """

    def __init__(self) -> None:
        self._index: dict = {"skills": [], "last_updated": "", "total_count": 0}
        self._loaded_skills: list[str] = []       # 실제 코드가 활성화된 스킬
        self._metadata_loaded: bool = False        # 메타데이터 로드 여부
        self._metadata_cache: dict[str, dict] = {} # name → metadata dict

    # ── Progressive Disclosure: 메타데이터 로드 ──────────────

    def load_metadata(self) -> int:
        """
        skill_index.json에서 메타데이터만 로드 (코드 import 없음).
        수천 개의 스킬이 있어도 즉시 완료.

        Returns:
            로드된 메타데이터 수
        """
        GENERATED_SKILLS_DIR.mkdir(exist_ok=True)
        self._ensure_index()

        skills = self._index.get("skills", [])
        self._metadata_cache = {s["name"]: s for s in skills}
        self._metadata_loaded = True

        logger.info(f"메타데이터 로드: {len(skills)}개 스킬 인덱싱 완료 (코드 미로드)")
        return len(skills)

    def get_metadata(self, skill_name: str) -> Optional[dict]:
        """특정 스킬의 메타데이터 반환 (코드 로드 없이)."""
        if not self._metadata_loaded:
            self.load_metadata()
        return self._metadata_cache.get(skill_name)

    def get_all_metadata(self) -> list[dict]:
        """전체 스킬 메타데이터 반환 (Progressive Disclosure용)."""
        if not self._metadata_loaded:
            self.load_metadata()
        return list(self._metadata_cache.values())

    # ── Progressive Disclosure: 온디맨드 활성화 ──────────────

    def activate(self, skill_name: str) -> bool:
        """
        특정 스킬을 온디맨드로 활성화 (코드 import + executor 등록).
        이미 활성화된 스킬은 즉시 True 반환.

        Args:
            skill_name: 활성화할 스킬 이름

        Returns:
            활성화 성공 여부
        """
        if skill_name in self._loaded_skills:
            return True

        from core.executor import _TOOL_REGISTRY

        # 이미 executor에 등록되어 있으면 로드 목록에만 추가
        if skill_name in _TOOL_REGISTRY:
            self._loaded_skills.append(skill_name)
            return True

        skill_file = GENERATED_SKILLS_DIR / f"{skill_name}.py"
        if not skill_file.exists():
            logger.warning(f"스킬 파일 없음: {skill_file}")
            return False

        try:
            spec = importlib.util.spec_from_file_location(
                f"generated_skills.{skill_name}", skill_file
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            if skill_name in _TOOL_REGISTRY:
                self._loaded_skills.append(skill_name)
                logger.info(f"✅ 스킬 온디맨드 활성화: {skill_name}")
                return True

            func = getattr(module, skill_name, None)
            if func and callable(func):
                _TOOL_REGISTRY[skill_name] = func
                self._loaded_skills.append(skill_name)
                logger.info(f"✅ 스킬 수동 활성화: {skill_name}")
                return True

            logger.warning(f"⚠️ 스킬 함수를 찾을 수 없음: {skill_name}")
            return False

        except Exception as e:
            logger.error(f"❌ 스킬 활성화 실패 {skill_name}: {e}")
            return False

    def is_activated(self, skill_name: str) -> bool:
        """스킬이 활성화(코드 로드) 상태인지 확인."""
        return skill_name in self._loaded_skills

    # ── 전체 로드 (하위 호환) ─────────────────────────────────

    def load_all(self) -> int:
        """
        generated_skills/ 내 모든 .py 파일을 동적 import하여 executor 등록.
        소규모 라이브러리(<100개)에 적합. 대규모는 load_metadata() + activate() 권장.

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

                registered = [k for k in _TOOL_REGISTRY if k == skill_name]
                if registered:
                    self._loaded_skills.append(skill_name)
                    loaded_count += 1
                    logger.info(f"  ✅ 스킬 로드: {skill_name}")
                else:
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

        # 메타데이터도 동기화
        self._metadata_cache = {s["name"]: s for s in self._index.get("skills", [])}
        self._metadata_loaded = True

        logger.info(f"스킬 라이브러리 로드 완료: {loaded_count}/{len(skill_files)}개")
        return loaded_count

    # ── 스킬 조회 ─────────────────────────────────────────────

    def get_all_skills(self) -> list[dict]:
        """등록된 모든 스킬 메타데이터 반환."""
        self._ensure_index()
        return self._index.get("skills", [])

    def find_matching_skill(self, query: str) -> dict | None:
        """
        키워드 기반 유사 스킬 캐시 조회.
        Progressive Disclosure: 메타데이터에서만 검색 (코드 로드 불필요).
        """
        if not self._metadata_loaded:
            self.load_metadata()

        query_lower = query.lower()
        skills = list(self._metadata_cache.values())

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

    # ── 스킬 삭제 ─────────────────────────────────────────────

    def delete_skill(self, skill_name: str) -> dict:
        """스킬을 완전히 삭제."""
        from core.executor import _TOOL_REGISTRY

        skill_file = GENERATED_SKILLS_DIR / f"{skill_name}.py"
        results = []

        if skill_file.exists():
            skill_file.unlink()
            results.append(f"✅ 파일 삭제: generated_skills/{skill_name}.py")
        else:
            results.append(f"⚠️ 파일 없음: generated_skills/{skill_name}.py")

        if skill_name in _TOOL_REGISTRY:
            del _TOOL_REGISTRY[skill_name]
            results.append(f"✅ Tool 레지스트리에서 제거: {skill_name}")
        else:
            results.append(f"⚠️ 레지스트리에 없음: {skill_name}")

        if skill_name in self._loaded_skills:
            self._loaded_skills.remove(skill_name)

        if skill_name in self._metadata_cache:
            del self._metadata_cache[skill_name]

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

    # ── 사용 통계 ─────────────────────────────────────────────

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
        total = self._index.get("total_count", 0)
        activated = len(self._loaded_skills)
        return {
            "total_skills": total,
            "loaded_count": activated,
            "metadata_only": total - activated,
            "last_updated": self._index.get("last_updated", "없음"),
            "skill_names": [s["name"] for s in self._index.get("skills", [])],
            "activated_skills": list(self._loaded_skills),
            "progressive_loading": total > activated,
        }

    # ── 내부 헬퍼 ─────────────────────────────────────────────

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
