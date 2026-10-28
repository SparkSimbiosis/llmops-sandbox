"""Зміна з підтвердженням для git-проєкту: задача -> пакет контексту -> diff -> гейти -> заявка -> Approve -> .patch.

Робоча копія — тимчасова тека (WORKSPACE_LLM_OPS_DIR). Стенд у неї не пише: після Approve видається лише .patch.
"""

import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from sandbox.agent import gates, harness, pipeline
from sandbox.agent.patches import PatchParseError, apply_file, parse_unified_diff, unified_diff
from sandbox.llm import fake_fixtures as fx
from sandbox.main import app

client = TestClient(app)
RUNBOOK = ("# Incident runbook\n\n## Circuit breaker\n\nAfter 3 failures in a row the model is open for 30 s.\n"
           "Then one probe request every 5 s.\n\n## Fallback\n\nSecondary model.\n")
GOAL = "Додай у INCIDENT_RUNBOOK.md до розділу про circuit breaker примітку, як перевірити стан breaker"


@pytest.fixture
def ops(monkeypatch, tmp_path):
    (tmp_path / "INCIDENT_RUNBOOK.md").write_text(RUNBOOK, encoding="utf-8", newline="\n")
    (tmp_path / "README.md").write_text("# LLM Ops\n\nCourse repository.\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv("WORKSPACE_LLM_OPS_DIR", str(tmp_path))
    harness.PREPARED.clear()
    yield tmp_path
    harness.PREPARED.clear()


async def propose(goal=GOAL, **kwargs):
    prepared = await harness.prepare("llm-ops", goal, profile="thorough", kind="change", **kwargs)
    return prepared, await harness.run(prepared["preparation_id"])


# ---------- розбір і канонічний diff ----------

def test_recount_fixes_model_counters_but_strict_mode_does_not():
    text = "--- a/f.md\n+++ b/f.md\n@@ -1,2 +1,2 @@\n a\n+b\n c\n"
    with pytest.raises(PatchParseError):
        parse_unified_diff(text)
    patch = parse_unified_diff(text, recount=True)
    assert patch.recounted and patch.files[0].hunks[0].new_len == 3
    assert apply_file("a\nc\n", patch.files[0]) == "a\nb\nc\n"


def test_recount_accepts_blank_context_without_space_and_trailing_blank_lines():
    text = "--- a/f.md\n+++ b/f.md\n@@ -1,3 +1,4 @@\n a\n\n+b\n c\n\n\n"
    patch = parse_unified_diff(text, recount=True)
    assert apply_file("a\n\nc\n", patch.files[0]) == "a\n\nb\nc\n"


def test_canonical_diff_roundtrips():
    old, new = RUNBOOK, RUNBOOK.replace("Secondary model.\n", "Secondary model.\nThird line.\n")
    diff = unified_diff("INCIDENT_RUNBOOK.md", old, new)
    assert diff.startswith("--- a/INCIDENT_RUNBOOK.md\n+++ b/INCIDENT_RUNBOOK.md\n@@ ")
    assert apply_file(old, parse_unified_diff(diff).files[0]) == new
    assert unified_diff("x.md", old, old) == ""
    assert "\\ No newline at end of file" in unified_diff("x.md", "a\n", "a\nb")


# ---------- mock-модель ----------

def test_mock_repo_diff_uses_real_line_numbers_and_skips_unknown_types():
    body = "3| ## Circuit breaker\n4| \n5| After 3 failures.\n6| Then a probe.\n7| \n"
    diff = fx.repo_diff("Додай примітку", [("logo.svg", "1| <svg/>\n"), ("INCIDENT_RUNBOOK.md", body)])
    assert diff.startswith("--- a/INCIDENT_RUNBOOK.md\n+++ b/INCIDENT_RUNBOOK.md\n@@ -4,3 +4,5 @@\n")
    assert diff.endswith("+> " + fx.REPO_NOTE + "Додай примітку\n")
    assert fx.repo_diff("x", [("logo.svg", "1| <svg/>\n")]) == fx.NO_CHANGE
    assert fx.repo_diff("x", [("a.py", "1| def f():\n2|     return 1\n")]).endswith("+# " + fx.REPO_NOTE + "x\n")


# ---------- гейти .patch ----------

def _files():
    return [{"path": "INCIDENT_RUNBOOK.md", "content": RUNBOOK, "version": "v"}]


def test_repo_gates_pass_for_pack_file():
    diff = "--- a/INCIDENT_RUNBOOK.md\n+++ b/INCIDENT_RUNBOOK.md\n@@ -9,2 +9,3 @@\n \n Secondary model.\n+Third.\n"
    reports, patch, new = gates.run_repo_gates(diff, _files(), pipeline.gate_rules)
    assert all(r["ok"] for r in reports), reports
    assert [r["gate"] for r in reports] == ["parse_ok", "allowed_paths", "secrets", "size_limits", "base_version"]
    assert new["INCIDENT_RUNBOOK.md"].endswith("Secondary model.\nThird.\n")


def test_repo_gates_block_paths_outside_pack_and_secrets():
    outside = "--- a/README.md\n+++ b/README.md\n@@ -1,1 +1,2 @@\n # LLM Ops\n+x\n"
    reports, _patch, new = gates.run_repo_gates(outside, _files(), pipeline.gate_rules)
    failed = {r["gate"] for r in reports if not r["ok"]}
    assert "allowed_paths" in failed and new == {}
    key = "AKIA" + "0" * 16                      # шаблон ключа збирається в тесті, у репозиторії його немає
    secret = f"--- a/INCIDENT_RUNBOOK.md\n+++ b/INCIDENT_RUNBOOK.md\n@@ -10,1 +10,2 @@\n Secondary model.\n+{key}\n"
    reports, _patch, new = gates.run_repo_gates(secret, _files(), pipeline.gate_rules)
    report = next(r for r in reports if r["gate"] == "secrets")
    assert not report["ok"] and report["hits"][0]["rule"] == "secret_pattern" and new == {}
    assert key not in repr(report), "звіт гейта не повторює сам секрет"


# ---------- harness: дозволи ----------

@pytest.mark.asyncio
async def test_change_kind_needs_patch_permission(mem, ops):
    with pytest.raises(harness.HarnessError, match="patch_proposals_forbidden"):
        await harness.prepare("notes-app", GOAL, kind="change")
    with pytest.raises(harness.HarnessError, match="patch_proposals_forbidden"):
        await harness.prepare("llm-ops", GOAL, environment="staging", kind="change")
    with pytest.raises(harness.HarnessError, match="invalid_kind"):
        await harness.prepare("llm-ops", GOAL, kind="write")
    assert not mem.tasks and not harness.PREPARED


# ---------- повний шлях ----------

@pytest.mark.asyncio
async def test_change_proposes_patch_approval_without_touching_repository(mem, ops):
    prepared, out = await propose()
    assert prepared["kind"] == "change"
    assert [s["role"] for s in prepared["stages"]] == ["context_builder", "editor", "checker", "human"]
    assert prepared["packet"]["agent"] == "repo_editor"
    user = next(b for b in prepared["packet"]["blocks"] if b["id"] == "pack_paths")
    assert user["where"] == "user" and user["ref"] == "1 файл."
    assert prepared["context"]["items"][0]["path"] == "INCIDENT_RUNBOOK.md"

    r = out["result"]
    assert out["kind"] == "change" and r["status"] == "proposed", r
    assert all(g["ok"] for g in r["gates"])
    assert r["patch"]["files"] == ["INCIDENT_RUNBOOK.md"] and fx.REPO_NOTE in r["patch"]["diff"]
    assert r["approval"]["action"] == "export_patch:llm-ops" and r["approval"]["status"] == "pending"
    assert {s["role"]: s["state"] for s in out["stages"]}["human"] == "waiting"
    task = mem.tasks[next(iter(mem.tasks))]
    assert task["intent"] == "repo_patch:llm-ops" and task["context_sha256"] == prepared["packet"]["pack_sha256"]
    assert [c["step"] for c in mem.calls_of(task["task_id"])] == ["propose"]
    assert (ops / "INCIDENT_RUNBOOK.md").read_text(encoding="utf-8") == RUNBOOK

    aid = r["approval"]["approval_id"]
    assert client.get(f"/agent/approvals/{aid}/patch").status_code == 409
    listed = client.get("/agent/approvals?status=pending").json()["approvals"]
    assert listed[0]["project"] == "llm-ops"
    approved = client.post(f"/agent/approvals/{aid}/approve")
    assert approved.status_code == 200
    assert approved.json()["download"] == f"/agent/approvals/{aid}/patch"
    patch = client.get(f"/agent/approvals/{aid}/patch")
    assert patch.status_code == 200 and patch.headers["content-type"].startswith("text/x-diff")
    assert patch.headers["content-disposition"] == f'attachment; filename="llm-ops-{aid[:8]}.patch"'
    assert patch.text == r["patch"]["diff"]
    after = apply_file(RUNBOOK, parse_unified_diff(patch.text).files[0])
    assert "> " + fx.REPO_NOTE in after and after.startswith(RUNBOOK[:60])
    assert (ops / "INCIDENT_RUNBOOK.md").read_text(encoding="utf-8") == RUNBOOK, "Approve не пише в робочу копію"


@pytest.mark.asyncio
async def test_changed_working_copy_makes_approval_stale(mem, ops):
    _prepared, out = await propose()
    aid = out["result"]["approval"]["approval_id"]
    (ops / "INCIDENT_RUNBOOK.md").write_text(RUNBOOK + "Edited by a human.\n", encoding="utf-8", newline="\n")
    stale = client.post(f"/agent/approvals/{aid}/approve")
    assert stale.status_code == 409 and stale.json()["conflicts"][0]["path"] == "INCIDENT_RUNBOOK.md"
    assert client.get(f"/agent/approvals/{aid}").json()["status"] == "stale"
    assert client.get(f"/agent/approvals/{aid}/patch").status_code == 409


def test_site_approval_has_no_patch_download(mem):
    d = client.post("/agent/tasks", json={"task": "Онови рік у підвалі сайту"}).json()
    aid = d["approval"]["approval_id"]
    assert client.get(f"/agent/approvals/{aid}").json()["project"] is None
    assert client.get(f"/agent/approvals/{aid}/patch").status_code == 404
    assert client.get("/agent/approvals/not-a-uuid/patch").status_code == 404


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.asyncio
async def test_exported_patch_passes_git_apply_check(mem, ops):
    _prepared, out = await propose()
    aid = out["result"]["approval"]["approval_id"]
    client.post(f"/agent/approvals/{aid}/approve")
    (ops / "change.patch").write_text(client.get(f"/agent/approvals/{aid}/patch").text, encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q"], cwd=ops, check=True)
    check = subprocess.run(["git", "apply", "--check", "change.patch"], cwd=ops, capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
