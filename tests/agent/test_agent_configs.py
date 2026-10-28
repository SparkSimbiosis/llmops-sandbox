"""C-1: конфігурація агентів файлами (агенти, ролі, скіли, інструменти, рецепти контексту) і компілятор контексту."""

import importlib
import pathlib

from fastapi.testclient import TestClient

from sandbox.agent import builder, compiler, configs, context, pipeline
from sandbox.agent.prompts import AgentPromptRegistry
from sandbox.core.hashing import sha256_hex
from sandbox.main import app
from tests.agent.memory_repo import MemoryAgentRepo

client = TestClient(app)


def test_config_is_consistent_and_listed():
    assert configs.validate() == []
    c = client.get("/agent/config").json()
    assert c["errors"] == []
    assert [a["id"] for a in c["agents"]] == ["planner", "editor", "repo_editor", "chat", "explorer", "critic", "architect", "researcher", "documenter"]
    assert {a["id"] for a in c["agents"] if a["pipeline"]} == {"planner", "editor", "repo_editor", "chat", "explorer"}
    names = {f["name"] for f in c["files"]}
    assert {"agents.yaml", "tools.yaml", "context.yaml", "engines.yaml", "roles/editor.md",
            "skills/safe-diff/SKILL.md"} <= names
    assert all(f["sha256"] == sha256_hex(f["text"]) and f["path"].startswith("sandbox/agent/studio/") for f in c["files"])
    # кожен рушій має свою роль і пак промпту зі змінними, які конструктор уміє підставити
    for e in builder.ENGINES.values():
        assert e["role"] in configs.ROLES and set(e["template"]["variables"]) <= set(builder.SECTION_TITLES)


def test_validate_catches_broken_links(monkeypatch):
    bad = {**configs.AGENTS["editor"], "role": "ghost", "skills": ["nope"], "tools": ["rm_rf"], "context": "x",
           "contract": "y", "engine": "z", "model": {"class": "huge"}}
    monkeypatch.setitem(configs.AGENTS, "editor", bad)
    errors = configs.validate()
    for needle in ("'ghost'", "'nope'", "'rm_rf'", "'x'", "'y'", "'z'", "'huge'"):
        assert any(needle in e for e in errors), needle


def test_roles_and_skills_cannot_carry_format_markers(monkeypatch):
    # інакше «зламаний» промпт editor перестав би ламати крок: формат задає лише версіонований промпт
    monkeypatch.setitem(configs.ROLES, "editor", {**configs.ROLES["editor"], "body": "Reply with a unified diff."})
    assert any("unified diff" in e for e in configs.validate())


def test_tool_impls_resolve():
    for tid, t in configs.TOOLS.items():
        if t["impl"] is None:
            assert t["enabled"] is False and t["needs_approval"], tid
            continue
        # найдовший префікс, що імпортується як модуль, далі — атрибути
        parts = t["impl"].split(".")
        for i in range(len(parts), 0, -1):
            try:
                obj = importlib.import_module(".".join(parts[:i]))
            except ModuleNotFoundError:
                continue
            for name in parts[i:]:
                obj = getattr(obj, name)
            break
        else:
            raise AssertionError(tid)


def test_task_records_which_role_and_skills_were_used(mem):
    d = client.post("/agent/tasks", json={"task": "Онови рік у підвалі сайту"}).json()
    assert d["status"] == "proposed"
    assert [(a["agent"], a["role"]) for a in d["agents"]] == [("planner", "planner"), ("editor", "editor")]
    assert d["agents"][1]["role_sha256"] == configs.ROLES["editor"]["sha256"]
    assert [s["name"] for s in d["agents"][1]["skills"]] == ["safe-diff"]


def test_compile_planner_matches_pipeline_messages(mem):
    task = "Онови рік у підвалі сайту"
    r = client.post("/agent/config/compile", json={"agent": "planner", "task": task})
    assert r.status_code == 200
    c = r.json()
    assert [b["id"] for b in c["blocks"]] == ["prompt", "policy", "role", "skills", "task", "allowed_paths"]
    assert {b["trust"] for b in c["blocks"] if b["where"] == "system"} == {"instruction"}
    assert {b["trust"] for b in c["blocks"] if b["where"] == "user"} == {"data"}
    planner = pipeline_prompt("agent-planner")
    expected = context.plan_messages(context.system_text(planner, configs.system_extra("planner")), task)
    assert c["messages"] == expected                      # превʼю = реальний виклик байт у байт
    assert c["route"]["resolved_model"] == "fake-mini" and c["estimate"]["real_model"] == "deepseek-chat"
    assert c["estimate"]["prompt_tokens"] > 0 and c["estimate"]["real_upper_usd"] > 0
    # у тестах denylist не змонтовано: гейт закритий — real цей виклик не пропустив би
    assert c["outgoing_gate"] == {"ok": False, "hits": [{"rule": "gate_not_configured", "message": None}]}


def pipeline_prompt(name: str) -> str:
    import asyncio
    return asyncio.run(pipeline.registry.get_active(name)).body


def test_compile_editor_has_site_files_and_critic_needs_no_db(mem, monkeypatch):
    c = client.post("/agent/config/compile", json={"agent": "editor", "task": "Онови рік у підвалі",
                                                   "files": ["demo-site/footer.html", "../etc/passwd"]}).json()
    files = next(b for b in c["blocks"] if b["id"] == "site_files")
    assert files["ref"] == "demo-site/footer.html" and "<site_file" in files["text"]
    monkeypatch.setattr(pipeline, "repo", MemoryAgentRepo(down=True))
    monkeypatch.setattr(pipeline, "registry", AgentPromptRegistry(pipeline.repo))
    assert client.post("/agent/config/compile", json={"agent": "editor", "task": "x"}).status_code == 503
    crit = client.post("/agent/config/compile", json={"agent": "critic", "task": "Перевір diff"}).json()
    assert [b["id"] for b in crit["blocks"]] == ["engine_prompt", "policy", "role", "skills", "task", "patch"]
    assert compiler.PATCH_PLACEHOLDER in crit["messages"][1]["content"]
    assert (crit["route"]["resolved_model"], crit["estimate"]["real_model"]) == ("fake-strong", "deepseek-reasoner")


def test_compile_unknown_agent_404_and_bad_input_422():
    assert client.post("/agent/config/compile", json={"agent": "nope", "task": "x"}).status_code == 404
    assert client.post("/agent/config/compile", json={"agent": "planner", "task": ""}).status_code == 422
    assert client.post("/agent/config/compile", json={"agent": "planner", "task": "x", "mode": "live"}).status_code == 422


def test_engine_template_render_and_approve_threshold(mem):
    b = builder.build("architecture_engine", {"title": "Кеш сторінок", "task_goal": "Спроєктувати кеш для сайту",
                                              "constraints": ["без нових сервісів"]})
    assert "Ти — Architecture Engine" in b["prompt"] and "- без нових сервісів" in b["prompt"]
    assert "### ОБМЕЖЕННЯ" not in b["prompt"]             # поле вже в шаблоні — окремим розділом не дублюється
    assert b["review"]["missing_fields"].count("acceptance_criteria") == 1
    weak = builder.build("architecture_engine", {"title": "x"})
    assert weak["approvable"] is False and weak["review"]["quality_score"] < builder.APPROVE_THRESHOLD
    r = client.post("/agent/studio/save", json={"engine": "code_engine", "draft": {"title": "x"}})
    assert r.status_code == 422 and r.json()["approve_threshold"] == 70


def test_config_tab_markup():
    ui = pathlib.Path(configs.__file__).resolve().parent / "ui"
    html = (ui / "index.html").read_text(encoding="utf-8")
    js = (ui / "app.js").read_text(encoding="utf-8")
    assert 'data-tab="config"' in html and 'id="tab-config"' in html and 'id="cf-compile"' in html
    assert '<button class="tab" data-tab="config">' in html                 # видно в обох режимах
    assert '["config", "policy", "roles", "packs"].includes(name)' in js and '"/config/compile"' in js and "innerHTML" not in js
