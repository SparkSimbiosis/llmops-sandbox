"""Реальна модель (третій маршрут, бюджет) і опційні пункти hw3: TTL кешу, перевірка аргументів, timeout інструмента."""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from sandbox.cache.response_cache import CachedAnswer, ResponseCache
from sandbox.course import pipeline
from sandbox.llm.budget import decide
from sandbox.llm.gateway_provider import GatewayProvider
from sandbox.llm.pricing import cost_usd
from sandbox.llm.router import route
from sandbox.llm.types import Usage
from sandbox.main import app
from sandbox.prompts.registry import Registry
from sandbox.tools import runtime
from tests.memory_prompts import MemoryPrompts

client = TestClient(app)


@pytest.mark.parametrize("msg,real,expected", [
    ("Що ти можеш?", "", "mock-mini"),                      # реальна вимкнена — як у курсі
    ("Що ти можеш?", "deepseek-chat", "deepseek-chat"),     # «все інше» -> реальна
    ("Як скинути пароль?", "deepseek-chat", "mock-mini"),   # FAQ -> дешевий mock
    ("Де моє замовлення #12?", "deepseek-chat", "mock-mini"),
    ("поверніть гроші терміново", "deepseek-chat", "mock-strong"),  # ескалація -> HITL на mock
])
def test_three_routes(msg, real, expected):
    assert route(msg, "mock", real) == expected


def test_budget_thresholds():
    assert decide("deepseek-reasoner", 0.10, 1.0, "mock-mini") == "deepseek-reasoner"
    assert decide("deepseek-reasoner", 0.85, 1.0, "mock-mini") == "deepseek-chat"   # 80% -> дешевша
    assert decide("deepseek-chat", 0.85, 1.0, "mock-mini") == "deepseek-chat"
    assert decide("deepseek-chat", 1.00, 1.0, "mock-mini") == "mock-mini"           # 100% -> mock


def test_deepseek_has_price():
    assert cost_usd("deepseek-chat", Usage(1000, 1000)) == Decimal("0.001370")


class Spent:
    def __init__(self, value):
        self.value = value

    async def real_today_usd(self, models):
        return self.value


class Rows:
    def __init__(self):
        self.rows = []

    async def insert_safe(self, request_id, model, active, latency_ms, usage, cost, status):
        self.rows.append({"model": model, "cost": cost})


@pytest.fixture
def real(monkeypatch):
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": "реальна відповідь"}}],
                                         "usage": {"prompt_tokens": 20, "completion_tokens": 10}})

    rows = Rows()
    monkeypatch.setattr(pipeline, "REAL_MODEL", "deepseek-chat")
    monkeypatch.setattr(pipeline, "registry", Registry(MemoryPrompts(), pause_s=0))
    monkeypatch.setattr(pipeline, "requests_repo", rows)
    monkeypatch.setattr(pipeline, "gateway",
                        GatewayProvider(name="course", base_url="http://gw", transport=httpx.MockTransport(handler)))
    return seen, rows, monkeypatch


def test_general_question_goes_to_real_model_with_max_tokens(real):
    seen, rows, mp = real
    mp.setattr(pipeline, "cost_repo", Spent(0.0))
    client.post("/chat", json={"message": "Розкажи про ваш сервіс"})
    assert seen[-1]["model"] == "deepseek-chat" and seen[-1]["max_tokens"] == pipeline.REAL_MAX_TOKENS
    assert rows.rows[-1]["model"] == "deepseek-chat" and rows.rows[-1]["cost"] > 0


def test_budget_exhausted_falls_back_to_mock(real):
    seen, rows, mp = real
    mp.setattr(pipeline, "cost_repo", Spent(5.0))
    client.post("/chat", json={"message": "Розкажи про ваш сервіс 2"})
    assert seen[-1]["model"] == "mock-mini" and "max_tokens" not in seen[-1]


def test_cache_ttl_expires():
    t = {"now": 0.0}
    cache = ResponseCache(ttl_s=10, clock=lambda: t["now"])
    cache.put("k", CachedAnswer("a", None))
    t["now"] = 5
    assert cache.get("k") is not None
    t["now"] = 11
    assert cache.get("k") is None


@pytest.mark.parametrize("raw,ok", [("{}", True), ('{"order_id": "123"}', True),
                                    ("not json", False), ('{"order_id": 123}', False), ('{"x": "1"}', False)])
def test_tool_args_validation(raw, ok):
    assert (runtime.validate_args("lookup_order", raw) is not None) is ok


def test_invalid_args_refused_not_executed():
    out = asyncio.run(runtime.run_read_only("lookup_order", "not json"))
    assert out == runtime.TOOL_FAILED_TEXT


def test_tool_timeout(monkeypatch):
    import time

    def slow(args):
        time.sleep(0.5)
        return "late"

    monkeypatch.setitem(runtime.READ_ONLY_TOOLS, "lookup_order", slow)
    monkeypatch.setattr(runtime, "TOOL_TIMEOUT_S", 0.05)
    assert asyncio.run(runtime.run_read_only("lookup_order", "{}")) == runtime.TOOL_FAILED_TEXT
