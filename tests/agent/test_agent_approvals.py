"""W4: заявки на застосування diff — approve пише нові версії, reject і stale — ні; гейти forbidden_patterns,
size_limits, base_version; превʼю сайту з CSP і reset до seed."""

import pathlib
import re

from fastapi.testclient import TestClient

from sandbox.agent import gates
from sandbox.agent.patches import parse_unified_diff
from sandbox.main import app
from tests.agent.memory_repo import seed_files

client = TestClient(app)
ROOT = pathlib.Path(__file__).resolve().parents[2]
FOOTER = "demo-site/footer.html"
TASK = "Онови рік у підвалі сайту"


def post(task=TASK, **body):
    return client.post("/agent/tasks", json={"task": task, **body}).json()


def current(mem, path=FOOTER):
    return mem._current()[path]


def test_proposal_becomes_pending_approval(mem):
    d = post()
    assert d["status"] == "proposed" and d["approval"]["status"] == "pending"
    aid = d["approval"]["approval_id"]
    lst = client.get("/agent/approvals?status=pending").json()["approvals"]
    assert [a["approval_id"] for a in lst] == [aid] and lst[0]["files"] == [FOOTER]
    one = client.get(f"/agent/approvals/{aid}").json()
    assert one["diff"] == d["patch"]["diff"] and one["base_versions"] == {FOOTER: 1}
    assert client.get(f"/agent/tasks/{d['task_id']}").json()["approval_id"] == aid
    assert current(mem)["version"] == 1                       # пропозиція нічого не змінила


def test_approve_writes_new_version_once(mem):
    d = post()
    aid = d["approval"]["approval_id"]
    r = client.post(f"/agent/approvals/{aid}/approve")
    assert r.status_code == 200 and r.json() == {"status": "applied", "files": [{"path": FOOTER, "version": 2}]}
    f = current(mem)
    assert (f["version"], f["source"], str(f["source_task_id"]), str(f["approval_id"])) == (2, "approval", d["task_id"], aid)
    assert f["content"] != seed_files()[FOOTER] and "2025" not in f["content"]
    assert client.post(f"/agent/approvals/{aid}/approve").status_code == 404     # повторний approve
    assert client.post(f"/agent/approvals/{aid}/reject").status_code == 404
    hist = client.get("/agent/site/history").json()["versions"]
    assert hist[0]["path"] == FOOTER and hist[0]["source"] == "approval" and hist[0]["approval_id"] == aid


def test_reject_changes_nothing(mem):
    aid = post()["approval"]["approval_id"]
    r = client.post(f"/agent/approvals/{aid}/reject", json={"quote": "не зараз"})
    assert r.status_code == 200 and r.json() == {"status": "rejected"}
    a = client.get(f"/agent/approvals/{aid}").json()
    assert (a["status"], a["reason"]) == ("rejected", "не зараз") and current(mem)["version"] == 1
    assert client.post(f"/agent/approvals/{aid}/approve").status_code == 404


def test_changed_base_makes_approval_stale(mem):
    aid = post()["approval"]["approval_id"]
    mem.add_file_version(FOOTER, seed_files()[FOOTER].replace("Demo Site", "Demo"))   # хтось змінив файл
    r = client.post(f"/agent/approvals/{aid}/approve")
    assert r.status_code == 409 and r.json()["error"] == "stale"
    assert r.json()["conflicts"] == [{"path": FOOTER, "base": 1, "current": 2}]
    assert client.get(f"/agent/approvals/{aid}").json()["status"] == "stale" and current(mem)["version"] == 2


def test_same_proposal_is_one_approval(mem):
    a, b = post(), post()                                     # другий — з кешу, той самий diff і база
    assert b["cache_hits"] == 2 and a["approval"]["approval_id"] == b["approval"]["approval_id"]
    assert len(mem.approvals) == 1
    client.post(f"/agent/approvals/{a['approval']['approval_id']}/approve")
    c = post()                                                # база вже v2 з новим роком: змінювати нічого
    assert (c["status"], c["approval"]) == ("no_change", None) and len(mem.approvals) == 1


def test_seed_base_is_preview_only(mem):
    d = post(base="seed")
    assert d["status"] == "proposed" and d["approval"] is None and mem.approvals == {}


def test_tracker_is_rejected_by_forbidden_patterns(mem):
    d = post("Додай на сайт аналітику з зовнішнім трекером")
    assert d["status"] == "rejected_by_gate" and d["approval"] is None
    g = next(g for g in d["gates"] if g["gate"] == "forbidden_patterns")
    assert g["ok"] is False and {"script_tag", "external_url"} <= {h["rule"] for h in g["hits"]}


def test_forbidden_patterns_check_only_added_lines():
    removed = "--- a/demo-site/index.html\n+++ b/demo-site/index.html\n@@ -1,2 +1 @@\n-<script src=\"https://x.io/a.js\"></script>\n <p>a</p>\n"
    assert gates.gate_forbidden_patterns(parse_unified_diff(removed))["ok"] is True
    added = "--- a/demo-site/index.html\n+++ b/demo-site/index.html\n@@ -1 +1,2 @@\n <p>a</p>\n+<p onclick=\"x()\">b</p>\n"
    r = gates.gate_forbidden_patterns(parse_unified_diff(added))
    assert r["hits"] == [{"path": "demo-site/index.html", "line": 2, "rule": "inline_handler"}]


def test_size_limits():
    body = "".join(f"+line {i}\n" for i in range(41))
    big = f"--- a/demo-site/index.html\n+++ b/demo-site/index.html\n@@ -0,0 +1,41 @@\n{body}"
    r = gates.gate_size_limits(big, parse_unified_diff(big))
    assert r["ok"] is False and r["changed_lines"] == 41 and r["over"] == {"changed_lines": 40}


def test_base_version_dry_run_detects_wrong_base():
    files = [{"path": FOOTER, "version": 1, "content": seed_files()[FOOTER]}]
    bad = f"--- a/{FOOTER}\n+++ b/{FOOTER}\n@@ -2 +2 @@\n-  <p>© 1999 Demo Site</p>\n+  <p>© 2026 Demo Site</p>\n"
    report, new = gates.gate_base_version(parse_unified_diff(bad), files)
    assert report["ok"] is False and report["problems"][0]["reason"] == "does_not_apply" and new == {}
    good = bad.replace("1999", "2025")
    report, new = gates.gate_base_version(parse_unified_diff(good), files)
    assert report == {"gate": "base_version", "ok": True, "versions": {FOOTER: 1}} and "2026" in new[FOOTER]


def test_preview_has_csp_and_shows_after_state(mem):
    r = client.get("/agent/site/preview")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "© 2025 Demo Site" in r.text and "<style>" in r.text and "include:" not in r.text
    aid = post()["approval"]["approval_id"]
    after = client.get(f"/agent/site/preview?approval={aid}").text
    assert "© 2025" not in after and current(mem)["version"] == 1          # превʼю «після» нічого не пише
    assert client.get("/agent/site/preview?approval=nope").status_code == 404
    assert client.get("/agent/site/preview?approval=00000000-0000-0000-0000-000000000000").status_code == 404


def test_reset_restores_seed_and_stales_old_proposals(mem):
    first = post()["approval"]["approval_id"]
    client.post(f"/agent/approvals/{first}/approve")
    assert client.post("/agent/site/reset").json() == {"reset": [FOOTER]}
    f = current(mem)
    assert (f["version"], f["source"], f["content"]) == (3, "reset", seed_files()[FOOTER])
    assert client.post("/agent/site/reset").json() == {"reset": []}           # вже seed — нічого не пишемо


def test_bad_ids_and_decisions():
    assert client.get("/agent/approvals/not-a-uuid").status_code == 404
    assert client.post("/agent/approvals/not-a-uuid/approve").status_code == 404
    assert client.post("/agent/approvals/00000000-0000-0000-0000-000000000000/merge").status_code == 422


def test_migration_is_additive_and_in_schema():
    body = (ROOT / "db" / "agent_migrations" / "hw4_approvals.sql").read_text(encoding="utf-8")
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    assert body in schema and schema.index("-- === hw3 (кінець) ===") < schema.index(body)
    code = [l for l in body.splitlines() if l.strip() and not l.lstrip().startswith("--")]
    assert all(not l.upper().lstrip().startswith(("DROP", "DELETE", "UPDATE", "TRUNCATE")) for l in code)
    assert "IF NOT EXISTS" in body


def test_ui_has_approvals_and_site_tabs():
    ui = ROOT / "sandbox" / "agent" / "ui"
    html = (ui / "index.html").read_text(encoding="utf-8")
    js = (ui / "app.js").read_text(encoding="utf-8")
    assert 'data-tab="approvals"' in html and 'data-tab="site"' in html
    assert "Підтвердження · скоро" not in html and "Сайт · скоро" not in html
    assert html.count('sandbox="allow-same-origin"') == 2 and not re.search(r'sandbox="[^"]*allow-scripts', html)   # без скриптів
    assert '"/approvals/" + id + "/" + decision' in js and '"/site/reset"' in js and "innerHTML" not in js
