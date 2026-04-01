import os
import logging
import httpx
import asyncio
from typing import Optional

logger = logging.getLogger(__name__)

class TelegramClient:
    def __init__(self, token: Optional[str] = None, user_id: Optional[str] = None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.user_id = user_id or os.getenv("TELEGRAM_USER_ID")
        self.base_url = f"https://api.telegram.org/bot{self.token}" if self.token else None
        self._is_polling = False
        self._last_update_id = 0

    async def send_message(self, text: str, user_id: Optional[str] = None, reply_markup: Optional[dict] = None) -> dict:
        """텔레그램 메시지 전송 (버튼 포함 가능)."""
        if not self.token:
            return {"error": "TELEGRAM_BOT_TOKEN is not set"}
        
        target_vuid = user_id or self.user_id
        if not target_vuid:
            return {"error": "Target user ID is missing"}

        url = f"{self.base_url}/sendMessage"
        payload = {"chat_id": target_vuid, "text": text}
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
