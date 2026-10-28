"""Кеш кроків агента (W3, урок 5): точний ключ, лише в пам'яті процесу, окремо від курсового кешу.

Ключ = режим + крок + модель + назва, версія й sha256 промпта + sha256 повідомлень. У повідомленнях уже є
текст задачі, роль і скіли агента та файли сайту з версіями (context_sha256), тож будь-яка зміна промпта,
конфігурації чи сайту дає інший ключ — застарілу відповідь кеш не віддасть. Кешуються лише успішні відповіді
моделі: збій (503/429/таймаут) і заблокований гейтом real-виклик сюди не потрапляють.
Hit — виклику моделі немає: рядок agent_calls з provider='cache', cache_hit=true, токени 0, cost_usd 0.
"""

import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal

from sandbox.core.hashing import sha256_hex


@dataclass
class Entry:
    key: str
    mode: str
    step: str
    model: str
    prompt: str                   # "agent-editor@v2"
    content: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal | None      # скільки коштувала оригінальна відповідь — стільки заощаджує кожен hit
    created: float = field(default_factory=time.time)
    hits: int = 0


class StepCache:
    def __init__(self, ttl_s: int, max_entries: int):
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._items: OrderedDict[str, Entry] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.saved_usd = Decimal("0")
        self.saved_tokens = 0

    @staticmethod
    def key(mode: str, step: str, model: str, prompt, messages: list[dict]) -> str:
        return sha256_hex(json.dumps({"mode": mode, "step": step, "model": model, "prompt": prompt.name,
                                      "version": prompt.version, "prompt_sha256": prompt.sha256,
                                      "messages_sha256": sha256_hex(json.dumps(messages, ensure_ascii=False))},
                                     sort_keys=True))

    def get(self, key: str) -> Entry | None:
        e = self._items.get(key)
        if e is not None and time.time() - e.created > self.ttl_s:
            del self._items[key]
            e = None
        if e is None:
            self.misses += 1
            return None
        self._items.move_to_end(key)
        e.hits += 1
        self.hits += 1
        self.saved_usd += e.cost_usd or 0
        self.saved_tokens += e.prompt_tokens + e.completion_tokens
        return e

    def put(self, entry: Entry) -> None:
        self._items[entry.key] = entry
        self._items.move_to_end(entry.key)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)          # LRU: найдавніше використаний

    def clear(self) -> int:
        n = len(self._items)
        self._items.clear()
        return n

    def snapshot(self) -> dict:
        now = time.time()
        total = self.hits + self.misses
        return {"entries": len(self._items), "max_entries": self.max_entries, "ttl_s": self.ttl_s,
                "hits": self.hits, "misses": self.misses,
                "hit_pct": round(100 * self.hits / total, 1) if total else None,
                "saved_usd": float(self.saved_usd), "saved_tokens": self.saved_tokens,
                "items": [{"key": e.key[:12], "mode": e.mode, "step": e.step, "model": e.model, "prompt": e.prompt,
                           "age_s": int(now - e.created), "hits": e.hits,
                           "cost_usd": None if e.cost_usd is None else float(e.cost_usd)}
                          for e in reversed(self._items.values())]}
