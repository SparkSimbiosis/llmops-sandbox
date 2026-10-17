"""hw4 ч.2: create_ticket лише після approve (повтор -> 404, тікет один); email маскується до кешу й моделі."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from sandbox.core.pii import mask_pii
from sandbox.course import pipeline
from sandbox.llm.gateway_provider import GatewayProvider
from sandbox.main import app
from sandbox.prompts.registry import Registry
from sandbox.tools.runtime import TICKETS
from tests.memory_prompts import MemoryPrompts

client = TestClient(app)


class Rows:
    def __init__(self):
        self.rows = []

    async def insert_safe(self, request_id, model, active, latency_ms, usage, cost_usd, status):
        self.rows.append({"pt": usage.prompt_tokens})


@pytest.fixture
def stack(monkeypatch):
    seen, rows = [], Rows()

    def handler(req):
        body = json.loads(req.content)
        user = body["messages"][1]["content"]
        seen.append(user)
        msg = {"content": "Створюю тікет і ескалюю на оператора."} if "поверн" in user else {"content": "ok"}
        if "поверн" in user:
            msg["tool_calls"] = [{"id": "c", "type": "function", "function": {"name": "create_ticket", "arguments": "{}"}}]
        return httpx.Response(200, json={"choices": [{"message": msg}], "usage": {"prompt_tokens": 9, "completion_tokens": 3}})

    monkeypatch.setattr(pipeline, "registry", Registry(MemoryPrompts(), pause_s=0))
    monkeypatch.setattr(pipeline, "requests_repo", rows)
    monkeypatch.setattr(pipeline, "gateway",
                        GatewayProvider(name="course", base_url="http://gw", transport=httpx.MockTransport(handler)))
    return seen, rows


def test_ticket_waits_for_approval_and_is_created_once(stack):
    body = client.post("/chat", json={"message": "поверніть гроші"}).json()
    assert body["tool"] == "create_ticket" and body["content"] == pipeline.HITL_TEXT
    assert "#T-" not in body["content"] and TICKETS == {}            # до approve нічого не сталося

    q = client.get("/approvals").json()
    assert len(q["pending"]) == 1 and set(q["pending"][0]) == {"id", "action"} and q["completed"] == []
    aid = q["pending"][0]["id"]

    r1 = client.post(f"/approvals/{aid}/approve", json={})
    assert r1.status_code == 200 and r1.json()["result"].startswith("тікет #T-")
    r2 = client.post(f"/approvals/{aid}/approve")
    assert r2.status_code == 404 and r2.json() == {"error": "not found"}
    assert len(TICKETS) == 1

    q = client.get("/approvals").json()
    assert q["pending"] == [] and q["completed"][0]["result"] == r1.json()["result"]


def test_unknown_approval_is_404():
    assert client.post("/approvals/deadbeef/approve").status_code == 404


def test_each_refund_request_is_its_own_approval(stack):
    client.post("/chat", json={"message": "поверніть гроші"})
    client.post("/chat", json={"message": "поверніть гроші"})   # create_ticket не кешується
    assert len(client.get("/approvals").json()["pending"]) == 2


def test_mask_pii():
    assert mask_pii("мій email a@b.com, скиньте пароль") == "мій email [email], скиньте пароль"
    assert mask_pii("write to first.last+tag@sub.example.org now") == "write to [email] now"
    assert mask_pii("без адреси") == "без адреси"


def test_masking_happens_before_cache_and_model(stack):
    seen, rows = stack
    client.post("/chat", json={"message": "мій email a@b.com, скиньте пароль"})
    client.post("/chat", json={"message": "мій email c@d.net, скиньте пароль"})
    assert seen == ["мій email [email], скиньте пароль"]   # у модель пішов лише замаскований текст, один раз
    assert rows.rows[-1]["pt"] == 0                          # другий — з кешу
