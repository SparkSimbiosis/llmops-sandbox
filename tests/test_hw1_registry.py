"""Реєстр промптів (hw1): версія з БД, чесний дефолт 'none', 503 при недоступній БД, атомарна активація."""

import asyncio
import pathlib
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from sandbox.course import pipeline
from sandbox.llm.gateway_provider import GatewayProvider
from sandbox.main import app
from sandbox.prompts.registry import FALLBACK_PROMPT, Registry, render
from tests.memory_prompts import V1, MemoryPrompts

client = TestClient(app)


class MemoryRequests:
    def __init__(self):
        self.rows: list[dict] = []

    async def insert_safe(self, request_id, model, active, latency_ms, usage, cost_usd, status):
        self.rows.append({"prompt_name": active.name, "prompt_version": active.version,
                          "prompt_version_id": active.version_id, "prompt_sha256": active.sha256,
                          "status": status})


@pytest.fixture
def stack(monkeypatch):
    """Підміняє реєстр, gateway і лог; повертає (prompts, requests, system-промпти, що пішли в модель)."""
    prompts, requests, systems = MemoryPrompts(), MemoryRequests(), []

    def handler(req: httpx.Request) -> httpx.Response:
        import json
        body = json.loads(req.content)
        systems.append(body["messages"][0]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    monkeypatch.setattr(pipeline, "registry", Registry(prompts, pause_s=0))
    monkeypatch.setattr(pipeline, "requests_repo", requests)
    monkeypatch.setattr(pipeline, "gateway",
                        GatewayProvider(name="course", base_url="http://gw", transport=httpx.MockTransport(handler)))
    return prompts, requests, systems


def test_fallback_prompt_is_honest():
    # без «support»: mock на ньому відповідає «не знаю» — поломку реєстру видно
    assert "support" not in FALLBACK_PROMPT.lower()
    assert FALLBACK_PROMPT != V1


def test_empty_registry_gives_none_and_fallback_sha():
    active = asyncio.run(Registry(MemoryPrompts(seed=False), pause_s=0).get_active())
    assert active.version == "none" and active.version_id is None
    assert active.body == FALLBACK_PROMPT and active.sha256.startswith("e76dfffd99ba")


def test_chat_logs_active_version_and_sha(stack):
    prompts, requests, systems = stack
    assert client.post("/chat", json={"message": "hi"}).status_code == 200
    row = requests.rows[-1]
    assert (row["prompt_name"], row["prompt_version"]) == ("support-system", "v2")
    assert row["prompt_sha256"].startswith("984012c8139a") and row["prompt_version_id"] is not None
    assert systems[-1] == "You are a support assistant. Be concise and helpful."


def test_activate_v1_switches_log_version(stack):
    prompts, requests, systems = stack
    assert client.post("/prompts/v1/activate", json={}).status_code == 200
    client.post("/chat", json={"message": "hi"})
    assert requests.rows[-1]["prompt_version"] == "v1"
    assert requests.rows[-1]["prompt_sha256"].startswith("00b00008e83f")
    assert systems[-1] == V1


def test_broken_registry_uses_fallback_and_none(stack):
    prompts, requests, systems = stack
    for r in prompts.rows:
        r["active"] = False                     # як UPDATE prompts SET active=false;
    client.post("/chat", json={"message": "hi"})
    assert requests.rows[-1]["prompt_version"] == "none"
    assert systems[-1] == FALLBACK_PROMPT


def test_db_unavailable_chat_503_without_log_row(stack, monkeypatch):
    _, requests, systems = stack
    monkeypatch.setattr(pipeline, "registry", Registry(MemoryPrompts(down=True), pause_s=0))
    r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 503
    assert r.json()["error"] == "prompt_registry_unavailable" and "request_id" in r.json()
    assert requests.rows == [] and systems == []  # у модель не ходили, рядка в логу немає


def test_db_unavailable_prompts_and_activate_503(monkeypatch):
    monkeypatch.setattr(pipeline, "registry", Registry(MemoryPrompts(down=True), pause_s=0))
    assert client.get("/prompts").status_code == 503
    assert client.post("/prompts/v1/activate").status_code == 503


def test_get_prompts_shape(stack):
    body = client.get("/prompts").json()
    assert [(p["name"], p["version"], p["active"]) for p in body] == \
        [("support-system", "v1", False), ("support-system", "v2", True)]
    assert {"version_id", "sha256", "created_at"} <= set(body[0])
    assert body[0]["sha256"].startswith("00b00008e83f") and body[1]["sha256"].startswith("984012c8139a")


def test_activate_unknown_is_404_without_changes_or_audit(stack):
    prompts, _, _ = stack
    r = client.post("/prompts/v99/activate", json={})
    assert r.status_code == 404
    assert r.json() == {"error": "prompt_version_not_found", "version": "v99"}
    assert [r["active"] for r in prompts.rows] == [False, True] and prompts.activations == []


def test_activate_ok_shape_and_audit(stack):
    prompts, _, _ = stack
    r = client.post("/prompts/v1/activate", json={"quote": "rollback drill"})
    assert r.status_code == 200
    body = r.json()
    assert (body["name"], body["version"], body["previous_version"]) == ("support-system", "v1", "v2")
    assert [(v["version"], v["active"]) for v in body["versions"]] == [("v1", True), ("v2", False)]
    assert prompts.activations[-1] == {"name": "support-system", "version": "v1", "previous_version": "v2",
                                       "actor": "anonymous", "approval_quote": "rollback drill"}


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1,2]"])
def test_activate_ignores_unparsable_body(stack, raw):
    r = client.post("/prompts/v1/activate", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 200


def test_render_unknown_placeholder_raises():
    assert render("Hi {{name}}", {"name": "x"}) == "Hi x"
    with pytest.raises(KeyError):
        render("Hi {{who}}", {})


def test_no_prompt_seed_in_code():
    # seed версій живе лише в db/schema.sql; шаблон зібрано без літерала, щоб тест не знаходив сам себе
    pat = re.compile(r"insert\s+into\s+" + "prompts", re.I)
    root = pathlib.Path(__file__).resolve().parents[1]
    hits = [p for p in (root / "sandbox").rglob("*.py") if pat.search(p.read_text(encoding="utf-8"))]
    assert hits == []
