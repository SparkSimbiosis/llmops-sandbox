"""R-1: real-режим на DeepSeek через gateway — без мережі (httpx.MockTransport замість gateway).

Перевіряємо: real лише явним запитом; real-аліаси з routing.yaml; гейт вихідного контексту й бюджет
зупиняють виклик ДО відправки (жодного HTTP-запиту, жодного рядка agent_calls); справжні ціни DeepSeek;
відповідь у ```-огорожі розгортається.
"""

import json
import uuid
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from sandbox.agent import outgoing, pipeline, pricing, routing, settings
from sandbox.api import routes_agent
from sandbox.llm.fake_provider import FakeProvider
from sandbox.llm.gateway_provider import GatewayProvider
from sandbox.llm.provider_router import LLMRouter
from sandbox.main import app

client = TestClient(app)
TASK = "Онови рік у підвалі сайту"


class FakeGateway:
    """Відповіді gateway за кроком: plan — JSON-план, propose — diff (обидва в ```-огорожі, як у живих моделей)."""

    def __init__(self, diff: str, files: list[str]):
        self.diff = diff
        self.files = files
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        system = body["messages"][0]["content"]
        if "JSON plan" in system:
            plan = {"intent": "footer_year", "files": self.files, "complexity": "simple"}
            content = "```json\n" + json.dumps(plan) + "\n```"
        else:
            content = "```diff\n" + self.diff + "```"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 200}})


@pytest.fixture
def real(mem, monkeypatch, tmp_path):
    """Оточення з AGENT_PROVIDER_MODE=real, змонтованим denylist і gateway на MockTransport."""
    (tmp_path / "denylist.txt").write_text("PrivateCodename\n", encoding="utf-8")
    monkeypatch.setattr(pipeline, "gate_rules", outgoing.load(str(tmp_path)))
    monkeypatch.setattr(routes_agent, "AGENT_PROVIDER_MODE", "real")
    monkeypatch.setattr(pipeline, "real_budget_usd", 1.0)
    patch = client.post("/agent/tasks", json={"task": TASK}).json()["patch"]   # mock-прогін дає валідний diff
    mem.tasks.clear()
    mem.calls.clear()
    gw = FakeGateway(patch["diff"], patch["files"])
    monkeypatch.setattr(pipeline, "llm", LLMRouter(FakeProvider(delay_ms=0), timeout_s=5, gateway=GatewayProvider(
        "agent", "http://gateway:4000", transport=httpx.MockTransport(gw.handler))))
    return gw


def test_env_mode_and_resolve_mode():
    assert settings.env_mode("real") == "real" and settings.env_mode("mock") == "mock"
    assert settings.env_mode("REAL") == "mock" and settings.env_mode(None) == "mock"
    assert settings.resolve_mode("real", None) == "mock"            # без явного запиту — ніколи не real
    assert settings.resolve_mode("real", "real") == "real"
    with pytest.raises(settings.RealModeDisabled):
        settings.resolve_mode("mock", "real")


def test_budget_cap_cannot_be_raised():
    assert settings.real_budget("50") == 1.0 and settings.real_budget("0.2") == 0.2
    assert settings.real_budget("-3") == 0.0 and settings.real_budget("junk") == 1.0
    assert settings.real_budget(None) == 1.0 and settings.AGENT_REAL_BUDGET_USD <= settings.AGENT_REAL_BUDGET_CAP_USD


def test_real_aliases_come_from_routing_yaml():
    assert routing.route_task("plan", mode="real") == "deepseek-chat"
    assert routing.route_task("propose", "simple", "real") == "deepseek-chat"
    assert routing.route_task("propose", "complex", "real") == routing.CLASSES["balanced"]["real"]
    assert routing.route_task("propose", "complex") == "fake-strong"              # mock не змінився


def test_real_task_proposes_with_real_prices(real, mem):
    d = client.post("/agent/tasks", json={"task": TASK, "mode": "real"}).json()
    assert (d["status"], d["mode"], d["model"], d["calls"]) == ("proposed", "real", "deepseek-chat", 2)
    assert [r["model"] for r in real.requests] == ["deepseek-chat", "deepseek-chat"]
    assert all(r["max_tokens"] == settings.AGENT_REAL_MAX_TOKENS for r in real.requests)
    one = pricing.cost_usd("deepseek-chat", 1000, 200)
    assert one == Decimal("0.000490") and Decimal(str(d["cost_usd"])) == one * 2
    cost = client.get("/agent/cost").json()
    assert cost["real_today_usd"] == pytest.approx(float(one * 2)) and cost["real_budget_usd"] == 1.0


def test_context_gate_blocks_before_any_request(real, mem):
    for task in ("Онови рік у підвалі. PrivateCodename", "Додай ключ sk-" + "a" * 24):
        d = client.post("/agent/tasks", json={"task": task, "mode": "real"}).json()
        assert (d["status"], d["calls"], d["cost_usd"]) == ("blocked_by_context_gate", 0, None)
        assert d["gates"][0]["gate"] == "outgoing_context" and "PrivateCodename" not in json.dumps(d["gates"])
    assert real.requests == [] and mem.calls == []


def test_gate_without_denylist_is_closed(real, mem, monkeypatch):
    monkeypatch.setattr(pipeline, "gate_rules", outgoing.load(""))
    d = client.post("/agent/tasks", json={"task": TASK, "mode": "real"}).json()
    assert d["status"] == "blocked_by_context_gate" and d["gates"][0]["hits"] == [{"rule": "gate_not_configured", "message": None}]
    assert real.requests == []


def test_budget_blocks_before_request(real, mem, monkeypatch):
    monkeypatch.setattr(pipeline, "real_budget_usd", 0.0)
    d = client.post("/agent/tasks", json={"task": TASK, "mode": "real"}).json()
    assert (d["status"], d["calls"]) == ("blocked_by_budget", 0)
    assert d["gates"][0]["gate"] == "real_budget" and real.requests == [] and mem.calls == []


def test_budget_counts_spent_today(real, mem, monkeypatch):
    client.post("/agent/tasks", json={"task": TASK, "mode": "real"})            # 2 виклики ~ $0.00098
    spent = float(pricing.cost_usd("deepseek-chat", 1000, 200) * 2)
    plan_estimate = 0.0025                                                     # ~ 2000 токенів виходу chat
    monkeypatch.setattr(pipeline, "real_budget_usd", spent + plan_estimate / 2)
    d = client.post("/agent/tasks", json={"task": TASK, "mode": "real", "cache": False}).json()
    assert d["status"] == "blocked_by_budget" and len(real.requests) == 2


def test_mock_tasks_never_touch_gateway(real, mem):
    d = client.post("/agent/tasks", json={"task": TASK}).json()
    assert (d["mode"], d["status"], d["model"]) == ("mock", "proposed", "fake-mini") and real.requests == []
    client.post("/agent/tasks", json={"task": TASK, "mode": "real"})
    assert [t["mode"] for t in client.get("/agent/tasks").json()["tasks"]] == ["real", "mock"]   # історія показує режим


def test_unfence_and_gate_rules():
    assert pipeline.unfence("```json\n{\"a\": 1}\n```") == '{"a": 1}\n'
    assert pipeline.unfence("```diff\n--- a\n+++ b\n```") == "--- a\n+++ b\n"     # \n наприкінці diff лишається
    assert pipeline.unfence("--- a\n+++ b\n") == "--- a\n+++ b\n"
    rules = outgoing.Rules(secrets=outgoing.load("").secrets, denylist=())
    assert outgoing.check([{"content": "x"}], rules) == [{"rule": "gate_not_configured", "message": None}]
    loaded = outgoing.load("")
    # фейкові значення збираємо з частин, щоб сам тест не спрацьовував у pre-push скані секретів
    assert any(p.search("pass" + "word = " + "abcdefgh" * 2) for p in loaded.secrets)
    assert any(p.search("postgresql" + "://u:p@h/db") for p in loaded.secrets)


def test_ui_sends_real_only_by_manual_switch_with_real_env():
    from pathlib import Path
    js = (Path(pipeline.__file__).resolve().parent / "ui" / "app.js").read_text(encoding="utf-8")
    assert 'provider === "real" && providerMode === "real" ? "real" : "mock"' in js
    assert 'setProvider("mock")' in js                       # після завантаження сторінки — завжди mock
    assert 'runTask(b.task, "mock")' in js                   # «Зламати» ніколи не шле маркери в real
    assert "const runMode = forceMode || taskMode()" in js and "blocked_by_context_gate" in js and "blocked_by_budget" in js
    assert "innerHTML" not in js


def test_bad_denylist_entry_closes_gate(tmp_path):
    (tmp_path / "denylist.txt").write_text("GoodName\n(broken[\n", encoding="utf-8")
    rules = outgoing.load(str(tmp_path))
    assert not rules.ready
    assert outgoing.check([{"content": "nothing private"}], rules)[0]["rule"] == "gate_not_configured"


def test_models_list_and_availability(monkeypatch):
    d = client.get("/agent/models").json()
    by = {m["model"]: m for m in d["models"]}
    assert {"fake-mini", "fake-strong", "deepseek-chat", "deepseek-reasoner"} <= set(by)
    assert by["fake-mini"]["available"] and by["fake-mini"]["side"] == "test"
    assert by["deepseek-reasoner"]["classes"] == ["reasoning"] and by["deepseek-reasoner"]["usd_per_1m"]["output"] == 2.19
    assert by["deepseek-chat"]["available"] is (d["provider_mode"] == "real")
    assert routing.models("real")[0]["available"] and not [m for m in routing.models("mock") if m["side"] == "real" and m["available"]]


def test_manual_model_in_mock_and_wrong_side_422(mem):
    d = client.post("/agent/tasks", json={"task": TASK, "model": "fake-strong"}).json()
    assert (d["status"], d["model"], d["model_choice"]) == ("proposed", "fake-strong", "manual")
    assert [c["model"] for c in mem.calls] == ["fake-mini", "fake-strong"]      # план — як завжди дешевою
    r = client.post("/agent/tasks", json={"task": TASK, "model": "deepseek-chat"})
    assert r.status_code == 422 and r.json()["error"] == "model_not_available"
    assert len(mem.tasks) == 1                                                  # відмова — до запису задачі
    assert client.post("/agent/tasks", json={"task": TASK}).json()["model_choice"] == "auto"


def test_manual_real_model_goes_through_budget_and_gate(real, mem):
    d = client.post("/agent/tasks", json={"task": TASK, "mode": "real", "model": "deepseek-reasoner"}).json()
    assert (d["status"], d["model"]) == ("proposed", "deepseek-reasoner")
    assert [r["model"] for r in real.requests] == ["deepseek-chat", "deepseek-reasoner"]
    assert client.post("/agent/tasks", json={"task": TASK, "mode": "real", "model": "fake-mini"}).status_code == 422
