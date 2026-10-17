"""Кеш відповідей моделі (W3): однаковий запит удруге не йде в модель.

Ключ — модель + промпт (ім'я, версія і sha256 тексту, що пішов у модель) + повідомлення.
Якщо рахувати ключ лише з повідомлення, після відкату промпта кеш віддавав би стару відповідь
і rollback з hw1 тихо зламався б. Епоха — додатковий запобіжник: активація промпта її збільшує.
"""

import time
from dataclasses import dataclass, field

from sandbox.core.config import CACHE_TTL_S
from sandbox.core.hashing import canonical_json, sha256_hex


@dataclass(frozen=True)
class CachedAnswer:
    content: str          # сирий текст моделі (без результату інструмента)
    tool: str | None      # назва read-only інструмента, який треба виконати знову на hit
    tool_args: str = "{}"
    stored_at: float = field(default_factory=time.monotonic)


class ResponseCache:
    def __init__(self, ttl_s: float = CACHE_TTL_S, clock=time.monotonic):
        self.entries: dict[str, CachedAnswer] = {}
        self.epoch = 0
        # (опц. hw3) TTL: старий запис протухає і перезапитується — відповіді не застарівають назавжди
        self.ttl_s = ttl_s
        self.clock = clock

    def key(self, model: str, prompt_name: str, prompt_version: str, system_prompt: str, message: str) -> str:
        return sha256_hex(canonical_json({
            "epoch": self.epoch, "model": model, "prompt_name": prompt_name,
            "prompt_version": prompt_version, "system_sha256": sha256_hex(system_prompt), "message": message,
        }))

    def get(self, key: str) -> CachedAnswer | None:
        item = self.entries.get(key)
        if item is not None and self.clock() - item.stored_at > self.ttl_s:
            del self.entries[key]      # протух — це промах
            return None
        return item

    def put(self, key: str, answer: CachedAnswer) -> None:
        self.entries[key] = CachedAnswer(answer.content, answer.tool, answer.tool_args, self.clock())

    def invalidate(self) -> None:
        # змінився промпт — старі відповіді більше не відповідають тому, що бачила б модель
        self.epoch += 1
        self.entries.clear()


CACHE = ResponseCache()
