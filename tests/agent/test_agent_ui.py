"""UI-0: сторінка стенда й редактор промптів — нова версія vN+1 неактивною, текст версії, журнал активацій."""

import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from sandbox.agent import pipeline
from sandbox.agent.prompts import AgentPromptRegistry
from sandbox.agent.repo import next_version_number
from sandbox.core.hashing import sha256_hex
from sandbox.main import app
from tests.agent.memory_repo import MemoryAgentRepo, prompt_body

client = TestClient(app)
UI = pathlib.Path(__file__).resolve().parents[2] / "sandbox" / "agent" / "ui"


@pytest.fixture
def repo(monkeypatch):
    mem = MemoryAgentRepo()
    monkeypatch.setattr(pipeline, "repo", mem)
    monkeypatch.setattr(pipeline, "registry", AgentPromptRegistry(mem))
    return mem


def test_ui_page_and_assets_are_served():
    r = client.get("/agent/ui")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "/agent/ui/app.js" in r.text
    assert client.get("/agent/ui/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/agent/ui/app.css").headers["content-type"].startswith("text/css")


@pytest.mark.parametrize("name", ["index.html", "../routes_agent.py", "..%2Fsettings.py", "nope.js"])
def test_ui_serves_only_listed_assets(name):
    assert client.get("/agent/ui/" + name).status_code == 404


def test_ui_calls_only_agent_routes_and_never_inner_html():
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert 'fetch("/agent" + path' in js and js.count("fetch(") == 1   # єдина точка виходу в мережу
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", js)


def test_next_version_number_is_numeric():
    assert next_version_number(["v1", "v2", "v10", "draft"]) == 11
    assert next_version_number([]) == 1


def test_create_version_is_next_and_inactive(repo):
    r = client.post("/agent/prompts/agent-editor", json={"body": "Reply with a unified diff, please."})
    assert r.status_code == 201
    assert r.json() == {"name": "agent-editor", "version": "v3", "created": True,
                        "sha256": sha256_hex("Reply with a unified diff, please.")}
    active = [p["version"] for p in repo.prompts if p["name"] == "agent-editor" and p["active"]]
    assert active == ["v2"]                                         # активна не змінилась
    assert repo.activations == []


def test_same_body_returns_existing_version(repo):
    r = client.post("/agent/prompts/agent-editor", json={"body": prompt_body("agent-editor", "v2")})
    assert r.status_code == 200 and r.json()["version"] == "v2" and r.json()["created"] is False
    assert len([p for p in repo.prompts if p["name"] == "agent-editor"]) == 2


def test_unknown_name_is_404_and_nothing_created(repo):
    before = len(repo.prompts)
    assert client.post("/agent/prompts/agent-nope", json={"body": "x"}).status_code == 404
    assert len(repo.prompts) == before


@pytest.mark.parametrize("body", [{}, {"body": ""}, {"body": "   "}, {"body": "a\x00b"}, {"body": "x" * 4001}])
def test_invalid_body_is_422(repo, body):
    assert client.post("/agent/prompts/agent-editor", json=body).status_code == 422


def test_get_version_body_and_404(repo):
    r = client.get("/agent/prompts/agent-editor/v1")
    assert r.status_code == 200
    assert r.json()["body"] == prompt_body("agent-editor", "v1") and r.json()["active"] is False
    assert client.get("/agent/prompts/agent-editor/v9").status_code == 404


def test_activations_log_newest_first(repo):
    client.post("/agent/prompts/agent-editor/v1/activate", json={})
    client.post("/agent/prompts/agent-editor/v2/activate", json={})
    log = client.get("/agent/prompts/agent-editor/activations").json()["activations"]
    assert [(a["previous_version"], a["version"]) for a in log] == [("v1", "v2"), ("v2", "v1")]


def test_new_version_then_activate_and_rollback(repo):
    v = client.post("/agent/prompts/agent-editor", json={"body": "Answer as a unified diff only."}).json()["version"]
    assert client.post(f"/agent/prompts/agent-editor/{v}/activate", json={}).status_code == 200
    assert client.post("/agent/prompts/agent-editor/rollback", json={}).json()["version"] == "v2"


def test_editor_routes_db_down_is_503(monkeypatch):
    mem = MemoryAgentRepo(down=True)
    monkeypatch.setattr(pipeline, "registry", AgentPromptRegistry(mem))
    assert client.post("/agent/prompts/agent-editor", json={"body": "x"}).status_code == 503
    assert client.get("/agent/prompts/agent-editor/v1").status_code == 503
    assert client.get("/agent/prompts/agent-editor/activations").status_code == 503


def test_projects_only_demo_site_available_until_p1():
    r = client.get("/agent/projects")
    assert r.status_code == 200
    data = r.json()
    assert data["provider_mode"] == "mock"
    assert [(p["id"], p["available"]) for p in data["projects"]] == \
        [("demo-site", True), ("llm-ops", False), ("test-repo", False)]
    assert all(p["reason"] for p in data["projects"] if not p["available"])   # UI показує, чому недоступно


def test_ui_is_agent_development_scenario_without_modes():
    html = (UI / "index.html").read_text(encoding="utf-8")
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert "data-mode=" not in html and "test-only" not in html and "setMode" not in js   # Тест/Робота прибрано
    assert 'id="project"' in html and 'id="console-link"' in html                     # LLMOps — курсова консоль
    for section in ("demo", "scenario", "lab", "settings", "control"):
        assert html.count(f'data-section="{section}"') == 1
    assert 'id="scenario-kind"' in html and 'id="demo-gallery"' in html
    for tab in ("task", "config", "prompts", "break", "cache"):
        assert f'data-tab="{tab}"' in html


