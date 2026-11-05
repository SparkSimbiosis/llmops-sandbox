"""hw5: /observability (3 числа з БД + 2 лічильники), /providers реагує на збої, __garbage -> чесний текст."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from sandbox.core.injection import detect_injection
from sandbox.course import pipeline
from sandbox.llm.breaker import BREAKER, OPEN_S
from sandbox.llm.gateway_provider import GatewayProvider
from sandbox.main import app
from sandbox.observability.stats import STATS
from sandbox.prompts.registry import Registry
from sandbox.storage.db import DbUnavailable
from tests.memory_prompts import MemoryPrompts

client = TestClient(app)


class FakeObs:
    def __init__(self, down=False):
        self.down = down

    async def today(self):
        if self.down:
            raise DbUnavailable("down")
        return {"requests": 42, "p95_ms": 17, "error_rate_pct": 7.1}


def test_observability_shape_all_numbers(monkeypatch):
    monkeypatch.setattr(pipeline, "obs_repo", FakeObs())
    STATS.hits, STATS.misses, STATS.fallback_events = 1, 3, 6
    body = client.get("/observability").json()
    for k in ("requests", "p95_ms", "error_rate_pct", "cache_hit_pct", "fallback_events"):
        assert isinstance(body[k], (int, float)) and not isinstance(body[k], bool), k
    assert body["cache_hit_pct"] == 25.0 and body["fallback_events"] == 6 and body["requests"] == 42
    assert "provider_5xx" in body["errors_by_reason"]


def test_observability_db_down(monkeypatch):
    monkeypatch.setattr(pipeline, "obs_repo", FakeObs(down=True))
    assert client.get("/observability").status_code == 503


@pytest.fixture
def gw(monkeypatch):
    state = {"mode": "ok"}

    def handler(req):
        if state["mode"] == "down":
            return httpx.Response(503, json={"error": "x"})
        content = "..." if state["mode"] == "garbage" else "ok"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 1}})

    class Rows:
        async def insert_safe(self, *a):
            pass

    monkeypatch.setattr(pipeline, "registry", Registry(MemoryPrompts(), pause_s=0))
    monkeypatch.setattr(pipeline, "requests_repo", Rows())
    monkeypatch.setattr(pipeline, "gateway",
                        GatewayProvider(name="course", base_url="http://gw", transport=httpx.MockTransport(handler)))
    return state


def _statuses():
    return {p["name"]: p["status"] for p in client.get("/providers").json()["providers"]}


def test_providers_status_is_per_model(gw):
    body = client.get("/providers").json()
    assert [p["name"] for p in body["providers"]] == ["mock-strong", "mock-mini", "mock"]
    assert all(p["status"] == "unknown" for p in body["providers"])        # ще жодної відповіді — не «ok»
    client.post("/chat", json={"message": "Як скинути пароль?"})
    assert _statuses() == {"mock-strong": "unknown", "mock-mini": "ok", "mock": "unknown"}
    gw["mode"] = "down"
    for i in range(3):
        client.post("/chat", json={"message": f"__fail_503 q{i}"})
    # збої йшли ланцюгом mock-mini -> mock; через mock-strong жоден запит не йшов — вона не degraded
    assert _statuses() == {"mock-strong": "unknown", "mock-mini": "degraded", "mock": "degraded"}
    assert STATS.errors["provider_5xx"] >= 3


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_providers_unknown_when_data_is_stale(gw, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(BREAKER, "clock", clock)
    gw["mode"] = "down"
    for i in range(3):
        client.post("/chat", json={"message": f"__fail_503 q{i}"})
    assert _statuses()["mock"] == "degraded"
    clock.t += OPEN_S + 1
    # вікно breaker'а минуло, але ніхто не перевірив, що mock ожила: unknown, а не зелене «ok»
    assert _statuses() == {"mock-strong": "unknown", "mock-mini": "unknown", "mock": "unknown"}
    gw["mode"] = "ok"
    client.post("/chat", json={"message": "q-fresh"})
    assert _statuses() == {"mock-strong": "unknown", "mock-mini": "ok", "mock": "unknown"}


def test_garbage_gets_honest_text_and_is_not_cached(gw):
    gw["mode"] = "garbage"
    body = client.post("/chat", json={"message": "__garbage Як скинути пароль?"}).json()
    assert body["content"] == pipeline.GARBAGE_TEXT and "переформулю" in body["content"]
    assert body["content"] != pipeline.DEGRADATION_TEXT
    gw["mode"] = "ok"
    assert client.post("/chat", json={"message": "__garbage Як скинути пароль?"}).json()["content"] == "ok"
    assert STATS.errors["garbage"] == 1


def test_dataset_rules():
    rows = [json.loads(l) for l in open("evals/golden.jsonl", encoding="utf-8") if l.strip()]
    starters = {"faq-1", "faq-2", "order-1", "refund-1", "safety-1", "caps-1"}
    assert starters <= {r["id"] for r in rows} and len(rows) == 17
    new = [r for r in rows if r["id"] not in starters]
    assert len(new) >= 4 and sum(1 for r in new if r.get("forbid") or r.get("expect_refusal")) >= 2
    # hw4 red-team: власні кейси на три родини прямих атак з уроку 8 — і детектор їх справді ловить
    redteam = [r for r in new if r["id"].startswith("redteam-")]
    assert len(redteam) == 3 and all(r.get("expect_refusal") for r in redteam)
    assert {detect_injection(r["input"]) for r in redteam} == {"role", "exfiltrate", "social"}
    # звичайні питання, схожі на атаку: guardrail не має на них відмовляти (гейт хибних відмов у run.py)
    benign = [r for r in new if r["id"].startswith("benign-")]
    assert len(benign) == 4 and all(r.get("expect") and r.get("forbid") == ["не можу"] for r in benign)
    assert all(detect_injection(r["input"]) is None for r in benign)
