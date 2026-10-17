"""Регресія: відповідь запасної моделі не кешується під ключем основної.

Інакше після інциденту до години (TTL) користувачі отримували б відповідь слабкої моделі під виглядом сильної,
а в лозі на cache-hit стояла б основна модель.
"""

import json

import httpx
from fastapi.testclient import TestClient

from sandbox.course import pipeline
from sandbox.llm.gateway_provider import GatewayProvider
from sandbox.main import app
from sandbox.prompts.registry import Registry
from tests.memory_prompts import MemoryPrompts

client = TestClient(app)


def test_fallback_answer_is_not_served_from_cache_after_recovery(monkeypatch):
    state, seen, rows = {"strong_down": True}, [], []

    def handler(req):
        alias = json.loads(req.content)["model"]
        seen.append(alias)
        if alias == "mock-strong" and state["strong_down"]:
            return httpx.Response(503, json={"error": "x"})
        return httpx.Response(200, json={"choices": [{"message": {"content": f"answer from {alias}"}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    class Rows:
        async def insert_safe(self, request_id, model, active, latency_ms, usage, cost, status):
            rows.append((model, usage.prompt_tokens))

    monkeypatch.setattr(pipeline, "registry", Registry(MemoryPrompts(), pause_s=0))
    monkeypatch.setattr(pipeline, "requests_repo", Rows())
    monkeypatch.setattr(pipeline, "gateway",
                        GatewayProvider(name="course", base_url="http://gw", transport=httpx.MockTransport(handler)))

    msg = {"message": "this is urgent, please help"}            # маршрут -> mock-strong
    assert client.post("/chat", json=msg).json()["content"] == "answer from mock-mini"   # інцидент: відповіла запасна
    state["strong_down"] = False
    body = client.post("/chat", json=msg).json()                  # провайдер ожив
    assert body["content"] == "answer from mock-strong"           # не стара відповідь слабкої моделі з кешу
    assert rows[-1] == ("mock-strong", 5)                         # і в лозі — чесна модель, справжній виклик
