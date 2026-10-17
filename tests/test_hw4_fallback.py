"""hw4 ч.1: ланцюг моделей з лічильником перемикань і ввічлива заглушка без вигаданих фактів."""

import json
import re

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


class Rows:
    def __init__(self):
        self.rows = []

    async def insert_safe(self, request_id, model, active, latency_ms, usage, cost_usd, status):
        self.rows.append({"model": model, "status": status, "cost": cost_usd, "pt": usage.prompt_tokens})


@pytest.fixture
def gw(monkeypatch):
    """codes: alias -> HTTP-код відповіді gateway; за замовчуванням 200."""
    codes, seen, rows = {}, [], Rows()

    def handler(req):
        alias = json.loads(req.content)["model"]
        seen.append(alias)
        code = codes.get(alias, 200)
        if code != 200:
            return httpx.Response(code, json={"error": "x"})
        return httpx.Response(200, json={"choices": [{"message": {"content": f"answer from {alias}"}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    monkeypatch.setattr(pipeline, "registry", Registry(MemoryPrompts(), pause_s=0))
    monkeypatch.setattr(pipeline, "requests_repo", rows)
    monkeypatch.setattr(pipeline, "gateway",
                        GatewayProvider(name="course", base_url="http://gw", transport=httpx.MockTransport(handler)))
    return codes, seen, rows


def test_first_model_fails_second_answers(gw):
    codes, seen, rows = gw
    codes["mock-mini"] = 503
    body = client.post("/chat", json={"message": "hello"}).json()
    assert seen == ["mock-mini", "mock"] and body["content"] == "answer from mock"
    assert STATS.fallback_events == 1 and STATS.consecutive_failures == 0
    assert rows.rows[-1]["model"] == "mock" and rows.rows[-1]["status"] == "200" and rows.rows[-1]["cost"] is not None


def test_429_is_also_retryable(gw):
    codes, seen, _ = gw
    codes["mock-strong"] = 429
    client.post("/chat", json={"message": "refund please"})
    assert seen == ["mock-strong", "mock-mini"]


def test_whole_chain_down_gives_polite_200(gw):
    codes, seen, rows = gw
    codes.update({"mock-mini": 503, "mock": 503})
    r = client.post("/chat", json={"message": "__fail_503 where is my order"})
    assert r.status_code == 200
    text = r.json()["content"]
    assert text == pipeline.DEGRADATION_TEXT
    assert not re.search(r"\d", text) and "тікет" not in text.lower()   # без вигаданих фактів
    assert rows.rows[-1]["status"] == "503" and rows.rows[-1]["cost"] is None
    assert STATS.fallback_events == 1 and STATS.consecutive_failures == 1


def test_non_retryable_error_does_not_switch(gw):
    codes, seen, _ = gw
    codes["mock-mini"] = 400
    client.post("/chat", json={"message": "hello"})
    assert seen == ["mock-mini"] and STATS.fallback_events == 0


def test_success_resets_consecutive_failures(gw):
    codes, _, _ = gw
    codes.update({"mock-mini": 503, "mock": 503})
    client.post("/chat", json={"message": "a"})
    client.post("/chat", json={"message": "b"})
    assert STATS.consecutive_failures == 2
    codes.clear()
    client.post("/chat", json={"message": "c"})
    assert STATS.consecutive_failures == 0


def test_failure_is_not_cached(gw):
    codes, seen, _ = gw
    codes.update({"mock-mini": 503, "mock": 503})
    client.post("/chat", json={"message": "same"})
    codes.clear()
    body = client.post("/chat", json={"message": "same"}).json()
    assert body["content"] == "answer from mock-mini"
