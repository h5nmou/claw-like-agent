"""
자동 생성 스킬: send_email_via_smtp
생성일: 2026-04-03T11:25:36.250821
전략: Gmail SMTP
"""

from __future__ import annotations
import os
import smtplib
from core.executor import tool

# 필요한 패키지: None (기본 라이브러리 사용)

@tool
async def send_email_via_smtp(to_email: str, subject: str, body: str) -> dict:
    """
    Gmail SMTP를 통해 이메일을 전송하는 비동기 함수입니다.

    Args:
        to_email (str): 수신자 이메일 주소입니다.
        subject (str): 이메일 제목입니다.
        body (str): 이메일 본문입니다.

    Returns:
        dict: 이메일 전송 결과를 포함하는 사전입니다. 에러가 발생할 경우 에러 정보를 포함합니다.
    """
    smtp_server = "smtp.gmail.com"
    smtp_port = 465
    
    smtp_user = "h5nmou@gmail.com"
    smtp_password = os.getenv("SMTP_PASSWORD")

    if not smtp_password:
        return {"error": "환경변수 오류", "detail": "'SMTP_PASSWORD' 환경변수가 설정되지 않았습니다."}

    try:
        server = smtplib.SMTP_SSL(smtp_server, smtp_port)
        server.login(smtp_user, smtp_password)
        
        message = f"Subject: {subject}\n\n{body}"
        
        server.sendmail(smtp_user, to_email, message)
        server.quit()
        
        return {"메시지": "이메일 전송이 완료되었습니다.", "수신자": to_email}
    except Exception as e:
        return {"error": "이메일 전송 오류", "detail": str(e)}