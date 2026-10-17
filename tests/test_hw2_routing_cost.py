"""hw2: маршрут за питанням (UA + EN), ціна за моделлю, GET /cost, записаний fallback-порядок."""

import asyncio
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from sandbox.course import pipeline
from sandbox.llm.pricing import PRICES_PER_1K, cost_usd
from sandbox.llm.router import FALLBACK_CHAINS, providers_for, route
from sandbox.llm.types import Usage
from sandbox.main import app
from sandbox.storage.db import DbUnavailable

client = TestClient(app)


@pytest.mark.parametrize("msg,model", [
    ("How do I reset my password?", "mock-mini"),
    ("Як скинути пароль?", "mock-mini"),
    ("поверніть гроші терміново", "mock-strong"),
    ("I want a refund, this is urgent", "mock-strong"),
    ("Хочу подати скаргу", "mock-strong"),
    ("This is a complaint", "mock-strong"),
])
def test_route_both_languages(msg, model):
    assert route(msg, "mock") == model


def test_route_real_model_is_passthrough():
    assert route("поверніть гроші", "gpt-5-mini") == "gpt-5-mini"


def test_strong_unit_price_is_an_order_higher():
    mini, strong = PRICES_PER_1K["mock-mini"], PRICES_PER_1K["mock-strong"]
    assert strong[0] / mini[0] > 10 and strong[1] / mini[1] > 10


def test_cost_values_and_unknown_model_is_null():
    assert cost_usd("mock-strong", Usage(19, 9)) == Decimal("0.000138")   # 19*0.0025/1000 + 9*0.01/1000
    assert cost_usd("mock-mini", Usage(19, 11)) == Decimal("0.000009")
    assert cost_usd("unknown-model", Usage(10, 10)) is None


def test_fallback_chains_have_two_distinct_aliases():
    for head, chain in FALLBACK_CHAINS.items():
        assert chain[0] == head and len(set(chain)) >= 2
    assert providers_for("mock") == ["mock-strong", "mock-mini", "mock"]


class FakeCost:
    def __init__(self, value=None, down=False):
        self.value, self.down = value, down

    async def today_usd(self):
        if self.down:
            raise DbUnavailable("down")
        return self.value


def test_cost_endpoint_shape(monkeypatch):
    monkeypatch.setattr(pipeline, "cost_repo", FakeCost(0.0016))
    r = client.get("/cost")
    assert r.status_code == 200 and r.json() == {"today_usd": 0.0016, "budget_usd": 5.0}


def test_cost_endpoint_db_down(monkeypatch):
    monkeypatch.setattr(pipeline, "cost_repo", FakeCost(down=True))
    assert client.get("/cost").status_code == 503
