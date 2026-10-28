"""Конвеєр агента hw1: 1 задача = 2 виклики (plan, propose) з версіями промптів у кожному рядку,
статуси задачі, 403 на real, 422 на невалідне тіло, 503 при недоступній агентській БД.
З hw2 у відповіді є complexity і числовий cost_usd, а propose складної задачі йде на fake-strong
(деталі маршрутизації й вартості — test_agent_routing.py і test_agent_cost.py)."""

import json
import pathlib
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from sandbox.agent import pipeline
from sandbox.agent.prompts import AGENT_FALLBACK_PROMPT, AgentPromptRegistry
from sandbox.core.hashing import sha256_hex
from sandbox.llm.fake_provider import FakeProvider
from sandbox.llm.provider_router import LLMRouter
from sandbox.main import app
from tests.agent.memory_repo import MemoryAgentRepo, prompt_body, seed_files

client = TestClient(app)
EX = pathlib.Path(__file__).resolve().parents[2] / "examples" / "agent"

TASK_KEYS = {"task_id", "status", "task_text", "intent", "complexity", "mode", "base", "prompt", "model", "calls",
             "tokens", "cost_usd", "gates", "patch", "agents", "model_choice", "cache", "cache_hits", "approval",
             "message"}
CALL_KEYS = {"seq", "step", "attempt", "provider", "model", "prompt_version", "prompt_sha256", "prompt_tokens",
             "completion_tokens", "cost_usd", "latency_ms", "status", "cache_hit"}


def use_repo(monkeypatch, mem):
    monkeypatch.setattr(pipeline, "repo", mem)
    monkeypatch.setattr(pipeline, "registry", AgentPromptRegistry(mem))
    monkeypatch.setattr(pipeline, "llm", LLMRouter(FakeProvider(delay_ms=0)))
    return mem


@pytest.fixture
def mem(monkeypatch):
    return use_repo(monkeypatch, MemoryAgentRepo())


def example(name: str) -> dict:
    return json.loads((EX / name).read_text(encoding="utf-8"))


def post_task(body: dict):
    return client.post("/agent/tasks", json=body)


def test_header_task_is_proposed_with_two_versioned_calls(mem):
    r = post_task(example("task_header_login_uk.json"))
    assert r.status_code == 200
    d = r.json()
    assert set(d) == TASK_KEYS
    assert (d["status"], d["intent"], d["complexity"], d["model"], d["calls"]) == \
        ("proposed", "header_login_button", "complex", "fake-strong", 2)
    assert isinstance(d["cost_usd"], float) and d["cost_usd"] > 0
    assert (d["mode"], d["base"], d["prompt"]) == ("mock", "current", {"name": "agent-editor", "version": "v2"})
    assert [(g["gate"], g["ok"]) for g in d["gates"]] == [("parse_ok", True), ("allowed_paths", True),
        ("forbidden_patterns", True), ("size_limits", True), ("base_version", True)]
    assert d["approval"]["status"] == "pending"
    assert d["patch"]["files"] == ["demo-site/header.html", "demo-site/styles.css"]
    assert d["patch"]["sha256"] == sha256_hex(d["patch"]["diff"])
    assert "@@ -4,4 +4,5 @@" in d["patch"]["diff"] and "@@ -3,3 +3,4 @@" in d["patch"]["diff"]
    assert d["message"] == "Пропоную зміни: demo-site/header.html, demo-site/styles.css (2 додані рядки)."

    tid = uuid.UUID(d["task_id"])
    calls = mem.calls_of(tid)
    assert [(c["seq"], c["step"], c["provider"], c["model"], c["prompt_name"], c["prompt_version"],
             c["prompt_tokens"] > 0, c["status"]) for c in calls] == [
        (1, "plan", "fake", "fake-mini", "agent-planner", "v1", True, "200"),
        (2, "propose", "fake", "fake-strong", "agent-editor", "v2", True, "200")]
    assert calls[0]["prompt_sha256"] == sha256_hex(prompt_body("agent-planner", "v1"))
    assert calls[1]["prompt_sha256"] == sha256_hex(prompt_body("agent-editor", "v2"))

    task = mem.tasks[tid]
    assert (task["status"], task["prompt_version"], task["intent"]) == ("proposed", "v2", "header_login_button")
    assert task["base_versions"] == {"demo-site/header.html": 1, "demo-site/styles.css": 1}
    assert re.fullmatch(r"[0-9a-f]{64}", task["context_sha256"])
    assert task["total_prompt_tokens"] == sum(c["prompt_tokens"] for c in calls) == d["tokens"]["prompt"]
    assert task["task_text"] == example("task_header_login_uk.json")["task"]     # текст задачі зберігається


def test_same_context_gives_same_digest(mem):
    a = post_task(example("task_footer_year.json")).json()
    b = post_task(example("task_footer_year_en.json")).json()
    ta, tb = mem.tasks[uuid.UUID(a["task_id"])], mem.tasks[uuid.UUID(b["task_id"])]
    assert ta["context_sha256"] == tb["context_sha256"]      # ті самі файли й версії -> той самий відбиток


def test_fail_503_is_provider_error_with_http_200(mem):
    r = post_task(example("task_fail_503.json"))
    assert r.status_code == 200
    d = r.json()
    assert (d["status"], d["calls"], d["patch"]) == ("provider_error", 1, None)
    assert d["message"] == "Модель тимчасово недоступна: змін не запропоновано."
    calls = mem.calls_of(uuid.UUID(d["task_id"]))
    assert [(c["step"], c["status"], c["prompt_tokens"]) for c in calls] == [("plan", "503", 0)]
    assert mem.tasks[uuid.UUID(d["task_id"])]["error"] == "http_503"


def test_unknown_task_needs_clarification_without_propose(mem):
    d = post_task(example("task_unknown.json")).json()
    assert (d["status"], d["intent"], d["calls"]) == ("needs_clarification", "unknown", 1)
    assert d["prompt"] == {"name": "agent-editor", "version": None}


def test_editor_v1_gives_no_patch_and_logs_its_version(mem):
    assert client.post("/agent/prompts/agent-editor/v1/activate", json={}).json()["previous_version"] == "v2"
    d = post_task(example("task_footer_year.json")).json()
    assert d["status"] == "no_patch" and d["patch"] is None
    assert mem.calls_of(uuid.UUID(d["task_id"]))[-1]["prompt_version"] == "v1"


def test_broken_editor_registry_gives_no_patch_with_none(mem):
    for r in mem.prompts:
        if r["name"] == "agent-editor":
            r["active"] = False
    d = post_task(example("task_footer_year.json")).json()
    assert (d["status"], d["prompt"]["version"]) == ("no_patch", "none")
    last = mem.calls_of(uuid.UUID(d["task_id"]))[-1]
    assert (last["step"], last["prompt_version"], last["prompt_sha256"]) == \
        ("propose", "none", sha256_hex(AGENT_FALLBACK_PROMPT))


def test_no_change_on_current_but_proposed_on_seed_base(mem):
    footer = seed_files()["demo-site/footer.html"]
    mem.add_file_version("demo-site/footer.html", footer.replace("2025", "2026"))   # ніби вже застосовано
    current = post_task({"task": "Онови рік у підвалі сайту"}).json()
    assert current["status"] == "no_change" and current["patch"] is None
    seed = post_task({"task": "Онови рік у підвалі сайту", "base": "seed"}).json()
    assert (seed["status"], seed["base"]) == ("proposed", "seed")
    assert mem.tasks[uuid.UUID(seed["task_id"])]["base_versions"] == {"demo-site/footer.html": 1}


def test_real_mode_is_403_before_any_db_write(mem):
    r = post_task({"task": "Change the brand color to green", "mode": "real"})
    assert r.status_code == 403 and r.json() == {"error": "real_mode_disabled"}
    assert mem.tasks == {} and mem.calls == []


def test_explicit_mock_mode(mem):
    d = post_task({"task": "Change the brand color to green", "mode": "mock"}).json()
    assert (d["status"], d["mode"], d["intent"]) == ("proposed", "mock", "color_change")


@pytest.mark.parametrize("body", [
    {}, {"task": ""}, {"task": "x" * 2001}, {"task": "ok", "base": "other"}, {"task": "ok", "mode": "fast"},
    {"task": "a\u0000b"},
])
def test_invalid_body_is_422_without_task_row(mem, body):
    assert post_task(body).status_code == 422
    assert mem.tasks == {}


def test_agent_db_down_is_503(monkeypatch):
    use_repo(monkeypatch, MemoryAgentRepo(down=True))
    r = post_task({"task": "Change the brand color to green"})
    assert r.status_code == 503 and r.json() == {"error": "agent_db_unavailable"}


def test_db_failure_mid_run_leaves_task_received(monkeypatch):
    mem = use_repo(monkeypatch, MemoryAgentRepo(fail_on={"insert_call"}))
    r = post_task({"task": "Change the brand color to green"})
    assert r.status_code == 503
    assert [t["status"] for t in mem.tasks.values()] == ["received"]


def test_get_task_with_calls(mem):
    d = post_task(example("task_header_login_uk.json")).json()
    r = client.get(f"/agent/tasks/{d['task_id']}")
    assert r.status_code == 200
    t = r.json()
    assert (t["status"], t["intent"], t["complexity"], t["model"], t["cost_usd"]) == \
        ("proposed", "header_login_button", "complex", "fake-strong", d["cost_usd"])
    assert t["patch"]["files"] == ["demo-site/header.html", "demo-site/styles.css"]
    assert t["patch"]["sha256"] == d["patch"]["sha256"]
    assert [set(c) for c in t["calls"]] == [CALL_KEYS, CALL_KEYS]          # cost_usd — з hw2, cache_hit — з hw3
    assert [(c["seq"], c["step"], c["attempt"], c["prompt_version"]) for c in t["calls"]] == \
        [(1, "plan", 1, "v1"), (2, "propose", 1, "v2")]


@pytest.mark.parametrize("task_id", [str(uuid.uuid4()), "not-a-uuid"])
def test_get_unknown_task_is_404(mem, task_id):
    r = client.get(f"/agent/tasks/{task_id}")
    assert r.status_code == 404 and r.json() == {"error": "not found"}


def test_list_tasks_newest_first_with_cost(mem):
    first = post_task(example("task_footer_year.json")).json()
    second = post_task(example("task_unknown.json")).json()
    rows = client.get("/agent/tasks?limit=20").json()["tasks"]
    assert [r["task_id"] for r in rows] == [second["task_id"], first["task_id"]]
    assert set(rows[0]) == {"task_id", "created_at", "status", "intent", "mode", "model", "cost_usd"}
    assert rows[1]["model"] == "fake-mini"
    assert [r["cost_usd"] for r in rows] == [second["cost_usd"], first["cost_usd"]]
    assert all(isinstance(r["cost_usd"], float) and r["cost_usd"] > 0 for r in rows)
    assert len(client.get("/agent/tasks?limit=1").json()["tasks"]) == 1
    assert client.get("/agent/tasks?limit=0").status_code == 422


def test_site_files_routes(mem):
    files = seed_files()
    rows = client.get("/agent/site/files").json()["files"]
    assert [(f["path"], f["version"]) for f in rows] == [(p, 1) for p in sorted(files)]
    assert all(f["sha256"] == sha256_hex(files[f["path"]]) and f["bytes"] == len(files[f["path"]].encode())
               for f in rows)
    one = client.get("/agent/site/files/demo-site/footer.html").json()
    assert (one["path"], one["version"], one["content"]) == ("demo-site/footer.html", 1, files["demo-site/footer.html"])
    assert client.get("/agent/site/files/demo-site/footer.html?version=1").json() == one
    assert client.get("/agent/site/files/demo-site/footer.html?version=9").status_code == 404
    assert client.get("/agent/site/files/demo-site/nope.html").status_code == 404
