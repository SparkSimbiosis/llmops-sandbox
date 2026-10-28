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

from sandbox.agent.settings import AGENT_DB_ATTEMPTS, AGENT_DB_MAX_SIZE, AGENT_DB_PAUSE_S, CHAT_HISTORY_TURNS

APPROVAL_COLS = ("approval_id, task_id, created_at, decided_at, status, action, idempotency_key, base_versions, patch_diff, patch_sha256, files, reason, conflicts")

log = logging.getLogger("sandbox.agent.db")

# що означає «агентська БД недоступна», а не помилку в нашому коді; UndefinedTableError
# (схеми агента немає) — підклас PostgresError, тож теж сюди
DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, asyncio.TimeoutError, ConnectionError)


def cost_summary(calls) -> dict:
    """Облік записаних спроб; кеш не є зверненням до провайдера. NULL не дорівнює нулю."""
    from decimal import Decimal
    rows = list(calls)
    attempts = [c for c in rows if not c.get("cache_hit", False)]
    known = lambda group: sum((c["cost_usd"] for c in group if c["cost_usd"] is not None), Decimal("0"))
    real = [c for c in attempts if c.get("mode") == "real"]
    synthetic = [c for c in attempts if c.get("mode") != "real"]
    return {"cost_usd": known(rows), "real_estimate_usd": known(real),
            "synthetic_usd": known(synthetic), "unknown_cost_calls": sum(c["cost_usd"] is None for c in attempts),
            "real_unknown_cost_calls": sum(c["cost_usd"] is None for c in real),
            "provider_attempts": len(attempts), "real_provider_attempts": len(real),
            "synthetic_provider_attempts": len(synthetic), "cache_hits": len(rows) - len(attempts),
            "records": len(rows), "prompt_tokens": sum(c.get("prompt_tokens", 0) for c in attempts),
            "completion_tokens": sum(c.get("completion_tokens", 0) for c in attempts)}


def chat_action_projection(turn, own_calls, child_calls) -> dict:
    """Дочірні виклики додаються лише у read projection; збережена ціна ходу не змінюється."""
    own, child = list(own_calls), list(child_calls)
    mode = turn["mode"]
    own_summary = cost_summary([dict(c, mode=mode) for c in own])
    summary = cost_summary([dict(c, mode=mode) for c in own + child])
    return {"own_calls": own_summary["provider_attempts"], "calls": own_summary["provider_attempts"],
            "own_cost_usd": turn.get("total_cost_usd"), "action_cost_usd": summary["cost_usd"],
            "action_calls": summary["provider_attempts"], "action_cache_hits": summary["cache_hits"],
            "action_tokens": {"prompt": summary["prompt_tokens"], "completion": summary["completion_tokens"]},
            "history": {"limit_turns": CHAT_HISTORY_TURNS,
                        "included_turns": min(max(turn["seq"] - 1, 0), CHAT_HISTORY_TURNS),
                        "truncated": turn["seq"] > CHAT_HISTORY_TURNS + 1},
            "cache_hits": own_summary["cache_hits"], "accounting": summary}


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


def next_version_number(versions: list[str]) -> int:
    # 'v10' > 'v9' за числом, а не за рядком; версії не у форматі vN (якщо з'являться вручну) пропускаємо
    nums = [int(v[1:]) for v in versions if v[:1] == "v" and v[1:].isdigit()]
    return max(nums, default=0) + 1


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

    async def get_prompt(self, name: str, version: str):
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                "SELECT name, version, active, version_id, body, created_at FROM agent_prompts "
                "WHERE name = $1 AND version = $2", name, version)
        return None if row is None else dict(row)

    async def create_prompt_version(self, name: str, body: str):
        """Нова версія vN+1 неактивною. Версії незмінні: той самий текст уже є — повертаємо його версію.
        None — імені немає в реєстрі (нові імена з UI не створюються). Повертає (version, created)."""
        async with self._db.connection() as conn:
            async with conn.transaction():
                locked = await conn.fetch(
                    "SELECT version, body FROM agent_prompts WHERE name = $1 FOR UPDATE", name)
                if not locked:
                    return None
                same = next((r["version"] for r in locked if r["body"] == body), None)
                if same is not None:
                    return same, False
                version = f"v{next_version_number([r['version'] for r in locked])}"
                await conn.execute(
                    "INSERT INTO agent_prompts (name, version, body, active) VALUES ($1, $2, $3, false)",
                    name, version, body)
        return version, True

    async def list_activations(self, name: str, limit: int) -> list[dict]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                "SELECT version, previous_version, actor, approval_quote, activated_at "
                "FROM agent_prompt_activations WHERE name = $1 ORDER BY id DESC LIMIT $2", name, limit)
        return [dict(r) for r in rows]

    @staticmethod
    async def _switch(conn, name, version, previous, actor, quote) -> None:
        await conn.execute("UPDATE agent_prompts SET active = false WHERE name = $1", name)
        await conn.execute("UPDATE agent_prompts SET active = true WHERE name = $1 AND version = $2", name, version)
        await conn.execute(
            "INSERT INTO agent_prompt_activations (name, version, previous_version, actor, approval_quote) "
            "VALUES ($1, $2, $3, $4, $5)", name, version, previous, actor, quote)

    # ---------- реєстр інструкцій (I-1) ----------

    async def import_instruction(self, kind, name, body, sha256, source, source_path, source_ref):
        """Нова версія vN+1, якщо текст новий; той самий sha256 уже є — його версія. Повертає (version, created)."""
        async with self._db.connection() as conn:
            async with conn.transaction():
                # серіалізуємо імпорт одного (kind, name): рядків ще може не бути, тож lock — advisory
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"instr:{kind}:{name}")
                rows = await conn.fetch(
                    "SELECT version, sha256 FROM agent_instruction_items WHERE kind = $1 AND name = $2", kind, name)
                same = next((r["version"] for r in rows if r["sha256"] == sha256), None)
                if same is not None:
                    return same, False
                version = f"v{next_version_number([r['version'] for r in rows])}"
                await conn.execute(
                    "INSERT INTO agent_instruction_items (kind, name, version, sha256, body, source, source_path, "
                    "source_ref) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                    kind, name, version, sha256, body, source, source_path, source_ref)
        return version, True

    async def list_instructions(self, project: str) -> list[dict]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                "SELECT i.kind, i.name, count(*) AS versions, "
                "(array_agg(i.version ORDER BY i.imported_at DESC))[1] AS latest, "
                "(array_agg(i.source ORDER BY i.imported_at DESC))[1] AS source, "
                "(array_agg(i.source_path ORDER BY i.imported_at DESC))[1] AS source_path, "
                "a.version AS active_version, a.share_with_model "
                "FROM agent_instruction_items i "
                "LEFT JOIN agent_instruction_active a ON a.kind = i.kind AND a.name = i.name AND a.project = $1 "
                "GROUP BY i.kind, i.name, a.version, a.share_with_model ORDER BY i.kind, i.name", project)
        return [dict(r) for r in rows]

    async def instruction_versions(self, kind: str, name: str) -> list[dict]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                "SELECT version, sha256, source, source_ref, imported_at FROM agent_instruction_items "
                "WHERE kind = $1 AND name = $2 ORDER BY imported_at", kind, name)
        return [dict(r) for r in rows]

    async def get_instruction(self, kind: str, name: str, version: str):
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                "SELECT sha256, body, source, source_path, source_ref FROM agent_instruction_items "
                "WHERE kind = $1 AND name = $2 AND version = $3", kind, name, version)
        return None if row is None else dict(row)

    async def activate_instruction(self, project, kind, name, version):
        """Попередня активна версія (або None); False — такої версії немає, нічого не змінено."""
        async with self._db.connection() as conn:
            async with conn.transaction():
                exists = await conn.fetchval(
                    "SELECT 1 FROM agent_instruction_items WHERE kind = $1 AND name = $2 AND version = $3",
                    kind, name, version)
                if not exists:
                    return False
                previous = await conn.fetchval(
                    "SELECT version FROM agent_instruction_active WHERE project = $1 AND kind = $2 AND name = $3 "
                    "FOR UPDATE", project, kind, name)
                await conn.execute(
                    "INSERT INTO agent_instruction_active (project, kind, name, version) VALUES ($1, $2, $3, $4) "
                    "ON CONFLICT (project, kind, name) DO UPDATE SET version = EXCLUDED.version, updated_at = now()",
                    project, kind, name, version)
        return previous

    async def set_instruction_share(self, project, kind, name, share: bool) -> bool:
        async with self._db.connection() as conn:
            status = await conn.execute(
                "UPDATE agent_instruction_active SET share_with_model = $4, updated_at = now() "
                "WHERE project = $1 AND kind = $2 AND name = $3", project, kind, name, share)
        return status.endswith(" 1")

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
                          prompt_tokens: int, completion_tokens: int, latency_ms: int, status: str,
                          cost_usd, cache_hit: bool = False) -> None:
        # cost_usd — Decimal або None (модель без ціни чи відповіді не було); колонка — з міграції hw2,
        # cache_hit — з hw3
        async with self._db.connection() as conn:
            await conn.execute(
                "INSERT INTO agent_calls (task_id, seq, step, provider, model, prompt_name, prompt_version, "
                "prompt_sha256, prompt_tokens, completion_tokens, latency_ms, status, cost_usd, cache_hit) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) "
                "ON CONFLICT (task_id, seq) DO NOTHING",
                task_id, seq, step, provider, model, prompt_name, prompt_version, prompt_sha256,
                prompt_tokens, completion_tokens, latency_ms, status, cost_usd, cache_hit)

    async def finish_task(self, task_id, *, status: str, message: str, intent, prompt_version,
                          context_sha256, base_versions: dict, patch_diff, patch_sha256,
                          gate_report: list, prompt_tokens: int, completion_tokens: int, error,
                          complexity, total_cost_usd) -> None:
        async with self._db.connection() as conn:
            await conn.execute(
                "UPDATE agent_tasks SET status = $2, message = $3, intent = $4, prompt_version = $5, "
                "context_sha256 = $6, base_versions = $7::jsonb, patch_diff = $8, patch_sha256 = $9, "
                "gate_report = $10::jsonb, total_prompt_tokens = $11, total_completion_tokens = $12, "
                "error = $13, complexity = $14, total_cost_usd = $15, updated_at = now() WHERE task_id = $1",
                task_id, status, message, intent, prompt_version, context_sha256, _jsonb(base_versions),
                patch_diff, patch_sha256, _jsonb(gate_report), prompt_tokens, completion_tokens, error,
                complexity, total_cost_usd)

    async def cost_today(self) -> dict:
        """Вартість викликів агента за сьогодні (CURRENT_DATE — за UTC сервера): agent_calls задач,
        agent_chat_calls чату (W5) і agent_workspace_calls дослідника (W6), курсову requests не читаємо.
        real — виклики з mode='real'."""
        calls = ("SELECT c.model, c.cost_usd, c.created_at, c.cache_hit, c.prompt_tokens, c.completion_tokens, t.mode FROM agent_calls c "
                 "JOIN agent_tasks t ON t.task_id = c.task_id UNION ALL "
                 "SELECT c.model, c.cost_usd, c.created_at, c.cache_hit, c.prompt_tokens, c.completion_tokens, t.mode FROM agent_chat_calls c "
                 "JOIN agent_chat_turns t ON t.turn_id = c.turn_id UNION ALL "
                 "SELECT c.model, c.cost_usd, c.created_at, c.cache_hit, c.prompt_tokens, c.completion_tokens, r.mode FROM agent_workspace_calls c "
                 "JOIN agent_context_packs r ON r.run_id = c.run_id")
        async with self._db.connection() as conn:
            rows = [dict(r) for r in await conn.fetch(f"SELECT * FROM ({calls}) a WHERE created_at::date = CURRENT_DATE")]
        summary = cost_summary(rows)
        models = sorted({r["model"] for r in rows})
        by_model = [{"model": model, **cost_summary(r for r in rows if r["model"] == model)} for model in models]
        for row in by_model:
            row["calls"] = row["records"]  # старе поле; точна семантика у provider_attempts/cache_hits
        by_model.sort(key=lambda r: (-r["cost_usd"], r["model"]))
        return {**summary, "today_usd": summary["cost_usd"], "real_today_usd": summary["real_estimate_usd"],
                "synthetic_today_usd": summary["synthetic_usd"], "by_model": by_model}

    async def list_tasks(self, limit: int) -> list[dict]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                "SELECT t.task_id, t.created_at, t.status, t.intent, t.mode, t.total_cost_usd, "
                "(SELECT c.model FROM agent_calls c WHERE c.task_id = t.task_id ORDER BY c.seq DESC LIMIT 1) AS model "
                "FROM agent_tasks t ORDER BY t.created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def get_task(self, task_id):
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                "SELECT task_id, created_at, updated_at, task_text, intent, mode, base, status, message, "
                "prompt_version, context_sha256, base_versions, patch_diff, patch_sha256, gate_report, "
                "total_prompt_tokens, total_completion_tokens, error, complexity, total_cost_usd, approval_id "
                "FROM agent_tasks WHERE task_id = $1", task_id)
            if row is None:
                return None
            calls = await conn.fetch(
                "SELECT seq, step, provider, model, prompt_name, prompt_version, prompt_sha256, prompt_tokens, "
                "completion_tokens, latency_ms, status, cost_usd, cache_hit FROM agent_calls WHERE task_id = $1 "
                "ORDER BY seq",
                task_id)
        task = dict(row)
        task["base_versions"] = _from_jsonb(task["base_versions"], {})
        task["gate_report"] = _from_jsonb(task["gate_report"], [])
        task["calls"] = [dict(c) for c in calls]
        return task

    # ---------- заявки на застосування (W4) ----------

    @staticmethod
    def _approval(row) -> dict | None:
        if row is None:
            return None
        a = dict(row)
        for k, default in (("base_versions", {}), ("files", []), ("conflicts", [])):
            a[k] = _from_jsonb(a[k], default)
        return a

    async def create_approval(self, approval_id, task_id, key: str, base_versions: dict, diff: str,
                              patch_sha256: str, files: list, action: str = "apply_patch") -> dict:
        """Нова заявка або вже відкрита з тим самим ключем (ідемпотентність: та сама пропозиція)."""
        async with self._db.connection() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "INSERT INTO agent_approvals (approval_id, task_id, status, idempotency_key, base_versions, "
                    "patch_diff, patch_sha256, files, action) "
                    "VALUES ($1, $2, 'pending', $3, $4::jsonb, $5, $6, $7::jsonb, $8) "
                    "ON CONFLICT (idempotency_key) WHERE status = 'pending' DO NOTHING "
                    f"RETURNING {APPROVAL_COLS}",
                    approval_id, task_id, key, _jsonb(base_versions), diff, patch_sha256, _jsonb(files), action)
                if row is None:
                    row = await conn.fetchrow(f"SELECT {APPROVAL_COLS} FROM agent_approvals "
                                              "WHERE idempotency_key = $1 AND status = 'pending'", key)
                await conn.execute("UPDATE agent_tasks SET approval_id = $2 WHERE task_id = $1",
                                   task_id, row["approval_id"])
        return self._approval(row)

    async def get_approval(self, approval_id) -> dict | None:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(f"SELECT {APPROVAL_COLS} FROM agent_approvals WHERE approval_id = $1",
                                      approval_id)
        return self._approval(row)

    async def list_approvals(self, status: str | None, limit: int) -> list[dict]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"SELECT {APPROVAL_COLS} FROM agent_approvals WHERE ($1::text IS NULL OR status = $1) "
                "ORDER BY created_at DESC LIMIT $2", status, limit)
        return [self._approval(r) for r in rows]

    async def close_approval(self, approval_id, status: str, reason: str | None, conflicts: list) -> bool:
        """pending -> rejected / stale. False — заявки немає або її вже вирішено."""
        async with self._db.connection() as conn:
            done = await conn.execute(
                "UPDATE agent_approvals SET status = $2, reason = $3, conflicts = $4::jsonb, decided_at = now() "
                "WHERE approval_id = $1 AND status = 'pending'", approval_id, status, reason, _jsonb(conflicts))
        return done.endswith(" 1")

    async def apply_approval(self, approval_id, task_id, new_files: list[dict]) -> str:
        """Атомарно: заявка pending -> applied і нові версії файлів (append-only). 'applied', 'gone' (вже
        вирішено) або 'stale' (хтось записав ту саму версію раніше — конфлікт PK)."""
        async with self._db.connection() as conn:
            try:
                async with conn.transaction():
                    done = await conn.execute(
                        "UPDATE agent_approvals SET status = 'applied', decided_at = now() "
                        "WHERE approval_id = $1 AND status = 'pending'", approval_id)
                    if not done.endswith(" 1"):
                        return "gone"
                    await conn.executemany(
                        "INSERT INTO agent_site_file_versions (path, version, content, sha256, source, "
                        "source_task_id, approval_id) VALUES ($1, $2, $3, $4, 'approval', $5, $6)",
                        [(f["path"], f["version"], f["content"], f["sha256"], task_id, approval_id) for f in new_files])
            except asyncpg.UniqueViolationError:
                await self.close_approval(approval_id, "stale", "base changed while applying", [])
                return "stale"
        return "applied"

    async def reset_site(self, seed: list[dict]) -> list[str]:
        """Нова версія = seed для кожного файлу, що відрізняється від seed (історія не стирається)."""
        changed = []
        async with self._db.connection() as conn:
            async with conn.transaction():
                for f in seed:
                    cur = await conn.fetchrow("SELECT version, sha256 FROM agent_site_files_current WHERE path = $1",
                                              f["path"])
                    if cur is None or cur["sha256"] == f["sha256"]:
                        continue
                    await conn.execute(
                        "INSERT INTO agent_site_file_versions (path, version, content, sha256, source) "
                        "VALUES ($1, $2, $3, $4, 'reset')", f["path"], cur["version"] + 1, f["content"], f["sha256"])
                    changed.append(f["path"])
        return changed

    async def site_history(self, limit: int) -> list[dict]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                "SELECT path, version, sha256, source, source_task_id, approval_id, created_at "
                "FROM agent_site_file_versions ORDER BY created_at DESC, path LIMIT $1", limit)
        return [dict(r) for r in rows]

    # ---------- чат (W5) ----------

    async def create_chat_turn(self, turn_id, chat_id, seq: int, mode: str, user_text: str, case_id) -> None:
        async with self._db.connection() as conn:
            await conn.execute(
                "INSERT INTO agent_chat_turns (turn_id, chat_id, seq, mode, user_text, status, case_id) "
                "VALUES ($1, $2, $3, $4, $5, 'received', $6)", turn_id, chat_id, seq, mode, user_text, case_id)

    async def insert_chat_call(self, turn_id, seq: int, step: str, provider: str, model: str,
                               prompt_name: str, prompt_version: str, prompt_sha256: str,
                               prompt_tokens: int, completion_tokens: int, latency_ms: int, status: str,
                               cost_usd, cache_hit: bool = False) -> None:
        # та сама сигнатура, що insert_call: _Run пише сюди виклики ходу чату
        async with self._db.connection() as conn:
            await conn.execute(
                "INSERT INTO agent_chat_calls (turn_id, seq, step, provider, model, prompt_name, prompt_version, "
                "prompt_sha256, prompt_tokens, completion_tokens, latency_ms, status, cost_usd, cache_hit) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) "
                "ON CONFLICT (turn_id, seq) DO NOTHING",
                turn_id, seq, step, provider, model, prompt_name, prompt_version, prompt_sha256,
                prompt_tokens, completion_tokens, latency_ms, status, cost_usd, cache_hit)

    async def finish_chat_turn(self, turn_id, *, status: str, reply: str, proposal_text, task_id, gate_report: list,
                               prompt_tokens: int, completion_tokens: int, total_cost_usd, error) -> None:
        async with self._db.connection() as conn:
            await conn.execute(
                "UPDATE agent_chat_turns SET status = $2, reply = $3, proposal_text = $4, task_id = $5, "
                "gate_report = $6::jsonb, total_prompt_tokens = $7, total_completion_tokens = $8, "
                "total_cost_usd = $9, error = $10, updated_at = now() WHERE turn_id = $1",
                turn_id, status, reply, proposal_text, task_id, _jsonb(gate_report), prompt_tokens,
                completion_tokens, total_cost_usd, error)

    async def chat_history(self, chat_id, limit: int) -> list[dict]:
        """Останні limit завершених ходів розмови за зростанням seq — те, що модель бачить блоком <history>."""
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                "SELECT seq, user_text, reply, status FROM (SELECT * FROM agent_chat_turns WHERE chat_id = $1 "
                "ORDER BY seq DESC LIMIT $2) t ORDER BY seq", chat_id, limit)
        return [dict(r) for r in rows]

    async def get_chat(self, chat_id) -> list[dict]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                "SELECT t.turn_id, t.chat_id, t.seq, t.created_at, t.mode, t.user_text, t.status, t.reply, "
                "t.proposal_text, t.task_id, t.case_id, t.gate_report, t.total_cost_usd, t.error, a.approval_id, "
                "a.status AS approval_status FROM agent_chat_turns t "
                "LEFT JOIN agent_tasks k ON k.task_id = t.task_id "
                "LEFT JOIN agent_approvals a ON a.approval_id = k.approval_id "
                "WHERE t.chat_id = $1 ORDER BY t.seq", chat_id)
            own = [dict(c) for c in await conn.fetch(
                "SELECT c.* FROM agent_chat_calls c JOIN agent_chat_turns t ON t.turn_id = c.turn_id WHERE t.chat_id = $1", chat_id)]
            child = [dict(c) for c in await conn.fetch(
                "SELECT c.* FROM agent_calls c WHERE c.task_id IN (SELECT task_id FROM agent_chat_turns WHERE chat_id = $1)", chat_id)]
        out = []
        for r in rows:
            d = dict(r)
            d["gate_report"] = _from_jsonb(d["gate_report"], [])
            d.update(chat_action_projection(d, [c for c in own if c["turn_id"] == d["turn_id"]],
                                            [c for c in child if c["task_id"] == d["task_id"]]))
            out.append(d)
        return out

    async def list_chats(self, limit: int) -> list[dict]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                "SELECT chat_id, min(created_at) AS started_at, max(created_at) AS last_at, count(*) AS turns, "
                "(array_agg(user_text ORDER BY seq))[1] AS first_text, (array_agg(case_id ORDER BY seq))[1] AS case_id, "
                "COALESCE(SUM(total_cost_usd), 0) AS cost_usd FROM agent_chat_turns "
                "GROUP BY chat_id ORDER BY max(created_at) DESC LIMIT $1", limit)
        result = []
        for row in rows:
            d = dict(row)
            turns = await self.get_chat(d["chat_id"])
            summaries = [t["accounting"] for t in turns]
            d["action_cost_usd"] = sum(t["action_cost_usd"] for t in turns)
            d["real_estimate_usd"] = sum(t["real_estimate_usd"] for t in summaries)
            d["synthetic_usd"] = sum(t["synthetic_usd"] for t in summaries)
            d["unknown_cost_calls"] = sum(t["unknown_cost_calls"] for t in summaries)
            result.append(d)
        return result

    # ---------- робочі простори (W6) ----------

    async def save_workspace_snapshot(self, project: str, snapshot_sha256: str, files: list[dict]) -> None:
        async with self._db.connection() as conn:
            await conn.execute(
                "INSERT INTO agent_workspace_snapshots (project, snapshot_sha256, files) VALUES ($1, $2, $3::jsonb) "
                "ON CONFLICT (project, snapshot_sha256) DO NOTHING", project, snapshot_sha256, _jsonb(files))

    async def create_context_run(self, run_id, project: str, snapshot_sha256: str, agent: str, mode: str,
                                 question: str, budget_tokens: int, pack: dict, package: dict, pack_sha256: str,
                                 package_sha256: str) -> None:
        async with self._db.connection() as conn:
            await conn.execute(
                "INSERT INTO agent_context_packs (run_id, project, snapshot_sha256, agent, mode, question, budget_tokens, "
                "pack, package, pack_sha256, package_sha256, status) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10, $11, 'received')",
                run_id, project, snapshot_sha256, agent, mode, question, budget_tokens, _jsonb(pack),
                _jsonb(package), pack_sha256, package_sha256)

    async def insert_workspace_call(self, run_id, seq: int, step: str, provider: str, model: str,
                                    prompt_name: str, prompt_version: str, prompt_sha256: str,
                                    prompt_tokens: int, completion_tokens: int, latency_ms: int, status: str,
                                    cost_usd, cache_hit: bool = False) -> None:
        # та сама сигнатура, що insert_call: _Run пише сюди виклик дослідника
        async with self._db.connection() as conn:
            await conn.execute(
                "INSERT INTO agent_workspace_calls (run_id, seq, step, provider, model, prompt_name, prompt_version, "
                "prompt_sha256, prompt_tokens, completion_tokens, latency_ms, status, cost_usd, cache_hit) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14) "
                "ON CONFLICT (run_id, seq) DO NOTHING",
                run_id, seq, step, provider, model, prompt_name, prompt_version, prompt_sha256,
                prompt_tokens, completion_tokens, latency_ms, status, cost_usd, cache_hit)

    async def finish_context_run(self, run_id, *, status: str, answer, citations: list, missing: list,
                                 gate_report: list, prompt_tokens: int, completion_tokens: int, total_cost_usd,
                                 error) -> None:
        async with self._db.connection() as conn:
            await conn.execute(
                "UPDATE agent_context_packs SET status = $2, answer = $3, citations = $4::jsonb, missing = $5::jsonb, "
                "gate_report = $6::jsonb, total_prompt_tokens = $7, total_completion_tokens = $8, "
                "total_cost_usd = $9, error = $10, updated_at = now() WHERE run_id = $1",
                run_id, status, answer, _jsonb(citations), _jsonb(missing), _jsonb(gate_report), prompt_tokens,
                completion_tokens, total_cost_usd, error)

    async def list_context_runs(self, limit: int) -> list[dict]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                "SELECT run_id, created_at, project, mode, question, status, budget_tokens, pack_sha256, "
                "package_sha256, total_cost_usd, (pack->>'used_tokens')::int AS used_tokens, "
                "jsonb_array_length(pack->'items') AS chunks FROM agent_context_packs "
                "ORDER BY created_at DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def get_context_run(self, run_id) -> dict | None:
        async with self._db.connection() as conn:
            row = await conn.fetchrow("SELECT * FROM agent_context_packs WHERE run_id = $1", run_id)
            calls = await conn.fetch(
                "SELECT seq, step, provider, model, prompt_name, prompt_version, prompt_tokens, completion_tokens, "
                "latency_ms, status, cost_usd, cache_hit FROM agent_workspace_calls WHERE run_id = $1 ORDER BY seq",
                run_id)
        if row is None:
            return None
        d = dict(row)
        for k, default in (("pack", {}), ("package", {}), ("citations", []), ("missing", []), ("gate_report", [])):
            d[k] = _from_jsonb(d[k], default)
        d["calls"] = [dict(c) for c in calls]
        return d
