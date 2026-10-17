"""hw3: кеш з повним ключем (модель + промпт + повідомлення), окремі hit/miss, виконання lookup_order."""

import json
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from sandbox.course import pipeline
from sandbox.llm.gateway_provider import GatewayProvider
from sandbox.main import app
from sandbox.observability.stats import STATS
from sandbox.prompts.registry import Registry
from tests.memory_prompts import MemoryPrompts

client = TestClient(app)


class MemoryRequests:
    def __init__(self):
        self.rows = []

    async def insert_safe(self, request_id, model, active, latency_ms, usage, cost_usd, status):
        self.rows.append({"model": model, "prompt_version": active.version, "pt": usage.prompt_tokens,
                          "ct": usage.completion_tokens, "cost": cost_usd, "status": status})


@pytest.fixture
def stack(monkeypatch):
    calls = []
    reply = {"tool": None, "status": 200}

    def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        if reply["status"] != 200:
            return httpx.Response(reply["status"], json={"error": "x"})
        msg = {"content": "Перевіряю статус вашого замовлення…" if reply["tool"] else "Відповідь"}
        if reply["tool"]:
            msg["tool_calls"] = [{"id": "c1", "type": "function",
                                  "function": {"name": reply["tool"], "arguments": "{}"}}]
        return httpx.Response(200, json={"choices": [{"message": msg}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    rows = MemoryRequests()
    monkeypatch.setattr(pipeline, "registry", Registry(MemoryPrompts(), pause_s=0))
    monkeypatch.setattr(pipeline, "requests_repo", rows)
    monkeypatch.setattr(pipeline, "gateway",
                        GatewayProvider(name="course", base_url="http://gw", transport=httpx.MockTransport(handler)))
    return calls, rows, reply


def test_repeat_is_served_from_cache_with_zero_tokens(stack):
    calls, rows, _ = stack
    a = client.post("/chat", json={"message": "Як скинути пароль?"}).json()
    b = client.post("/chat", json={"message": "Як скинути пароль?"}).json()
    assert len(calls) == 1 and a["content"] == b["content"]
    assert (rows.rows[-1]["pt"], rows.rows[-1]["ct"], rows.rows[-1]["cost"], rows.rows[-1]["status"]) == \
        (0, 0, Decimal("0"), "200")
    assert (STATS.hits, STATS.misses) == (1, 1)


def test_prompt_change_is_a_miss(stack):
    calls, rows, _ = stack
    client.post("/chat", json={"message": "Як скинути пароль?"})
    client.post("/prompts/v1/activate", json={})
    client.post("/chat", json={"message": "Як скинути пароль?"})
    assert len(calls) == 2
    assert rows.rows[-1]["prompt_version"] == "v1" and rows.rows[-1]["pt"] > 0


def test_model_is_part_of_the_key(stack):
    calls, _, _ = stack
    client.post("/chat", json={"message": "hello"})
    client.post("/chat", json={"message": "hello, this is urgent"})   # інше повідомлення й модель
    assert len(calls) == 2


def test_failed_answer_is_not_cached(stack):
    calls, _, reply = stack
    reply["status"] = 503
    client.post("/chat", json={"message": "x"})
    reply["status"] = 200
    client.post("/chat", json={"message": "x"})
    assert len(calls) == 2


def test_lookup_order_result_is_in_answer_and_on_hit(stack):
    calls, rows, reply = stack
    reply["tool"] = "lookup_order"
    a = client.post("/chat", json={"message": "Де моє замовлення #123?"}).json()
    b = client.post("/chat", json={"message": "Де моє замовлення #123?"}).json()
    expected = "Перевіряю статус вашого замовлення… (статус: оплачено, доставку призначено)"
    assert a["content"] == expected and b["content"] == expected
    assert a["tool"] == b["tool"] == "lookup_order"
    assert len(calls) == 1 and rows.rows[-1]["pt"] == 0   # повтор теж з кешу, інструмент виконано знову


def test_create_ticket_is_never_cached(stack):
    calls, _, reply = stack
    reply["tool"] = "create_ticket"
    client.post("/chat", json={"message": "поверніть гроші"})
    client.post("/chat", json={"message": "поверніть гроші"})
    assert len(calls) == 2
