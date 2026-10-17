"""Semantic cache (опційно, hw3): схоже питання, а не лише однакове, відповідається з кешу.

Як працює:
- текст (вже замаскований) перетворюється на вектор локальною моделлю ембеддингів (контейнер `embeddings`,
  multilingual-e5-small — розуміє українську); ключів зовнішніх провайдерів не треба;
- записи лежать у Redis у «кошику» області: модель + промпт (ім'я, версія, sha тексту) + епоха кешу —
  тобто промпт входить у ключ так само, як у точному кеші;
- схожість — косинусна; поріг SEMANTIC_THRESHOLD (0.88). У e5 схожість «стиснута»: непов'язані питання
  дають ~0.72–0.73, перефразування — 0.89–0.99 (заміри на цій моделі), тож 0.88 пропускає перефразування
  і з запасом відсікає чуже: краще промах, ніж чужа відповідь.

Обмеження, свідомо:
- питання з цифрами (номер замовлення, сума) не шукаються і не зберігаються: «замовлення #123» і «#124»
  дуже схожі за змістом, але відповіді в них різні;
- відповіді з інструментом не зберігаються — результат інструмента залежить від аргументів;
- Redis чи ембеддинги недоступні -> тихо працює лише точний кеш, користувач помилки не бачить.
"""

import json
import logging
import math
import re
import time

import httpx

from sandbox.core.hashing import sha256_hex

log = logging.getLogger("sandbox.semantic")

_DIGITS = re.compile(r"\d")
MAX_PER_SCOPE = 200            # не більше стількох записів в одному кошику (LTRIM)


def eligible(text: str) -> bool:
    return not _DIGITS.search(text)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class SemanticCache:
    def __init__(self, *, enabled: bool, redis_client, embed_url: str, threshold: float, ttl_s: float,
                 transport: httpx.AsyncBaseTransport | None = None, clock=time.time):
        self.enabled = enabled
        self._redis = redis_client
        self._embed_url = embed_url.rstrip("/")
        self.threshold = threshold
        self.ttl_s = ttl_s
        self._transport = transport
        self._clock = clock

    @staticmethod
    def scope(model: str, prompt_name: str, prompt_version: str, system_prompt: str, epoch: int) -> str:
        return "semcache:" + sha256_hex(f"{epoch}|{model}|{prompt_name}|{prompt_version}|{sha256_hex(system_prompt)}")

    async def _embed(self, text: str) -> list[float] | None:
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=5.0) as client:
                # e5-моделі чекають префікс "query: " для пошукових запитів
                r = await client.post(f"{self._embed_url}/embed", json={"inputs": "query: " + text})
            r.raise_for_status()
            return r.json()[0]
        except Exception:
            log.warning("semantic cache: embeddings unavailable")
            return None

    async def lookup(self, scope: str, text: str) -> tuple[str, float] | None:
        """(відповідь, схожість) найближчого запису вище порогу або None."""
        if not (self.enabled and eligible(text)):
            return None
        vec = await self._embed(text)
        if vec is None:
            return None
        try:
            items = await self._redis.lrange(scope, 0, -1)
        except Exception:
            log.warning("semantic cache: redis unavailable")
            return None
        now, best = self._clock(), None
        for raw in items:
            item = json.loads(raw)
            if now - item["ts"] > self.ttl_s:
                continue                       # протухло — як TTL точного кешу
            score = cosine(vec, item["v"])
            if score >= self.threshold and (best is None or score > best[1]):
                best = (item["content"], score)
        return best

    async def store(self, scope: str, text: str, content: str) -> None:
        if not (self.enabled and eligible(text)):
            return
        vec = await self._embed(text)
        if vec is None:
            return
        try:
            await self._redis.lpush(scope, json.dumps({"v": vec, "content": content, "ts": self._clock()},
                                                      ensure_ascii=False))
            await self._redis.ltrim(scope, 0, MAX_PER_SCOPE - 1)
            await self._redis.expire(scope, int(self.ttl_s))
        except Exception:
            log.warning("semantic cache: redis unavailable")
