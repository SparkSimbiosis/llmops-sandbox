"""Реєстр агентських промптів: чесний дефолт 'none', activate з аудитом, 404 без змін, rollback і 409."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from sandbox.agent import pipeline
from sandbox.agent.prompts import AGENT_FALLBACK_PROMPT, AgentPromptRegistry
from sandbox.core.hashing import sha256_hex
from sandbox.main import app
from tests.agent.memory_repo import MemoryAgentRepo, prompt_body

client = TestClient(app)


@pytest.fixture
def repo(monkeypatch):
    mem = MemoryAgentRepo()
    monkeypatch.setattr(pipeline, "repo", mem)
    monkeypatch.setattr(pipeline, "registry", AgentPromptRegistry(mem))
    return mem


def active(mem, name):
    return [r["version"] for r in mem.prompts if r["name"] == name and r["active"]]


def test_empty_registry_gives_none_and_fallback():
    p = asyncio.run(AgentPromptRegistry(MemoryAgentRepo(seed=False)).get_active("agent-editor"))
    assert (p.version, p.body) == ("none", AGENT_FALLBACK_PROMPT)
    assert p.version_id is None and p.sha256 == sha256_hex(AGENT_FALLBACK_PROMPT)


def test_fallback_prompt_sets_no_answer_format():
    # без формату відповіді фейкова модель не дає ні плану, ні diff — поломку видно в статусі
    low = AGENT_FALLBACK_PROMPT.lower()
    assert "json plan" not in low and "unified diff" not in low


def test_list_shape_and_order(repo):
    r = client.get("/agent/prompts")
    assert r.status_code == 200
    rows = r.json()["prompts"]
    assert [(p["name"], p["version"], p["active"]) for p in rows] == \
        [("agent-editor", "v1", False), ("agent-editor", "v2", True), ("agent-planner", "v1", True)]
    assert set(rows[0]) == {"name", "version", "active", "version_id", "sha256"}
    assert rows[1]["sha256"] == sha256_hex(prompt_body("agent-editor", "v2"))


def test_activate_unknown_version_is_404_without_changes(repo):
    r = client.post("/agent/prompts/agent-editor/v99/activate", json={})
    assert r.status_code == 404 and r.json() == {"error": "not found"}
    assert active(repo, "agent-editor") == ["v2"] and repo.activations == []


def test_activate_ok_shape_and_audit(repo):
    r = client.post("/agent/prompts/agent-editor/v1/activate", json={"quote": "drill"})
    assert r.status_code == 200
    assert r.json() == {"name": "agent-editor", "version": "v1", "active": True, "previous_version": "v2"}
    assert active(repo, "agent-editor") == ["v1"] and active(repo, "agent-planner") == ["v1"]
    assert repo.activations == [{"name": "agent-editor", "version": "v1", "previous_version": "v2",
                                 "actor": "anonymous", "approval_quote": "drill"}]


def test_rollback_without_history_is_409(repo):
    r = client.post("/agent/prompts/agent-editor/rollback", json={})
    assert r.status_code == 409 and r.json() == {"error": "no previous version"}
    assert active(repo, "agent-editor") == ["v2"] and repo.activations == []


def test_rollback_returns_to_previous_version(repo):
    client.post("/agent/prompts/agent-editor/v1/activate", json={})
    r = client.post("/agent/prompts/agent-editor/rollback", json={})
    assert r.status_code == 200
    assert r.json() == {"name": "agent-editor", "version": "v2", "active": True, "previous_version": "v1"}
    assert active(repo, "agent-editor") == ["v2"] and len(repo.activations) == 2


def test_activate_when_nothing_active_then_rollback_has_nowhere_to_go(repo):
    for r in repo.prompts:
        if r["name"] == "agent-editor":
            r["active"] = False                  # як UPDATE agent_prompts SET active=false WHERE name='agent-editor'
    r = client.post("/agent/prompts/agent-editor/v2/activate", json={})
    assert r.status_code == 200 and r.json()["previous_version"] is None
    assert client.post("/agent/prompts/agent-editor/rollback", json={}).status_code == 409


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1,2]"])
def test_activate_ignores_unparsable_body(repo, raw):
    r = client.post("/agent/prompts/agent-editor/v1/activate", content=raw,
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 200 and repo.activations[-1]["approval_quote"] is None


def test_db_unavailable_is_503(monkeypatch):
    mem = MemoryAgentRepo(down=True)
    monkeypatch.setattr(pipeline, "repo", mem)
    monkeypatch.setattr(pipeline, "registry", AgentPromptRegistry(mem))
    for method, path in [("get", "/agent/prompts"), ("post", "/agent/prompts/agent-editor/v1/activate"),
                         ("post", "/agent/prompts/agent-editor/rollback")]:
        r = getattr(client, method)(path)
        assert r.status_code == 503 and r.json() == {"error": "agent_db_unavailable"}
