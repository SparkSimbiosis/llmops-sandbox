"""Реєстр промптів у пам'яті з тією ж семантикою, що PromptsRepo над Postgres (для тестів без БД)."""

import uuid
from datetime import datetime, timedelta, timezone

from sandbox.storage.db import DbUnavailable

V1 = "You are an assistant."
V2 = "You are a support assistant. Be concise and helpful."


class MemoryPrompts:
    def __init__(self, *, seed: bool = True, down: bool = False):
        self.down = down
        self.activations: list[dict] = []
        t0 = datetime(2026, 10, 17, tzinfo=timezone.utc)
        self.rows: list[dict] = []
        if seed:
            # той самий seed, що в db/schema.sql: дві версії, активна v2
            self.rows = [
                {"name": "support-system", "version": "v1", "body": V1, "active": False,
                 "version_id": uuid.uuid4(), "created_at": t0},
                {"name": "support-system", "version": "v2", "body": V2, "active": True,
                 "version_id": uuid.uuid4(), "created_at": t0 + timedelta(seconds=1)},
            ]

    def _check(self):
        if self.down:
            raise DbUnavailable("down")

    async def fetch_active(self, name):
        self._check()
        active = [r for r in self.rows if r["name"] == name and r["active"]]
        if not active:
            return None
        r = max(active, key=lambda x: x["created_at"])
        return r["version"], r["body"], r["version_id"]

    async def list_all(self):
        self._check()
        return [dict(r) for r in sorted(self.rows, key=lambda x: (x["name"], x["created_at"], x["version"]))]

    async def activate(self, name, version, actor, quote):
        self._check()
        mine = [r for r in self.rows if r["name"] == name]
        if not any(r["version"] == version for r in mine):
            return None  # нічого не змінено, аудиту немає
        previous = next((r["version"] for r in sorted(mine, key=lambda x: x["created_at"], reverse=True)
                         if r["active"]), None)
        for r in mine:
            r["active"] = r["version"] == version
        self.activations.append({"name": name, "version": version, "previous_version": previous,
                                 "actor": actor, "approval_quote": quote})
        return previous, await self.list_all()
