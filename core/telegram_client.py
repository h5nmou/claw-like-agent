import os
import re
import json
import time
import hashlib
import logging
import httpx
import asyncio
from typing import Optional


# 짧은 시간 내 동일 메시지 중복 dashboard broadcast 방지용 (최대 32건, TTL 10초)
_RECENT_BROADCAST: list[tuple[str, float]] = []
_DEDUP_TTL = 10.0


def _is_duplicate_broadcast(text: str) -> bool:
    """같은 텍스트가 최근 _DEDUP_TTL초 내에 broadcast되었으면 True."""
    now = time.time()
    h = hashlib.sha1((text or "").encode("utf-8")).hexdigest()
    # 만료된 항목 제거
    _RECENT_BROADCAST[:] = [(k, t) for (k, t) in _RECENT_BROADCAST if now - t < _DEDUP_TTL]
    for k, _ in _RECENT_BROADCAST:
        if k == h:
            return True
    _RECENT_BROADCAST.append((h, now))
    if len(_RECENT_BROADCAST) > 32:
        _RECENT_BROADCAST.pop(0)
    return False


def _strip_html_tags(text: str) -> str:
    """텔레그램 HTML 태그(<b>, <i>, <code>, <a>, <br> 등)를 제거해 평문으로 변환."""
    if not text:
        return ""
    # <br> 계열은 줄바꿈으로
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # <a href="...">라벨</a> → "라벨"만 유지
    text = re.sub(r"<a\s[^>]*>(.*?)</a>", r"\1", text, flags=re.IGNORECASE | re.DOTALL)
    # 나머지 태그 제거
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)
    return text

logger = logging.getLogger(__name__)

class TelegramClient:
    def __init__(self, token: Optional[str] = None, user_id: Optional[str] = None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.user_id = user_id or os.getenv("TELEGRAM_USER_ID")
        self.base_url = f"https://api.telegram.org/bot{self.token}" if self.token else None
        # 운영 플래그: .env에 TELEGRAM_ENABLED=false 로 설정하면 전송·폴링 모두 중단
        self.enabled = os.getenv("TELEGRAM_ENABLED", "true").strip().lower() != "false"
        self._is_polling = False
        self._last_update_id = 0

    async def send_message(self, text: str, user_id: Optional[str] = None, reply_markup: Optional[dict] = None, parse_mode: Optional[str] = None) -> dict:
        """텔레그램 메시지 전송 (버튼 포함 가능).

        TELEGRAM_ENABLED 플래그와 무관하게 대시보드 채팅창에도 동일 메시지를 푸시한다.
        (enabled=false 면 텔레그램 전송은 생략되고 대시보드로만 간다.)

        Args:
            text: 메시지 본문
            user_id: 수신자 ID (생략 시 기본 user_id)
            reply_markup: 인라인 키보드 등
            parse_mode: "HTML" 또는 "MarkdownV2" (생략 시 plain text)
        """
        # ── 대시보드 브로드캐스트 (모든 호출에서 단일 관문) ──
        try:
            from core.engine import broadcaster
            buttons: list = []
            if reply_markup and isinstance(reply_markup, dict):
                for row in reply_markup.get("inline_keyboard", []) or []:
                    for btn in row or []:
                        label = (btn.get("text") if isinstance(btn, dict) else None) \
                                or (btn.get("callback_data") if isinstance(btn, dict) else None)
                        if label:
                            buttons.append(str(label))
            # 텔레그램 HTML 태그(<b>, <i> 등)는 대시보드에 평문으로 노출되므로 제거
            _dashboard_text = _strip_html_tags(text) if (parse_mode or "").upper() == "HTML" else text
            # 짧은 시간 내(10초) 같은 메시지가 이미 broadcast되었다면 중복 카드 방지
            _dedup_key = _dashboard_text + "||" + json.dumps(buttons, ensure_ascii=False, sort_keys=True)
            if _is_duplicate_broadcast(_dedup_key):
                logger.info("Dashboard broadcast deduplicated (동일 메시지 10초 내 중복)")
            else:
                payload = {"message": _dashboard_text, "buttons": buttons}
                await broadcaster.emit(
                    "dashboard_prompt",
                    json.dumps(payload, ensure_ascii=False),
                    "대시보드 프롬프트",
                )
        except Exception as e:
            logger.debug(f"Dashboard broadcast skipped: {e}")

        # ── 텔레그램 전송 ──
        if not self.enabled:
            return {"status": "skipped", "reason": "TELEGRAM_ENABLED=false"}

        if not self.token:
            return {"error": "TELEGRAM_BOT_TOKEN is not set"}

        target_vuid = user_id or self.user_id
        if not target_vuid:
            return {"error": "Target user ID is missing"}

        url = f"{self.base_url}/sendMessage"
        payload = {"chat_id": target_vuid, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(url, json=payload, timeout=10.0)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                logger.error(f"Telegram send error: {e}")
                return {"error": str(e)}

    async def start_polling(self, message_callback):
        """백그라운드에서 텔레그램 메시지 수신 (Long Polling)."""
        if not self.enabled:
            logger.info("Telegram disabled via TELEGRAM_ENABLED=false. Polling skipped.")
            return
        if not self.token:
            logger.warning("Telegram token not set. Polling disabled.")
            return

        self._is_polling = True
        logger.info("Starting Telegram Bot Polling...")

        async with httpx.AsyncClient(timeout=30.0) as client:
            while self._is_polling:
                url = f"{self.base_url}/getUpdates"
                params = {"offset": self._last_update_id + 1, "timeout": 20}
                
                try:
                    resp = await client.get(url, params=params)
                    data = resp.json()

                    if data.get("ok"):
                        for update in data.get("result", []):
                            self._last_update_id = update["update_id"]
                            
                            if "message" in update and "text" in update["message"]:
                                text = update["message"]["text"]
                                chat_id = str(update["message"]["chat"]["id"])
                                
                                # 지정된 사장님(user_id)의 메시지만 처리하여 보안 유지
                                if not self.user_id or chat_id == self.user_id:
                                    logger.info(f"Received Telegram message: {text}")
                                    # 콜백 함수(엔진 루프) 트리거
                                    asyncio.create_task(message_callback(text, chat_id))
                                else:
                                    logger.warning(f"Unauthorized Telegram message from {chat_id}")
                                    
                            # 인라인 버튼(콜백 쿼리) 클릭 이벤트 처리
                            elif "callback_query" in update:
                                cb = update["callback_query"]
                                data_str = cb.get("data")
                                chat_id = str(cb["message"]["chat"]["id"])
                                cb_id = cb["id"]
                                
                                if not self.user_id or chat_id == self.user_id:
                                    logger.info(f"Received Telegram button click: {data_str}")
                                    # 버튼 클릭 데이터도 동일하게 엔진 루프로 전달
                                    asyncio.create_task(message_callback(data_str, chat_id))
                                else:
                                    logger.warning(f"Unauthorized Telegram callback from {chat_id}")
                                
                                # 텔레그램 서버에 확인 응답 (버튼 스피너 해제)
                                asyncio.create_task(self.answer_callback_query(cb_id))

                except Exception as e:
                    logger.error(f"Telegram polling error: {e}")
                    await asyncio.sleep(5)  # 에러 시 대기

    async def answer_callback_query(self, callback_query_id: str):
        """버튼 클릭에 응답하여 로딩 상태를 해제."""
        url = f"{self.base_url}/answerCallbackQuery"
        async with httpx.AsyncClient() as client:
            try:
                await client.post(url, json={"callback_query_id": callback_query_id})
            except Exception as e:
                logger.error(f"Failed to answer callback query: {e}")

    def stop_polling(self):
        self._is_polling = False

# 싱글톤 객체 생성 (엔진에서 사용)
telegram_client = TelegramClient()
