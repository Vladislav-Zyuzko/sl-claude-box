"""Рассылка в Telegram напрямую через Bot API.

Второй поллер getUpdates запускать нельзя (Telegram разрешает один на токен), а
sendMessage тем же токеном может звать кто угодно — поэтому дайджест шлёт сам и
не трогает цикл бота. Клиент telegram-бота сюда не тащим: нужен один метод.
"""
import asyncio
import logging
from typing import Dict, List

import httpx

from .render import split

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"


class Telegram:
    def __init__(self, token: str, timeout: float = 20.0):
        self._token = token
        self._timeout = timeout

    def _url(self, method: str) -> str:
        return f"{API}/bot{self._token}/{method}"

    async def send(self, chat_id: int, text_html: str) -> None:
        """Одно сообщение (при необходимости — несколькими частями)."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for chunk in split(text_html):
                r = await client.post(
                    self._url("sendMessage"),
                    json={
                        "chat_id": chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                if r.status_code != 200:
                    # текст ответа Telegram полезен (какой тег не понравился),
                    # а токен в него не попадает — он только в URL
                    raise RuntimeError(f"sendMessage {r.status_code}: {r.text[:300]}")

    async def broadcast(self, recipients: List[int], text_html: str) -> Dict[str, int]:
        """Ошибка одного адресата не должна мешать остальным: шлём по очереди,
        каждому в своём try. Последовательно — адресатов единицы, а параллельный
        залп упёрся бы в лимиты Telegram."""
        sent, failed = 0, 0
        for chat_id in recipients:
            try:
                await self.send(chat_id, text_html)
                sent += 1
            except Exception as e:
                failed += 1
                logger.warning("не смог отправить дайджест %s: %s", chat_id, str(e)[:200])
            await asyncio.sleep(0.2)   # мягкий зазор против 429
        return {"sent": sent, "failed": failed}
