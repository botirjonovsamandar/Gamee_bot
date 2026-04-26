"""Periodic getMe / get_dialogs — реальные Telegram-клиенты регулярно
обращаются к Telegram API даже без действий пользователя (refresh профиля,
sync чатов). Отсутствие этих вызовов — bot signature.

Используется как fire-and-forget hook из BotWorker. Каждые 30-90 минут
для случайного (не каждого) аккаунта вызываем get_me() и опционально
get_dialogs(limit=5).
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from pathlib import Path

from telethon import TelegramClient

log = logging.getLogger(__name__)


class TelegramPoller:
    """Фоновый поллер getMe/get_dialogs для realism.

    Запускается в отдельном threading.Thread, работает пока stop() не вызван.
    """

    def __init__(self, sessions: dict[str, str], api_id: int, api_hash: str):
        """sessions: label -> path к session файлу."""
        self._sessions = dict(sessions)
        self._api_id = api_id
        self._api_hash = api_hash
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not self._sessions or self._api_id <= 0 or not self._api_hash:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"poller-{int(time.time()) & 0xffff:x}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None

    def _loop(self) -> None:
        # Первый запрос — через 5-15 минут после старта (не сразу)
        delay = random.uniform(300.0, 900.0)
        if self._stop.wait(delay):
            return
        while not self._stop.is_set():
            try:
                self._poll_random_account()
            except Exception:
                log.debug("polling step failed", exc_info=True)
            # Следующая итерация через 30-90 минут
            delay = random.uniform(1800.0, 5400.0)
            if self._stop.wait(delay):
                return

    def _poll_random_account(self) -> None:
        if not self._sessions:
            return
        label = random.choice(list(self._sessions.keys()))
        session_path = self._sessions.get(label)
        if not session_path or not Path(session_path).exists():
            return
        try:
            asyncio.run(self._poll_account_async(label, session_path))
        except Exception:
            log.debug("poll for %s failed", label, exc_info=True)

    async def _poll_account_async(self, label: str, session_path: str) -> None:
        client = TelegramClient(
            session_path,
            self._api_id,
            self._api_hash,
            connection_retries=2,
            request_retries=2,
            timeout=15,
        )
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return
            # getMe — главный realistic call (real client делает это часто)
            try:
                me = await client.get_me()
                log.debug("[%s] getMe ok: id=%s", label, getattr(me, "id", "?"))
            except Exception:
                pass
            # 30% шанс get_dialogs (chat list refresh)
            if random.random() < 0.30:
                try:
                    await client.get_dialogs(limit=random.randint(3, 10))
                    log.debug("[%s] get_dialogs ok", label)
                except Exception:
                    pass
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
