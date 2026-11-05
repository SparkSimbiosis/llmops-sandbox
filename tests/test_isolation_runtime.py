"""Рантайм-ізоляція агента: прогони агента (збої, повтори, перемикання промптів) не торкаються
курсових репозиторіїв, gateway і стану курсового модуля. З hw3/hw4 сюди додаються CACHE, STATS, QUEUE, TICKETS."""

import asyncio
import importlib
import json
import sys

import asyncpg
import httpx
import pytest
from fastapi.testclient import TestClient

import sandbox.main
from sandbox.agent import pipeline as agent_pipeline
from sandbox.agent.prompts import AgentPromptRegistry
from sandbox.agent.repo import AgentDatabase, AgentDbUnavailable, AgentRepo
from sandbox.agent.state import AGENT_STATE
from sandbox.course import pipeline as course_pipeline
from sandbox.llm.fake_provider import FakeProvider
from sandbox.llm.gateway_provider import GatewayProvider
from sandbox.llm.provider_router import LLMRouter
from sandbox.prompts.registry import Registry
from tests.agent.memory_repo import MemoryAgentRepo
from tests.memory_prompts import MemoryPrompts

client = TestClient(sandbox.main.app)


class SpyPrompts(MemoryPrompts):
    def __init__(self):
        super().__init__()
        self.touched: list[str] = []

    async def fetch_active(self, name):
        self.touched.append("fetch_active")
        return await super().fetch_active(name)

    async def list_all(self):
        self.touched.append("list_all")
        return await super().list_all()

    async def activate(self, *args):
        self.touched.append("activate")
        return await super().activate(*args)


class SpyRequests:
    def __init__(self):
        self.rows: list = []

    async def insert_safe(self, *args):
        self.rows.append(args)


@pytest.fixture
def stack(monkeypatch):
    """Курс на шпигунах (реєстр, лог, gateway), агент — на репозиторії в пам'яті."""
    prompts, requests, gateway_calls = SpyPrompts(), SpyRequests(), []

    def handler(req: httpx.Request) -> httpx.Response:
        gateway_calls.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Вітаю!"}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    monkeypatch.setattr(course_pipeline, "registry", Registry(prompts, pause_s=0))
    monkeypatch.setattr(course_pipeline, "requests_repo", requests)
    monkeypatch.setattr(course_pipeline, "gateway",
                        GatewayProvider(name="course", base_url="http://gw", transport=httpx.MockTransport(handler)))
    mem = MemoryAgentRepo()
    monkeypatch.setattr(agent_pipeline, "repo", mem)
    monkeypatch.setattr(agent_pipeline, "registry", AgentPromptRegistry(mem))
    monkeypatch.setattr(agent_pipeline, "llm", LLMRouter(FakeProvider(delay_ms=0)))
    return prompts, requests, gateway_calls, mem


def course_state() -> dict:
    # ідентичність усіх об'єктів курсового модуля: агент нічого в ньому не підміняє й не створює
    return {k: id(v) for k, v in vars(course_pipeline).items()}


def test_agent_runs_do_not_touch_course(stack):
    prompts, requests, gateway_calls, mem = stack
    before, agent_before = course_state(), AGENT_STATE.snapshot()

    for _ in range(3):
        assert client.post("/agent/tasks", json={"task": "__fail_503 Онови рік у підвалі сайту"}).json()["status"] \
            == "provider_error"
    for _ in range(2):                                           # повтор тієї самої задачі
        assert client.post("/agent/tasks", json={"task": "Онови рік у підвалі сайту"}).json()["status"] == "proposed"
    client.post("/agent/prompts/agent-editor/v1/activate", json={})
    client.post("/agent/prompts/agent-editor/rollback", json={})
    for path in ("/agent/prompts", "/agent/site/files", "/agent/tasks"):
        assert client.get(path).status_code == 200

    assert prompts.touched == [] and requests.rows == [] and gateway_calls == []
    assert prompts.activations == [] and [r["active"] for r in prompts.rows] == [False, True]
    assert course_state() == before
    after = AGENT_STATE.snapshot()
    assert after["calls"] - agent_before["calls"] == 3 + 2       # повтор узяв обидва кроки з кешу (W3)
    assert after["call_errors"] - agent_before["call_errors"] == 3

    # курс після агента працює як завжди: версія курсового промпта не змінилася
    r = client.post("/chat", json={"message": "hello"})
    assert r.status_code == 200 and requests.rows[-1][2].version == "v2"


def test_agent_db_down_keeps_course_and_health(stack, monkeypatch):
    _, requests, _, _ = stack
    down = MemoryAgentRepo(down=True)
    monkeypatch.setattr(agent_pipeline, "repo", down)
    monkeypatch.setattr(agent_pipeline, "registry", AgentPromptRegistry(down))
    assert client.get("/agent/prompts").status_code == 503
    assert client.post("/agent/tasks", json={"task": "Change the brand color to green"}).status_code == 503
    assert client.get("/health").json() == {"status": "ok"}
    assert client.post("/chat", json={"message": "hello"}).status_code == 200 and len(requests.rows) == 1


class _NoSchemaConn:
    """З'єднання з БД, у якій немає агентських таблиць (том, створений до агентського блоку схеми)."""

    async def fetch(self, *args, **kwargs):
        raise asyncpg.exceptions.UndefinedTableError('relation "agent_prompts" does not exist')


class _Pool:
    def __init__(self):
        self.released = 0

    async def acquire(self, timeout=None):
        return _NoSchemaConn()

    async def release(self, conn):
        self.released += 1


def test_missing_agent_schema_is_503_while_course_works(stack, monkeypatch):
    _, requests, _, _ = stack
    pool = _Pool()

    async def create_pool(*args, **kwargs):
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    real = AgentRepo(AgentDatabase("dsn-not-used", pause_s=0))       # справжній репозиторій і пул агента
    monkeypatch.setattr(agent_pipeline, "repo", real)
    monkeypatch.setattr(agent_pipeline, "registry", AgentPromptRegistry(real))
    r = client.get("/agent/prompts")
    assert r.status_code == 503 and r.json() == {"error": "agent_db_unavailable"}
    assert pool.released == 1                                        # з'єднання повернуто в пул
    assert client.get("/health").status_code == 200
    assert client.post("/chat", json={"message": "hello"}).status_code == 200 and len(requests.rows) == 1


def test_agent_pool_is_lazy_small_and_retries_three_times(monkeypatch):
    attempts: list[dict] = []

    async def create_pool(*args, **kwargs):
        attempts.append(kwargs)
        raise OSError("connection refused")

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    db = AgentDatabase("dsn-not-used", pause_s=0)
    assert attempts == []                                            # на створенні об'єкта до БД не ходимо

    async def use():
        async with db.connection():
            pass

    with pytest.raises(AgentDbUnavailable):
        asyncio.run(use())
    assert len(attempts) == 3 and attempts[0]["max_size"] == 3 and attempts[0]["min_size"] == 0


def _agent_status(app) -> int:
    # mode=real відсікається до БД: 403 — маршрут агента є, 404 — його не підключено
    c = TestClient(app)
    assert c.get("/health").status_code == 200
    return c.post("/agent/tasks", json={"task": "x", "mode": "real"}).status_code


def test_kill_switch_and_broken_agent_import_keep_course(monkeypatch):
    try:
        with monkeypatch.context() as m:
            m.setenv("AGENT_ENABLED", "0")
            assert _agent_status(importlib.reload(sandbox.main).app) == 404
        with monkeypatch.context() as m:
            m.setitem(sys.modules, "sandbox.api.routes_agent", None)    # імпорт агента падає
            assert _agent_status(importlib.reload(sandbox.main).app) == 404
    finally:
        app = importlib.reload(sandbox.main).app                         # звичайний застосунок назад
    assert _agent_status(app) == 403
