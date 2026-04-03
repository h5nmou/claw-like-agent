"""
자동 생성 스킬: send_email_via_smtp
생성일: 2026-04-03T16:42:24.592444
전략: Gmail SMTP
"""

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
