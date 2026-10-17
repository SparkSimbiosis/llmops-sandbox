"""(опц. hw3) Semantic cache: схоже питання — з кешу; цифри й інструменти — ні; збої Redis/ембеддингів не ламають /chat."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from sandbox.cache.semantic import SemanticCache, cosine, eligible
from sandbox.course import pipeline
from sandbox.llm.gateway_provider import GatewayProvider
from sandbox.main import app
from sandbox.observability.stats import STATS
from sandbox.prompts.registry import Registry
from tests.memory_prompts import MemoryPrompts

client = TestClient(app)

# «ембеддинги»: близькі за змістом фрази — близькі вектори
VECTORS = {
    "query: як скинути пароль?": [1.0, 0.05, 0.0],
    "query: як мені скинути свій пароль?": [0.99, 0.08, 0.0],
    "query: що ти вмієш?": [0.0, 1.0, 0.1],
}


def embed_transport(down=False):
    def handler(req):
        if down:
            return httpx.Response(503)
        text = json.loads(req.content)["inputs"].lower()
        return httpx.Response(200, json=[VECTORS.get(text, [0.0, 0.0, 1.0])])
    return httpx.MockTransport(handler)


class FakeRedis:
    def __init__(self, down=False):
        self.data, self.down = {}, down

    def _check(self):
        if self.down:
            raise ConnectionError("redis down")

    async def lrange(self, key, a, b):
        self._check(); return list(self.data.get(key, []))

    async def lpush(self, key, value):
        self._check(); self.data.setdefault(key, []).insert(0, value)

    async def ltrim(self, key, a, b):
        self._check(); self.data[key] = self.data[key][a:b + 1]

    async def expire(self, key, ttl):
        self._check()


def make(down_redis=False, down_embed=False, clock=None):
    return SemanticCache(enabled=True, redis_client=FakeRedis(down_redis), embed_url="http://emb",
                         threshold=0.92, ttl_s=3600, transport=embed_transport(down_embed),
                         clock=clock or (lambda: 1000.0))


def test_cosine_and_eligible():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0) and cosine([1, 0], [0, 1]) == 0
    assert eligible("як скинути пароль?") and not eligible("де замовлення #123?")


def test_similar_question_found_different_not():
    sc = make()
    asyncio.run(sc.store("s", "Як скинути пароль?", "Щоб скинути пароль…"))
    hit = asyncio.run(sc.lookup("s", "Як мені скинути свій пароль?"))
    assert hit is not None and hit[0] == "Щоб скинути пароль…" and hit[1] >= 0.92
    assert asyncio.run(sc.lookup("s", "Що ти вмієш?")) is None
    assert asyncio.run(sc.lookup("other-scope", "Як мені скинути свій пароль?")) is None   # промпт/модель у ключі


def test_ttl_and_failures_are_silent():
    t = {"now": 1000.0}
    sc = make(clock=lambda: t["now"])
    asyncio.run(sc.store("s", "Як скинути пароль?", "a"))
    t["now"] += 7200
    assert asyncio.run(sc.lookup("s", "Як скинути пароль?")) is None
    assert asyncio.run(make(down_redis=True).lookup("s", "Як скинути пароль?")) is None
    assert asyncio.run(make(down_embed=True).lookup("s", "Як скинути пароль?")) is None


@pytest.fixture
def stack(monkeypatch):
    seen = []

    def handler(req):
        seen.append(json.loads(req.content)["messages"][1]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "Щоб скинути пароль…"}}],
                                         "usage": {"prompt_tokens": 9, "completion_tokens": 4}})

    rows = []

    class Rows:
        async def insert_safe(self, request_id, model, active, latency_ms, usage, cost, status):
            rows.append(usage.prompt_tokens)

    monkeypatch.setattr(pipeline, "SEMANTIC", make())
    monkeypatch.setattr(pipeline, "registry", Registry(MemoryPrompts(), pause_s=0))
    monkeypatch.setattr(pipeline, "requests_repo", Rows())
    monkeypatch.setattr(pipeline, "gateway",
                        GatewayProvider(name="course", base_url="http://gw", transport=httpx.MockTransport(handler)))
    return seen, rows


def test_chat_semantic_hit(stack):
    seen, rows = stack
    client.post("/chat", json={"message": "Як скинути пароль?"})
    body = client.post("/chat", json={"message": "Як мені скинути свій пароль?"}).json()
    assert len(seen) == 1 and body["content"] == "Щоб скинути пароль…"
    assert rows[-1] == 0 and STATS.semantic_hits == 1 and STATS.hits == 1
