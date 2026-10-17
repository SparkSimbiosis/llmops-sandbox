"""Увесь SQL dev-агента. Лише таблиці agent_*: курсових таблиць цей модуль не читає й не пише.

Власний лінивий пул (не курсовий storage/db.py): max 3 з'єднання, на старті сервісу до БД
не ходимо. Будь-яка помилка БД — зокрема відсутня агентська схема — це AgentDbUnavailable,
маршрути /agent/* відповідають 503, а курс і /health працюють далі.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import asyncpg

from sandbox.agent.settings import AGENT_DB_ATTEMPTS, AGENT_DB_MAX_SIZE, AGENT_DB_PAUSE_S

log = logging.getLogger("sandbox.agent.db")

# що означає «агентська БД недоступна», а не помилку в нашому коді; UndefinedTableError
# (схеми агента немає) — підклас PostgresError, тож теж сюди
DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, asyncio.TimeoutError, ConnectionError)


class AgentDbUnavailable(Exception):
    pass


class AgentDatabase:
    def __init__(self, dsn: str, *, max_size: int = AGENT_DB_MAX_SIZE,
                 attempts: int = AGENT_DB_ATTEMPTS, pause_s: float = AGENT_DB_PAUSE_S):
        self._dsn = dsn
        self._max_size = max_size
        self._attempts = attempts
        self._pause_s = pause_s
        self._pool = None
        self._lock = asyncio.Lock()

    async def _get_pool(self):
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    self._dsn, min_size=0, max_size=self._max_size, timeout=2, command_timeout=5)
        return self._pool

    async def _acquire(self):
        # 3 спроби з паузою: postgres міг ще не піднятися (depends_on не чекає готовності)
        for attempt in range(1, self._attempts + 1):
            try:
                pool = await self._get_pool()
                return pool, await pool.acquire(timeout=2)
            except Exception as exc:  # будь-яка невдача підключення = БД недоступна
                if attempt == self._attempts:
                    raise AgentDbUnavailable(type(exc).__name__) from exc
                await asyncio.sleep(self._pause_s)

    @asynccontextmanager
    async def connection(self):
        pool, conn = await self._acquire()
        try:
            yield conn
        except DB_ERRORS as exc:
            raise AgentDbUnavailable(type(exc).__name__) from exc
        finally:
            await pool.release(conn)


def _jsonb(value) -> str:
    # asyncpg без кодека віддає й приймає jsonb рядком; серіалізуємо самі, явно
    return json.dumps(value, ensure_ascii=False)


def _from_jsonb(value, default):
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


class AgentRepo:
    def __init__(self, db):
        self._db = db

    # ---------- реєстр агентських промптів ----------

    async def list_prompts(self) -> list[dict]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                "SELECT name, version, active, version_id, body FROM agent_prompts ORDER BY name, version")
        return [dict(r) for r in rows]

    async def fetch_active_prompt(self, name: str):
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                "SELECT version, body, version_id FROM agent_prompts WHERE name = $1 AND active LIMIT 1", name)
        return None if row is None else (row["version"], row["body"], row["version_id"])

    async def activate_prompt(self, name: str, version: str, actor: str, quote: str | None):
        """Одна транзакція: EXISTS цілі -> усі false -> ціль true -> рядок аудиту.
        Два UPDATE, бо частковий унікальний індекс не дозволяє двох активних навіть на мить.
        None — такої версії немає, нічого не змінено й аудиту немає."""
        async with self._db.connection() as conn:
            async with conn.transaction():
                locked = await conn.fetch(
                    "SELECT version, active FROM agent_prompts WHERE name = $1 FOR UPDATE", name)
                if not any(r["version"] == version for r in locked):
                    return None
                previous = next((r["version"] for r in locked if r["active"]), None)
                await self._switch(conn, name, version, previous, actor, quote)
        return version, previous   # previous може бути None: до активації активної версії не було

    async def rollback_prompt(self, name: str, actor: str, quote: str | None):
        """Повернути версію, яка була активною до останньої активації (з журналу активацій).
        None — повертатися нема куди: історії немає або попередня версія невідома."""
        async with self._db.connection() as conn:
            async with conn.transaction():
                locked = await conn.fetch(
                    "SELECT version, active FROM agent_prompts WHERE name = $1 FOR UPDATE", name)
                last = await conn.fetchrow(
                    "SELECT previous_version FROM agent_prompt_activations "
                    "WHERE name = $1 ORDER BY id DESC LIMIT 1", name)
                target = None if last is None else last["previous_version"]
                if target is None or not any(r["version"] == target for r in locked):
                    return None
                previous = next((r["version"] for r in locked if r["active"]), None)
                await self._switch(conn, name, target, previous, actor, quote)
        return target, previous

    @staticmethod
    async def _switch(conn, name, version, previous, actor, quote) -> None:
        await conn.execute("UPDATE agent_prompts SET active = false WHERE name = $1", name)
        await conn.execute("UPDATE agent_prompts SET active = true WHERE name = $1 AND version = $2", name, version)
        await conn.execute(
            "INSERT INTO agent_prompt_activations (name, version, previous_version, actor, approval_quote) "
            "VALUES ($1, $2, $3, $4, $5)", name, version, previous, actor, quote)

    # ---------- файли фейкового сайту ----------

    async def list_site_files(self) -> list[dict]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                "SELECT path, version, sha256, octet_length(convert_to(content, 'UTF8')) AS bytes "
                "FROM agent_site_files_current ORDER BY path")
        return [dict(r) for r in rows]

    async def get_site_file(self, path: str, version: int | None):
        async with self._db.connection() as conn:
            if version is None:
                row = await conn.fetchrow(
                    "SELECT path, version, sha256, content FROM agent_site_files_current WHERE path = $1", path)
            else:
                row = await conn.fetchrow(
                    "SELECT path, version, sha256, content FROM agent_site_file_versions "
                    "WHERE path = $1 AND version = $2", path, version)
        return None if row is None else dict(row)

    async def site_files(self, paths: list[str], base: str) -> list[dict]:
        """Файли для контексту задачі: base=current — поточні версії, base=seed — версія 1."""
        async with self._db.connection() as conn:
            if base == "seed":
                rows = await conn.fetch(
                    "SELECT path, version, sha256, content FROM agent_site_file_versions "
                    "WHERE version = 1 AND path = ANY($1::text[])", paths)
            else:
                rows = await conn.fetch(
                    "SELECT path, version, sha256, content FROM agent_site_files_current "
                    "WHERE path = ANY($1::text[])", paths)
        by_path = {r["path"]: dict(r) for r in rows}
        return [by_path[p] for p in paths if p in by_path]   # порядок — як у плані

    # ---------- задачі й виклики ----------

    async def create_task(self, task_id, task_text: str, mode: str, base: str) -> None:
        # рядок 'received' з'являється до першого виклику моделі: якщо БД впаде посеред
        # прогону, у таблиці лишиться слід незавершеної задачі
        async with self._db.connection() as conn:
            await conn.execute(
                "INSERT INTO agent_tasks (task_id, task_text, mode, base, status) "
                "VALUES ($1, $2, $3, $4, 'received')", task_id, task_text, mode, base)

    async def insert_call(self, task_id, seq: int, step: str, provider: str, model: str,
                          prompt_name: str, prompt_version: str, prompt_sha256: str,
                          prompt_tokens: int, completion_tokens: int, latency_ms: int, status: str) -> None:
        async with self._db.connection() as conn:
            await conn.execute(
                "INSERT INTO agent_calls (task_id, seq, step, provider, model, prompt_name, prompt_version, "
                "prompt_sha256, prompt_tokens, completion_tokens, latency_ms, status) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
                task_id, seq, step, provider, model, prompt_name, prompt_version, prompt_sha256,
                prompt_tokens, completion_tokens, latency_ms, status)

    async def finish_task(self, task_id, *, status: str, message: str, intent, prompt_version,
                          context_sha256, base_versions: dict, patch_diff, patch_sha256,
                          gate_report: list, prompt_tokens: int, completion_tokens: int, error) -> None:
        async with self._db.connection() as conn:
            await conn.execute(
                "UPDATE agent_tasks SET status = $2, message = $3, intent = $4, prompt_version = $5, "
                "context_sha256 = $6, base_versions = $7::jsonb, patch_diff = $8, patch_sha256 = $9, "
                "gate_report = $10::jsonb, total_prompt_tokens = $11, total_completion_tokens = $12, "
                "error = $13, updated_at = now() WHERE task_id = $1",
                task_id, status, message, intent, prompt_version, context_sha256, _jsonb(base_versions),
                patch_diff, patch_sha256, _jsonb(gate_report), prompt_tokens, completion_tokens, error)

    async def list_tasks(self, limit: int) -> list[dict]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                "SELECT t.task_id, t.created_at, t.status, t.intent, "
                "(SELECT c.model FROM agent_calls c WHERE c.task_id = t.task_id ORDER BY c.seq DESC LIMIT 1) AS model "
                "FROM agent_tasks t ORDER BY t.created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def get_task(self, task_id):
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                "SELECT task_id, created_at, updated_at, task_text, intent, mode, base, status, message, "
                "prompt_version, context_sha256, base_versions, patch_diff, patch_sha256, gate_report, "
                "total_prompt_tokens, total_completion_tokens, error FROM agent_tasks WHERE task_id = $1", task_id)
            if row is None:
                return None
            calls = await conn.fetch(
                "SELECT seq, step, provider, model, prompt_name, prompt_version, prompt_sha256, prompt_tokens, "
                "completion_tokens, latency_ms, status FROM agent_calls WHERE task_id = $1 ORDER BY seq", task_id)
        task = dict(row)
        task["base_versions"] = _from_jsonb(task["base_versions"], {})
        task["gate_report"] = _from_jsonb(task["gate_report"], [])
        task["calls"] = [dict(c) for c in calls]
        return task
