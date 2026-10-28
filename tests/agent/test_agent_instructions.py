"""I-1: реєстр інструкцій — розкладка каталогу, імпорт read-only з версіями, активна версія й share на проєкт."""

import pathlib

import pytest
from fastapi.testclient import TestClient

from sandbox.agent import instructions, pipeline
from sandbox.agent.prompts import AgentPromptRegistry
from sandbox.main import app
from tests.agent.memory_repo import MemoryAgentRepo

client = TestClient(app)
ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(monkeypatch):
    mem = MemoryAgentRepo()
    monkeypatch.setattr(pipeline, "repo", mem)
    monkeypatch.setattr(pipeline, "registry", AgentPromptRegistry(mem))
    return mem


def write(root: pathlib.Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_sample_layout_covers_all_five_kinds():
    found = instructions.scan(instructions.SAMPLE_DIR)
    assert {f.kind for f in found} == set(instructions.KINDS)
    assert ("skill", "safe-diff", "skills/safe-diff/SKILL.md") in {(f.kind, f.name, f.path) for f in found}
    assert all(f.path != "README.md" for f in found)


def test_scan_ignores_non_layout_large_and_git_files(tmp_path):
    write(tmp_path, "agents/ok.md", "role")
    write(tmp_path, "notes/random.md", "not an instruction")
    write(tmp_path, "skills/README.md", "index, not a skill")
    write(tmp_path, ".git/agents/x.md", "git internals")
    write(tmp_path, "agents/.env", "SECRET=1")
    write(tmp_path, "policy/big.md", "x" * (instructions.MAX_FILE_BYTES + 1))
    assert [(f.kind, f.name) for f in instructions.scan(tmp_path)] == [("role", "ok")]


def test_overrides_change_kind_and_bad_entries_are_ignored(tmp_path):
    write(tmp_path, "policy/MODES.md", "modes")
    ov = instructions.parse_overrides("policy/MODES.md=workflow, junk, policy/X.md=nope")
    assert ov == {"policy/MODES.md": "workflow"}
    assert [(f.kind, f.name) for f in instructions.scan(tmp_path, ov)] == [("workflow", "MODES")]


def test_git_ref_reads_loose_and_packed_refs(tmp_path):
    sha = "a" * 40
    write(tmp_path, ".git/HEAD", "ref: refs/heads/main\n")
    write(tmp_path, ".git/refs/heads/main", sha + "\n")
    assert instructions.git_ref(tmp_path) == sha
    (tmp_path / ".git/refs/heads/main").unlink()
    write(tmp_path, ".git/packed-refs", "# pack\n" + "b" * 40 + " refs/heads/main\n")
    assert instructions.git_ref(tmp_path) == "b" * 40
    assert instructions.git_ref(tmp_path / "nope") is None


def test_import_sample_then_reimport_creates_no_new_versions(repo):
    r = client.post("/agent/instructions/import", json={"source": "sample"})
    assert r.status_code == 200
    first = r.json()
    assert first["imported"] == first["created"] == len(instructions.scan(instructions.SAMPLE_DIR))
    again = client.post("/agent/instructions/import", json={"source": "sample"}).json()
    assert again["created"] == 0 and {i["version"] for i in again["items"]} == {"v1"}


def test_changed_text_becomes_next_version(repo, tmp_path, monkeypatch):
    write(tmp_path, "agents/executor.md", "first")
    monkeypatch.setenv("INSTRUCTIONS_DIR", str(tmp_path))
    assert client.post("/agent/instructions/import", json={"source": "local"}).json()["created"] == 1
    write(tmp_path, "agents/executor.md", "second")
    assert client.post("/agent/instructions/import", json={"source": "local"}).json()["items"][0]["version"] == "v2"
    vers = client.get("/agent/instructions/role/executor").json()["versions"]
    assert [v["version"] for v in vers] == ["v1", "v2"]
    assert client.get("/agent/instructions/role/executor/v1").json()["body"] == "first"
    assert (tmp_path / "agents/executor.md").read_text(encoding="utf-8") == "second"   # джерело не змінюється


def test_local_import_without_mount_is_409(repo, monkeypatch):
    monkeypatch.delenv("INSTRUCTIONS_DIR", raising=False)
    assert client.get("/agent/instructions/sources").json() == {"sample": True, "local": False}
    assert client.post("/agent/instructions/import", json={"source": "local"}).status_code == 409


def test_activation_and_share_are_per_project(repo):
    client.post("/agent/instructions/import", json={"source": "sample"})
    url = "/agent/instructions/role/executor"
    assert client.post(url + "/share", json={"project": "demo-site", "share": True}).status_code == 409   # ще не активна
    r = client.post(url + "/v1/activate", json={"project": "demo-site"})
    assert r.status_code == 200 and r.json()["previous_version"] is None
    assert client.post(url + "/share", json={"project": "demo-site", "share": True}).status_code == 200

    def item(project):
        rows = client.get("/agent/instructions", params={"project": project}).json()["items"]
        return next(i for i in rows if (i["kind"], i["name"]) == ("role", "executor"))

    assert (item("demo-site")["active_version"], item("demo-site")["share_with_model"]) == ("v1", True)
    assert (item("llm-ops")["active_version"], item("llm-ops")["share_with_model"]) == (None, False)


@pytest.mark.parametrize("call", [
    lambda: client.post("/agent/instructions/role/executor/v9/activate", json={"project": "demo-site"}),
    lambda: client.get("/agent/instructions/role/nope"),
    lambda: client.get("/agent/instructions/role/executor/v9"),
])
def test_unknown_items_are_404(repo, call):
    client.post("/agent/instructions/import", json={"source": "sample"})
    assert call().status_code == 404


def test_unknown_project_and_kind_are_rejected(repo):
    assert client.get("/agent/instructions", params={"project": "nope"}).status_code == 404
    assert client.post("/agent/instructions/role/executor/v1/activate", json={"project": "nope"}).status_code == 404
    assert client.get("/agent/instructions/secret/executor").status_code == 422


def test_instruction_routes_db_down_is_503(monkeypatch):
    monkeypatch.setattr(pipeline, "repo", MemoryAgentRepo(down=True))
    assert client.post("/agent/instructions/import", json={"source": "sample"}).status_code == 503
    assert client.get("/agent/instructions").status_code == 503


def test_migration_block_mirrors_file_and_is_additive():
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    body = (ROOT / "db" / "agent_migrations" / "i1_instructions.sql").read_text(encoding="utf-8")
    assert body in schema and "\r" not in body
    assert schema.index("-- === hw2 (кінець) ===") < schema.index("-- === hw-i1: instructions (початок) ===")
    code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("--"))
    assert "DROP" not in code and "ALTER" not in code and code.count("CREATE TABLE IF NOT EXISTS agent_instruction_") == 2
