"""Маршрутизація агента hw2: intent -> complexity -> аліас; plan завжди на fake-mini;
задача довша за 300 символів — complex; порядок fallback записано поруч із роутером."""

import json
import pathlib
import uuid

import pytest
from fastapi.testclient import TestClient

from sandbox.agent import routing
from sandbox.agent.routing import AGENT_FALLBACK_CHAINS, MODEL_BY_COMPLEXITY, route_task, task_complexity
from sandbox.main import app

client = TestClient(app)
EX = pathlib.Path(__file__).resolve().parents[2] / "examples" / "agent"


def example(name: str) -> dict:
    return json.loads((EX / name).read_text(encoding="utf-8"))


def test_route_task_table():
    assert route_task("plan") == "fake-mini"
    assert route_task("plan", "complex") == "fake-mini"            # службовий виклик — завжди дешевий
    assert route_task("propose", "simple") == "fake-mini"
    assert route_task("propose", "complex") == "fake-strong"
    assert route_task("propose", None) == "fake-mini"


def test_task_complexity_length_rule_beats_plan():
    short, long_ = "x" * 300, "x" * 301
    assert task_complexity(short, "simple") == "simple"
    assert task_complexity(short, "complex") == "complex"
    assert task_complexity(long_, "simple") == "complex"
    assert task_complexity(short, None) == "simple"
    assert task_complexity(short, "huge") == "simple"               # невідоме значення від planner


def test_fallback_chains_recorded_next_to_router():
    assert set(AGENT_FALLBACK_CHAINS) == set(MODEL_BY_COMPLEXITY.values())
    for primary, chain in AGENT_FALLBACK_CHAINS.items():
        assert chain[0] == primary and len(chain) >= 2 and len(set(chain)) == len(chain)
        assert set(chain) <= set(MODEL_BY_COMPLEXITY.values())
    src = pathlib.Path(routing.__file__).read_text(encoding="utf-8")
    assert "recorded in hw2, executed in hw4" in src


@pytest.mark.parametrize("name, intent, complexity, propose_model", [
    ("task_footer_year.json", "footer_year", "simple", "fake-mini"),
    ("task_footer_year_en.json", "footer_year", "simple", "fake-mini"),
    ("task_color_green.json", "color_change", "simple", "fake-mini"),
    ("task_header_login_uk.json", "header_login_button", "complex", "fake-strong"),
    ("task_header_login_ru.json", "header_login_button", "complex", "fake-strong"),
    ("task_analytics.json", "analytics_script", "complex", "fake-strong"),
])
def test_intent_to_complexity_to_alias(mem, name, intent, complexity, propose_model):
    d = client.post("/agent/tasks", json=example(name)).json()
    assert (d["intent"], d["complexity"], d["model"]) == (intent, complexity, propose_model)
    calls = mem.calls_of(uuid.UUID(d["task_id"]))
    assert [(c["step"], c["model"]) for c in calls] == [("plan", "fake-mini"), ("propose", propose_model)]
    assert mem.tasks[uuid.UUID(d["task_id"])]["complexity"] == complexity


def test_long_simple_task_goes_to_strong(mem):
    task = "Онови рік у підвалі сайту. " + "Будь ласка, перевір усе уважно. " * 10
    assert len(task) > 300
    d = client.post("/agent/tasks", json={"task": task}).json()
    assert (d["status"], d["intent"], d["complexity"], d["model"]) == \
        ("proposed", "footer_year", "complex", "fake-strong")
    calls = mem.calls_of(uuid.UUID(d["task_id"]))
    assert [c["model"] for c in calls] == ["fake-mini", "fake-strong"]


def test_unknown_task_has_only_cheap_plan_call(mem):
    d = client.post("/agent/tasks", json=example("task_unknown.json")).json()
    assert (d["status"], d["complexity"], d["model"], d["calls"]) == ("needs_clarification", "simple", "fake-mini", 1)


def test_plan_failure_leaves_complexity_unknown(mem):
    d = client.post("/agent/tasks", json=example("task_fail_503.json")).json()
    assert (d["status"], d["complexity"]) == ("provider_error", None)
    assert mem.tasks[uuid.UUID(d["task_id"])]["complexity"] is None
