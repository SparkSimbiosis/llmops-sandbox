"""Доступ до таблиць курсу. Агент сюди не ходить: у нього власні таблиці agent_*."""

import logging

from sandbox.llm.types import Usage
from sandbox.storage.db import DB_ERRORS, Database, DbUnavailable

log = logging.getLogger("sandbox.requests")


class RequestsRepo:
    """Один рядок у requests на кожен POST /chat — з цього живуть observability (W1) і cost (W2)."""

    def __init__(self, db: Database):
        self._db = db

    async def insert_safe(self, request_id, model: str, active, latency_ms: int, usage: Usage,
                          cost_usd, status: str) -> None:
        # якщо лог упав — запит користувача все одно віддаємо (як LogRequest у стартері)
        try:
            async with self._db.connection() as conn:
                await conn.execute(
                    "INSERT INTO requests (request_id, model, prompt_name, prompt_version, prompt_version_id, "
                    "prompt_sha256, latency_ms, prompt_tokens, completion_tokens, cost_usd, status) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                    request_id, model, active.name, active.version, active.version_id, active.sha256,
                    latency_ms, usage.prompt_tokens, usage.completion_tokens, cost_usd, status)
        except Exception:
            # текст повідомлення в лог не пишемо — лише факт збою запису
            log.exception("requests insert failed request_id=%s", request_id)


class CostRepo:
    """Вартість за сьогодні — чесна сума з requests, округлення робить Postgres (як у перевірці курсу)."""

    def __init__(self, db: Database):
        self._db = db

    async def today_usd(self) -> float:
        try:
            async with self._db.connection() as conn:
                value = await conn.fetchval(
                    "SELECT COALESCE(round(SUM(cost_usd), 4), 0) FROM requests "
                    "WHERE created_at::date = CURRENT_DATE")
        except DB_ERRORS as exc:
            raise DbUnavailable(type(exc).__name__) from exc
        return float(value)

    async def real_today_usd(self, models) -> float:
        # (опц. hw2) скільки реальних грошей уже витрачено сьогодні — для бюджету реальної моделі
        try:
            async with self._db.connection() as conn:
                value = await conn.fetchval(
                    "SELECT COALESCE(SUM(cost_usd), 0) FROM requests "
                    "WHERE created_at::date = CURRENT_DATE AND model = ANY($1::text[])", list(models))
        except DB_ERRORS as exc:
            raise DbUnavailable(type(exc).__name__) from exc
        return float(value)


class PromptsRepo:
    """SQL реєстру промптів. Помилки БД -> DbUnavailable: недоступна база — не те саме, що порожній реєстр."""

    def __init__(self, db: Database):
        self._db = db

    async def fetch_active(self, name: str):
        try:
            async with self._db.connection() as conn:
                row = await conn.fetchrow(
                    "SELECT version, body, version_id FROM prompts "
                    "WHERE name = $1 AND active ORDER BY created_at DESC LIMIT 1", name)
        except DB_ERRORS as exc:
            raise DbUnavailable(type(exc).__name__) from exc
        return None if row is None else (row["version"], row["body"], row["version_id"])

    async def list_all(self) -> list[dict]:
        try:
            async with self._db.connection() as conn:
                return await _read_all(conn)
        except DB_ERRORS as exc:
            raise DbUnavailable(type(exc).__name__) from exc

    async def activate(self, name: str, version: str, actor: str, quote: str | None):
        try:
            async with self._db.connection() as conn:
                async with conn.transaction():
                    # блокуємо рядки цього промпта: паралельна активація чекає своєї черги,
                    # а previous_version у журналі — справді та версія, яку ми замінили
                    locked = await conn.fetch(
                        "SELECT version, active FROM prompts WHERE name = $1 ORDER BY created_at DESC FOR UPDATE",
                        name)
                    previous = next((r["version"] for r in locked if r["active"]), None)
                    # один UPDATE: обрана версія -> true, решта -> false. EXISTS не дає «тихо» зняти
                    # активність з усіх, коли версії немає (v99): тоді не оновиться жоден рядок
                    updated = await conn.fetch(
                        "WITH target AS (SELECT 1 FROM prompts WHERE name = $1 AND version = $2) "
                        "UPDATE prompts SET active = (version = $2) WHERE name = $1 "
                        "AND EXISTS (SELECT 1 FROM target) RETURNING version, active", name, version)
                    if not updated:
                        return None  # вихід з блоку transaction() без змін; рядка аудиту немає
                    # audit trail — у тій самій транзакції, що й зміна active
                    await conn.execute(
                        "INSERT INTO prompt_activations (name, version, previous_version, actor, approval_quote) "
                        "VALUES ($1, $2, $3, $4, $5)", name, version, previous, actor, quote)
                    rows = await _read_all(conn)
            return previous, rows
        except DB_ERRORS as exc:
            raise DbUnavailable(type(exc).__name__) from exc


async def _read_all(conn) -> list[dict]:
    rows = await conn.fetch(
        "SELECT name, version, active, version_id, body, created_at FROM prompts ORDER BY name, created_at, version")
    return [dict(r) for r in rows]
