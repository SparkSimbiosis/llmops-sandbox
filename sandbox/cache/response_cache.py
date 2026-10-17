"""Кеш відповідей моделі (W3): однаковий запит удруге не йде в модель.

Ключ — модель + промпт (ім'я, версія і sha256 тексту, що пішов у модель) + повідомлення.
Якщо рахувати ключ лише з повідомлення, після відкату промпта кеш віддавав би стару відповідь
і rollback з hw1 тихо зламався б. Епоха — додатковий запобіжник: активація промпта її збільшує.
"""

from dataclasses import dataclass

from sandbox.core.hashing import canonical_json, sha256_hex


@dataclass(frozen=True)
class CachedAnswer:
    content: str          # сирий текст моделі (без результату інструмента)
    tool: str | None      # назва read-only інструмента, який треба виконати знову на hit


class ResponseCache:
    def __init__(self):
        self.entries: dict[str, CachedAnswer] = {}
        self.epoch = 0

    def key(self, model: str, prompt_name: str, prompt_version: str, system_prompt: str, message: str) -> str:
        return sha256_hex(canonical_json({
            "epoch": self.epoch, "model": model, "prompt_name": prompt_name,
            "prompt_version": prompt_version, "system_sha256": sha256_hex(system_prompt), "message": message,
        }))

    def get(self, key: str) -> CachedAnswer | None:
        return self.entries.get(key)

    def put(self, key: str, answer: CachedAnswer) -> None:
        self.entries[key] = answer

    def invalidate(self) -> None:
        # змінився промпт — старі відповіді більше не відповідають тому, що бачила б модель
        self.epoch += 1
        self.entries.clear()


CACHE = ResponseCache()
