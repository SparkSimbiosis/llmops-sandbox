"""Лінивий пул asyncpg для курсу.

На старті сервіс до БД не ходить: у postgres немає healthcheck, а depends_on не чекає готовності.
Пул створюється при першому зверненні; невдача -> DbUnavailable, наступний виклик пробує знову.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import asyncpg

log = logging.getLogger("sandbox.db")

# помилки, які означають «БД недоступна», а не помилку в нашому коді
DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, asyncio.TimeoutError, ConnectionError)


class DbUnavailable(Exception):
    pass


class Database:
    def __init__(self, dsn: str, *, max_size: int = 5):
        self._dsn = dsn
        self._max_size = max_size
        self._pool = None
        self._lock = asyncio.Lock()

    async def _get_pool(self):
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                try:
                    self._pool = await asyncpg.create_pool(
                        self._dsn, min_size=0, max_size=self._max_size, timeout=2, command_timeout=5)
                except Exception as exc:  # будь-яка невдача створення пулу = БД недоступна
                    raise DbUnavailable(type(exc).__name__) from exc
        return self._pool

    @asynccontextmanager
    async def connection(self):
        pool = await self._get_pool()
        try:
            conn = await pool.acquire()
        except DB_ERRORS as exc:
            raise DbUnavailable(type(exc).__name__) from exc
        try:
            yield conn
        finally:
            await pool.release(conn)
