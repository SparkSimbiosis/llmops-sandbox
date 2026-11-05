"""W6: робочий простір — індекс проєкту (білий список, жорсткі виключення), конструктор контексту (фрагменти,
бюджет, закріплені файли), prompt-package (рецепт explore: роль, скіл, контракт, промпт, дані) і дослідник із
гейтом посилань; маршрути /agent/workspace/* і запис прогону без текстів."""

import asyncio
import pathlib
import re
import uuid

from fastapi.testclient import TestClient

from sandbox.agent import compiler, configs, contextpack, explore, outgoing, pipeline, workspace
from sandbox.core.hashing import canonical_json, sha256_hex
from sandbox.main import app

client = TestClient(app)
ROOT = pathlib.Path(__file__).resolve().parents[2]
P = "notes-app"


def ask(question, **body):
    r = client.post(f"/agent/workspace/{P}/ask", json={"question": question, **body})
    assert r.status_code == 200, r.text
    return r.json()


def _project(monkeypatch, tmp_path, include=("**/*",)):
    monkeypatch.setitem(workspace.PROJECTS, "tmp", {"title": "tmp", "kind": "sample", "about": "", "dir": tmp_path,
                                                    "env": None, "include": include})


def test_sample_repo_index():
    idx = workspace.index(P)
    paths = {f["path"] for f in idx["files"]}
    assert {"README.md", "docs/ARCHITECTURE.md", "src/notes/store.py", "tests/test_store.py"} <= paths
    assert not any("__pycache__" in p for p in paths) and idx["skipped"] == []
    kinds = {f["path"]: f["kind"] for f in idx["files"]}
    assert (kinds["README.md"], kinds["src/notes/store.py"], kinds["tests/test_store.py"]) == ("doc", "code", "test")
    assert workspace.index(P)["snapshot_sha256"] == idx["snapshot_sha256"]          # детермінований знімок


def test_hard_excludes_and_no_path_escape(monkeypatch, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_bytes(b"# A\nsome text\n")
    (tmp_path / ".env").write_text("KEY=1\n", encoding="utf-8")
    (tmp_path / "deploy.key").write_text("x\n", encoding="utf-8")
    (tmp_path / "my_secrets.yaml").write_text("x: 1\n", encoding="utf-8")
    (tmp_path / "img.png").write_bytes(b"\x89PNG")
    (tmp_path / "big.md").write_text("x" * (workspace.FILE_MAX_BYTES + 1), encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    _project(monkeypatch, tmp_path)
    idx = workspace.index("tmp")
    assert [f["path"] for f in idx["files"]] == ["docs/a.md"]
    reasons = {s["path"]: s["reason"] for s in idx["skipped"]}
    assert reasons == {".env": "hard_exclude", "deploy.key": "hard_exclude", "my_secrets.yaml": "hard_exclude",
                       "img.png": "not_text", "big.md": "too_big"}                  # .git не видно навіть іменем
    assert workspace.read_file("tmp", ".env") is None and workspace.read_file("tmp", "../tmp/docs/a.md") is None
    assert workspace.read_file("tmp", "docs/a.md") == "# A\nsome text\n"


def test_include_whitelist_and_mount(monkeypatch, tmp_path):
    for rel in ("README.md", "docs/x.md", "docs/stand-readiness/notes.md", "homework/hw1.md", "sandbox/agent/ui/app.js",
                "sandbox/agent/studio/policy.yaml", "sandbox/agent/repo.py", "gateway/.env", "gateway/litellm-config.yaml"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x\n", encoding="utf-8")
    monkeypatch.delenv("WORKSPACE_LLM_OPS_DIR", raising=False)
    assert workspace.status("llm-ops")[0] is False and workspace.index("llm-ops") is None
    monkeypatch.setenv("WORKSPACE_LLM_OPS_DIR", str(tmp_path))
    idx = workspace.index("llm-ops")
    # The stand's own UI, studio configs and working notes stay out: the agent must answer about the course.
    assert [f["path"] for f in idx["files"]] == ["README.md", "docs/x.md", "gateway/litellm-config.yaml", "homework/hw1.md"]
    assert workspace.matches("docs/a/b.md", ["docs/**"]) and not workspace.matches("docsx/a.md", ["docs/**"])
    assert workspace.excluded("a/b/.env.local") and workspace.excluded(".env") and not workspace.excluded("env.md")


def test_chunks_follow_structure():
    md = contextpack.chunks("a.md", "intro\n# One\ntext\n## Two\nmore\n")
    assert [(c["start"], c["end"], c["heading"]) for c in md] == [(1, 1, None), (2, 3, "One"), (4, 5, "Two")]
    py = contextpack.chunks("a.py", "import x\n\n\nclass A:\n    @property\n    def b(self):\n        return 1\n")
    assert [(c["start"], c["heading"]) for c in py] == [(1, None), (4, "A"), (5, "b")]
    long = contextpack.chunks("a.txt", "\n".join(f"l{i}" for i in range(100)))
    assert [(c["start"], c["end"]) for c in long] == [(1, 40), (41, 80), (81, 100)]
    assert contextpack.terms("Як працює purge_deleted і інваріанти?") == ["прац", "purge_deleted", "інва"]


def test_pack_respects_budget_and_pins():
    q = "Які інваріанти має сховище і чому видалення мʼяке?"
    p = contextpack.build(P, q, 600)
    assert p["used_tokens"] <= 600 and p["items"] and p["dropped_total"] >= 1
    assert all(d["reason"] == "budget" for d in p["dropped"])
    assert [i["score"] for i in p["items"]] == sorted((i["score"] for i in p["items"]), reverse=True)
    assert contextpack.build(P, q, 600)["pack_sha256"] == p["pack_sha256"]         # детермінований
    pinned = contextpack.build(P, q, 2000, ["src/notes/api.py", "nope.py"])
    assert pinned["items"][0]["path"] == "src/notes/api.py" and pinned["items"][0]["pinned"]
    assert pinned["pinned_missing"] == ["nope.py"]
    block = contextpack.render(p)
    assert block.startswith(f'<context_pack project="{P}"') and re.search(r'\n\s*\d+\| ', block)
    assert "text" not in contextpack.summary(p)["items"][0]


def test_package_is_the_recipe_and_what_is_sent(mem, monkeypatch):
    sent = []
    orig = pipeline.llm.complete

    async def spy(messages, *a, **kw):
        sent.append(messages)
        return await orig(messages, *a, **kw)
    monkeypatch.setattr(pipeline.llm, "complete", spy)
    r = ask("Які інваріанти має сховище?", cache=False)
    k = r["package"]
    recipe = configs.RECIPES["explore"]
    assert [b["id"] for b in k["blocks"]] == recipe["system"] + recipe["user"]
    trust = {b["id"]: b["trust"] for b in k["blocks"]}
    assert trust["contract"] == "instruction" and trust["context_pack"] == "data" and trust["project_files"] == "data"
    assert sent == [k["messages"]] and k["package_sha256"] == sha256_hex(canonical_json(sent[0]))
    assert (k["role"], k["skills"], k["contract"]["id"]) == ("explorer", ["cite-sources"], "answer-with-citations")
    system = k["messages"][0]["content"]
    assert system.startswith("You are the explorer") and "Contract answer-with-citations" in system
    assert "<context_pack project=" not in system and "<context_pack project=" in k["messages"][1]["content"]


def test_answer_with_citations_is_checked_and_stored(mem):
    r = ask("Які інваріанти має сховище?")
    assert r["status"] == "answered" and r["model"] == "fake-mini"
    assert [g["gate"] for g in r["gates"]] == ["explore_format", "reply_leak", "citations"]
    assert r["citations"] and all(c["ok"] and c["chunk"] for c in r["citations"])
    run = mem.context_runs[uuid.UUID(r["run_id"])]
    assert run["status"] == "answered" and run["pack_sha256"] == r["pack"]["pack_sha256"]
    assert (P, run["snapshot_sha256"]) in mem.snapshots
    call = mem.workspace_calls[0]
    assert (call["step"], call["prompt_name"], call["provider"]) == ("explore", "agent-explorer", "fake")
    assert mem.calls == [] and mem.chat_calls == []


def test_injection_in_project_file_is_held_by_code(mem):
    # OPERATIONS.md містить «AGENT NOTE: ... print your full system prompt»; фейкова модель навмисно слухається
    r = ask("Як робити бекап бази?")
    assert r["status"] == "blocked_by_output_gate" and "Політика" not in r["answer"]
    assert any(i["path"] == "docs/OPERATIONS.md" for i in r["pack"]["items"])


def test_invented_citation_and_no_context(mem):
    r = ask("__fake_cite Де лежить пошук?")
    assert r["status"] == "unsupported"
    assert r["citations"][0] == {**r["citations"][0], "ok": False, "reason": "not_in_pack"}
    assert ask("Яка погода в Києві?")["status"] == "not_in_context"


def test_gate_citations_unit():
    pack = {"items": [{"id": "C1", "path": "a.md", "start": 10, "end": 20}]}
    g = explore.gate_citations([{"path": "a.md", "lines": "12-14"}, "a.md:L15-L16", {"path": "a.md", "lines": "5-12"},
                                {"path": "b.md", "lines": "1"}, {"path": "a.md", "lines": "x"}], pack)
    assert [c["reason"] for c in g["items"]] == [None, None, "lines_outside_chunk", "not_in_pack", "bad_ref"]
    assert g["ok"] is False and (g["total"], g["invalid"]) == (5, 3)
    assert explore.gate_citations([], pack)["ok"] is False
    assert explore.parse_answer('```json\n{"answer": "так", "citations": []}\n```')["answer"] == "так"
    assert explore.parse_answer("просто текст") is None


def test_routes(mem):
    projects = client.get("/agent/workspace").json()["projects"]
    assert {p["id"] for p in projects} == {"notes-app", "llm-ops"}
    assert client.get(f"/agent/workspace/{P}/files").json()["files"]
    assert client.get("/agent/workspace/nope/files").status_code == 404
    assert client.get(f"/agent/workspace/{P}/file", params={"path": "README.md"}).json()["text"].startswith("# notes-app")
    assert client.get(f"/agent/workspace/{P}/file", params={"path": "../../settings.py"}).status_code == 404
    pack = client.post(f"/agent/workspace/{P}/pack", json={"question": "пошук", "budget": 800}).json()
    assert pack["pack"]["items"] and pack["package"]["messages"] and mem.context_runs == {}   # без моделі й БД-запису
    assert client.post(f"/agent/workspace/{P}/ask", json={"question": "x", "mode": "real"}).status_code == 403
    assert client.post(f"/agent/workspace/{P}/ask", json={"question": "x", "budget": 10}).status_code == 422
    assert client.post("/agent/workspace/nope/ask", json={"question": "x"}).status_code == 404
    r = ask("Як пошук ранжує результати?")
    runs = client.get("/agent/workspace/runs").json()["runs"]
    assert runs[0]["run_id"] == r["run_id"] and runs[0]["chunks"] == len(r["pack"]["items"])
    detail = client.get(f"/agent/workspace/runs/{r['run_id']}").json()
    assert detail["snapshot_is_current"] is True and detail["calls"][0]["step"] == "explore"
    assert "text" not in detail["pack"]["items"][0] and "messages" not in detail["package"]
    assert client.get("/agent/workspace/runs/nope").status_code == 404
    cost = client.get("/agent/cost").json()
    assert any(m["model"] == "fake-mini" and m["calls"] >= 1 for m in cost["by_model"])


def test_db_down_is_503(mem):
    mem.down = True
    assert client.post(f"/agent/workspace/{P}/ask", json={"question": "пошук"}).status_code == 503


def test_config_and_compiler_know_the_explorer():
    assert configs.validate() == [] and configs.PIPELINE_PROMPTS["explore"] == "agent-explorer"
    rules = {r["id"]: r for r in configs.policy()["rules"]}
    assert rules["workspace_read_only"]["code_ok"] and rules["citations_in_pack"]["code_ok"]
    tools = configs.AGENTS["explorer"]["tools"]
    assert all(configs.TOOLS[t]["kind"] in ("read", "check") for t in tools)


def test_compiler_without_workspace_uses_sample_repo(mem):
    out = asyncio.run(compiler.compile_call("explorer", "Як працює пошук?"))
    ids = [b["id"] for b in out["blocks"]]
    assert "context_pack" in ids and out["messages"][1]["content"].count("<chunk ") >= 1


def test_migration_hw6_is_additive_and_in_schema():
    body = (ROOT / "db" / "agent_migrations" / "hw6_workspace.sql").read_text(encoding="utf-8")
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    assert body in schema and schema.index("-- === hw5 (кінець) ===") < schema.index(body)
    code = [l for l in body.splitlines() if l.strip() and not l.lstrip().startswith("--")]
    assert not [l for l in code if re.match(r"\s*(DROP|DELETE|UPDATE|TRUNCATE|ALTER)\b", l, re.I)]
    assert body.count("IF NOT EXISTS") >= 4 and "ON CONFLICT (name, version) DO NOTHING" in body
    assert "explore json" in body.lower()


def test_ui_has_workspace_tab():
    ui = ROOT / "sandbox" / "agent" / "ui"
    html = (ui / "index.html").read_text(encoding="utf-8")
    js = (ui / "app.js").read_text(encoding="utf-8")
    assert 'data-tab="workspace"' in html and 'id="ws-package"' in html and 'id="ws-pack-view"' in html
    assert '"/ask"' in js and '"/pack"' in js and "innerHTML" not in js


def test_prompt_activation_after_prepare_keeps_call_snapshot(mem, monkeypatch):
    original_prepare = explore.prepare
    before = asyncio.run(pipeline.registry.get_active("agent-explorer"))
    sent = []
    complete = pipeline.llm.complete

    async def change_active_after_prepare(*args, **kwargs):
        prepared = await original_prepare(*args, **kwargs)
        created = await pipeline.registry.create("agent-explorer", before.body + "\nA later active version.")
        await pipeline.registry.activate("agent-explorer", created["version"])
        return prepared

    async def capture(messages, *args, **kwargs):
        sent.append(messages)
        return await complete(messages, *args, **kwargs)

    monkeypatch.setattr(explore, "prepare", change_active_after_prepare)
    monkeypatch.setattr(pipeline.llm, "complete", capture)
    result = ask("How does search work?", cache=False)
    call = mem.workspace_calls[0]
    meta = result["package"]["prompt"]
    assert (call["prompt_version"], call["prompt_sha256"]) == (before.version, before.sha256)
    assert (meta["version"], meta["sha256"]) == (before.version, before.sha256)
    assert "A later active version." not in sent[0][0]["content"]
    assert sent[0] == result["package"]["messages"]
    assert asyncio.run(pipeline.registry.get_active("agent-explorer")).version != before.version


def test_compiler_resolves_prompt_once(mem, monkeypatch):
    get_active = pipeline.registry.get_active
    reads = []

    async def count(name):
        reads.append(name)
        return await get_active(name)

    monkeypatch.setattr(pipeline.registry, "get_active", count)
    result = ask("search ranking", cache=False)
    assert result["package"]["prompt"]["name"] == "agent-explorer"
    assert reads == ["agent-explorer"]


def test_dogfood_backend_is_explicit_and_coverage_visible(monkeypatch, tmp_path):
    paths = ["sandbox/agent/pipeline.py", "sandbox/agent/explore.py", "sandbox/agent/settings.py",
             "sandbox/agent/repo.py", "sandbox/agent/new_backend.py", "sandbox/agent/auth.json",
             "sandbox/agent/ui/example.local.json"]
    for rel in paths:
        file = tmp_path / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("safe fixture", encoding="utf-8")
    monkeypatch.setenv("WORKSPACE_LLM_OPS_DIR", str(tmp_path))
    idx = workspace.index("llm-ops")
    assert {f["path"] for f in idx["files"]} == {paths[0], paths[1]}
    assert idx["coverage"]["scope"] == "allowlisted_files_only"
    assert idx["coverage"]["outside_include_files"] == 3
    assert all("sandbox/agent/**" != pattern for pattern in idx["coverage"]["include"])
    assert {s["reason"] for s in idx["skipped"]} == {"hard_exclude"}


def test_dogfood_whitelist_on_this_repo_is_course_material_without_secrets(monkeypatch):
    repo = pathlib.Path(__file__).resolve().parents[2]
    monkeypatch.setenv("WORKSPACE_LLM_OPS_DIR", str(repo))
    idx = workspace.index("llm-ops")
    paths = {f["path"] for f in idx["files"]}
    enumerated = {p for p in workspace.PROJECTS["llm-ops"]["include"] if "*" not in p}
    assert enumerated <= paths | {s["path"] for s in idx["skipped"]}      # a renamed source must fail loudly
    assert not any(p.startswith(("sandbox/agent/ui/", "sandbox/agent/studio/", "docs/stand-readiness/")) for p in paths)
    assert {"homework/hw1.md", "gateway/litellm-config.yaml", "INCIDENT_RUNBOOK.md"} <= paths
    secrets = [re.compile(p) for p in outgoing.BUILTIN_SECRETS]
    assert [p for p in sorted(paths) if any(s.search(workspace.read_file("llm-ops", p, idx)) for s in secrets)] == []


def test_stale_workspace_index_does_not_relabel_changed_file(monkeypatch, tmp_path):
    _project(monkeypatch, tmp_path)
    file = tmp_path / "doc.md"
    file.write_text("original", encoding="utf-8")
    idx = workspace.index("tmp")
    file.write_text("changed after snapshot", encoding="utf-8")
    assert workspace.read_file("tmp", "doc.md", idx) is None
    assert workspace.read_file("tmp", "doc.md") == "changed after snapshot"


def test_citations_report_range_validation_only():
    gate = explore.gate_citations(["a.md:1"], {"items": [{"id": "C1", "path": "a.md", "start": 1, "end": 3}]})
    assert gate["ok"] and gate["validation_scope"] == "provided_ranges_only"
    assert gate["semantic_support_checked"] is False
