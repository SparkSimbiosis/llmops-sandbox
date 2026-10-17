"""Гейт allowed_paths: лише файли зі списку, без обходу шляху, перейменувань, створення й видалення."""

import json

import pytest

from sandbox.agent.gates import path_problem, run_gates
from sandbox.agent.settings import ALLOWED_PATHS
from sandbox.llm import fake_fixtures as fx
from tests.agent.memory_repo import seed_files


def diff_between(old_header: str, new_header: str) -> str:
    return f"--- {old_header}\n+++ {new_header}\n@@ -1 +1 @@\n-a\n+b\n"


def test_allowlisted_diff_passes_both_gates():
    files = seed_files()
    diff = fx.edit_diff("Онови рік у підвалі сайту", [("demo-site/footer.html", files["demo-site/footer.html"])])
    report, patch = run_gates(diff)
    assert report == [{"gate": "parse_ok", "ok": True}, {"gate": "allowed_paths", "ok": True}]
    assert patch.paths == ["demo-site/footer.html"]


def test_every_allowlisted_path_is_ok():
    assert all(path_problem(p) is None for p in ALLOWED_PATHS)


@pytest.mark.parametrize("old,new,reason", [
    ("a/../etc/passwd", "b/../etc/passwd", "dot_segment"),
    ("a/demo-site/../demo-site/header.html", "b/demo-site/../demo-site/header.html", "dot_segment"),
    ("/etc/passwd", "/etc/passwd", "absolute"),
    ("a/C:/Windows/win.ini", "b/C:/Windows/win.ini", "absolute"),
    ("a/demo-site\\header.html", "b/demo-site\\header.html", "backslash"),
    ("a/demo-site/header.html\x00", "b/demo-site/header.html\x00", "nul_byte"),
    ("/dev/null", "b/demo-site/header.html", "create_or_delete"),
    ("a/demo-site/header.html", "/dev/null", "create_or_delete"),
    ("a/demo-site/header.html", "b/demo-site/footer.html", "rename"),
    ("a/demo-site/admin.html", "b/demo-site/admin.html", "not_allowed"),
    ("a/README.md", "b/README.md", "not_allowed"),
])
def test_bad_paths_are_rejected(old, new, reason):
    report, _ = run_gates(diff_between(old, new))
    parse_ok, allowed = report
    assert parse_ok == {"gate": "parse_ok", "ok": True}
    assert allowed["gate"] == "allowed_paths" and allowed["ok"] is False
    assert reason in [v["reason"] for v in allowed["violations"]]
    # звіт можна зберегти в jsonb: сирого NUL у ньому немає, лише екранований \x00
    assert all("\x00" not in v["path"] for v in allowed["violations"])
    json.dumps(report)


def test_unparsable_diff_closes_allowed_paths():
    report, patch = run_gates("not a diff at all")
    assert [g["ok"] for g in report] == [False, False] and patch is None
