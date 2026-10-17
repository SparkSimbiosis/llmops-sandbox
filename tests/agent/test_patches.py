"""Розбір unified diff (hw1 — лише розбір; застосування й перерахунок ханків тестуються в hw3)."""

import difflib

import pytest

from sandbox.agent.gates import gate_parse_ok
from sandbox.agent.patches import PatchParseError, diff_paths, looks_like_diff, parse_unified_diff
from sandbox.llm import fake_fixtures as fx
from tests.agent.memory_repo import seed_files

FILES = seed_files()


def make(path: str, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile="a/" + path, tofile="b/" + path, n=3))


def test_single_file_paths_and_counts():
    old = FILES["demo-site/footer.html"]
    patch = parse_unified_diff(make("demo-site/footer.html", old, old.replace("2025", "2026")))
    assert patch.paths == ["demo-site/footer.html"]
    f = patch.files[0]
    assert (f.old_path, f.new_path) == ("demo-site/footer.html", "demo-site/footer.html")
    h = f.hunks[0]
    assert (h.old_start, h.old_len, h.new_start, h.new_len) == (1, 3, 1, 3)
    assert (patch.added, patch.removed) == (1, 1)


def test_two_files_from_fake_editor():
    task = "Внеси зміни в шапку сайту: додай кнопку логіну"
    diff = fx.edit_diff(task, [(p, FILES[p]) for p in ("demo-site/header.html", "demo-site/styles.css")])
    patch = parse_unified_diff(diff)
    assert patch.paths == ["demo-site/header.html", "demo-site/styles.css"]
    assert [(h.old_start, h.old_len, h.new_start, h.new_len) for f in patch.files for h in f.hunks] == \
        [(4, 4, 4, 5), (3, 3, 3, 4)]
    assert (patch.added, patch.removed) == (2, 0)


def test_hunk_length_defaults_to_one():
    patch = parse_unified_diff("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
    h = patch.files[0].hunks[0]
    assert (h.old_len, h.new_len) == (1, 1)


def test_removed_line_starting_with_dashes_is_not_a_header():
    diff = make("demo-site/styles.css", "a\n-- note\nb\n", "a\nb\n")
    assert "\n--- note\n" in diff
    patch = parse_unified_diff(diff)
    assert len(patch.files) == 1 and patch.removed == 1


def test_no_newline_marker_and_header_timestamp():
    diff = "--- a/x\t2026-10-17 10:00:00\n+++ b/x\t2026-10-17 10:00:01\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n"
    patch = parse_unified_diff(diff)
    assert patch.paths == ["x"]


@pytest.mark.parametrize("text", [
    "",
    "just some words",
    "--- a/x\n@@ -1 +1 @@\n-a\n+b\n",                       # немає +++
    "--- a/x\n+++ b/x\n",                                   # заголовок без ханків
    "--- a/x\n+++ b/x\n@@ nonsense @@\n-a\n",               # битий заголовок ханка
    "--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n",              # ханк коротший за заголовок
    "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n a\n?b\n",          # чужий рядок усередині ханка
    "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\ntrailing words\n",   # текст після diff
])
def test_broken_diff_fails_parse_ok(text):
    with pytest.raises(PatchParseError):
        parse_unified_diff(text)
    report, patch = gate_parse_ok(text)
    assert report["gate"] == "parse_ok" and report["ok"] is False and patch is None


def test_looks_like_diff():
    assert not looks_like_diff(fx.NO_FORMAT_TEXT)
    assert not looks_like_diff("...")
    assert looks_like_diff("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")


def test_diff_paths_is_lenient_for_display():
    assert diff_paths("--- a/demo-site/x.html\n+++ b/demo-site/x.html\n@@ broken\n") == ["demo-site/x.html"]
