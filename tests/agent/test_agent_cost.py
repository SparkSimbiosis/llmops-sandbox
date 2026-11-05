"""Вартість агента hw2: cost_usd кожного виклику, total_cost_usd задачі = сума викликів (NULL-правило),
числовий cost_usd у /agent/tasks і форма GET /agent/cost."""

import json
import pathlib
import uuid
from decimal import Decimal

from fastapi.testclient import TestClient

from sandbox.agent import pipeline
from sandbox.agent.prompts import AgentPromptRegistry
from sandbox.main import app
from tests.agent.memory_repo import MemoryAgentRepo

client = TestClient(app)
EX = pathlib.Path(__file__).resolve().parents[2] / "examples" / "agent"
UNIT = {"fake-mini": Decimal("0.000003"), "fake-strong": Decimal("0.000050")}


def post(name: str) -> dict:
    return client.post("/agent/tasks", json=json.loads((EX / name).read_text(encoding="utf-8"))).json()


def test_each_call_priced_by_its_model_and_task_is_the_sum(mem):
    d = post("task_header_login_uk.json")
    tid = uuid.UUID(d["task_id"])
    calls = mem.calls_of(tid)
    for c in calls:
        assert c["cost_usd"] == (c["prompt_tokens"] + c["completion_tokens"]) * UNIT[c["model"]]
    total = sum(c["cost_usd"] for c in calls)
    assert mem.tasks[tid]["total_cost_usd"] == total
    assert d["cost_usd"] == float(total)
    # propose на fake-strong дорожчий за plan на fake-mini
    assert calls[1]["cost_usd"] > calls[0]["cost_usd"]


def test_failed_call_has_null_cost_and_task_null_total(mem):
    d = post("task_fail_503.json")
    tid = uuid.UUID(d["task_id"])
    assert [c["cost_usd"] for c in mem.calls_of(tid)] == [None]
    assert mem.tasks[tid]["total_cost_usd"] is None and d["cost_usd"] is None


def test_garbage_answer_is_still_priced(mem):
    # відповідь моделі (хоч і сміття) — оцінена; NULL лише коли відповіді не було
    d = client.post("/agent/tasks", json={"task": "__garbage Онови рік у підвалі сайту"}).json()
    assert d["status"] == "needs_clarification"
    assert d["cost_usd"] is not None and d["cost_usd"] > 0


def test_task_routes_return_numeric_cost(mem):
    d = post("task_footer_year.json")
    t = client.get(f"/agent/tasks/{d['task_id']}").json()
    assert t["cost_usd"] == d["cost_usd"] and isinstance(t["cost_usd"], float)
    assert t["complexity"] == "simple"
    assert [c["cost_usd"] for c in t["calls"]] == [float(c["cost_usd"]) for c in mem.calls_of(uuid.UUID(d["task_id"]))]
    assert sum(Decimal(str(c["cost_usd"])) for c in t["calls"]) == Decimal(str(t["cost_usd"]))
    rows = client.get("/agent/tasks").json()["tasks"]
    assert rows[0]["cost_usd"] == d["cost_usd"]


def test_task_created_before_migration_shows_null(mem):
    d = post("task_footer_year.json")
    mem.tasks[uuid.UUID(d["task_id"])]["total_cost_usd"] = None     # рядок, створений до міграції hw2
    assert client.get("/agent/tasks").json()["tasks"][0]["cost_usd"] is None
    assert client.get(f"/agent/tasks/{d['task_id']}").json()["cost_usd"] is None


def test_agent_cost_empty_day(mem):
    d = client.get("/agent/cost").json()
    assert {k: d[k] for k in ("pricing", "today_usd", "real_today_usd", "real_budget_usd", "by_model")} == {
        "pricing": {"fake": "synthetic", "real": "deepseek_list_price"},
        "today_usd": 0.0, "real_today_usd": 0.0, "real_budget_usd": 1.0, "by_model": []}


def test_agent_cost_shape_and_sum(mem):
    tasks = [post("task_header_login_uk.json"), post("task_footer_year.json"), post("task_fail_503.json")]
    r = client.get("/agent/cost")
    assert r.status_code == 200
    d = r.json()
    assert {"pricing", "today_usd", "real_today_usd", "real_budget_usd", "by_model"} <= set(d)
    assert d["pricing"] == {"fake": "synthetic", "real": "deepseek_list_price"}
    assert (d["real_today_usd"], d["real_budget_usd"]) == (0.0, 1.0)
    # ендпоінт не бреше: сума дня = сума cost_usd задач = сума by_model
    total = sum(Decimal(str(t["cost_usd"])) for t in tasks if t["cost_usd"] is not None)
    assert Decimal(str(d["today_usd"])) == total
    assert sum(Decimal(str(m["cost_usd"])) for m in d["by_model"]) == total
    # на mini — 3 plan (один упав і коштує NULL) + propose підвалу, на strong — propose шапки; дорожча першою
    assert [(m["model"], m["calls"]) for m in d["by_model"]] == [("fake-strong", 1), ("fake-mini", 4)]
    assert all({"model", "calls", "cost_usd", "provider_attempts", "cache_hits", "unknown_cost_calls"} <= set(m) and isinstance(m["cost_usd"], float) for m in d["by_model"])


def test_agent_cost_db_down_is_503(monkeypatch):
    down = MemoryAgentRepo(down=True)
    monkeypatch.setattr(pipeline, "repo", down)
    monkeypatch.setattr(pipeline, "registry", AgentPromptRegistry(down))
    r = client.get("/agent/cost")
    assert r.status_code == 503 and r.json() == {"error": "agent_db_unavailable"}


def test_cost_summary_separates_real_synthetic_unknown_and_cache():
    from sandbox.agent.repo import cost_summary
    rows = [
        {"mode": "real", "cost_usd": Decimal("0.01"), "prompt_tokens": 10},
        {"mode": "mock", "cost_usd": Decimal("2")},
        {"mode": "real", "cost_usd": None},
        {"mode": "mock", "cost_usd": None},
        {"mode": "real", "cost_usd": Decimal("0"), "cache_hit": True, "prompt_tokens": 999},
    ]
    d = cost_summary(rows)
    assert d["real_estimate_usd"] == Decimal("0.01") and d["synthetic_usd"] == 2
    assert d["unknown_cost_calls"] == 2 and d["real_unknown_cost_calls"] == 1
    assert d["provider_attempts"] == 4 and d["cache_hits"] == 1 and d["records"] == 5
    assert d["real_provider_attempts"] == 2 and d["synthetic_provider_attempts"] == 2
    assert d["prompt_tokens"] == 10


def test_daily_cache_rows_do_not_increment_attempts(mem):
    post("task_footer_year.json")
    post("task_footer_year.json")
    d = client.get("/agent/cost").json()
    assert d["cache_hits"] > 0
    assert d["provider_attempts"] + d["cache_hits"] == sum(m["calls"] for m in d["by_model"])
    assert d["synthetic_today_usd"] == d["today_usd"] and d["real_today_usd"] == 0
