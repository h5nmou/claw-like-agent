"""
onboarding_manager.py — 최초 1회 온보딩 매니저

Agent 최초 구동 시:
1. RSA Keypair 생성
2. 통신사 앱(Mock)을 통해 사장님 승인 유도
3. 위임장(.agent_cert) 수신 및 저장
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import serialization, hashes

logger = logging.getLogger("onboarding")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class OnboardingManager:
    """Agent 최초 실행 시 신뢰 관계를 형성하는 매니저."""

    CERT_PATH = PROJECT_ROOT / ".agent_cert"
    PRIVATE_KEY_PATH = PROJECT_ROOT / ".agent_private.pem"
    PUBLIC_KEY_PATH = PROJECT_ROOT / ".agent_public.pem"
    TELCO_PUBLIC_KEY_PATH = PROJECT_ROOT / ".telco_public.pem"

    def __init__(self) -> None:
        self._private_key = None
        self._public_key = None

    def needs_onboarding(self) -> bool:
        """온보딩이 필요한지 확인."""
        return not self.CERT_PATH.exists()

    def generate_keypair(self) -> str:
        """RSA Keypair 생성. Public Key PEM 문자열 반환."""
        logger.info("RSA Keypair 생성 중...")

        self._private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        self._public_key = self._private_key.public_key()

        # Private Key 저장
        private_pem = self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self.PRIVATE_KEY_PATH.write_bytes(private_pem)
        logger.info(f"Private Key 저장: {self.PRIVATE_KEY_PATH}")

        # Public Key PEM
        public_pem = self._public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.PUBLIC_KEY_PATH.write_bytes(public_pem)
        logger.info(f"Public Key 저장: {self.PUBLIC_KEY_PATH}")

        return public_pem.decode("utf-8")

    def save_certificate(self, cert_data: dict, telco_public_key_pem: str) -> None:
        """위임장과 Telco Public Key를 저장."""
        self.CERT_PATH.write_text(
            json.dumps(cert_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"위임장 저장: {self.CERT_PATH}")

        self.TELCO_PUBLIC_KEY_PATH.write_text(
            telco_public_key_pem,
            encoding="utf-8",
        )
        logger.info(f"Telco Public Key 저장: {self.TELCO_PUBLIC_KEY_PATH}")

    def load_certificate(self) -> dict:
        """저장된 위임장을 로드."""
        return json.loads(self.CERT_PATH.read_text(encoding="utf-8"))

    def get_agent_id(self) -> str | None:
        """위임장에서 agent_id를 추출."""
        if not self.CERT_PATH.exists():
            return None
        cert = self.load_certificate()
        return cert.get("agent_id")

    def get_public_key_pem(self) -> str | None:
        """저장된 Public Key PEM을 반환."""
        if not self.PUBLIC_KEY_PATH.exists():
            return None
        return self.PUBLIC_KEY_PATH.read_text(encoding="utf-8")

    def reset(self) -> None:
        """온보딩 초기화 (디버깅/테스트용)."""
        for path in [self.CERT_PATH, self.PRIVATE_KEY_PATH, self.PUBLIC_KEY_PATH, self.TELCO_PUBLIC_KEY_PATH]:
            if path.exists():
                path.unlink()
                logger.info(f"삭제: {path}")
