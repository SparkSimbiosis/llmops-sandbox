"""B-1: конструктор промптів — рушії, картки контрактів, сценарій і роутинг з YAML; збережений промпт іде в конвеєр."""

import pathlib

import pytest
import yaml
from fastapi.testclient import TestClient

from sandbox.agent import builder, routing
from sandbox.main import app
from tests.agent.memory_repo import MemoryAgentRepo

client = TestClient(app)
UI = pathlib.Path(builder.__file__).resolve().parent / "ui"

EXAMPLE = {
    "title": "Кнопка входу в шапці",
    "task_goal": "Додати в шапку демо-сайту кнопку «Увійти», що веде на /login.",
    "background_context": "Демо-сайт: index.html, styles.css, app.js.",
    "constraints": ["лише index.html і styles.css"],
    "acceptance_criteria": ["кнопка видима", "посилання веде на /login"],
    "complexity": "complex",
}


def test_catalog_has_five_engines_cards_scenario_and_routing():
    c = client.get("/agent/studio").json()
    assert [e["id"] for e in c["engines"]] == ["research_engine", "architecture_engine", "code_engine",
                                               "critic_engine", "documentation_engine"]
    assert c["approve_threshold"] == 70
    ids = {k["id"] for k in c["contracts"]}
    assert {e["contract"] for e in c["engines"]} <= ids and {"mode-line", "commit-message"} <= ids
    assert {s["tab"] for s in c["scenario"]["steps"]} <= {"task", "history", "builder", "config"}
    assert set(c["routing"]["classes"]) == {"fast", "balanced", "reasoning"}
    assert c["routing"]["rules"][-1]["when"] == "default"


def test_routing_yaml_keeps_hw2_table():
    assert (routing.CHEAP_MODEL, routing.STRONG_MODEL, routing.LONG_TASK_CHARS) == ("fake-mini", "fake-strong", 300)
    assert routing.MODEL_BY_COMPLEXITY == {"simple": "fake-mini", "complex": "fake-strong"}
    assert routing.AGENT_FALLBACK_CHAINS == {"fake-mini": ("fake-mini", "fake-strong"),
                                             "fake-strong": ("fake-strong", "fake-mini")}


@pytest.mark.parametrize("complexity, chars, flags, cls, rule", [
    ("simple", 10, ["security_flaw"], "reasoning", "high_risk"),
    ("simple", 301, [], "balanced", "long_task"),
    ("complex", 10, [], "balanced", "complex"),
    ("simple", 10, [], "fast", "default"),
    (None, 300, [], "fast", "default"),
])
def test_decide_rules_top_down(complexity, chars, flags, cls, rule):
    d = routing.decide(complexity, chars, flags)
    assert (d["chosen_model_class"], d["rule"]) == (cls, rule)
    assert d["resolved_model"] == routing.CLASSES[cls]["test"] and d["reason"]
    if not flags:   # без ризику рішення = те, що конвеєр бере для propose
        assert d["resolved_model"] == routing.route_task("propose", routing.task_complexity("x" * chars, complexity))


def test_build_code_engine_example():
    r = client.post("/agent/studio/build", json={"engine": "code_engine", "draft": EXAMPLE})
    assert r.status_code == 200
    b = r.json()
    assert b["review"]["quality_score"] == 100 and b["review"]["missing_fields"] == []
    assert (b["route"]["chosen_model_class"], b["route"]["resolved_model"]) == ("balanced", "fake-strong")
    assert "unified diff" in b["prompt"] and "NO_CHANGE" in b["prompt"] and "### МЕТОДИКА" in b["prompt"]
    snap = yaml.safe_load(b["yaml"])["built_prompt"]
    assert snap["prompt"] == b["prompt"] and snap["sha256"] == b["sha256"] and snap["engine"] == "code_engine"


def test_review_flags_missing_fields_broad_scope_and_risks():
    empty = builder.build("critic_engine", {})["review"]
    assert empty["quality_score"] < 50
    assert {"title", "task_goal", "input_data", "constraints", "acceptance_criteria"} <= set(empty["missing_fields"])
    risky = builder.build("critic_engine", {"title": "Перевір весь сайт", "task_goal": "чи нема зовнішнього скрипта"})
    assert {"broad_scope_risk", "security_flaw"} <= set(risky["review"]["risk_flags"])
    assert risky["route"]["chosen_model_class"] == "reasoning"


def test_unknown_engine_is_404_and_bad_complexity_422():
    assert client.post("/agent/studio/build", json={"engine": "nope"}).status_code == 404
    assert client.post("/agent/studio/save", json={"engine": "nope"}).status_code == 404
    assert client.post("/agent/studio/build", json={"engine": "code_engine", "draft": {"complexity": "huge"}}).status_code == 422


def test_save_creates_inactive_editor_version_then_dedupes(mem):
    r = client.post("/agent/studio/save", json={"engine": "code_engine", "draft": EXAMPLE})
    assert r.status_code == 201
    v = r.json()["version"]
    assert r.json()["name"] == "agent-editor" and r.json()["route"]["resolved_model"] == "fake-strong"
    assert client.get(f"/agent/prompts/agent-editor/{v}").json()["active"] is False
    assert client.post("/agent/studio/save", json={"engine": "code_engine", "draft": EXAMPLE}).status_code == 200


def test_saved_prompt_drives_pipeline(mem):
    v = client.post("/agent/studio/save", json={"engine": "code_engine", "draft": EXAMPLE}).json()["version"]
    assert client.post(f"/agent/prompts/agent-editor/{v}/activate", json={}).status_code == 200
    d = client.post("/agent/tasks", json={"task": "Додай кнопку логіну в шапку"}).json()
    assert (d["status"], d["prompt"]["version"]) == ("proposed", v)


def test_save_engine_without_target_is_409_and_db_down_503(monkeypatch):
    assert client.post("/agent/studio/save", json={"engine": "critic_engine", "draft": EXAMPLE}).status_code == 409
    from sandbox.agent import pipeline
    from sandbox.agent.prompts import AgentPromptRegistry
    down = MemoryAgentRepo(down=True)
    monkeypatch.setattr(pipeline, "registry", AgentPromptRegistry(down))
    assert client.post("/agent/studio/save", json={"engine": "code_engine", "draft": EXAMPLE}).status_code == 503


def test_builder_tab_markup():
    html = (UI / "index.html").read_text(encoding="utf-8")
    js = (UI / "app.js").read_text(encoding="utf-8")
    assert 'data-tab="builder"' in html and 'id="tab-builder"' in html
    assert 'id="b-save"' in html
    assert 'name === "builder"' in js and "innerHTML" not in js
