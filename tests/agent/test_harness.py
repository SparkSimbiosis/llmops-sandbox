"""Harness tests: permissions, immutable preparation, single dispatch and profile enforcement."""
import asyncio
from copy import deepcopy
import pytest
from fastapi.testclient import TestClient
from sandbox.agent import harness, passports, pipeline
from sandbox.main import app

@pytest.fixture(autouse=True)
def preparations():
    harness.PREPARED.clear()
    yield
    harness.PREPARED.clear()

@pytest.mark.asyncio
async def test_research_prepares_without_provider_and_dispatches_once(mem, monkeypatch):
    calls = []
    async def answer(*args, **kwargs):
        calls.append(kwargs)
        return {"status": "answered", "gates": [{"ok": True}], "calls": 1}
    monkeypatch.setattr(harness.explore, "ask", answer)
    prepared = await harness.prepare("notes-app", "How are notes stored?", profile="thorough")
    assert not calls
    assert prepared["passport"]["limits"]["max_tokens"] == 1600
    context = prepared["context"]
    assert context["budget_tokens"] == prepared["passport"]["limits"]["context_budget"]
    assert context["files_total"] > 0 and context["items"] and context["used_tokens"] <= context["budget_tokens"]
    assert all("text" not in item for item in context["items"]), "selection summary carries no chunk texts"
    result = await harness.run(prepared["preparation_id"])
    assert result["status"] == "answered"
    assert calls[0]["execution_options"]["max_tokens"] == 1600
    assert calls[0]["prepared"]["package"]["package_sha256"] == prepared["packet"]["package_sha256"]
    with pytest.raises(harness.HarnessError, match="missing_or_expired"):
        await harness.run(prepared["preparation_id"])
    assert len(calls) == 1

@pytest.mark.asyncio
async def test_critical_needs_explicit_confirmation(mem, monkeypatch):
    prepared = await harness.prepare("notes-app", "Explain API", risk="critical")
    with pytest.raises(harness.HarnessError, match="critical_confirmation_required"):
        await harness.run(prepared["preparation_id"])
    assert prepared["preparation_id"] in harness.PREPARED

@pytest.mark.asyncio
async def test_config_change_rejects_before_provider(mem, monkeypatch):
    prepared = await harness.prepare("notes-app", "Explain API")
    original = passports.passport
    def changed(*args, **kwargs):
        data = original(*args, **kwargs)
        data["fingerprint"] = "changed"
        return data
    monkeypatch.setattr(passports, "passport", changed)
    with pytest.raises(harness.HarnessError, match="policy_changed"):
        await harness.run(prepared["preparation_id"])

@pytest.mark.asyncio
async def test_project_change_rejects_before_provider(mem, monkeypatch):
    prepared = await harness.prepare("notes-app", "Explain API")
    original = harness.workspace.index
    def changed(project):
        data = deepcopy(original(project)); data["snapshot_sha256"] = "changed"; return data
    monkeypatch.setattr(harness.workspace, "index", changed)
    with pytest.raises(harness.HarnessError, match="project_changed"):
        await harness.run(prepared["preparation_id"])

@pytest.mark.asyncio
async def test_concurrent_submission_calls_provider_once(mem, monkeypatch):
    calls = []
    async def answer(*args, **kwargs):
        calls.append(1); await asyncio.sleep(0)
        return {"status": "answered", "gates": []}
    monkeypatch.setattr(harness.explore, "ask", answer)
    prepared = await harness.prepare("notes-app", "Explain API")
    results = await asyncio.gather(*(harness.run(prepared["preparation_id"]) for _ in range(2)), return_exceptions=True)
    assert len(calls) == 1
    assert sum(isinstance(x, harness.HarnessError) for x in results) == 1

@pytest.mark.asyncio
async def test_dev_environment_blocks_demo_write_before_compilation(mem):
    with pytest.raises(harness.HarnessError, match="demo_changes_forbidden"):
        await harness.prepare("demo-site", "Add login", environment="staging")
    assert not mem.tasks

@pytest.mark.asyncio
async def test_demo_pipeline_accepts_frozen_prompt_and_profile(mem):
    prepared = await harness.prepare("demo-site", "Додай кнопку логіну в шапку")
    result = await harness.run(prepared["preparation_id"])
    assert result["result"]["status"] == "proposed"
    assert result["result"]["approval"]
    assert result["passport"]["limits"]["max_tokens"] == 600

@pytest.mark.asyncio
async def test_expired_preparation_cannot_run(mem, monkeypatch):
    prepared = await harness.prepare("notes-app", "Explain API")
    harness.PREPARED[prepared["preparation_id"]]["expires"] = -1
    with pytest.raises(harness.HarnessError, match="missing_or_expired"):
        await harness.run(prepared["preparation_id"])


def test_api_rejects_unknown_profiles_and_returns_cards(mem):
    with TestClient(app) as client:
        assert client.get("/agent/harness/projects").status_code == 200
        assert client.get("/agent/projects/notes-app/passport?profile=unknown").status_code == 422
        result = client.post("/agent/harness/prepare", json={"project": "notes-app", "goal": "Explain API", "mode": "invalid"})
        assert result.status_code == 422

@pytest.mark.asyncio
async def test_demo_editor_uses_prepared_base_after_planner_await(mem, monkeypatch):
    prepared = await harness.prepare("demo-site", "Додай кнопку логіну в шапку")
    original = pipeline.llm.complete
    original_files = mem.site_files
    planned = False
    async def complete(*args, **kwargs):
        nonlocal planned
        response = await original(*args, **kwargs)
        planned = True
        return response
    async def site_files(*args, **kwargs):
        if planned:
            raise AssertionError("editor re-read current files after the prepared base was pinned")
        return await original_files(*args, **kwargs)
    monkeypatch.setattr(pipeline.llm, "complete", complete)
    monkeypatch.setattr(mem, "site_files", site_files)
    result = await harness.run(prepared["preparation_id"])
    assert result["result"]["status"] == "proposed"

@pytest.mark.asyncio
async def test_blocked_editor_does_not_mark_planner_blocked_or_checker_completed(mem, monkeypatch):
    prepared = await harness.prepare("demo-site", "Додай кнопку логіну в шапку")
    async def blocked(*args, **kwargs):
        return 200, {"status": "blocked_by_budget", "calls": 1, "cache_hits": 0,
                     "gates": [{"gate": "real_budget", "ok": False}]}
    monkeypatch.setattr(pipeline, "run_task", blocked)
    result = await harness.run(prepared["preparation_id"])
    states = {s["role"]: s["state"] for s in result["stages"]}
    assert states == {"planner": "completed", "editor": "blocked", "checker": "not_reached", "human": "not_reached"}

@pytest.mark.asyncio
async def test_harness_cache_is_explicit_and_bypass_sends_again(mem):
    first = await harness.prepare("notes-app", "Explain the notes API", cache=True)
    a = await harness.run(first["preparation_id"])
    assert a["result"]["calls"] == 1
    second = await harness.prepare("notes-app", "Explain the notes API", cache=True)
    b = await harness.run(second["preparation_id"])
    assert b["result"]["calls"] == 0 and b["result"]["cache_hits"] == 1
    third = await harness.prepare("notes-app", "Explain the notes API", cache=False)
    c = await harness.run(third["preparation_id"])
    assert third["cache"] is False
    assert c["result"]["calls"] == 1 and c["result"]["cache_hits"] == 0
