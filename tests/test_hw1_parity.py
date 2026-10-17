"""Паритет з C#-стартером (service/Program.cs): форма /chat, обробка збоїв gateway, лог, заглушки.

Без мережі й БД: gateway підміняється через httpx.MockTransport, репозиторій — пам'яттю.
Ці тести не замінюють перевірок ментора, лише фіксують поведінку стартера.
"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from sandbox.course import pipeline
from sandbox.llm.gateway_provider import GatewayProvider
from sandbox.main import app
from sandbox.prompts.registry import Registry
from tests.memory_prompts import MemoryPrompts
from sandbox.storage.db import DbUnavailable
from sandbox.storage.repositories import RequestsRepo

# маршрути, що ще не ожили, і їхній дослівний текст заглушки; список скорочується з тижнями
PENDING_STUBS = {
    "/cost": "sum cost_usd for today + budget",
    "/observability": "aggregate from requests table",
    "/providers": "provider health",
    "/approvals": "pending HITL approvals",
}


class MemoryRequests:
    def __init__(self):
        self.rows: list[dict] = []

    async def insert_safe(self, request_id, model, active, latency_ms, usage, cost_usd, status):
        self.rows.append({"request_id": request_id, "model": model, "prompt_version": active.version,
                          "prompt_sha256": active.sha256, "latency_ms": latency_ms,
                          "prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
                          "cost_usd": cost_usd, "status": status})


class DownDatabase:
    """БД, до якої не можна підключитися: connection() одразу кидає DbUnavailable."""

    def connection(self):
        raise DbUnavailable("down")


def completion(content="Вітаю!", tool_calls=None, usage=True, drop_content=False):
    message = {"role": "assistant", "content": content}
    if drop_content:
        del message["content"]
    if tool_calls:
        message["tool_calls"] = [{"id": "call_" + n, "type": "function",
                                  "function": {"name": n, "arguments": "{}"}} for n in tool_calls]
    body = {"choices": [{"index": 0, "message": message}]}
    if usage:
        body["usage"] = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    return body


@pytest.fixture
def wire(monkeypatch):
    """Підміняє gateway і репозиторій; повертає (сховище запитів до gateway, репозиторій, setter відповіді)."""
    seen: list[dict] = []
    reply = {"handler": lambda req: httpx.Response(200, json=completion())}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return reply["handler"](request)

    repo = MemoryRequests()
    monkeypatch.setattr(pipeline, "registry", Registry(MemoryPrompts(), pause_s=0))
    monkeypatch.setattr(pipeline, "gateway",
                        GatewayProvider(name="course", base_url="http://gw", transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(pipeline, "requests_repo", repo)

    def set_reply(fn):
        reply["handler"] = fn

    return seen, repo, set_reply


client = TestClient(app)


@pytest.mark.parametrize("path,todo", sorted(PENDING_STUBS.items()))
def test_pending_stubs_verbatim(path, todo):
    r = client.get(path)
    assert r.status_code == 200
    assert r.json() == {"todo": todo}


def test_health_does_not_touch_db(monkeypatch):
    monkeypatch.setattr(pipeline, "requests_repo", RequestsRepo(DownDatabase()))
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_both_gateway_instances_report_provider_gateway():
    ok = httpx.MockTransport(lambda req: httpx.Response(200, json=completion()))
    for name in ("course", "agent"):
        res = asyncio.run(GatewayProvider(name=name, base_url="http://gw", transport=ok)
                          .complete([{"role": "user", "content": "hi"}], "mock", timeout_s=5))
        assert res.provider == "gateway" and res.ok


def test_chat_shape_and_log_row(wire):
    seen, repo, _ = wire
    r = client.post("/chat", json={"message": "Як скинути пароль?"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"request_id", "content", "tool", "latency_ms"}
    assert body["content"] == "Вітаю!" and body["tool"] is None and isinstance(body["latency_ms"], int)
    assert len(repo.rows) == 1
    row = repo.rows[0]
    assert str(row["request_id"]) == body["request_id"]
    assert (row["model"], row["prompt_tokens"], row["completion_tokens"], row["cost_usd"], row["status"]) == \
        ("mock", 7, 3, None, "200")


def test_payload_has_exactly_model_and_messages(wire):
    seen, _, _ = wire
    client.post("/chat", json={"message": "hello"})
    assert set(seen[0]) == {"model", "messages"}
    assert [m["role"] for m in seen[0]["messages"]] == ["system", "user"]
    assert seen[0]["messages"][1]["content"] == "hello"


def test_gateway_503_gives_unavailable_text_and_status(wire):
    _, repo, set_reply = wire
    set_reply(lambda req: httpx.Response(503, json={"error": "upstream"}))
    body = client.post("/chat", json={"message": "x"}).json()
    assert body["content"] == "Сервіс тимчасово недоступний." and body["tool"] is None
    assert repo.rows[-1]["status"] == "503" and repo.rows[-1]["prompt_tokens"] == 0


def test_network_error_logs_status_zero(wire):
    _, repo, set_reply = wire

    def boom(req):
        raise httpx.ConnectError("refused")

    set_reply(boom)
    body = client.post("/chat", json={"message": "x"}).json()
    assert body["content"] == "Сервіс тимчасово недоступний."
    assert repo.rows[-1]["status"] == "0"


@pytest.mark.parametrize("kwargs", [{"drop_content": True}, {"usage": False}])
def test_200_without_content_or_usage_is_unavailable_with_status_200(wire, kwargs):
    _, repo, set_reply = wire
    set_reply(lambda req: httpx.Response(200, json=completion(**kwargs)))
    body = client.post("/chat", json={"message": "x"}).json()
    assert body["content"] == "Сервіс тимчасово недоступний."
    assert repo.rows[-1]["status"] == "200" and repo.rows[-1]["prompt_tokens"] == 0


def test_null_content_becomes_empty_string(wire):
    _, _, set_reply = wire
    set_reply(lambda req: httpx.Response(200, json=completion(content=None)))
    assert client.post("/chat", json={"message": "x"}).json()["content"] == ""


def test_tool_call_name_is_returned_not_executed(wire):
    _, repo, set_reply = wire
    set_reply(lambda req: httpx.Response(
        200, json=completion(content="Перевіряю статус вашого замовлення…", tool_calls=["lookup_order"])))
    body = client.post("/chat", json={"message": "Де моє замовлення #123?"}).json()
    assert body["tool"] == "lookup_order"
    assert body["content"] == "Перевіряю статус вашого замовлення…"   # результату інструмента ще немає
    assert len(repo.rows) == 1


def test_log_failure_does_not_break_chat(wire, monkeypatch):
    # справжній RequestsRepo над недоступною БД: помилка запису ковтається, користувач отримує відповідь
    monkeypatch.setattr(pipeline, "requests_repo", RequestsRepo(DownDatabase()))
    r = client.post("/chat", json={"message": "x"})
    assert r.status_code == 200 and r.json()["content"] == "Вітаю!"


def test_insert_completes_before_handler_returns(wire):
    _, repo, _ = wire
    asyncio.run(pipeline.handle_chat("x"))
    # рядок уже є в момент повернення з обробника — не BackgroundTasks і не fire-and-forget
    assert len(repo.rows) == 1


@pytest.mark.parametrize("payload", [{}, {"msg": "x"}, "not json"])
def test_invalid_body_is_422_without_log_row(wire, payload):
    _, repo, _ = wire
    if isinstance(payload, str):
        r = client.post("/chat", content=payload, headers={"Content-Type": "application/json"})
    else:
        r = client.post("/chat", json=payload)
    assert r.status_code == 422
    assert repo.rows == []
