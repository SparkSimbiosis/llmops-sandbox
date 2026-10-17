"""Доступ до таблиць курсу. Агент сюди не ходить: у нього власні таблиці agent_*."""

import logging

from sandbox.llm.types import Usage
from sandbox.storage.db import Database

log = logging.getLogger("sandbox.requests")


class RequestsRepo:
    """Один рядок у requests на кожен POST /chat — з цього живуть observability (W1) і cost (W2)."""

    def __init__(self, db: Database):
        self._db = db

    async def insert_safe(self, request_id, model: str, latency_ms: int, usage: Usage,
                          cost_usd, status: str) -> None:
        # якщо лог упав — запит користувача все одно віддаємо (як LogRequest у стартері)
        try:
            async with self._db.connection() as conn:
                await conn.execute(
                    "INSERT INTO requests (request_id, model, latency_ms, prompt_tokens, completion_tokens, "
                    "cost_usd, status) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    request_id, model, latency_ms, usage.prompt_tokens, usage.completion_tokens,
                    cost_usd, status)
        except Exception:
            # текст повідомлення в лог не пишемо — лише факт збою запису
            log.exception("requests insert failed request_id=%s", request_id)
